"""生成器 JSON 容错解析 + Pipeline 编排/自修复的离线测试（FakeLLM，不联网）。"""

from __future__ import annotations

import json

import pytest

from data.seed_demo_db import DB_PATH, N_USERS, seed
from src.agent.executor import Executor
from src.agent.generator import GenerationError, SQLGenerator, _parse_json_object
from src.agent.guard import SQLGuard
from src.agent.pipeline import Pipeline
from src.agent.schema_provider import load_catalog
from src.agent.validator import SemanticValidator, SQLCritic
from src.config import AgentConfig, DatabaseConfig, SchemaSourceConfig, SecurityConfig


class FakeLLM:
    """按序返回预设回复，并记录收到的 messages。"""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def chat(self, messages, temperature=None) -> str:
        self.calls.append(messages)
        return self.replies.pop(0)


def reply(sql: str, explanation: str = "测试说明") -> str:
    return json.dumps({"sql": sql, "explanation": explanation, "assumptions": []}, ensure_ascii=False)


# ---------------------------------------------------------------- JSON 容错解析

def test_parse_plain_json():
    assert _parse_json_object('{"sql": "SELECT 1"}')["sql"] == "SELECT 1"


def test_parse_fenced_json():
    raw = '```json\n{"sql": "SELECT 1", "explanation": "x"}\n```'
    assert _parse_json_object(raw)["sql"] == "SELECT 1"


def test_parse_with_think_tag_and_prose():
    raw = '<think>先看看表结构……</think>好的，结果如下：{"sql": "SELECT 2"} 以上。'
    assert _parse_json_object(raw)["sql"] == "SELECT 2"


def test_parse_garbage_raises():
    with pytest.raises(GenerationError):
        _parse_json_object("我不知道怎么写这个查询")


def test_repair_prompt_includes_error_feedback():
    llm = FakeLLM([reply("SELECT 1")])
    gen = SQLGenerator(llm, "sqlite")
    gen.generate("问题", "schema", prev_sql="SELECT bad FROM users", error_feedback="no such column: bad")
    user_msg = llm.calls[0][-1]["content"]
    assert "no such column: bad" in user_msg
    assert "SELECT bad FROM users" in user_msg


def test_system_prompt_defines_recent_days_as_natural_days():
    llm = FakeLLM([reply("SELECT 1")])
    gen = SQLGenerator(llm, "sqlite")
    gen.generate("最近 7 天订单", "schema")
    system_msg = llm.calls[0][0]["content"]
    assert "最近 N 天" in system_msg
    assert "包含今天在内的 N 个自然日" in system_msg
    assert "DATE('2026-07-03', '-6 days')" in system_msg


def test_system_prompt_defers_enum_values_to_schema():
    """规则不再硬编码状态映射，改为引导模型看 Schema 的「取值:」标注。"""
    llm = FakeLLM([reply("SELECT 1")])
    gen = SQLGenerator(llm, "sqlite")
    gen.generate("已支付订单", "schema")
    system_msg = llm.calls[0][0]["content"]
    assert "不要把用户问题中的中文描述" in system_msg
    assert "取值:" in system_msg                 # 指向 Schema 里的真实枚举标注
    assert "已支付" in system_msg                 # 点名这个典型幻觉


# ---------------------------------------------------------------- Pipeline

@pytest.fixture(scope="module")
def env():
    if not DB_PATH.exists():
        seed()
    db = DatabaseConfig(type="sqlite", path=str(DB_PATH))
    security = SecurityConfig(max_rows=500, blocked_tables=["users_password"])
    catalog = load_catalog(db, SchemaSourceConfig(source="introspect"), security)
    guard = SQLGuard(security, db.dialect)
    executor = Executor(db, security)
    return db, security, catalog, guard, executor


def make_pipeline(
    env, replies: list[str], max_repair: int = 2, validator=None, critic=None
) -> tuple[Pipeline, FakeLLM]:
    _, _, catalog, guard, executor = env
    llm = FakeLLM(replies)
    pipeline = Pipeline(
        SQLGenerator(llm, "sqlite"), guard, executor, catalog,
        AgentConfig(max_repair_attempts=max_repair),
        validator=validator, critic=critic,
    )
    return pipeline, llm


