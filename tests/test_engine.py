import os
import shutil
import sqlite3
import tempfile
import unittest

from schema_engine import Engine
from schema_engine.engine import VersionLostError

SIDE_SUFFIXES = ("-wal", "-shm", "-journal")


def write_csv(source_dir, rel_path, content):
    path = os.path.join(source_dir, rel_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as fh:
        fh.write(content)


def restore_db(db_path, backup_path):
    """引擎关闭后：用备份覆盖数据库文件，并删除旁路文件。"""
    shutil.copyfile(backup_path, db_path)
    for suffix in SIDE_SUFFIXES:
        side = db_path + suffix
        if os.path.exists(side):
            os.remove(side)


class TempDirsMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data_dir = os.path.join(self.tmp, "data")
        self.source_dir = os.path.join(self.tmp, "src")
        os.makedirs(self.source_dir)
        self.catalog_db = os.path.join(self.data_dir, "catalog.db")
        self.history_db = os.path.join(self.data_dir, "history.db")


class EngineFunctionalTests(TempDirsMixin):
    def test_crawl_creates_versions_and_completed_job(self):
        write_csv(self.source_dir, "a.csv", "id,name\n1,alice\n")
        write_csv(self.source_dir, "sub/b.csv", "x,y\n1.5,hi\n")
        with Engine.open(self.data_dir) as eng:
            job_id = eng.crawl(self.source_dir)
            job = eng.get_job(job_id)
            self.assertEqual(job["status"], "completed")
            self.assertIsNotNone(job["started_at"])
            self.assertIsNotNone(job["ended_at"])
            self.assertGreaterEqual(job["duration_seconds"], 0)
            self.assertIsNone(job["error"])
            outputs = {(o["asset"], o["version"]): o["status"] for o in job["outputs"]}
            self.assertEqual(outputs, {("a.csv", 1): "ok", ("sub/b.csv", 1): "ok"})
            schema = {c["name"]: c["type"] for c in eng.get_versions("a.csv")[0]["schema"]}
            self.assertEqual(schema, {"id": "integer", "name": "text"})

    def test_type_inference_real_and_all_empty(self):
        write_csv(self.source_dir, "t.csv", "i,r,e\n1,1.5,\n2,2.0,\n")
        with Engine.open(self.data_dir) as eng:
            eng.crawl(self.source_dir)
            schema = {c["name"]: c["type"] for c in eng.get_versions("t.csv")[0]["schema"]}
            self.assertEqual(schema, {"i": "integer", "r": "real", "e": "text"})

    def test_crawl_without_change_produces_no_versions(self):
        write_csv(self.source_dir, "a.csv", "id\n1\n")
        with Engine.open(self.data_dir) as eng:
            eng.crawl(self.source_dir)
            job_id = eng.crawl(self.source_dir)
            job = eng.get_job(job_id)
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["outputs"], [])
            self.assertEqual(len(eng.get_versions("a.csv")), 1)

    def test_diff_summary(self):
        write_csv(self.source_dir, "a.csv", "id,name,drop\n1,alice,x\n")
        with Engine.open(self.data_dir) as eng:
            eng.crawl(self.source_dir)
            write_csv(self.source_dir, "a.csv", "name,score,id\nalice,1.5,1\n")
            eng.crawl(self.source_dir)
            versions = eng.get_versions("a.csv")
            self.assertEqual(len(versions), 2)
            diff = versions[1]["diff"]
            self.assertEqual(diff["added"], ["score"])
            self.assertEqual(diff["removed"], ["drop"])
            self.assertTrue(diff["reordered"])

    def test_failed_crawl_records_error_and_no_versions(self):
        write_csv(self.source_dir, "good.csv", "id\n1\n")
        write_csv(self.source_dir, "bad.csv", "h1,h2\n1,2,3\n")
        with Engine.open(self.data_dir) as eng:
            job_id = eng.crawl(self.source_dir)
            job = eng.get_job(job_id)
            self.assertEqual(job["status"], "failed")
            self.assertTrue(job["error"])
            self.assertEqual(job["outputs"], [])
            self.assertEqual(eng.get_versions("good.csv"), [])

    def test_rollback_creates_new_version_with_target_schema(self):
        write_csv(self.source_dir, "a.csv", "id,name\n1,alice\n")
        with Engine.open(self.data_dir) as eng:
            eng.crawl(self.source_dir)
            write_csv(self.source_dir, "a.csv", "id,name,extra\n1,alice,z\n")
            eng.crawl(self.source_dir)
            job_id = eng.rollback("a.csv", 1)
            versions = eng.get_versions("a.csv")
            self.assertEqual(len(versions), 3)
            self.assertEqual(
                [c["name"] for c in versions[2]["schema"]], ["id", "name"]
            )
            self.assertEqual(versions[2]["diff"]["removed"], ["extra"])
            job = eng.get_job(job_id)
            self.assertEqual(job["op"], "rollback")
            self.assertEqual(job["outputs"][0]["version"], 3)
            self.assertEqual(job["outputs"][0]["status"], "ok")

    def test_rollback_lost_version_raises(self):
        write_csv(self.source_dir, "a.csv", "id\n1\n")
        with Engine.open(self.data_dir) as eng:
            eng.crawl(self.source_dir)
            with self.assertRaises(VersionLostError):
                eng.rollback("a.csv", 99)
            with self.assertRaises(VersionLostError):
                eng.rollback("ghost.csv", 1)

    def test_get_job_unknown_returns_none(self):
        with Engine.open(self.data_dir) as eng:
            self.assertIsNone(eng.get_job(12345))


