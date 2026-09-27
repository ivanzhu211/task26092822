"""引擎主类：维护 catalog 与 history 两个 SQLite 文件。"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import time

from .csvschema import infer_schema, schema_fingerprint
from .diff import compute_diff
from .recovery import recover

_CATALOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS versions (
    asset_id INTEGER NOT NULL REFERENCES assets (id),
    version INTEGER NOT NULL,
    schema_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    diff_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    job_id INTEGER NOT NULL,
    PRIMARY KEY (asset_id, version)
);
"""

_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    error TEXT,
    outputs_json TEXT NOT NULL DEFAULT '[]'
);
"""


def _crash_barrier(point: str) -> None:
    """测试用崩溃点：环境变量 SCHEMACAT_CRASH_AT 命中时用 SIGKILL 自杀。"""
    if os.environ.get("SCHEMACAT_CRASH_AT") == point:
        os.kill(os.getpid(), signal.SIGKILL)


class Engine:
    """数据目录模式版本引擎。

    在一个数据目录下维护两个 SQLite 文件：
    - catalog.db：资产及其模式版本
    - history.db：作业记录
    """

    CATALOG_FILENAME = "catalog.db"
    HISTORY_FILENAME = "history.db"

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._catalog = sqlite3.connect(os.path.join(data_dir, self.CATALOG_FILENAME))
        self._history = sqlite3.connect(os.path.join(data_dir, self.HISTORY_FILENAME))
        self._catalog.executescript(_CATALOG_SCHEMA)
        self._catalog.commit()
        self._history.executescript(_HISTORY_SCHEMA)
        self._history.commit()
        recover(self._catalog, self._history)

    @classmethod
    def open(cls, data_dir: str) -> "Engine":
        """打开数据目录，并完成需要的恢复。"""
        return cls(data_dir)

    def close(self) -> None:
        self._catalog.close()
        self._history.close()

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---- 编号分配：两个文件中的最大值 + 1 ----

    def _next_job_id(self) -> int:
        in_history = self._history.execute("SELECT MAX(id) FROM jobs").fetchone()[0]
        in_catalog = self._catalog.execute(
            "SELECT MAX(job_id) FROM versions"
        ).fetchone()[0]
        return max(in_history or 0, in_catalog or 0) + 1

    def _next_version(self, asset_id: int, asset_path: str) -> int:
        in_catalog = self._catalog.execute(
            "SELECT MAX(version) FROM versions WHERE asset_id = ?", (asset_id,)
        ).fetchone()[0]
        in_history = self._history.execute(
            """
            SELECT MAX(CAST(json_extract(value, '$.version') AS INTEGER))
            FROM jobs, json_each(jobs.outputs_json)
            WHERE json_extract(value, '$.asset') = ?
            """,
            (asset_path,),
        ).fetchone()[0]
        return max(in_catalog or 0, in_history or 0) + 1

    # ---- 作业 ----

    def crawl(self, source_dir: str) -> int:
        """扫描源目录，为新出现或模式有变化的资产生成新版本。返回作业号。"""
        job_id = self._next_job_id()
        started_at = time.time()
        self._history.execute(
            "INSERT INTO jobs (id, kind, status, started_at, outputs_json)"
            " VALUES (?, 'crawl', 'running', ?, '[]')",
            (job_id, started_at),
        )
        _crash_barrier("pre_history_commit")
        self._history.commit()
        _crash_barrier("post_history_commit")
        try:
            outputs = self._crawl_collect(source_dir, job_id)
        except Exception as exc:
            self._catalog.rollback()
            self._history.execute(
                "UPDATE jobs SET status = 'failed', ended_at = ?, error = ?"
                " WHERE id = ?",
                (time.time(), str(exc), job_id),
            )
            self._history.commit()
            return job_id
        _crash_barrier("pre_catalog_commit")
        self._catalog.commit()
        _crash_barrier("post_catalog_commit")
        self._history.execute(
            "UPDATE jobs SET status = 'completed', ended_at = ?, outputs_json = ?"
            " WHERE id = ?",
            (time.time(), json.dumps(outputs, ensure_ascii=False), job_id),
        )
        _crash_barrier("pre_complete_commit")
        self._history.commit()
        _crash_barrier("post_complete_commit")
        return job_id

    def _crawl_collect(self, source_dir: str, job_id: int) -> list:
        csv_files = []
        for root, dirs, files in os.walk(source_dir):
            dirs.sort()
            for name in sorted(files):
                if name.lower().endswith(".csv"):
                    full = os.path.join(root, name)
                    csv_files.append((os.path.relpath(full, source_dir), full))
        csv_files.sort()

        outputs = []
        for rel_path, full_path in csv_files:
            schema = infer_schema(full_path)
            fingerprint = schema_fingerprint(schema)
            row = self._catalog.execute(
                "SELECT id FROM assets WHERE path = ?", (rel_path,)
            ).fetchone()
            if row is None:
                cursor = self._catalog.execute(
                    "INSERT INTO assets (path) VALUES (?)", (rel_path,)
                )
                asset_id = cursor.lastrowid
                current = None
            else:
                asset_id = row[0]
                current = self._catalog.execute(
                    "SELECT version, schema_json, fingerprint FROM versions"
                    " WHERE asset_id = ? ORDER BY version DESC LIMIT 1",
                    (asset_id,),
                ).fetchone()
            if current is not None and current[2] == fingerprint:
                continue
            old_schema = json.loads(current[1]) if current is not None else None
            diff = compute_diff(old_schema, schema)
            version = self._next_version(asset_id, rel_path)
            self._catalog.execute(
                "INSERT INTO versions (asset_id, version, schema_json,"
                " fingerprint, diff_json, created_at, job_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    asset_id,
                    version,
                    json.dumps(
                        [[name, ctype] for name, ctype in schema],
                        ensure_ascii=False,
                    ),
                    fingerprint,
                    json.dumps(diff, ensure_ascii=False),
                    time.time(),
                    job_id,
                ),
            )
            outputs.append(
                {"asset": rel_path, "version": version, "fingerprint": fingerprint}
            )
        return outputs

    def rollback(self, asset: str, version: int) -> int:
        """生成一个模式与目标版本相同的新版本。返回作业号。"""
        target = self._catalog.execute(
            "SELECT a.id, v.schema_json, v.fingerprint FROM versions v"
            " JOIN assets a ON a.id = v.asset_id"
            " WHERE a.path = ? AND v.version = ?",
            (asset, version),
        ).fetchone()
        if target is None:
            raise KeyError(f"版本不存在或已丢失，无法回退: {asset}@{version}")
        asset_id, schema_json, fingerprint = target

        job_id = self._next_job_id()
        started_at = time.time()
        self._history.execute(
            "INSERT INTO jobs (id, kind, status, started_at, outputs_json)"
            " VALUES (?, 'rollback', 'running', ?, '[]')",
            (job_id, started_at),
        )
        _crash_barrier("pre_history_commit")
        self._history.commit()
        _crash_barrier("post_history_commit")

        current = self._catalog.execute(
            "SELECT schema_json FROM versions WHERE asset_id = ?"
            " ORDER BY version DESC LIMIT 1",
            (asset_id,),
        ).fetchone()
        old_schema = json.loads(current[0]) if current is not None else None
        new_schema = json.loads(schema_json)
        diff = compute_diff(old_schema, new_schema)
        new_version = self._next_version(asset_id, asset)
        self._catalog.execute(
            "INSERT INTO versions (asset_id, version, schema_json, fingerprint,"
            " diff_json, created_at, job_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                asset_id,
                new_version,
                schema_json,
                fingerprint,
                json.dumps(diff, ensure_ascii=False),
                time.time(),
                job_id,
            ),
        )
        _crash_barrier("pre_catalog_commit")
        self._catalog.commit()
        _crash_barrier("post_catalog_commit")

        outputs = [{"asset": asset, "version": new_version, "fingerprint": fingerprint}]
        self._history.execute(
            "UPDATE jobs SET status = 'completed', ended_at = ?, outputs_json = ?"
            " WHERE id = ?",
            (time.time(), json.dumps(outputs, ensure_ascii=False), job_id),
        )
        _crash_barrier("pre_complete_commit")
        self._history.commit()
        _crash_barrier("post_complete_commit")
        return job_id

    # ---- 查询 ----

    def get_job(self, job_id: int) -> dict:
        """返回作业状态、起止时间、耗时、错误及产出的资产与版本号。"""
        row = self._history.execute(
            "SELECT id, kind, status, started_at, ended_at, error, outputs_json"
            " FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"作业不存在: {job_id}")
        _, kind, status, started_at, ended_at, error, outputs_json = row
        outputs = []
        for entry in json.loads(outputs_json):
            found = self._catalog.execute(
                "SELECT v.fingerprint FROM versions v"
                " JOIN assets a ON a.id = v.asset_id"
                " WHERE a.path = ? AND v.version = ?",
                (entry["asset"], entry["version"]),
            ).fetchone()
            if found is None:
                output_status = "lost"
            elif found[0] != entry.get("fingerprint"):
                output_status = "mismatch"
            else:
                output_status = "ok"
            outputs.append(
                {
                    "asset": entry["asset"],
                    "version": entry["version"],
                    "status": output_status,
                }
            )
        return {
            "id": job_id,
            "kind": kind,
            "status": status,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration": (ended_at - started_at) if ended_at is not None else None,
            "error": error,
            "outputs": outputs,
        }

    def get_versions(self, asset: str) -> list:
        """返回该资产的全部版本。"""
        rows = self._catalog.execute(
            "SELECT v.version, v.schema_json, v.diff_json, v.created_at, v.job_id"
            " FROM versions v JOIN assets a ON a.id = v.asset_id"
            " WHERE a.path = ? ORDER BY v.version",
            (asset,),
        ).fetchall()
        job_ids = {row[4] for row in rows}
        known_jobs = set()
        if job_ids:
            placeholders = ",".join("?" for _ in job_ids)
            known_jobs = {
                row[0]
                for row in self._history.execute(
                    f"SELECT id FROM jobs WHERE id IN ({placeholders})",
                    tuple(job_ids),
                )
            }
        versions = []
        for version, schema_json, diff_json, created_at, job_id in rows:
            versions.append(
                {
                    "asset": asset,
                    "version": version,
                    "schema": [tuple(pair) for pair in json.loads(schema_json)],
                    "diff": json.loads(diff_json),
                    "created_at": created_at,
                    "job_id": job_id,
                    "source": "ok" if job_id in known_jobs else "missing",
                }
            )
        return versions

    def backup(self, which: str, dest_path: str) -> None:
        """把 catalog 或 history 单独导出成一份一致的副本。"""
        sources = {"catalog": self._catalog, "history": self._history}
        if which not in sources:
            raise ValueError(f"which 必须是 'catalog' 或 'history'，收到: {which!r}")
        dest = sqlite3.connect(dest_path)
        try:
            sources[which].backup(dest)
        finally:
            dest.close()


def open(data_dir: str) -> Engine:
    """模块级便捷入口，等价于 Engine.open(data_dir)。"""
    return Engine.open(data_dir)