# 复现题目二那条 bug SQL：pay.status 用了库里不存在的中文值「已支付」
_BAD_ENUM_SQL = (
    "SELECT p.id AS product_id, SUM(oi.quantity * oi.unit_price) AS total_sales "
    "FROM order_items oi JOIN orders o ON oi.order_id = o.id "
    "JOIN payments pay ON pay.order_id = o.id JOIN products p ON oi.product_id = p.id "
    "WHERE pay.status = '已支付' GROUP BY p.id ORDER BY total_sales DESC LIMIT 5"
)
_GOOD_ENUM_SQL = _BAD_ENUM_SQL.replace("已支付", "paid")


def test_happy_path_preview_and_execute(env):
    pipeline, _ = make_pipeline(env, [reply("SELECT COUNT(*) AS n FROM users")])
    preview = pipeline.generate_preview("有多少用户？")
    assert preview.ok
    assert "LIMIT" in preview.sql.upper()          # 守卫注入了 LIMIT
    outcome = pipeline.execute_confirmed(preview.sql)
    assert outcome.success
    assert outcome.result.rows == [[N_USERS]]
    assert not outcome.repaired


def test_preview_blocks_dangerous_generation(env):
    pipeline, _ = make_pipeline(env, [reply("DROP TABLE users")])
    preview = pipeline.generate_preview("删掉用户表")
    assert not preview.ok
    assert "R2" in preview.blocked_reason or "R3" in preview.blocked_reason


def test_execute_revalidates_client_sql(env):
    """服务端不信任客户端回传：直接把危险 SQL 发给 execute 也会被拦。"""
    pipeline, _ = make_pipeline(env, [])
    outcome = pipeline.execute_confirmed("DELETE FROM orders")
    assert not outcome.success
    assert "R2" in outcome.error or "R3" in outcome.error


def test_self_repair_loop_fixes_bad_column(env):
    """第一次列名写错 → 报错回喂 → 第二次修好。"""
    pipeline, llm = make_pipeline(
        env,
        [reply("SELECT COUNT(*) AS n FROM users")],   # 修复调用返回的正确 SQL
    )
    outcome = pipeline.execute_confirmed(
        "SELECT wrong_col FROM users", question="有多少用户？"
    )
    assert outcome.success
    assert outcome.repaired and outcome.attempts == 1
    assert outcome.result.rows == [[N_USERS]]
    # 修复调用收到了数据库报错
    assert "wrong_col" in llm.calls[0][-1]["content"]


def test_repair_product_still_guarded(env):
    """自修复产物若是危险语句，同样被守卫拦截，不会成为旁路。"""
    pipeline, _ = make_pipeline(
        env,
        [reply("DROP TABLE users")],                  # 修复时 LLM「叛变」
    )
    outcome = pipeline.execute_confirmed(
        "SELECT wrong_col FROM users", question="问题"
    )
    assert not outcome.success
    repair_steps = [s for s in outcome.steps if s.name == "repair"]
    assert repair_steps and not repair_steps[0].ok
    assert "拦截" in repair_steps[0].detail


def test_repair_attempts_capped(env):
    """重试次数受 max_repair_attempts 限制。"""
    pipeline, llm = make_pipeline(
        env,
        [reply("SELECT still_wrong FROM users"),
         reply("SELECT still_wrong2 FROM users")],
        max_repair=2,
    )
    outcome = pipeline.execute_confirmed(
        "SELECT wrong_col FROM users", question="问题"
    )
    assert not outcome.success
    assert len(llm.calls) == 2                        # 只重试了 max_repair_attempts 次
    assert outcome.error                              # 保留最后一次报错


# ---------------------------------------------------- Layer2：生成后语义校验 + 自愈

