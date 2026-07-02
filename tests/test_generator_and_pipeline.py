"""生成器 JSON 容错解析 + Pipeline 编排/自修复的离线测试（FakeLLM，不联网）。"""

from __future__ import annotations

import json

import pytest

from data.seed_demo_db import DB_PATH, seed
from src.agent.executor import Executor
from src.agent.generator import GenerationError, SQLGenerator, _parse_json_object
from src.agent.guard import SQLGuard
from src.agent.pipeline import Pipeline
from src.agent.schema_provider import load_catalog
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


def make_pipeline(env, replies: list[str], max_repair: int = 2) -> tuple[Pipeline, FakeLLM]:
    _, _, catalog, guard, executor = env
    llm = FakeLLM(replies)
    pipeline = Pipeline(
        SQLGenerator(llm, "sqlite"), guard, executor, catalog,
        AgentConfig(max_repair_attempts=max_repair),
    )
    return pipeline, llm


def test_happy_path_preview_and_execute(env):
    pipeline, _ = make_pipeline(env, [reply("SELECT COUNT(*) AS n FROM users")])
    preview = pipeline.generate_preview("有多少用户？")
    assert preview.ok
    assert "LIMIT" in preview.sql.upper()          # 守卫注入了 LIMIT
    outcome = pipeline.execute_confirmed(preview.sql)
    assert outcome.success
    assert outcome.result.rows == [[50]]
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
    assert outcome.result.rows == [[50]]
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
