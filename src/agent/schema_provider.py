"""Schema 感知：两种来源输出统一的 Schema Catalog。

- introspect：SQLAlchemy Inspector 从数据库拉取元数据
- ddl：sqlglot 解析 CREATE TABLE 语句（DDL 文件）

security.blocked_tables 中的表不会出现在 Catalog 里——LLM 从一开始就看不到
敏感表（第一道防线），即使被诱导手写 SQL 访问，也会被 SQLGuard R4 拦截（第二道）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlalchemy import create_engine, inspect

from src.config import DatabaseConfig, SchemaSourceConfig, SecurityConfig


@dataclass
class ColumnInfo:
    name: str
    type: str
    comment: str = ""


@dataclass
class TableInfo:
    name: str
    columns: list[ColumnInfo] = field(default_factory=list)


@dataclass
class SchemaCatalog:
    tables: list[TableInfo] = field(default_factory=list)

    @property
    def table_names(self) -> list[str]:
        return [t.name for t in self.tables]

    def to_prompt_text(self) -> str:
        """给 LLM 的紧凑文本表示。"""
        lines: list[str] = []
        for t in self.tables:
            lines.append(f"表 {t.name}:")
            for c in t.columns:
                comment = f"  -- {c.comment}" if c.comment else ""
                lines.append(f"  {c.name} {c.type}{comment}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """给前端 Schema 浏览面板的 JSON 表示。"""
        return {
            "tables": [
                {
                    "name": t.name,
                    "columns": [
                        {"name": c.name, "type": c.type, "comment": c.comment}
                        for c in t.columns
                    ],
                }
                for t in self.tables
            ]
        }


def load_catalog(
    db: DatabaseConfig,
    schema_cfg: SchemaSourceConfig,
    security: SecurityConfig,
) -> SchemaCatalog:
    blocked = {t.lower() for t in security.blocked_tables}
    if schema_cfg.source == "ddl":
        catalog = _from_ddl(Path(schema_cfg.ddl_path), db.dialect)
    else:
        catalog = _from_introspection(db)
    catalog.tables = [t for t in catalog.tables if t.name.lower() not in blocked]
    return catalog


def _from_introspection(db: DatabaseConfig) -> SchemaCatalog:
    engine = create_engine(db.sqlalchemy_url(read_only=True))
    try:
        insp = inspect(engine)
        tables = []
        for name in sorted(insp.get_table_names()):
            columns = [
                ColumnInfo(
                    name=col["name"],
                    type=str(col["type"]),
                    comment=col.get("comment") or "",
                )
                for col in insp.get_columns(name)
            ]
            tables.append(TableInfo(name=name, columns=columns))
        return SchemaCatalog(tables=tables)
    finally:
        engine.dispose()


def _from_ddl(ddl_path: Path, dialect: str) -> SchemaCatalog:
    if not ddl_path.exists():
        raise FileNotFoundError(f"DDL 文件不存在：{ddl_path}")
    statements = sqlglot.parse(ddl_path.read_text(encoding="utf-8"), read=dialect)
    tables = []
    for st in statements:
        if not isinstance(st, exp.Create) or (st.kind or "").upper() != "TABLE":
            continue
        table_node = st.find(exp.Table)
        if table_node is None:
            continue
        columns = []
        for cd in st.find_all(exp.ColumnDef):
            kind = cd.args.get("kind")
            comment = " ".join(c.strip() for c in (cd.comments or [])).strip()
            columns.append(
                ColumnInfo(
                    name=cd.name,
                    type=kind.sql(dialect=dialect) if kind else "",
                    comment=comment,
                )
            )
        tables.append(TableInfo(name=table_node.name, columns=columns))
    return SchemaCatalog(tables=tables)
