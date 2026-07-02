"""SchemaProvider 与 Executor 的集成测试（跑在真实演示库上）。"""

from __future__ import annotations

import pytest

from data.seed_demo_db import DB_PATH, seed
from src.agent.executor import ExecutionError, Executor, ExecResult
from src.agent.schema_provider import load_catalog
from src.config import DatabaseConfig, SchemaSourceConfig, SecurityConfig

BUSINESS_TABLES = {"users", "products", "orders", "order_items"}


@pytest.fixture(scope="session")
def demo_db():
    if not DB_PATH.exists():
        seed()
    return DB_PATH


@pytest.fixture(scope="session")
def db_cfg(demo_db) -> DatabaseConfig:
    return DatabaseConfig(type="sqlite", path=str(demo_db))


@pytest.fixture(scope="session")
def security() -> SecurityConfig:
    return SecurityConfig(max_rows=10, blocked_tables=["users_password"])


# ---------------------------------------------------------------- SchemaProvider

def test_introspect_excludes_blocked_tables(db_cfg, security):
    catalog = load_catalog(db_cfg, SchemaSourceConfig(source="introspect"), security)
    assert set(catalog.table_names) == BUSINESS_TABLES
    assert "users_password" not in catalog.table_names


def test_ddl_mode_matches_introspection(db_cfg, security):
    catalog = load_catalog(
        db_cfg,
        SchemaSourceConfig(source="ddl", ddl_path="data/schema.sql"),
        security,
    )
    assert set(catalog.table_names) == BUSINESS_TABLES


def test_prompt_text_contains_columns(db_cfg, security):
    catalog = load_catalog(db_cfg, SchemaSourceConfig(source="introspect"), security)
    prompt = catalog.to_prompt_text()
    assert "表 orders" in prompt
    assert "total_amount" in prompt


# ---------------------------------------------------------------- Executor

@pytest.fixture(scope="session")
def executor(db_cfg, security) -> Executor:
    return Executor(db_cfg, security)


def test_simple_query(executor):
    result: ExecResult = executor.execute("SELECT COUNT(*) AS n FROM users")
    assert result.columns == ["n"]
    assert result.rows == [[50]]
    assert not result.truncated


def test_truncation_at_max_rows(executor):
    result = executor.execute("SELECT * FROM orders")
    assert result.row_count == 10          # max_rows=10
    assert result.truncated is True


def test_readonly_connection_rejects_write(executor):
    """纵深防御第 3 层：即使守卫全破，只读连接也写不进去。"""
    with pytest.raises(ExecutionError) as exc_info:
        executor.execute(
            "INSERT INTO users (id, name, email, city, created_at) "
            "VALUES (999, 'x', 'x@x.com', '北京', '2026-01-01')"
        )
    assert "readonly" in str(exc_info.value).lower()


def test_bad_column_raises_execution_error(executor):
    """列名写错 → ExecutionError，message 供自修复循环回喂 LLM。"""
    with pytest.raises(ExecutionError) as exc_info:
        executor.execute("SELECT no_such_column FROM users")
    assert "no_such_column" in str(exc_info.value)
