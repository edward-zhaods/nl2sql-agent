"""受控执行器：安全守卫之外的纵深防御层。

即使守卫存在未知漏洞，执行层仍然兜底：
- SQLite 以 mode=ro 只读打开，写操作在数据库层直接失败
- MySQL/PostgreSQL 要求只读账号连接，PG 会话级 statement_timeout
- 应用层 fetchmany(max_rows + 1) 截断超大结果集
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from src.config import DatabaseConfig, SecurityConfig


class ExecutionError(Exception):
    """执行失败（语法/列名/超时等），message 会回喂给 LLM 做自修复。"""


@dataclass
class ExecResult:
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: int = 0


def _jsonable(value):
    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    return str(value)


class Executor:
    def __init__(self, db: DatabaseConfig, security: SecurityConfig):
        self.security = security
        connect_args = {}
        if db.type == "postgresql":
            connect_args["options"] = f"-c statement_timeout={security.statement_timeout_ms}"
        self._engine = create_engine(
            db.sqlalchemy_url(read_only=True),
            connect_args=connect_args,
            pool_pre_ping=True,
        )

    def execute(self, sql: str) -> ExecResult:
        max_rows = self.security.max_rows
        start = time.perf_counter()
        try:
            with self._engine.connect() as conn:
                result = conn.execute(text(sql))
                columns = list(result.keys())
                raw = result.fetchmany(max_rows + 1)
        except SQLAlchemyError as e:
            message = str(getattr(e, "orig", None) or e).strip()
            raise ExecutionError(message) from e
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        truncated = len(raw) > max_rows
        rows = [[_jsonable(v) for v in row] for row in raw[:max_rows]]
        return ExecResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=elapsed_ms,
        )

    def dispose(self) -> None:
        self._engine.dispose()
