"""CSV 模式推断：表头 + 列类型（integer / real / text）。"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class Column:
    name: str
    type: str  # "integer" | "real" | "text"


# 模式 = 有序的 Column 列表
Schema = List[Column]

_INT, _REAL, _TEXT = "integer", "real", "text"


def _cell_kind(value: str) -> str:
    """单个非空单元格的类型。"""
    try:
        int(value)
        return _INT
    except ValueError:
        pass
    try:
        float(value)
        return _REAL
    except ValueError:
        pass
    return _TEXT


def _merge(kind: str, cell: str) -> str:
    """列类型与单元格类型合并：integer+real=real，其余含 text 即 text。"""
    if kind == _TEXT or cell == _TEXT:
        return _TEXT
    if kind == _REAL or cell == _REAL:
        return _REAL
    return _INT


def infer_schema(csv_path: str) -> Schema:
    """解析 CSV 文件并推断模式。解析失败抛出 ValueError。

    首行为表头；类型由全部非空单元格推断；全空列算 text。
    """
    try:
        with open(csv_path, "r", newline="", encoding="utf-8") as fh:
            rows = csv.reader(fh)
            header = next(rows, None)
            if header is None or not header:
                raise ValueError(f"{csv_path}: 缺少表头")
            kinds = [_INT] * len(header)  # 暂用 integer 表示“尚未见更宽类型”
            seen = [False] * len(header)
            for lineno, row in enumerate(rows, start=2):
                if len(row) != len(header):
                    raise ValueError(
                        f"{csv_path}: 第 {lineno} 行列数 {len(row)} 与表头 {len(header)} 不一致"
                    )
                for idx, value in enumerate(row):
                    if value == "":
                        continue
                    seen[idx] = True
                    kinds[idx] = _merge(kinds[idx], _cell_kind(value))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise ValueError(f"{csv_path}: 解析失败: {exc}") from exc
    return [
        Column(name, kinds[idx] if seen[idx] else _TEXT)
        for idx, name in enumerate(header)
    ]


def diff_schema(old: Schema, new: Schema) -> dict:
    """相对旧模式的差异摘要：增列、删列、改类型、顺序变化。"""
    old_by_name = {c.name: c for c in old}
    new_by_name = {c.name: c for c in new}
    added = [c.name for c in new if c.name not in old_by_name]
    removed = [c.name for c in old if c.name not in new_by_name]
    retyped = [
        {"column": c.name, "from": old_by_name[c.name].type, "to": c.type}
        for c in new
        if c.name in old_by_name and old_by_name[c.name].type != c.type
    ]
    common_old = [c.name for c in old if c.name in new_by_name]
    common_new = [c.name for c in new if c.name in old_by_name]
    reordered = common_old != common_new
    return {
        "added": added,
        "removed": removed,
        "retyped": retyped,
        "reordered": reordered,
    }
