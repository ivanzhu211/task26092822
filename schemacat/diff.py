"""相邻版本之间的模式差异摘要。"""

from __future__ import annotations


def compute_diff(old_schema: list | None, new_schema: list) -> dict:
    """计算差异摘要：增列、删列、改类型、顺序变化。

    old_schema 为 None 表示这是该资产的第一个版本。
    返回 {"added": [...], "removed": [...], "type_changes": [...],
          "order_changed": bool}。
    """
    if old_schema is None:
        return {
            "added": [name for name, _ in new_schema],
            "removed": [],
            "type_changes": [],
            "order_changed": False,
        }

    old_types = dict(old_schema)
    new_types = dict(new_schema)
    added = [name for name, _ in new_schema if name not in old_types]
    removed = [name for name, _ in old_schema if name not in new_types]
    type_changes = [
        {"column": name, "from": old_types[name], "to": new_types[name]}
        for name, _ in new_schema
        if name in old_types and old_types[name] != new_types[name]
    ]
    old_common = [name for name, _ in old_schema if name in new_types]
    new_common = [name for name, _ in new_schema if name in old_types]
    order_changed = old_common != new_common
    return {
        "added": added,
        "removed": removed,
        "type_changes": type_changes,
        "order_changed": order_changed,
    }
