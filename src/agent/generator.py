"""SQL 生成器：自然语言问题 → 结构化 {sql, explanation, assumptions}。

支持自修复模式：把上次失败的 SQL 与数据库报错回喂，要求重新生成。
Prompt 里的"只生成 SELECT"是第一层引导，不作为安全依据——
真正的防线是 SQLGuard（见 docs/design.md §3.6）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date


class GenerationError(Exception):
    """LLM 输出无法解析或缺少 SQL。"""


@dataclass
class GenerationResult:
    sql: str
    explanation: str = ""
    assumptions: list[str] = field(default_factory=list)


_SYSTEM_PROMPT = """你是一名资深数据分析师，负责把业务问题翻译成 SQL 查询。

规则：
1. 只生成一条 {dialect} 方言的 SELECT 查询，绝不生成任何写操作或 DDL
2. 只使用给定 Schema 中真实存在的表和列，绝不编造表名或列名
3. 涉及时间范围时，基于给出的「当前日期」计算；“最近 N 天”默认表示包含今天在内的 N 个自然日，
   例如当前日期为 2026-07-03 时，“最近 7 天”应从 2026-06-27 开始，即 DATE('2026-07-03', '-6 days')
4. 状态、类别等枚举字段，必须使用 Schema 中每列「取值:」标注里给出的真实值；
   不要把用户问题中的中文描述（如「已支付」）直接当作字段值填入 WHERE
5. 输出必须是一个 JSON 对象，除 JSON 外不要输出任何其他内容，格式：
{{"sql": "生成的 SQL", "explanation": "一两句中文说明查询思路", "assumptions": ["对问题做出的假设；无则为空数组"]}}"""

_USER_TEMPLATE = """数据库 Schema（{dialect} 方言）：
{schema_text}

当前日期：{today}

用户问题：{question}"""

_REPAIR_TEMPLATE = """
注意：你上一次为这个问题生成的 SQL 执行失败了，请修复后重新生成。
上次生成的 SQL：
{prev_sql}
数据库报错信息：
{error}"""


class SQLGenerator:
    def __init__(self, llm, dialect: str = "sqlite"):
        """llm 只需实现 chat(messages) -> str（便于测试注入 Fake）。"""
        self.llm = llm
        self.dialect = dialect

    def generate(
        self,
        question: str,
        schema_text: str,
        *,
        prev_sql: str | None = None,
        error_feedback: str | None = None,
    ) -> GenerationResult:
        user_content = _USER_TEMPLATE.format(
            dialect=self.dialect,
            schema_text=schema_text,
            today=date.today().isoformat(),
            question=question,
        )
        if error_feedback:
            user_content += _REPAIR_TEMPLATE.format(
                prev_sql=prev_sql or "（未提供）", error=error_feedback
            )
        raw = self.llm.chat(
            [
                {"role": "system", "content": _SYSTEM_PROMPT.format(dialect=self.dialect)},
                {"role": "user", "content": user_content},
            ]
        )
        data = _parse_json_object(raw)
        sql = str(data.get("sql") or "").strip()
        if not sql:
            raise GenerationError(f"LLM 输出中没有 sql 字段：{raw[:200]}")
        assumptions = data.get("assumptions") or []
        if not isinstance(assumptions, list):
            assumptions = [str(assumptions)]
        return GenerationResult(
            sql=sql,
            explanation=str(data.get("explanation") or "").strip(),
            assumptions=[str(a) for a in assumptions],
        )


def _parse_json_object(raw: str) -> dict:
    """容错解析：剥离思考标签与代码围栏后提取 JSON 对象。"""
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    raise GenerationError(f"LLM 输出无法解析为 JSON：{raw[:200]}")
