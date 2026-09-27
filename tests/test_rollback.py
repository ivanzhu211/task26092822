"""备份/回退一致性测试。

- catalog 回退：先 crawl 出 v1、v2，备份 catalog，再 crawl 出 v3，关闭后
  用备份覆盖 catalog 并删除旁路文件。重新打开后：作业记录里的 v3 标为
  已丢失，当前版本为 v2，再 crawl 新模式得到 v4，且对已丢失的 v3 执行
  rollback 必须报错。
- history 回退（对称）：备份 history 后再 crawl 出 v3，恢复 history 后：
  作业号不得复用，catalog 中 v3 显示来源缺失。
"""

import os
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import schemacat

SCHEMA_V1 = "id,name\n1,alice\n2,bob\n"
SCHEMA_V2 = "id,name,score\n1,alice,9\n2,bob,8\n"
SCHEMA_V3 = "id,name,score,extra\n1,alice,9,x\n2,bob,8,y\n"
SCHEMA_V4 = "id,name,score,extra,note\n1,alice,9,x,hi\n2,bob,8,y,yo\n"


def restore_backup(data_dir, which, backup_path):
    """引擎关闭后，用备份覆盖对应数据库文件并删除旁路文件。"""
    target = os.path.join(data_dir, f"{which}.db")
    shutil.copyfile(backup_path, target)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = target + suffix
        if os.path.exists(sidecar):
            os.remove(sidecar)


class RollbackConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="schemacat_rollback_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data_dir = os.path.join(self.tmp, "data")
        self.source_dir = os.path.join(self.tmp, "src")
        os.makedirs(self.source_dir)

    def _write_source(self, content):
        with open(os.path.join(self.source_dir, "a.csv"), "w") as handle:
            handle.write(content)

    def _crawl_three_versions(self, backup_which):
        """crawl 出 v1、v2，备份 backup_which，再 crawl 出 v3，关闭引擎。"""
        engine = schemacat.open(self.data_dir)
        self._write_source(SCHEMA_V1)
        job1 = engine.crawl(self.source_dir)
        self._write_source(SCHEMA_V2)
        job2 = engine.crawl(self.source_dir)
        backup_path = os.path.join(self.tmp, f"{backup_which}.bak")
        engine.backup(backup_which, backup_path)
        self._write_source(SCHEMA_V3)
        job3 = engine.crawl(self.source_dir)
        engine.close()
        return job1, job2, job3, backup_path

    def test_catalog_rollback(self):
        job1, job2, job3, backup_path = self._crawl_three_versions("catalog")
        restore_backup(self.data_dir, "catalog", backup_path)

        engine = schemacat.open(self.data_dir)

        # 作业记录里的 v3 明确标为已丢失
        info3 = engine.get_job(job3)
        self.assertEqual(info3["status"], "completed")
        self.assertEqual(
            [(o["asset"], o["version"], o["status"]) for o in info3["outputs"]],
            [("a.csv", 3, "lost")],
        )

        # 当前版本是 v2，catalog 里只有 v1、v2
        versions = engine.get_versions("a.csv")
        self.assertEqual([v["version"] for v in versions], [1, 2])
        self.assertTrue(all(v["source"] == "ok" for v in versions))

        # 对已丢失的 v3 执行 rollback 必须报错
        with self.assertRaises(KeyError):
            engine.rollback("a.csv", 3)

        # 再 crawl 一个新模式：版本号取两文件最大值加一，得到 v4
        self._write_source(SCHEMA_V4)
        job4 = engine.crawl(self.source_dir)
        self.assertGreater(job4, job3)
        outputs = engine.get_job(job4)["outputs"]
        self.assertIn(("a.csv", 4, "ok"), [(o["asset"], o["version"], o["status"]) for o in outputs])
        current = engine.get_versions("a.csv")[-1]
        self.assertEqual(current["version"], 4)
        self.assertEqual(current["source"], "ok")
        engine.close()

    def test_history_rollback(self):
        job1, job2, job3, backup_path = self._crawl_three_versions("history")
        restore_backup(self.data_dir, "history", backup_path)

        engine = schemacat.open(self.data_dir)

        # history 里没有 job3：查询报错，且不得被复原
        with self.assertRaises(KeyError):
            engine.get_job(job3)

        # catalog 中 v3 仍在，但来源作业明确标为缺失
        versions = engine.get_versions("a.csv")
        self.assertEqual([v["version"] for v in versions], [1, 2, 3])
        sources = {v["version"]: v["source"] for v in versions}
        self.assertEqual(sources[1], "ok")
        self.assertEqual(sources[2], "ok")
        self.assertEqual(sources[3], "missing")

        # 作业号不得复用：catalog 还记得 job3，新作业号必须大于它
        self._write_source(SCHEMA_V4)
        job4 = engine.crawl(self.source_dir)
        self.assertGreater(job4, job3)
        outputs = engine.get_job(job4)["outputs"]
        self.assertIn(("a.csv", 4, "ok"), [(o["asset"], o["version"], o["status"]) for o in outputs])
        current = engine.get_versions("a.csv")[-1]
        self.assertEqual(current["version"], 4)
        self.assertEqual(current["source"], "ok")
        engine.close()


if __name__ == "__main__":
    unittest.main()
