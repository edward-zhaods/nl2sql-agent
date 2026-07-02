"""SQLGuard 安全测试套件：恶意/危险语句必须全部被拦截，正常查询必须放行。

这是 PDF 硬性要求（默认仅 SELECT、拦截危险语句、强制 LIMIT）的可复现证据：
    .venv/bin/pytest tests/test_guard_security.py -v
"""

from __future__ import annotations

import pytest

from src.agent.guard import SQLGuard
from src.config import SecurityConfig


def make_guard(**overrides) -> SQLGuard:
    defaults = dict(max_rows=500, blocked_tables=["users_password"])
    defaults.update(overrides)
    return SQLGuard(SecurityConfig(**defaults), dialect="sqlite")


# ---------------------------------------------------------------- 必须拦截

BLOCKED_CASES = [
    # 直接写操作
    ("DROP TABLE users", "直接 DROP"),
    ("DELETE FROM users", "直接 DELETE"),
    ("UPDATE users SET name = 'x'", "直接 UPDATE"),
    ("INSERT INTO users (id) VALUES (1)", "直接 INSERT"),
    ("TRUNCATE TABLE users", "TRUNCATE"),
    ("ALTER TABLE users ADD COLUMN hacked INT", "ALTER"),
    ("CREATE TABLE evil (id INT)", "CREATE"),
    # 多语句注入
    ("SELECT 1; DROP TABLE users", "分号多语句注入"),
    ("SELECT * FROM users; DELETE FROM orders", "查询后追加删除"),
    # 混淆绕过（正则黑名单的经典盲区）
    ("DR/**/OP TABLE users", "注释混淆 DROP"),
    ("dRoP tAbLe users", "大小写混淆"),
    # 嵌套/变体写操作
    ("WITH t AS (SELECT 1) DELETE FROM users", "CTE 掩护的 DELETE"),
    ("SELECT * INTO backup FROM users", "SELECT INTO 建表"),
    # 环境/元数据操作
    ("PRAGMA table_info(users)", "PRAGMA"),
    ("ATTACH DATABASE '/tmp/evil.db' AS evil", "ATTACH"),
    ("GRANT ALL ON users TO attacker", "GRANT"),
    # 危险函数
    ("SELECT load_extension('/tmp/evil.so')", "load_extension"),
    # 敏感表访问（含大小写与 JOIN 侧路）
    ("SELECT * FROM users_password", "直查敏感表"),
    ("SELECT * FROM USERS_PASSWORD", "敏感表大小写绕过"),
    ("SELECT u.name, p.password_hash FROM users u JOIN users_password p ON u.id = p.user_id",
     "JOIN 侧路访问敏感表"),
    ("SELECT * FROM (SELECT * FROM users_password)", "子查询访问敏感表"),
]


@pytest.mark.parametrize("sql, label", BLOCKED_CASES, ids=[c[1] for c in BLOCKED_CASES])
def test_dangerous_sql_is_blocked(sql: str, label: str):
    result = make_guard().check(sql)
    assert not result.ok, f"[{label}] 应被拦截但通过了：{sql}"
    assert result.rule, "拦截必须给出命中的规则编号"
    assert result.reason, "拦截必须给出明确的中文提示"


def test_multi_statement_hits_rule_r1():
    result = make_guard().check("SELECT 1; DROP TABLE users")
    assert result.rule == "R1"
    assert "多语句" in result.reason


def test_drop_reason_is_explicit():
    result = make_guard().check("DROP TABLE users")
    assert not result.ok
    assert "拦截" in result.reason or "拒绝" in result.reason or "禁止" in result.reason


# ---------------------------------------------------------------- 必须放行

ALLOWED_CASES = [
    "SELECT * FROM users",
    "SELECT city, COUNT(*) AS n FROM users GROUP BY city ORDER BY n DESC",
    """SELECT u.city, SUM(o.total_amount) AS gmv
       FROM orders o JOIN users u ON o.user_id = u.id
       WHERE o.status = 'paid' GROUP BY u.city""",
    "SELECT * FROM orders WHERE created_at >= datetime('now', '-30 days')",
    "WITH top AS (SELECT user_id, SUM(total_amount) s FROM orders GROUP BY user_id) "
    "SELECT * FROM top ORDER BY s DESC",
    "SELECT id FROM users UNION SELECT id FROM products",
]


@pytest.mark.parametrize("sql", ALLOWED_CASES)
def test_normal_queries_pass(sql: str):
    result = make_guard().check(sql)
    assert result.ok, f"正常查询被误拦：{result.reason}"
    assert result.sql, "通过时必须返回规范化 SQL"


# ---------------------------------------------------------------- LIMIT 强制

def test_limit_injected_when_missing():
    result = make_guard().check("SELECT * FROM users")
    assert result.ok
    assert "LIMIT 500" in result.sql.upper()


def test_limit_clamped_to_max_rows():
    result = make_guard().check("SELECT * FROM orders LIMIT 100000")
    assert result.ok
    assert "LIMIT 500" in result.sql.upper()
    assert "100000" not in result.sql


def test_small_limit_kept():
    result = make_guard().check("SELECT * FROM orders LIMIT 10")
    assert result.ok
    assert "LIMIT 10" in result.sql.upper()


# ---------------------------------------------------------------- 白名单模式 & 规范化

def test_allowed_tables_whitelist_mode():
    guard = make_guard(allowed_tables=["users"])
    assert guard.check("SELECT * FROM users").ok
    result = guard.check("SELECT * FROM orders")
    assert not result.ok
    assert result.rule == "R4"


def test_cte_alias_not_treated_as_real_table():
    guard = make_guard(allowed_tables=["orders"])
    result = guard.check(
        "WITH x AS (SELECT * FROM orders) SELECT * FROM x"
    )
    assert result.ok, f"CTE 别名被误判为真实表：{result.reason}"


def test_executes_normalized_sql_not_original():
    """通过校验后返回的是 AST 重渲染的 SQL，注释被结构性剥离。"""
    result = make_guard().check("SELECT /* 恶意注释 */ id FROM users -- 尾注释")
    assert result.ok
    assert "恶意注释" not in result.sql
    assert "尾注释" not in result.sql
