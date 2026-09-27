"""子进程辅助脚本：执行一次 crawl 或 rollback，在指定落盘标记处停住等 SIGKILL。

用法: python tests/crash_child.py <data_dir> <source_dir> <mode> <pause_at> <marker_dir>
mode: crawl | rollback
pause_at: before_begin/after_begin/before_catalog/after_catalog/before_finish/after_finish
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schema_engine.engine import Engine  # noqa: E402

MARKERS = (
    "before_begin", "after_begin",
    "before_catalog", "after_catalog",
    "before_finish", "after_finish",
)


def main() -> None:
    data_dir, source_dir, mode, pause_at, marker_dir = sys.argv[1:6]
    os.makedirs(marker_dir, exist_ok=True)

    def emit(name):
        with open(os.path.join(marker_dir, name), "w") as fh:
            fh.write(name)
        if name == pause_at:
            while True:  # 等父进程 SIGKILL
                time.sleep(0.05)

    orig_begin = Engine._job_begin
    orig_catalog = Engine._catalog_commit
    orig_finish = Engine._job_finish

    def begin(self, *args):
        emit("before_begin")
        result = orig_begin(self, *args)
        emit("after_begin")
        return result

    def catalog(self, *args):
        emit("before_catalog")
        result = orig_catalog(self, *args)
        emit("after_catalog")
        return result

    def finish(self, *args):
        emit("before_finish")
        result = orig_finish(self, *args)
        emit("after_finish")
        return result

    Engine._job_begin = begin
    Engine._catalog_commit = catalog
    Engine._job_finish = finish

    eng = Engine.open(data_dir)
    if mode == "crawl":
        eng.crawl(source_dir)
    elif mode == "rollback":
        eng.rollback("a.csv", 1)
    else:
        raise SystemExit(f"unknown mode {mode}")
    eng.close()


if __name__ == "__main__":
    main()
