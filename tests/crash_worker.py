"""在子进程中执行一次 crawl 或 rollback，供 SIGKILL 崩溃测试使用。

用法:
    python tests/crash_worker.py <data_dir> <source_dir> crawl
    python tests/crash_worker.py <data_dir> <source_dir> rollback <asset> <version>

环境变量 SCHEMACAT_CRASH_AT 命中引擎内部崩溃点时，进程被 SIGKILL。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schemacat


def main() -> None:
    data_dir, source_dir, op = sys.argv[1], sys.argv[2], sys.argv[3]
    engine = schemacat.open(data_dir)
    if op == "crawl":
        engine.crawl(source_dir)
    elif op == "rollback":
        engine.rollback(sys.argv[4], int(sys.argv[5]))
    else:
        raise SystemExit(f"unknown op: {op}")
    engine.close()


if __name__ == "__main__":
    main()
