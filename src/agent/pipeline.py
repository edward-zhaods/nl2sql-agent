"""Agent 流水线编排：生成 → 守卫 → 执行 → 自修复。

两条入口对应两段式交互（docs/design.md §2）：
- generate_preview：生成 SQL + 守卫校验，返回预览（不执行）
- execute_confirmed：对回传 SQL 重新完整校验后执行（不信任客户端），
  执行失败时带报错回喂 LLM 重试，每次重试产物重新过守卫

所有环节记录 steps 轨迹，供 UI 展示 Agent 过程。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.agent.executor import ExecResult, ExecutionError, Executor
from src.agent.generator import GenerationError, SQLGenerator
from src.agent.guard import GuardResult, SQLGuard
from src.agent.schema_provider import SchemaCatalog
from src.config import AgentConfig


@dataclass
class Step:
    name: str          # generate / guard / execute / repair
    ok: bool
    detail: str = ""


@dataclass
class Preview:
    ok: bool                       # SQL 已生成且通过守卫，可以进入确认执行
    sql: str = ""                  # 守卫规范化后的 SQL（用户确认执行的就是它）
    explanation: str = ""
    assumptions: list[str] = field(default_factory=list)
    blocked_reason: str = ""
    steps: list[Step] = field(default_factory=list)


@dataclass
class Outcome:
    success: bool
    sql: str = ""                  # 最终实际执行的 SQL（可能是修复后的）
    result: ExecResult | None = None
    error: str = ""
    repaired: bool = False
    attempts: int = 0              # 自修复重试次数
    steps: list[Step] = field(default_factory=list)


class Pipeline:
    def __init__(
        self,
        generator: SQLGenerator,
        guard: SQLGuard,
        executor: Executor,
        catalog: SchemaCatalog,
        agent_cfg: AgentConfig,
    ):
        self.generator = generator
        self.guard = guard
        self.executor = executor
        self.catalog = catalog
        self.agent_cfg = agent_cfg

    # ------------------------------------------------------- 第一段：生成预览

    def generate_preview(self, question: str) -> Preview:
        steps: list[Step] = []
        try:
            gen = self.generator.generate(question, self.catalog.to_prompt_text())
        except GenerationError as e:
            steps.append(Step("generate", False, str(e)))
            return Preview(False, blocked_reason=f"SQL 生成失败：{e}", steps=steps)
        steps.append(Step("generate", True, gen.sql))

        guard_result = self.guard.check(gen.sql)
        steps.append(Step("guard", guard_result.ok, guard_result.reason))
        if not guard_result.ok:
            return Preview(
                False,
                sql=gen.sql,
                explanation=gen.explanation,
                blocked_reason=f"[{guard_result.rule}] {guard_result.reason}",
                steps=steps,
            )
        return Preview(
            True,
            sql=guard_result.sql,
            explanation=gen.explanation,
            assumptions=gen.assumptions,
            steps=steps,
        )

    # ------------------------------------------------------- 第二段：确认执行

    def execute_confirmed(self, sql: str, question: str | None = None) -> Outcome:
        """question 提供时启用自修复；每次重试产物必须重新过守卫。"""
        steps: list[Step] = []

        guard_result = self.guard.check(sql)
        steps.append(Step("guard", guard_result.ok, guard_result.reason))
        if not guard_result.ok:
            return Outcome(
                False, sql=sql,
                error=f"[{guard_result.rule}] {guard_result.reason}", steps=steps,
            )

        current_sql = guard_result.sql
        last_error = ""
        for attempt in range(self.agent_cfg.max_repair_attempts + 1):
            try:
                result = self.executor.execute(current_sql)
                steps.append(Step("execute", True, f"{result.row_count} 行 / {result.elapsed_ms}ms"))
                return Outcome(
                    True, sql=current_sql, result=result,
                    repaired=attempt > 0, attempts=attempt, steps=steps,
                )
            except ExecutionError as e:
                last_error = str(e)
                steps.append(Step("execute", False, last_error))

            if question is None or attempt >= self.agent_cfg.max_repair_attempts:
                break

            # 自修复：报错回喂 LLM 重新生成
            try:
                gen = self.generator.generate(
                    question,
                    self.catalog.to_prompt_text(),
                    prev_sql=current_sql,
                    error_feedback=last_error,
                )
            except GenerationError as e:
                steps.append(Step("repair", False, f"修复生成失败：{e}"))
                break
            repair_guard = self.guard.check(gen.sql)
            if not repair_guard.ok:
                # 修复不能成为绕过安全校验的旁路
                steps.append(
                    Step("repair", False, f"修复产物被守卫拦截：[{repair_guard.rule}] {repair_guard.reason}")
                )
                break
            steps.append(Step("repair", True, repair_guard.sql))
            current_sql = repair_guard.sql

        return Outcome(False, sql=current_sql, error=last_error, steps=steps)
