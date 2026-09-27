"""模式版本引擎：catalog 与 history 两个独立 SQLite 文件。

- catalog.db：资产及其模式版本。
- history.db：作业记录及其产出清单。
两个文件各自在自己的事务里提交，可各自独立备份与回退。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from .infer import Column, diff_schema, infer_schema

CATALOG = "catalog"
HISTORY = "history"

_CATALOG_FILE = "catalog.db"
_HISTORY_FILE = "history.db"

_SIDE_SUFFIXES = ("-wal", "-shm", "-journal")


class VersionLostError(Exception):
    """目标版本在 catalog 中已丢失（或从未存在），无法回退。"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Engine:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._catalog = self._connect(os.path.join(data_dir, _CATALOG_FILE))
        self._history = self._connect(os.path.join(data_dir, _HISTORY_FILE))
        self._init_tables()
        self._recover()

    @classmethod
    def open(cls, data_dir: str) -> "Engine":
        """打开数据目录并完成崩溃恢复。"""
        return cls(data_dir)

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @staticmethod
    def _connect(path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_tables(self) -> None:
        self._catalog.execute(
            """
            CREATE TABLE IF NOT EXISTS versions(
                asset TEXT NOT NULL,
                version INTEGER NOT NULL,
                schema_json TEXT NOT NULL,
                diff_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                job_id INTEGER NOT NULL,
                PRIMARY KEY(asset, version)
            )
            """
        )
        self._history.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs(
                job_id INTEGER PRIMARY KEY,
                op TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                error TEXT
            )
            """
        )
        self._history.execute(
            """
            CREATE TABLE IF NOT EXISTS job_outputs(
                job_id INTEGER NOT NULL,
                asset TEXT NOT NULL,
                version INTEGER NOT NULL,
                schema_json TEXT NOT NULL,
                PRIMARY KEY(job_id, asset)
            )
            """
        )

    def _recover(self) -> None:
        """把停在 running 的作业收口：产出齐全则 completed，否则回滚为 failed。"""
        running = self._history.execute(
            "SELECT job_id FROM jobs WHERE status='running'"
        ).fetchall()
        for (job_id,) in running:
            outputs = self._history.execute(
                "SELECT asset, version, schema_json FROM job_outputs WHERE job_id=?",
                (job_id,),
            ).fetchall()
            complete = bool(outputs) and all(
                self._catalog.execute(
                    "SELECT 1 FROM versions WHERE asset=? AND version=? AND schema_json=?",
                    (asset, version, schema_json),
                ).fetchone()
                for asset, version, schema_json in outputs
            )
            if complete:
                self._history.execute(
                    "UPDATE jobs SET status='completed', ended_at=? WHERE job_id=?",
                    (_utcnow(), job_id),
                )
            else:
                # 回滚：清掉 catalog 中可能已归属该作业的版本，再标记 failed。
                self._catalog.execute("BEGIN")
                try:
                    self._catalog.execute(
                        "DELETE FROM versions WHERE job_id=?", (job_id,)
                    )
                    self._catalog.execute("COMMIT")
                except Exception:
                    self._catalog.execute("ROLLBACK")
                    raise
                self._history.execute(
                    "UPDATE jobs SET status='failed', ended_at=?, error=? WHERE job_id=?",
                    (_utcnow(), "进程中断，作业未能完成", job_id),
                )

    def close(self) -> None:
        for conn in (self._catalog, self._history):
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            conn.close()

    # ---- 编号：两个文件中任一记得的编号都不得复用 ----

    def _next_job_id(self) -> int:
        (hist_max,) = self._history.execute(
            "SELECT COALESCE(MAX(job_id), 0) FROM jobs"
        ).fetchone()
        (cat_max,) = self._catalog.execute(
            "SELECT COALESCE(MAX(job_id), 0) FROM versions"
        ).fetchone()
        return max(hist_max, cat_max) + 1

    def _next_version(self, asset: str) -> int:
        (cat_max,) = self._catalog.execute(
            "SELECT COALESCE(MAX(version), 0) FROM versions WHERE asset=?", (asset,)
        ).fetchone()
        (hist_max,) = self._history.execute(
            "SELECT COALESCE(MAX(version), 0) FROM job_outputs WHERE asset=?",
            (asset,),
        ).fetchone()
        return max(cat_max, hist_max) + 1

    # ---- 备份 ----

    def backup(self, which: str, dest_path: str) -> None:
        """把 catalog 或 history 单独导出为一份一致的副本。"""
        if which == CATALOG:
            src = self._catalog
        elif which == HISTORY:
            src = self._history
        else:
            raise ValueError(f"which 必须是 'catalog' 或 'history'，得到 {which!r}")
        if os.path.abspath(dest_path) in (
            os.path.abspath(os.path.join(self.data_dir, _CATALOG_FILE)),
            os.path.abspath(os.path.join(self.data_dir, _HISTORY_FILE)),
        ):
            raise ValueError("备份目标不能是引擎正在使用的数据库文件")
        for suffix in ("",) + _SIDE_SUFFIXES:
            stale = dest_path + suffix
            if os.path.exists(stale):
                os.remove(stale)
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        dest = sqlite3.connect(dest_path)
        try:
            src.backup(dest)
        finally:
            dest.close()


    # ---- 扫描与写入 ----

    @staticmethod
    def _schema_to_json(schema) -> str:
        return json.dumps([[c.name, c.type] for c in schema], ensure_ascii=False)

    @staticmethod
    def _schema_from_json(payload: str):
        return [Column(name, ctype) for name, ctype in json.loads(payload)]

    def _current_version(self, asset: str):
        """catalog 中该资产编号最大的版本行，无则 None。"""
        return self._catalog.execute(
            "SELECT version, schema_json FROM versions WHERE asset=?"
            " ORDER BY version DESC LIMIT 1",
            (asset,),
        ).fetchone()

    def _scan(self, source_dir: str) -> dict:
        """扫描源目录下全部 CSV，返回 {相对路径: 模式}。解析失败抛 ValueError。"""
        found = {}
        for root, _dirs, files in os.walk(source_dir):
            for name in files:
                if not name.lower().endswith(".csv"):
                    continue
                full = os.path.join(root, name)
                asset = os.path.relpath(full, source_dir).replace(os.sep, "/")
                found[asset] = infer_schema(full)
        return found

    def _job_begin(self, job_id: int, op: str, started: str, outputs) -> None:
        """history 自己的事务：写入 running 作业及其计划产出清单。"""
        self._history.execute("BEGIN")
        try:
            self._history.execute(
                "INSERT INTO jobs(job_id, op, status, started_at) VALUES(?,?,?,?)",
                (job_id, op, "running", started),
            )
            for asset, version, schema_json in outputs:
                self._history.execute(
                    "INSERT INTO job_outputs(job_id, asset, version, schema_json)"
                    " VALUES(?,?,?,?)",
                    (job_id, asset, version, schema_json),
                )
            self._history.execute("COMMIT")
        except Exception:
            self._history.execute("ROLLBACK")
            raise

    def _catalog_commit(self, job_id: int, plans) -> None:
        """catalog 自己的事务：一次性写入本作业的全部新版本。"""
        created = _utcnow()
        self._catalog.execute("BEGIN")
        try:
            for asset, version, schema, diff in plans:
                self._catalog.execute(
                    "INSERT INTO versions(asset, version, schema_json, diff_json,"
                    " created_at, job_id) VALUES(?,?,?,?,?,?)",
                    (
                        asset,
                        version,
                        self._schema_to_json(schema),
                        json.dumps(diff, ensure_ascii=False),
                        created,
                        job_id,
                    ),
                )
            self._catalog.execute("COMMIT")
        except Exception:
            self._catalog.execute("ROLLBACK")
            raise

    def _job_finish(self, job_id: int, status: str, error=None) -> None:
        self._history.execute(
            "UPDATE jobs SET status=?, ended_at=?, error=? WHERE job_id=?",
            (status, _utcnow(), error, job_id),
        )

    def _run_plans(self, op: str, plans):
        """三段式落盘：history(running+产出) -> catalog(版本) -> history(completed)。"""
        job_id = self._next_job_id()
        started = _utcnow()
        outputs = [
            (asset, version, self._schema_to_json(schema))
            for asset, version, schema, _diff in plans
        ]
        self._job_begin(job_id, op, started, outputs)
        self._catalog_commit(job_id, plans)
        self._job_finish(job_id, "completed")
        return job_id

    # ---- 操作 ----

    def crawl(self, source_dir: str) -> int:
        """扫描源目录，为新出现或模式有变化的资产各生成一个新版本。

        一次扫描算一个作业，返回作业号。任一文件解析失败则整个作业
        failed、记录错误、不产出任何版本。
        """
        job_id = self._next_job_id()
        started = _utcnow()
        try:
            scanned = self._scan(source_dir)
        except ValueError as exc:
            self._history.execute(
                "INSERT INTO jobs(job_id, op, status, started_at, ended_at, error)"
                " VALUES(?,?,?,?,?,?)",
                (job_id, "crawl", "failed", started, _utcnow(), str(exc)),
            )
            return job_id
        plans = []
        for asset in sorted(scanned):
            schema = scanned[asset]
            current = self._current_version(asset)
            if current is not None and self._schema_from_json(current[1]) == schema:
                continue
            old = self._schema_from_json(current[1]) if current is not None else []
            plans.append(
                (asset, self._next_version(asset), schema, diff_schema(old, schema))
            )
        return self._run_plans("crawl", plans)

    def rollback(self, asset: str, version: int) -> int:
        """生成一个模式与目标版本相同的新版本，算一个作业，返回作业号。"""
        row = self._catalog.execute(
            "SELECT schema_json FROM versions WHERE asset=? AND version=?",
            (asset, version),
        ).fetchone()
        if row is None:
            raise VersionLostError(f"{asset} 的版本 {version} 在 catalog 中已丢失")
        target = self._schema_from_json(row[0])
        current = self._current_version(asset)
        old = self._schema_from_json(current[1]) if current is not None else []
        plans = [
            (asset, self._next_version(asset), target, diff_schema(old, target))
        ]
        return self._run_plans("rollback", plans)

    # ---- 查询 ----

    def get_job(self, job_id: int) -> Optional[dict]:
        """返回状态、起止时间、耗时、错误，以及产出的资产与版本号。

        每个产出若在 catalog 中读不到或内容不一致，标记为 lost。
        """
        row = self._history.execute(
            "SELECT op, status, started_at, ended_at, error FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        op, status, started_at, ended_at, error = row
        duration = None
        if ended_at is not None:
            duration = (
                datetime.fromisoformat(ended_at) - datetime.fromisoformat(started_at)
            ).total_seconds()
        outputs = []
        for asset, version, schema_json in self._history.execute(
            "SELECT asset, version, schema_json FROM job_outputs WHERE job_id=?"
            " ORDER BY asset",
            (job_id,),
        ):
            cat = self._catalog.execute(
                "SELECT schema_json FROM versions WHERE asset=? AND version=?",
                (asset, version),
            ).fetchone()
            readable = cat is not None and cat[0] == schema_json
            entry = {"asset": asset, "version": version,
                     "status": "ok" if readable else "lost"}
            if readable:
                entry["schema"] = [
                    {"name": c.name, "type": c.type}
                    for c in self._schema_from_json(schema_json)
                ]
            outputs.append(entry)
        return {
            "job_id": job_id,
            "op": op,
            "status": status,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_seconds": duration,
            "error": error,
            "outputs": outputs,
        }

    def get_versions(self, asset: str) -> List[dict]:
        """返回该资产的全部版本；来源作业在 history 中缺失时明确标记。"""
        result = []
        for version, schema_json, diff_json, created_at, job_id in self._catalog.execute(
            "SELECT version, schema_json, diff_json, created_at, job_id"
            " FROM versions WHERE asset=? ORDER BY version",
            (asset,),
        ):
            job = self._history.execute(
                "SELECT status FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            result.append({
                "asset": asset,
                "version": version,
                "schema": [
                    {"name": c.name, "type": c.type}
                    for c in self._schema_from_json(schema_json)
                ],
                "diff": json.loads(diff_json),
                "created_at": created_at,
                "job_id": job_id,
                "origin_missing": job is None,
                "origin_status": job[0] if job is not None else None,
            })
        return result
