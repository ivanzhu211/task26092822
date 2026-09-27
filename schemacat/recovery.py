"""open 时的恢复：消除 running 中间态，保证两文件各自一致。"""

from __future__ import annotations

import json
import time

_INTERRUPTED_ERROR = "interrupted: 进程在作业完成前被终止"


def recover(catalog_conn, history_conn) -> None:
    """对两个连接执行恢复（前滚策略）。

    恢复后：不存在停在 running 的作业；每个作业要么 completed 且产出全部
    可读，要么 failed 且 catalog 中没有归属于它的版本。

    对每个 running 作业：
    - 若 catalog 中已存在归属于它的版本（目录提交已完成），则将作业前滚为
      completed，并按 catalog 中的实际内容重建产出清单；
    - 否则将作业标记为 failed（目录提交未发生，catalog 中不会有它的版本）。
    """
    running = history_conn.execute(
        "SELECT id, kind FROM jobs WHERE status = 'running'"
    ).fetchall()
    if not running:
        return
    recovered_at = time.time()
    for job_id, kind in running:
        if kind in ("crawl", "rollback"):
            produced = catalog_conn.execute(
                "SELECT a.path, v.version, v.fingerprint FROM versions v"
                " JOIN assets a ON a.id = v.asset_id"
                " WHERE v.job_id = ? ORDER BY a.path, v.version",
                (job_id,),
            ).fetchall()
        else:
            produced = []
        if produced:
            outputs = [
                {"asset": path, "version": version, "fingerprint": fingerprint}
                for path, version, fingerprint in produced
            ]
            history_conn.execute(
                "UPDATE jobs SET status = 'completed', ended_at = ?,"
                " outputs_json = ? WHERE id = ?",
                (recovered_at, json.dumps(outputs, ensure_ascii=False), job_id),
            )
        else:
            history_conn.execute(
                "UPDATE jobs SET status = 'failed', ended_at = ?, error = ?"
                " WHERE id = ?",
                (recovered_at, _INTERRUPTED_ERROR, job_id),
            )
    history_conn.commit()
