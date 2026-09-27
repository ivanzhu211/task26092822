# task26092822

数据目录的模式版本引擎（纯标准库，含 sqlite3；测试用 unittest）。

## 结构

- `schema_engine/infer.py` — CSV 解析与列类型推断（integer / real / text，全空列算 text），模式差异摘要。
- `schema_engine/engine.py` — `Engine`：`open / crawl / rollback / get_job / get_versions / backup / close`。
- 数据目录下两个独立 SQLite 文件：`catalog.db`（资产与模式版本）、`history.db`（作业与产出清单）。

## 用法

```python
from schema_engine import Engine

with Engine.open("/path/to/data_dir") as eng:
    job_id = eng.crawl("/path/to/source_dir")   # 一次扫描一个作业
    eng.rollback("a.csv", 1)                    # 回退到 v1 的模式，生成新版本
    eng.get_job(job_id)                         # 状态/起止/耗时/错误/产出
    eng.get_versions("a.csv")                   # 全部版本，来源缺失会标记
    eng.backup("catalog", "/backups/catalog.db")  # 或 "history"，各自独立备份
```

## 回退（恢复备份）

引擎关闭后，用备份覆盖对应数据库文件，并删除其 `-wal` / `-shm` / `-journal` 旁路文件，再重新 `open`。

## 设计要点

- 每次写入只在自己文件的事务里提交：history(running+产出清单) → catalog(版本) → history(completed)。
- 崩溃恢复：`open` 时把 running 作业收口——产出在 catalog 中齐全则前滚为 completed，否则删除其 catalog 版本并回滚为 failed。
- 编号不复用：新版本号 / 作业号取两个文件中已出现过的最大值加一。
- 不静默错配：`get_job` 产出在 catalog 读不到或不一致时标 `lost`；`get_versions` 来源作业缺失时标 `origin_missing`。

## 测试

```
python3 -m unittest discover -s tests -v
```

- `tests/test_engine.py` — 功能与备份/回退对称场景。
- `tests/test_crash.py` + `tests/crash_child.py` — 子进程内在每次落盘提交前后各 SIGKILL 一次，重开断言三条性质。
