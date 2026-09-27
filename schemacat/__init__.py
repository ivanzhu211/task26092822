"""schemacat: 数据目录的模式版本引擎（仅标准库，含 sqlite3）。"""

from .engine import Engine, open

__all__ = ["Engine", "open"]