def test_preview_autofixes_hallucinated_enum(env):
    """题目二 bug 复现：第一次生成 '已支付' → 语义校验拦下 → 回喂重生成 → 修正为 'paid'。"""
    _, _, catalog, _, _ = env
    validator = SemanticValidator(catalog, "sqlite")
    pipeline, llm = make_pipeline(
        env, [reply(_BAD_ENUM_SQL), reply(_GOOD_ENUM_SQL)], validator=validator
    )
    preview = pipeline.generate_preview("已支付订单里销售额最高的 5 个商品")
    assert preview.ok
    assert "已支付" not in preview.sql
    assert "'paid'" in preview.sql
    assert not preview.warnings
    # 语义校验：第一次失败、最后一次通过
    validate_steps = [s for s in preview.steps if s.name == "validate"]
    assert len(validate_steps) == 2
    assert not validate_steps[0].ok and validate_steps[1].ok
    # 触发了一次重生成，且反馈里带上了具体非法值
    assert len(llm.calls) == 2
    assert "已支付" in llm.calls[1][-1]["content"]


def test_preview_warns_when_enum_unfixable(env):
    """模型死不悔改：用尽重试仍非法 → 不硬拦，但带 warnings 提示。"""
    _, _, catalog, _, _ = env
    validator = SemanticValidator(catalog, "sqlite")
    pipeline, llm = make_pipeline(
        env, [reply(_BAD_ENUM_SQL)] * 3, max_repair=2, validator=validator
    )
    preview = pipeline.generate_preview("已支付订单")
    assert preview.ok                                 # 有人工确认兜底，不误杀
    assert preview.warnings
    assert any("已支付" in w for w in preview.warnings)
    assert len(llm.calls) == 3                        # 初次 + 2 次重生成


def test_preview_valid_enum_passes_without_repair(env):
    """合法枚举一次过，不触发重生成。"""
    _, _, catalog, _, _ = env
    validator = SemanticValidator(catalog, "sqlite")
    pipeline, llm = make_pipeline(env, [reply(_GOOD_ENUM_SQL)], validator=validator)
    preview = pipeline.generate_preview("已支付订单")
    assert preview.ok and not preview.warnings
    assert len(llm.calls) == 1


def test_preview_llm_critic_triggers_regeneration(env):
    """确定性校验放行，但 LLM 审查挑刺 → 回喂重生成 → 再审通过。"""
    _, _, catalog, _, _ = env
    validator = SemanticValidator(catalog, "sqlite")
    critic_llm = FakeLLM([
        json.dumps({"valid": False, "issues": ["漏了 products 表的 JOIN"]}, ensure_ascii=False),
        json.dumps({"valid": True, "issues": []}, ensure_ascii=False),
    ])
    critic = SQLCritic(critic_llm, "sqlite")
    pipeline, gen_llm = make_pipeline(
        env, [reply(_GOOD_ENUM_SQL), reply(_GOOD_ENUM_SQL)],
        validator=validator, critic=critic,
    )
    preview = pipeline.generate_preview("已支付商品销售额 top5")
    assert preview.ok and not preview.warnings
    critic_steps = [s for s in preview.steps if s.name == "critic"]
    assert len(critic_steps) == 2
    assert not critic_steps[0].ok and critic_steps[1].ok
    assert len(gen_llm.calls) == 2                    # critic 挑刺触发了一次重生成
    assert len(critic_llm.calls) == 2


def test_preview_critic_skipped_when_validator_already_failed(env):
    """确定性校验没过就不浪费 LLM 审查调用（先便宜后昂贵）。"""
    _, _, catalog, _, _ = env
    validator = SemanticValidator(catalog, "sqlite")
    critic_llm = FakeLLM([json.dumps({"valid": True, "issues": []})] * 5)
    critic = SQLCritic(critic_llm, "sqlite")
    pipeline, _ = make_pipeline(
        env, [reply(_BAD_ENUM_SQL)] * 3, max_repair=2, validator=validator, critic=critic,
    )
    preview = pipeline.generate_preview("已支付订单")
    assert preview.warnings                            # 枚举一直非法
    assert len(critic_llm.calls) == 0                  # validator 先挡下，critic 未被调用
