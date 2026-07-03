"""生成后语义校验：SQL 语法/安全都没问题，但「答得对不对」需要单独把关。

守卫（guard）只管安全（能不能执行、会不会写库），执行器只在真报错时才反馈。
像 `WHERE status = '已支付'` 这种——语法合法、能执行、静默返回 0 行——三道关全过，
结果却是错的。本模块补上第三种校验：正确性。

两层，从轻到重：
- SemanticValidator：确定性、零 LLM 调用。拿生成 SQL 里的字面量比对 Schema 采样到的
  真实枚举值，逮住「把中文描述当字段值」这类幻觉。快、准、可单测。
- SQLCritic：可选的 LLM 审查 agent（config 开关，默认关）。抓确定性规则抓不到的
  逻辑错（join 错表、聚合口径错、漏条件）。代价是多一次 LLM 往返，故按需启用。

两者都不直接改写 SQL，只产出「问题描述」——由 Pipeline 当作反馈回喂生成器重生成，
复用既有的自修复循环（见 pipeline.py），修复产物照样重新过守卫，不构成安全旁路。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from src.agent.generator import GenerationError, _parse_json_object
from src.agent.schema_provider import SchemaCatalog


@dataclass
class ValidationResult:
    ok: bool
    issues: list[str] = field(default_factory=list)

    @property
    def feedback(self) -> str:
        return " ".join(self.issues)


# 只对「真正封闭的小枚举」做强校验。像城市、商品名这类开放型分类值虽然也被注入
# Prompt 做接地，但取值集会随数据增长，拿它卡 WHERE 容易误报，故不纳入强校验。
_ENFORCE_MAX_CARDINALITY = 8


class SemanticValidator:
    """确定性枚举校验：字面量必须落在该列的真实取值集合内。

    保守优先——只在「能确定列是封闭枚举列」且「值确实不在集合里」时才报错；
    列解析不出来、不是枚举列、或枚举过大（开放型分类），一律放过，避免误杀正确 SQL。
    注入 Prompt 的取值集（可达 enum_max_cardinality）用于接地；这里只对
    ≤ enforce_max_cardinality 的封闭枚举做强校验，两个阈值分工不同。
    """

    def __init__(
        self,
        catalog: SchemaCatalog,
        dialect: str = "sqlite",
        enforce_max_cardinality: int = _ENFORCE_MAX_CARDINALITY,
    ):
        self.dialect = dialect
        self.enum = {                                      # {(表,列) → 合法值集合}
            k: v for k, v in catalog.enum_catalog().items()
            if len(v) <= enforce_max_cardinality
        }
        self._col_to_tables: dict[str, set[str]] = {}      # 列名 → 拥有该枚举列的表集合
        for (tbl, col) in self.enum:
            self._col_to_tables.setdefault(col, set()).add(tbl)

    def validate(self, sql: str) -> ValidationResult:
        if not self.enum:
            return ValidationResult(True)
        try:
            root = sqlglot.parse_one(sql, read=self.dialect)
        except Exception:
            return ValidationResult(True)   # 解析问题交给守卫/执行器，这里不重复报
        if root is None:
            return ValidationResult(True)

        alias2table = self._alias_map(root)
        query_tables = set(alias2table.values())
        seen: set[tuple[str, str, str]] = set()
        issues: list[str] = []

        for node in root.find_all(exp.EQ, exp.NEQ, exp.In):
            col, values = _column_and_strings(node)
            if col is None:
                continue
            table = self._resolve_table(col, alias2table, query_tables)
            if table is None:
                continue
            allowed = self.enum.get((table, col.name.lower()))
            if allowed is None:
                continue   # 不是已知枚举列——列是否存在交给执行器/critic 判断
            for v in values:
                if v in allowed:
                    continue
                dedup = (table, col.name.lower(), v)
                if dedup in seen:
                    continue
                seen.add(dedup)
                issues.append(
                    f"字段 {table}.{col.name} 使用了非法取值 '{v}'——数据库中不存在该值，"
                    f"合法取值为 {sorted(allowed)}；请改用正确的英文枚举值。"
                )
        return ValidationResult(not issues, issues)

    # ------------------------------------------------------------------
    @staticmethod
    def _alias_map(root: exp.Expression) -> dict[str, str]:
        """别名/表名（小写）→ 真实表名（小写）。"""
        m: dict[str, str] = {}
        for tbl in root.find_all(exp.Table):
            if not tbl.name:
                continue
            real = tbl.name.lower()
            m[real] = real
            if tbl.alias:
                m[tbl.alias.lower()] = real
        return m

    def _resolve_table(
        self, col: exp.Column, alias2table: dict[str, str], query_tables: set[str]
    ) -> str | None:
        qualifier = (col.table or "").lower()
        if qualifier:
            return alias2table.get(qualifier, qualifier)
        # 未限定列名：仅当它在本次查询涉及的表里唯一对应某个枚举列时才敢解析
        cands = self._col_to_tables.get(col.name.lower(), set()) & query_tables
        return next(iter(cands)) if len(cands) == 1 else None


def _column_and_strings(node: exp.Expression) -> tuple[exp.Column | None, list[str]]:
    """从比较节点里抽出 (列, [字符串字面量])；抽不出返回 (None, [])。"""
    if isinstance(node, (exp.EQ, exp.NEQ)):
        left, right = node.this, node.expression
        if isinstance(left, exp.Column):
            col, lit = left, right
        elif isinstance(right, exp.Column):
            col, lit = right, left
        else:
            return None, []
        if isinstance(lit, exp.Literal) and lit.is_string:
            return col, [lit.this]
        return None, []
    if isinstance(node, exp.In):
        col = node.this
        if not isinstance(col, exp.Column):
            return None, []
        vals = [e.this for e in (node.expressions or []) if isinstance(e, exp.Literal) and e.is_string]
        return (col, vals) if vals else (None, [])
    return None, []


# ---------------------------------------------------------------- Layer2b：LLM 审查

@dataclass
class CriticResult:
    ok: bool
    issues: list[str] = field(default_factory=list)


_CRITIC_SYSTEM = """你是一名严格的 SQL 审查员，判断候选 SQL 是否正确回答了用户问题。
逐项检查：
1. 只使用 Schema 中真实存在的表和列，没有编造
2. JOIN 的关联键与外键关系是否正确，有没有连错表
3. 聚合、分组、排序的口径是否与问题一致
4. 过滤条件是否正确；枚举字段的值必须是 Schema「取值:」标注里的真实值，绝不能是中文描述
5. 是否遗漏了必要条件，或多加了问题没要求的条件
只输出一个 JSON 对象，除 JSON 外不要输出任何内容：
{"valid": true/false, "issues": ["用中文描述每个问题；valid=true 时为空数组"]}"""

_CRITIC_USER = """数据库 Schema：
{schema_text}

用户问题：{question}

候选 SQL：
{sql}"""


class SQLCritic:
    """LLM 审查 agent。自身故障（解析失败等）时默认放行，绝不阻断主流程。"""

    def __init__(self, llm, dialect: str = "sqlite"):
        self.llm = llm
        self.dialect = dialect

    def review(self, question: str, schema_text: str, sql: str) -> CriticResult:
        raw = self.llm.chat(
            [
                {"role": "system", "content": _CRITIC_SYSTEM},
                {"role": "user", "content": _CRITIC_USER.format(
                    schema_text=schema_text, question=question, sql=sql
                )},
            ],
            temperature=0,
        )
        try:
            data = _parse_json_object(raw)
        except GenerationError:
            return CriticResult(True)   # 审查器解析失败 → 不冤枉 SQL
        valid = bool(data.get("valid", True))
        issues = data.get("issues") or []
        if not isinstance(issues, list):
            issues = [str(issues)]
        issues = [str(i) for i in issues if str(i).strip()]
        return CriticResult(valid and not issues, issues)
