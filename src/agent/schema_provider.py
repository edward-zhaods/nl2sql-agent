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
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from src.config import DatabaseConfig, SchemaSourceConfig, SecurityConfig

# 采样枚举值时只考虑这些文本类型（大小写不敏感子串匹配）
_TEXTUAL_TYPE_HINTS = ("CHAR", "TEXT", "CLOB", "STRING", "ENUM")
# 即便基数低也不当枚举采样的列名特征：时间戳、邮箱、编号、哈希等——
# 避免把日期/标识符灌进 Prompt（小库里它们碰巧去重值也少）。
_NON_ENUM_NAME_HINTS = ("_at", "_date", "_time", "email", "phone", "code", "_no", "url", "hash", "token")


@dataclass
class ColumnInfo:
    name: str
    type: str
    comment: str = ""
    enum_values: list[str] | None = None   # 低基数列的真实取值（Layer1 注入）；None=未采样/高基数


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
        """给 LLM 的紧凑文本表示。

        低基数列会带上真实取值标注（如 `status TEXT  -- 取值: paid, refunded`），
        让模型基于库里的实际枚举写 WHERE，而不是把中文描述当成字段值瞎猜。
        """
        lines: list[str] = []
        for t in self.tables:
            lines.append(f"表 {t.name}:")
            for c in t.columns:
                notes = [c.comment] if c.comment else []
                if c.enum_values:
                    notes.append(f"取值: {', '.join(c.enum_values)}")
                suffix = f"  -- {'；'.join(notes)}" if notes else ""
                lines.append(f"  {c.name} {c.type}{suffix}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """给前端 Schema 浏览面板的 JSON 表示。"""
        return {
            "tables": [
                {
                    "name": t.name,
                    "columns": [
                        {
                            "name": c.name,
                            "type": c.type,
                            "comment": c.comment,
                            "enum_values": c.enum_values,
                        }
                        for c in t.columns
                    ],
                }
                for t in self.tables
            ]
        }

    def enum_catalog(self) -> dict[tuple[str, str], set[str]]:
        """{(表名, 列名) → 合法取值集合}，全部小写键；供语义校验器查表。"""
        out: dict[tuple[str, str], set[str]] = {}
        for t in self.tables:
            for c in t.columns:
                if c.enum_values:
                    out[(t.name.lower(), c.name.lower())] = set(c.enum_values)
        return out


def load_catalog(
    db: DatabaseConfig,
    schema_cfg: SchemaSourceConfig,
    security: SecurityConfig,
    *,
    enum_injection: bool = True,
    enum_max_cardinality: int = 30,
) -> SchemaCatalog:
    blocked = {t.lower() for t in security.blocked_tables}
    if schema_cfg.source == "ddl":
        catalog = _from_ddl(Path(schema_cfg.ddl_path), db.dialect)
    else:
        catalog = _from_introspection(db)
    catalog.tables = [t for t in catalog.tables if t.name.lower() not in blocked]
    # 枚举采样在过滤之后进行——敏感表已被剔除，绝不会去查它的值。
    # DDL 源没有活库可采样，故仅对 introspect 生效。
    if enum_injection and schema_cfg.source != "ddl":
        _inject_enum_values(db, catalog, enum_max_cardinality)
    return catalog


def _is_enum_candidate(col: ColumnInfo) -> bool:
    """文本类型、且列名不像时间戳/标识符——才作为枚举采样候选。"""
    if not any(h in col.type.upper() for h in _TEXTUAL_TYPE_HINTS):
        return False
    low = col.name.lower()
    return not any(h in low for h in _NON_ENUM_NAME_HINTS)


def _inject_enum_values(db: DatabaseConfig, catalog: SchemaCatalog, max_card: int) -> None:
    """对文本列采样：去重值数 ≤ max_card 的列，把真实取值写回 ColumnInfo.enum_values。

    仅用于读库拿元数据（只读连接），单列失败静默跳过，绝不阻断 Catalog 加载。
    注意：会对每个文本列做一次 COUNT(DISTINCT)，仅在启动时执行一次；超大库若有
    性能顾虑，可关掉 agent.enum_injection 或调低 enum_max_cardinality。
    """
    engine = create_engine(db.sqlalchemy_url(read_only=True))
    prep = engine.dialect.identifier_preparer
    try:
        with engine.connect() as conn:
            for tbl in catalog.tables:
                qt = prep.quote(tbl.name)
                for col in tbl.columns:
                    if not _is_enum_candidate(col):
                        continue
                    qc = prep.quote(col.name)
                    try:
                        n = conn.execute(
                            text(f"SELECT COUNT(DISTINCT {qc}) FROM {qt}")
                        ).scalar()
                        if not n or n > max_card:
                            continue
                        rows = conn.execute(
                            text(
                                f"SELECT DISTINCT {qc} FROM {qt} "
                                f"WHERE {qc} IS NOT NULL ORDER BY {qc} LIMIT {max_card}"
                            )
                        ).fetchall()
                    except SQLAlchemyError:
                        continue
                    vals = [r[0] for r in rows if isinstance(r[0], str) and r[0] != ""]
                    if vals:
                        col.enum_values = vals
    finally:
        engine.dispose()


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
