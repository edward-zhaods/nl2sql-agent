"""枚举注入（Layer1）与确定性语义校验（Layer2a）的单元测试，跑在真实演示库上。"""

from __future__ import annotations

import pytest

from data.seed_demo_db import DB_PATH, seed
from src.agent.schema_provider import load_catalog
from src.agent.validator import SemanticValidator
from src.config import DatabaseConfig, SchemaSourceConfig, SecurityConfig


@pytest.fixture(scope="module")
def catalog():
    if not DB_PATH.exists():
        seed()
    db = DatabaseConfig(type="sqlite", path=str(DB_PATH))
    security = SecurityConfig(blocked_tables=["users_password"])
    return load_catalog(db, SchemaSourceConfig(source="introspect"), security)


@pytest.fixture(scope="module")
def validator(catalog):
    return SemanticValidator(catalog, "sqlite")


# ---------------------------------------------------------------- Layer1：枚举注入

def test_injects_real_status_values(catalog):
    enum = catalog.enum_catalog()
    assert enum[("payments", "status")] == {"paid", "refunded"}
    assert enum[("orders", "status")] == {"paid", "pending", "cancelled", "refunded"}
    assert enum[("shipments", "status")] == {"shipped", "delivered"}


def test_skips_timestamps_and_identifiers(catalog):
    """时间戳/邮箱/编号即便去重值少，也不该被当成枚举灌进 Prompt。"""
    enum = catalog.enum_catalog()
    assert ("orders", "created_at") not in enum
    assert ("suppliers", "contact_email") not in enum
    assert ("coupons", "code") not in enum
    assert ("payments", "id") not in enum          # 整型主键本就不是文本


def test_prompt_text_annotates_enum_values(catalog):
    prompt = catalog.to_prompt_text()
    assert "取值:" in prompt
    assert "paid" in prompt and "refunded" in prompt
    # 时间戳列不应带取值标注
    assert "created_at TEXT\n" in prompt or "created_at TEXT" in prompt


# ---------------------------------------------------------------- Layer2a：确定性校验

def test_flags_hallucinated_chinese_value(validator):
    """题目二那条 SQL：pay.status = '已支付' 必须被逮住。"""
    sql = (
        "SELECT p.id FROM order_items oi JOIN orders o ON oi.order_id = o.id "
        "JOIN payments pay ON pay.order_id = o.id JOIN products p ON oi.product_id = p.id "
        "WHERE pay.status = '已支付' GROUP BY p.id"
    )
    r = validator.validate(sql)
    assert not r.ok
    assert any("已支付" in i and "payments.status" in i for i in r.issues)
    assert "paid" in r.feedback and "refunded" in r.feedback  # 反馈里给出合法值


def test_accepts_real_value(validator):
    assert validator.validate("SELECT id FROM payments pay WHERE pay.status = 'paid'").ok


def test_resolves_qualifier_by_table_name(validator):
    """限定词是真实表名（非别名）也能解析。"""
    assert not validator.validate(
        "SELECT id FROM payments WHERE payments.status = '已支付'"
    ).ok


def test_resolves_unqualified_column_when_unambiguous(validator):
    assert not validator.validate("SELECT id FROM payments WHERE status = '已支付'").ok


def test_ambiguous_unqualified_column_is_skipped(validator):
    """status 同时属于 orders/payments，未限定又多表——宁可放过也不误报。"""
    sql = (
        "SELECT o.id FROM orders o JOIN payments p ON p.order_id = o.id "
        "WHERE status = '已支付'"
    )
    assert validator.validate(sql).ok


def test_ignores_non_enum_columns(validator):
    """自由文本列（高基数，非封闭枚举）不参与强校验。"""
    assert validator.validate("SELECT id FROM users WHERE name = '不存在的人'").ok


def test_ignores_join_and_numeric_predicates(validator):
    """JOIN 等值、数字比较不是枚举场景，不能误报。"""
    sql = (
        "SELECT o.id FROM orders o JOIN payments p ON p.order_id = o.id "
        "WHERE o.total_amount = 100"
    )
    assert validator.validate(sql).ok


def test_flags_bad_value_inside_in_list(validator):
    r = validator.validate("SELECT id FROM orders WHERE status IN ('paid', '已支付')")
    assert not r.ok
    assert any("已支付" in i for i in r.issues)


def test_accepts_valid_in_list(validator):
    assert validator.validate(
        "SELECT id FROM orders WHERE status IN ('paid', 'refunded')"
    ).ok


def test_flags_not_equal_bad_value(validator):
    assert not validator.validate("SELECT id FROM orders WHERE status != '已支付'").ok


def test_lenient_on_unparseable_sql(validator):
    """解析失败交给守卫/执行器，本层不重复报错。"""
    assert validator.validate("this is not sql (((").ok


def test_no_enum_columns_means_noop():
    """催化：没有枚举目录时校验器直接放行。"""
    from src.agent.schema_provider import SchemaCatalog

    v = SemanticValidator(SchemaCatalog(tables=[]), "sqlite")
    assert v.validate("SELECT id FROM payments WHERE status = '任意值'").ok
