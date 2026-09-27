import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from schema_engine import Engine
from tests.crash_child import MARKERS

CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash_child.py")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write_csv(source_dir, rel_path, content):
    path = os.path.join(source_dir, rel_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as fh:
        fh.write(content)


def query(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


class CrashRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data_dir = os.path.join(self.tmp, "data")
        self.source_dir = os.path.join(self.tmp, "src")
        self.marker_dir = os.path.join(self.tmp, "markers")
        os.makedirs(self.source_dir)
        self.catalog_db = os.path.join(self.data_dir, "catalog.db")
        self.history_db = os.path.join(self.data_dir, "history.db")

    def _kill_during(self, mode, pause_at):
        """在子进程里执行 mode，于 pause_at 标记处 SIGKILL，然后重新 open。"""
        proc = subprocess.Popen(
            [sys.executable, CHILD, self.data_dir, self.source_dir,
             mode, pause_at, self.marker_dir],
            cwd=REPO_ROOT,
        )
        marker = os.path.join(self.marker_dir, pause_at)
        deadline = time.monotonic() + 15
        while not os.path.exists(marker):
            if proc.poll() is not None:
                self.fail(f"子进程在到达 {pause_at} 前退出，返回码 {proc.returncode}")
            if time.monotonic() > deadline:
                proc.kill()
                self.fail(f"等待标记 {pause_at} 超时")
            time.sleep(0.01)
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        self.assertEqual(proc.returncode, -signal.SIGKILL)

    def _assert_invariants_and_renumber(self):
        """重新 open，断言三条性质，并验证后续编号不复用。"""
        eng = Engine.open(self.data_dir)
        try:
            jobs = query(self.history_db, "SELECT job_id, status FROM jobs")
            # 性质 3：不存在 running 或其他中间态
            for job_id, status in jobs:
                self.assertIn(status, ("completed", "failed"),
                              f"作业 {job_id} 停在中间态 {status}")
            for job_id, status in jobs:
                job = eng.get_job(job_id)
                if status == "completed":
                    # completed：产出全部可读且与作业记录一致
                    self.assertTrue(
                        all(o["status"] == "ok" for o in job["outputs"]),
                        f"completed 作业 {job_id} 有不可读产出: {job['outputs']}",
                    )
                else:
                    # failed：catalog 中没有归属于它的版本
                    rows = query(self.catalog_db,
                                 "SELECT COUNT(*) FROM versions WHERE job_id=?",
                                 (job_id,))
                    self.assertEqual(rows[0][0], 0,
                                     f"failed 作业 {job_id} 在 catalog 留有版本")
            # 性质 2：get_versions 的来源作业都能对上（本场景 history 未回退）
            for (asset,) in query(self.catalog_db,
                                  "SELECT DISTINCT asset FROM versions"):
                for ver in eng.get_versions(asset):
                    self.assertFalse(ver["origin_missing"],
                                     f"{asset} v{ver['version']} 来源缺失")

            # 性质 1：杀掉一个作业后继续工作，编号不复用
            write_csv(self.source_dir, "a.csv", "id,name,score\n1,alice,2.5\n")
            old_job_ids = {j for j, _s in jobs}
            old_job_ids |= {r[0] for r in query(
                self.catalog_db, "SELECT DISTINCT job_id FROM versions")}
            old_versions = {r[0] for r in query(
                self.catalog_db,
                "SELECT version FROM versions WHERE asset='a.csv'")}
            old_versions |= {r[0] for r in query(
                self.history_db,
                "SELECT version FROM job_outputs WHERE asset='a.csv'")}
            new_job = eng.crawl(self.source_dir)
            self.assertNotIn(new_job, old_job_ids)
            self.assertGreater(new_job, max(old_job_ids, default=0))
            versions = eng.get_versions("a.csv")
            new_version = versions[-1]["version"]
            self.assertNotIn(new_version, old_versions)
            self.assertEqual(new_version, max(old_versions, default=0) + 1)
            self.assertEqual(eng.get_job(new_job)["status"], "completed")
        finally:
            eng.close()

    def _run_case(self, mode, pause_at):
        write_csv(self.source_dir, "a.csv", "id,name\n1,alice\n")
        if mode == "rollback":
            # 先正常造出 v1，子进程再 rollback 到 v1
            eng = Engine.open(self.data_dir)
            eng.crawl(self.source_dir)
            eng.close()
        self._kill_during(mode, pause_at)
        self._assert_invariants_and_renumber()


def _make_case(mode, marker):
    def case(self):
        self._run_case(mode, marker)
    case.__name__ = f"test_sigkill_{mode}_{marker}"
    return case


for _mode in ("crawl", "rollback"):
    for _marker in MARKERS:
        setattr(CrashRecoveryTests, _make_case.__name__ if False else
                f"test_sigkill_{_mode}_{_marker}", _make_case(_mode, _marker))


if __name__ == "__main__":
    unittest.main()
