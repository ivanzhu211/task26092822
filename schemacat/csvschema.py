"""CSV 解析与模式（列名+类型）推断。"""

from __future__ import annotations

import csv
import json
import math
import re

_INT_RE = re.compile(r"^[+-]?\d+$")


class CsvParseError(Exception):
    """CSV 文件解析失败。"""


def _is_int(cell: str) -> bool:
    return bool(_INT_RE.match(cell))


def _is_real(cell: str) -> bool:
    try:
        value = float(cell)
    except ValueError:
        return False
    return math.isfinite(value)


def infer_schema(path: str) -> list:
    """读取 CSV（首行表头），返回 [(列名, 类型), ...]。

    类型根据全部非空单元格推断为 integer / real / text 之一，全空列算 text。
    解析失败抛出 CsvParseError。
    """
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration:
                raise CsvParseError(f"{path}: 文件为空，缺少表头")
            if not header:
                raise CsvParseError(f"{path}: 表头为空")
            width = len(header)
            all_int = [True] * width
            all_num = [True] * width
            has_value = [False] * width
            for row in reader:
                if len(row) != width:
                    raise CsvParseError(
                        f"{path}: 行列数 {len(row)} 与表头列数 {width} 不一致"
                    )
                for index, cell in enumerate(row):
                    if cell == "":
                        continue
                    has_value[index] = True
                    if not _is_int(cell):
                        all_int[index] = False
                    if not _is_real(cell):
                        all_num[index] = False
    except CsvParseError:
        raise
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise CsvParseError(f"{path}: {exc}") from exc

    schema = []
    for index, name in enumerate(header):
        if not has_value[index]:
            column_type = "text"
        elif all_int[index]:
            column_type = "integer"
        elif all_num[index]:
            column_type = "real"
        else:
            column_type = "text"
        schema.append((name, column_type))
    return schema


def schema_fingerprint(schema: list) -> str:
    """模式的规范化指纹，用于判断模式是否变化。"""
    return json.dumps(
        [[name, column_type] for name, column_type in schema],
        ensure_ascii=False,
        separators=(",", ":"),
    )
