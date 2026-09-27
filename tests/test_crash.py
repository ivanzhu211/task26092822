"""SIGKILL 崩溃恢复测试。

在子进程里执行 crawl 和 rollback，在每一次落盘提交的前后各用 SIGKILL
杀一次，重新 open 后断言：

1. 版本号/作业号取两个文件中的最大值加一，不复用；
2. get_job 的产出要么可读一致、要么明确标为已丢失；get_versions 的来源
   作业要么存在、要么明确标为缺失；
3. 不存在停在 running 的作业；completed 的作业产出全部可读，failed 的
   作业在 catalog 中没有归属于它的版本。
"""

import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(REPO_ROOT, "tests", "crash_worker.py")

sys.path.insert(0, REPO_ROOT)

import schemacat

CRASH_POINTS = [
    "pre_history_commit",
    "post_history_commit",
    "pre_catalog_commit",
    "post_catalog_commit",
    "pre_complete_commit",
    "post_complete_commit",
]

SCHEMA_V1 = "id,name\n1,alice\n2,bob\n"
SCHEMA_V2 = "id,name,score\n1,alice,9\n2,bob,8\n"
SCHEMA_V3 = "id,name,score,extra\n1,alice,9,x\n2,bob,8,y\n"


def _read_history(data_dir):
    conn = sqlite3.connect(os.path.join(data_dir, "history.db"))
    try:
        return conn.execute(
            "SELECT id, kind, status, outputs_json FROM jobs ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _read_catalog_versions(data_dir):
    conn = sqlite3.connect(os.path.join(data_dir, "catalog.db"))
    try:
        return conn.execute(
            "SELECT a.path, v.version, v.job_id FROM versions v"
            " JOIN assets a ON a.id = v.asset_id ORDER BY a.path, v.version"
        ).fetchall()
    finally:
        conn.close()


class CrashRecoveryTest(unittest.TestCase):
    def setUp(self):
        self._tmp_dirs = []
        self._reset_dirs()

    def tearDown(self):
        for path in self._tmp_dirs:
            shutil.rmtree(path, True)

    def _reset_dirs(self):
        tmp = tempfile.mkdtemp(prefix="schemacat_crash_")
        self._tmp_dirs.append(tmp)
        self.data_dir = os.path.join(tmp, "data")
        self.source_dir = os.path.join(tmp, "src")
        os.makedirs(self.source_dir)

    # ---- 工具 ----

    def _write_source(self, name, content):
        with open(os.path.join(self.source_dir, name), "w") as handle:
            handle.write(content)

    def _run_worker(self, crash_point, *args):
        env = dict(os.environ)
        env["SCHEMACAT_CRASH_AT"] = crash_point
        env["PYTHONPATH"] = REPO_ROOT
        result = subprocess.run(
            [sys.executable, WORKER, self.data_dir, self.source_dir, *args],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            -signal.SIGKILL,
            f"子进程应被 SIGKILL 终止，实际返回 {result.returncode}: "
            f"{result.stderr}",
        )

    def _assert_invariants(self, engine):
        """断言三条必须成立的性质（对当前两个文件的全体内容）。"""
        jobs = _read_history(self.data_dir)
        versions = _read_catalog_versions(self.data_dir)
        job_ids_with_versions = {row[2] for row in versions}

        for job_id, _kind, status, _outputs_json in jobs:
            # 性质 3：不存在 running 或其他中间态
            self.assertIn(status, ("completed", "failed"))
            info = engine.get_job(job_id)
            self.assertEqual(info["status"], status)
            if status == "completed":
                # 性质 2/3：completed 的产出全部可读且一致
                self.assertTrue(info["outputs"])
                for output in info["outputs"]:
                    self.assertEqual(output["status"], "ok")
            else:
                # 性质 3：failed 的作业在 catalog 中没有归属于它的版本
                self.assertNotIn(job_id, job_ids_with_versions)
                self.assertIsNotNone(info["error"])

        # 性质 2：get_versions 的来源作业标注正确
        known_job_ids = {row[0] for row in jobs}
        for asset, _version, _job_id in versions:
            for entry in engine.get_versions(asset):
                if entry["job_id"] in known_job_ids:
                    self.assertEqual(entry["source"], "ok")
                else:
                    self.assertEqual(entry["source"], "missing")

    def _assert_numbering_continues(self, engine, asset):
        """性质 1：恢复后再爬一次，作业号与版本号都是两文件最大值加一。"""
        jobs = _read_history(self.data_dir)
        versions = _read_catalog_versions(self.data_dir)
        max_job = max(
            [row[0] for row in jobs] + [row[2] for row in versions] + [0]
        )
        asset_versions = [row[1] for row in versions if row[0] == asset]
        max_version = max(asset_versions + [0])

        self._write_source(asset, SCHEMA_V3)
        new_job = engine.crawl(self.source_dir)
        self.assertEqual(new_job, max_job + 1)
        produced = {
            (o["asset"], o["version"]) for o in engine.get_job(new_job)["outputs"]
        }
        self.assertIn((asset, max_version + 1), produced)

    # ---- crawl 崩溃矩阵 ----

    def test_crawl_crash_matrix(self):
        for point in CRASH_POINTS:
            with self.subTest(point=point):
                self._reset_dirs()
                self._write_source("a.csv", SCHEMA_V1)
                self._run_worker(point, "crawl")

                engine = schemacat.open(self.data_dir)
                jobs = _read_history(self.data_dir)
                versions = _read_catalog_versions(self.data_dir)

                if point == "pre_history_commit":
                    # 作业记录未落盘：两边都没有痕迹
                    self.assertEqual(jobs, [])
                    self.assertEqual(versions, [])
                elif point in ("post_history_commit", "pre_catalog_commit"):
                    # 目录提交未发生：作业 failed，无版本
                    self.assertEqual(len(jobs), 1)
                    self.assertEqual(jobs[0][2], "failed")
                    self.assertEqual(versions, [])
                else:
                    # 目录提交已完成：前滚为 completed，产出可读
                    self.assertEqual(len(jobs), 1)
                    self.assertEqual(jobs[0][2], "completed")
                    self.assertEqual(versions, [("a.csv", 1, jobs[0][0])])
                    info = engine.get_job(jobs[0][0])
                    self.assertEqual(
                        [
                            (o["asset"], o["version"], o["status"])
                            for o in info["outputs"]
                        ],
                        [("a.csv", 1, "ok")],
                    )

                self._assert_invariants(engine)
                self._assert_numbering_continues(engine, "a.csv")
                engine.close()

    # ---- rollback 崩溃矩阵 ----

    def test_rollback_crash_matrix(self):
        for point in CRASH_POINTS:
            with self.subTest(point=point):
                self._reset_dirs()
                self._write_source("a.csv", SCHEMA_V1)
                engine = schemacat.open(self.data_dir)
                engine.crawl(self.source_dir)
                self._write_source("a.csv", SCHEMA_V2)
                engine.crawl(self.source_dir)
                engine.close()

                self._run_worker(point, "rollback", "a.csv", "1")

                engine = schemacat.open(self.data_dir)
                jobs = _read_history(self.data_dir)
                versions = _read_catalog_versions(self.data_dir)
                version_numbers = [row[1] for row in versions]

                if point == "pre_history_commit":
                    self.assertEqual(len(jobs), 2)
                    self.assertEqual(version_numbers, [1, 2])
                elif point in ("post_history_commit", "pre_catalog_commit"):
                    self.assertEqual(len(jobs), 3)
                    self.assertEqual(jobs[-1][2], "failed")
                    self.assertEqual(version_numbers, [1, 2])
                else:
                    self.assertEqual(len(jobs), 3)
                    self.assertEqual(jobs[-1][2], "completed")
                    self.assertEqual(version_numbers, [1, 2, 3])
                    # 回退产生的新版本模式与 v1 相同
                    current = engine.get_versions("a.csv")[-1]
                    self.assertEqual(current["version"], 3)
                    self.assertEqual(
                        current["schema"], [("id", "integer"), ("name", "text")]
                    )
                    info = engine.get_job(jobs[-1][0])
                    self.assertEqual(
                        [
                            (o["asset"], o["version"], o["status"])
                            for o in info["outputs"]
                        ],
                        [("a.csv", 3, "ok")],
                    )

                self._assert_invariants(engine)
                self._assert_numbering_continues(engine, "a.csv")
                engine.close()


if __name__ == "__main__":
    unittest.main()
