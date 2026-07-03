"""Agent 流水线编排：生成 → 守卫 → 语义校验 → 执行 → 自修复。

两条入口对应两段式交互（docs/design.md §2）：
- generate_preview：生成 SQL → 守卫（安全）→ 语义校验（正确性）→ 返回预览（不执行）；
  语义校验发现枚举幻觉等问题时带反馈回喂 LLM 重生成，产物重新过守卫+校验
- execute_confirmed：对回传 SQL 重新完整校验后执行（不信任客户端），
  执行失败时带报错回喂 LLM 重试，每次重试产物重新过守卫

安全拦截是终态（不靠模型自我说服绕过）；语义存疑在用尽重试后降级为警告而非硬拦。
所有环节记录 steps 轨迹，供 UI 展示 Agent 过程。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.agent.executor import ExecResult, ExecutionError, Executor
from src.agent.generator import GenerationError, SQLGenerator
from src.agent.guard import GuardResult, SQLGuard
from src.agent.schema_provider import SchemaCatalog
from src.agent.validator import SemanticValidator, SQLCritic
from src.config import AgentConfig

# 语义校验失败时，回喂给生成器的反馈前缀
_SEMANTIC_FEEDBACK = "上一次生成的 SQL 语义有误，请修正后重新生成。问题："


@dataclass
class Step:
    name: str          # generate / guard / validate / critic / execute / repair
    ok: bool
    detail: str = ""


@dataclass
class Preview:
    ok: bool                       # SQL 已生成且通过守卫，可以进入确认执行
    sql: str = ""                  # 守卫规范化后的 SQL（用户确认执行的就是它）
    explanation: str = ""
    assumptions: list[str] = field(default_factory=list)
    blocked_reason: str = ""
    warnings: list[str] = field(default_factory=list)  # 语义存疑但已用尽重生成：提示而不硬拦
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
        validator: SemanticValidator | None = None,
        critic: SQLCritic | None = None,
    ):
        self.generator = generator
        self.guard = guard
        self.executor = executor
        self.catalog = catalog
        self.agent_cfg = agent_cfg
        self.validator = validator          # Layer2a：确定性枚举校验（None=关闭）
        self.critic = critic                # Layer2b：LLM 审查（None=关闭）

    # ------------------------------------------------------- 第一段：生成预览

    def generate_preview(self, question: str) -> Preview:
        """生成 → 守卫 → 语义校验（+可选 LLM 审查）。

        语义校验不需要真执行，故整个「发现问题→回喂重生成」的闭环放在预览阶段完成，
        用户在确认前看到的就是已经修正过的 SQL。安全拦截是终态，不参与重生成
        （安全问题不能靠模型自我说服绕过）。
        """
        steps: list[Step] = []
        schema_text = self.catalog.to_prompt_text()
        prev_sql: str | None = None
        feedback: str | None = None
        best: tuple | None = None   # (sql, explanation, assumptions, issues) 供用尽重试后兜底

        for _ in range(self.agent_cfg.max_repair_attempts + 1):
            try:
                gen = self.generator.generate(
                    question, schema_text, prev_sql=prev_sql, error_feedback=feedback
                )
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

            issues = self._semantic_check(question, schema_text, guard_result.sql, steps)
            if not issues:
                return Preview(
                    True,
                    sql=guard_result.sql,
                    explanation=gen.explanation,
                    assumptions=gen.assumptions,
                    steps=steps,
                )

            best = (guard_result.sql, gen.explanation, gen.assumptions, issues)
            prev_sql = guard_result.sql
            feedback = _SEMANTIC_FEEDBACK + " ".join(issues)

        # 重生成次数用尽仍有疑问：不硬拦——校验可能误报，且后面还有人工确认环节。
        # 把 SQL 连同警告一起返回，让用户带着风险提示自行决定。
        sql, explanation, assumptions, issues = best
        return Preview(
            True,
            sql=sql,
            explanation=explanation,
            assumptions=assumptions,
            warnings=issues,
            steps=steps,
        )

    def _semantic_check(
        self, question: str, schema_text: str, sql: str, steps: list[Step]
    ) -> list[str]:
        """跑语义校验层，返回问题列表（空=通过）。确定性校验先行，过了才动用 LLM 审查。"""
        issues: list[str] = []
        if self.validator is not None:
            vr = self.validator.validate(sql)
            steps.append(Step("validate", vr.ok, vr.feedback or "通过语义校验"))
            issues += vr.issues
        if not issues and self.critic is not None:
            cr = self.critic.review(question, schema_text, sql)
            steps.append(Step("critic", cr.ok, " ".join(cr.issues) or "通过 AI 审查"))
            issues += cr.issues
        return issues

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
