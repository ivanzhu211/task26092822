"""引擎基本行为测试：类型推断、失败作业、错误路径。"""

import os
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import schemacat
from schemacat.csvschema import infer_schema


class TypeInferenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="schemacat_types_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _infer(self, content):
        path = os.path.join(self.tmp, "t.csv")
        with open(path, "w") as handle:
            handle.write(content)
        return infer_schema(path)

    def test_integer_real_text(self):
        schema = self._infer("a,b,c\n1,1.5,x\n2,2.0,y\n")
        self.assertEqual(schema, [("a", "integer"), ("b", "real"), ("c", "text")])

    def test_all_empty_column_is_text(self):
        schema = self._infer("a,b\n1,\n2,\n")
        self.assertEqual(schema, [("a", "integer"), ("b", "text")])

    def test_int_and_real_mix_is_real(self):
        schema = self._infer("a\n1\n2.5\n")
        self.assertEqual(schema, [("a", "real")])

    def test_empty_file_fails(self):
        from schemacat.csvschema import CsvParseError

        with self.assertRaises(CsvParseError):
            self._infer("")

    def test_ragged_row_fails(self):
        from schemacat.csvschema import CsvParseError

        with self.assertRaises(CsvParseError):
            self._infer("a,b\n1,2,3\n")


class EngineBehaviorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="schemacat_engine_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data_dir = os.path.join(self.tmp, "data")
        self.source_dir = os.path.join(self.tmp, "src")
        os.makedirs(self.source_dir)

    def _write(self, name, content):
        path = os.path.join(self.source_dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(content)

    def test_failed_crawl_produces_no_versions(self):
        self._write("good.csv", "a\n1\n")
        self._write("bad.csv", "a,b\n1,2,3\n")
        engine = schemacat.open(self.data_dir)
        job_id = engine.crawl(self.source_dir)
        info = engine.get_job(job_id)
        self.assertEqual(info["status"], "failed")
        self.assertIsNotNone(info["error"])
        self.assertEqual(info["outputs"], [])
        # catalog 中没有任何版本，包括解析成功的 good.csv
        self.assertEqual(engine.get_versions("good.csv"), [])
        self.assertEqual(engine.get_versions("bad.csv"), [])

        # 修复后重新 crawl，作业号与版本号正常递增
        self._write("bad.csv", "a,b\n1,2\n")
        job2 = engine.crawl(self.source_dir)
        self.assertEqual(job2, job_id + 1)
        self.assertEqual(engine.get_job(job2)["status"], "completed")
        self.assertEqual(
            [v["version"] for v in engine.get_versions("good.csv")], [1]
        )
        engine.close()

    def test_nested_relative_path_asset(self):
        self._write(os.path.join("sub", "deep.csv"), "a\n1\n")
        engine = schemacat.open(self.data_dir)
        engine.crawl(self.source_dir)
        versions = engine.get_versions(os.path.join("sub", "deep.csv"))
        self.assertEqual([v["version"] for v in versions], [1])
        engine.close()

    def test_get_job_unknown_raises(self):
        engine = schemacat.open(self.data_dir)
        with self.assertRaises(KeyError):
            engine.get_job(42)
        engine.close()

    def test_backup_invalid_which_raises(self):
        engine = schemacat.open(self.data_dir)
        with self.assertRaises(ValueError):
            engine.backup("both", os.path.join(self.tmp, "x.db"))
        engine.close()

    def test_backup_produces_independent_consistent_copy(self):
        self._write("a.csv", "a\n1\n")
        engine = schemacat.open(self.data_dir)
        engine.crawl(self.source_dir)
        catalog_bak = os.path.join(self.tmp, "catalog.bak")
        history_bak = os.path.join(self.tmp, "history.bak")
        engine.backup("catalog", catalog_bak)
        engine.backup("history", history_bak)
        engine.close()

        # 备份文件自身就是可独立打开的完整数据库
        import sqlite3

        conn = sqlite3.connect(catalog_bak)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0], 1
        )
        self.assertEqual(
            conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
        )
        conn.close()
        conn = sqlite3.connect(history_bak)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
        conn.close()

    def test_job_fields(self):
        self._write("a.csv", "a\n1\n")
        engine = schemacat.open(self.data_dir)
        job_id = engine.crawl(self.source_dir)
        info = engine.get_job(job_id)
        self.assertEqual(info["kind"], "crawl")
        self.assertEqual(info["status"], "completed")
        self.assertIsInstance(info["started_at"], float)
        self.assertIsInstance(info["ended_at"], float)
        self.assertGreaterEqual(info["ended_at"], info["started_at"])
        self.assertAlmostEqual(
            info["duration"], info["ended_at"] - info["started_at"], places=9
        )
        self.assertIsNone(info["error"])
        engine.close()


if __name__ == "__main__":
    unittest.main()