class BackupRestoreTests(TempDirsMixin):
    def _crawl_three_versions(self, backup_which):
        """crawl 出 v1、v2，备份，再 crawl 出 v3，关闭引擎。返回 (备份路径, v3 作业号)。"""
        write_csv(self.source_dir, "a.csv", "id\n1\n")
        eng = Engine.open(self.data_dir)
        eng.crawl(self.source_dir)
        write_csv(self.source_dir, "a.csv", "id,name\n1,alice\n")
        eng.crawl(self.source_dir)
        backup_path = os.path.join(self.tmp, f"{backup_which}.bak")
        eng.backup(backup_which, backup_path)
        write_csv(self.source_dir, "a.csv", "id,name,score\n1,alice,2.5\n")
        job3 = eng.crawl(self.source_dir)
        eng.close()
        return backup_path, job3

    def test_restore_catalog_marks_v3_lost_and_reuses_no_numbers(self):
        backup_path, job3 = self._crawl_three_versions("catalog")
        restore_db(self.catalog_db, backup_path)
        eng = Engine.open(self.data_dir)
        try:
            # 作业记录里的 v3 应标为已丢失
            job = eng.get_job(job3)
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["outputs"][0]["version"], 3)
            self.assertEqual(job["outputs"][0]["status"], "lost")
            # 当前版本应为 v2
            versions = eng.get_versions("a.csv")
            self.assertEqual([v["version"] for v in versions], [1, 2])
            # 对已丢失的 v3 执行 rollback 必须报错
            with self.assertRaises(VersionLostError):
                eng.rollback("a.csv", 3)
            # 再 crawl 一个新模式应得到 v4（编号不复用）
            write_csv(self.source_dir, "a.csv", "id,name,score,extra\n1,alice,2.5,z\n")
            job4 = eng.crawl(self.source_dir)
            versions = eng.get_versions("a.csv")
            self.assertEqual([v["version"] for v in versions], [1, 2, 4])
            self.assertGreater(eng.get_job(job4)["job_id"], job3)
        finally:
            eng.close()

    def test_restore_history_marks_origin_missing_and_reuses_no_job_ids(self):
        backup_path, job3 = self._crawl_three_versions("history")
        restore_db(self.history_db, backup_path)
        eng = Engine.open(self.data_dir)
        try:
            # history 回退到 v2 时代：job3 的记录消失
            self.assertIsNone(eng.get_job(job3))
            # catalog 中 v3 仍在，但来源作业缺失
            versions = eng.get_versions("a.csv")
            self.assertEqual([v["version"] for v in versions], [1, 2, 3])
            by_version = {v["version"]: v for v in versions}
            self.assertFalse(by_version[1]["origin_missing"])
            self.assertFalse(by_version[2]["origin_missing"])
            self.assertTrue(by_version[3]["origin_missing"])
            self.assertEqual(by_version[3]["job_id"], job3)
            # 再 crawl：作业号不得复用（必须大于 job3），版本号为 v4
            write_csv(self.source_dir, "a.csv", "id,name,score,extra\n1,alice,2.5,z\n")
            job4 = eng.crawl(self.source_dir)
            self.assertGreater(job4, job3)
            versions = eng.get_versions("a.csv")
            self.assertEqual([v["version"] for v in versions], [1, 2, 3, 4])
            self.assertFalse(eng.get_versions("a.csv")[3]["origin_missing"])
        finally:
            eng.close()

    def test_backup_files_are_independent(self):
        write_csv(self.source_dir, "a.csv", "id\n1\n")
        with Engine.open(self.data_dir) as eng:
            eng.crawl(self.source_dir)
            cat_bak = os.path.join(self.tmp, "cat.bak")
            hist_bak = os.path.join(self.tmp, "hist.bak")
            eng.backup("catalog", cat_bak)
            eng.backup("history", hist_bak)
        # 两份备份各自是可独立打开的合法 SQLite
        for path, table in ((cat_bak, "versions"), (hist_bak, "jobs")):
            conn = sqlite3.connect(path)
            try:
                rows = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                self.assertGreater(rows[0], 0)
            finally:
                conn.close()
        with self.assertRaises(ValueError):
            with Engine.open(self.data_dir) as eng:
                eng.backup("nonsense", os.path.join(self.tmp, "x.bak"))


if __name__ == "__main__":
    unittest.main()
