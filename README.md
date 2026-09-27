# task26092822

`schemacat`：数据目录的模式版本引擎（纯标准库，含 sqlite3，测试用 unittest）。

## 结构

- `schemacat/engine.py` — 引擎主类，维护 `catalog.db`（资产与模式版本）与
  `history.db`（作业记录）两个 SQLite 文件
- `schemacat/csvschema.py` — CSV 解析与列类型推断（integer / real / text，
  全空列算 text）
- `schemacat/diff.py` — 相邻版本的差异摘要（增列、删列、改类型、顺序变化）
- `schemacat/recovery.py` — open 时的恢复：running 作业前滚为 completed
  （目录提交已完成）或标记 failed（目录提交未发生）

## 用法

```python
import schemacat

engine = schemacat.open("data")        # 打开并完成恢复
job_id = engine.crawl("src")           # 扫描源目录，返回作业号
engine.rollback("a.csv", 1)            # 生成与 v1 同模式的新版本
engine.get_job(job_id)                 # 状态/起止/耗时/错误/产出（丢失标 lost）
engine.get_versions("a.csv")           # 全部版本（来源作业缺失标 missing）
engine.backup("catalog", "c.bak")      # 单独导出一致的副本
engine.close()
```

回退：引擎关闭后，用某次 `backup` 的产物覆盖对应数据库文件，并删除它的
`-wal` / `-shm` / `-journal` 旁路文件，再重新 `open`。

## 一致性约定

- 版本号/作业号取两个文件中出现过的最大值加一，绝不复用。
- 每次写入只在所属文件自己的事务里提交，不用 ATTACH 跨文件。
- 进程在任意时刻被 SIGKILL，重新 open 后不存在 running 中间态：
  completed 的作业产出全部可读，failed 的作业在 catalog 中无版本。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

- `tests/test_crash.py` — 子进程内执行 crawl/rollback，在每次落盘提交前后
  各 SIGKILL 一次，重新 open 后断言三条不变量。
- `tests/test_rollback.py` — 备份后回退 catalog / history 的一致性。
- `tests/test_engine.py` — 类型推断、失败作业、备份与查询的基本行为。
