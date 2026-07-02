"""SQL 安全守卫：基于 sqlglot AST 的白名单校验（设计见 docs/design.md §3.3）。

原则：
- 白名单思路——只放行已知安全的结构，而不是枚举危险关键字（正则黑名单可被
  注释混淆、多语句、CTE 嵌套写操作绕过）
- 校验通过后返回 AST 重渲染的规范化 SQL，执行的不是用户/LLM 原文，
  注释与混淆在重渲染时被结构性消除
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from src.config import SecurityConfig

# R3 全树节点黑名单：任何位置（含 CTE、子查询）出现即拦截。
# 按名称动态取，兼容不同 sqlglot 版本的节点命名差异。
_BLOCKED_NODE_NAMES = [
    "Insert", "Update", "Delete", "Drop", "Create", "Alter", "AlterTable",
    "TruncateTable", "Truncate", "Grant", "Revoke", "Merge", "Use", "Command",
    "Pragma", "Attach", "Detach", "Into", "Transaction", "Rollback", "Commit",
    "Set", "Copy", "LoadData", "Call", "Kill",
]
BLOCKED_NODES: tuple[type, ...] = tuple(
    getattr(exp, name) for name in _BLOCKED_NODE_NAMES if hasattr(exp, name)
)

# 节点类型 → 展示给用户的操作名
_NODE_LABELS = {
    "Insert": "INSERT", "Update": "UPDATE", "Delete": "DELETE", "Drop": "DROP",
    "Create": "CREATE", "Alter": "ALTER", "AlterTable": "ALTER TABLE",
    "TruncateTable": "TRUNCATE", "Truncate": "TRUNCATE", "Grant": "GRANT",
    "Revoke": "REVOKE", "Merge": "MERGE", "Use": "USE", "Command": "非查询命令",
    "Pragma": "PRAGMA", "Attach": "ATTACH", "Detach": "DETACH",
    "Into": "SELECT INTO", "Transaction": "事务控制", "Rollback": "ROLLBACK",
    "Commit": "COMMIT", "Set": "SET", "Copy": "COPY", "LoadData": "LOAD DATA",
    "Call": "CALL", "Kill": "KILL",
}


@dataclass
class GuardResult:
    ok: bool
    sql: str = ""                                  # 通过时：规范化后的可执行 SQL
    rule: str = ""                                 # 拦截时：命中的规则编号
    reason: str = ""                               # 人类可读的通过/拦截说明
    tables: list[str] = field(default_factory=list)


class SQLGuard:
    """六条规则顺序执行，任一失败即拦截并返回具体原因。"""

    def __init__(self, security: SecurityConfig, dialect: str = "sqlite"):
        self.security = security
        self.dialect = dialect
        self._blocked_tables = {t.lower() for t in security.blocked_tables}
        self._allowed_tables = {t.lower() for t in security.allowed_tables}
        self._blocked_funcs = {f.lower() for f in security.blocked_functions}

    def check(self, sql: str) -> GuardResult:
        # R1 必须成功解析，且恰好一条语句（拦截多语句注入）
        try:
            statements = [s for s in sqlglot.parse(sql or "", read=self.dialect) if s is not None]
        except Exception as e:  # ParseError / TokenizeError 等
            return GuardResult(False, rule="R1", reason=f"SQL 无法解析，已拒绝：{e}")
        if not statements:
            return GuardResult(False, rule="R1", reason="空语句")
        if len(statements) > 1:
            return GuardResult(
                False, rule="R1",
                reason=f"检测到 {len(statements)} 条语句，禁止多语句执行（防注入）",
            )
        root = statements[0]

        # R2 根节点类型白名单（默认仅 SELECT / UNION）
        allowed_roots = self._allowed_root_types()
        if not isinstance(root, allowed_roots):
            label = _NODE_LABELS.get(type(root).__name__, type(root).__name__)
            return GuardResult(
                False, rule="R2",
                reason=f"仅允许 {'/'.join(self.security.allowed_statements)} 查询，"
                       f"当前语句是 {label}，已拦截",
            )

        # R3 全树遍历节点黑名单（拦截 CTE/子查询里嵌套的写操作、SELECT INTO、PRAGMA 等）
        bad = next(iter(root.find_all(*BLOCKED_NODES)), None) if BLOCKED_NODES else None
        if bad is not None:
            label = _NODE_LABELS.get(type(bad).__name__, type(bad).__name__)
            return GuardResult(
                False, rule="R3",
                reason=f"语句内部包含危险操作 {label}（嵌套写操作同样会被拦截）",
            )

        # R4 表级黑/白名单（CTE 别名不算真实表）
        cte_names = {c.alias_or_name.lower() for c in root.find_all(exp.CTE)}
        tables = sorted(
            {t.name.lower() for t in root.find_all(exp.Table) if t.name} - cte_names
        )
        for t in tables:
            if t in self._blocked_tables:
                return GuardResult(
                    False, rule="R4", reason=f"表 {t} 为受保护的敏感表，禁止访问"
                )
        if self._allowed_tables:
            outside = set(tables) - self._allowed_tables
            if outside:
                return GuardResult(
                    False, rule="R4",
                    reason=f"表 {', '.join(sorted(outside))} 不在白名单中，禁止访问",
                )

        # R5 危险函数黑名单
        for func in root.find_all(exp.Func):
            names = set()
            try:
                names.add(func.sql_name().lower())
            except Exception:
                pass
            if isinstance(func, exp.Anonymous) and func.name:
                names.add(func.name.lower())
            hit = names & self._blocked_funcs
            if hit:
                return GuardResult(
                    False, rule="R5", reason=f"禁止调用函数 {hit.pop()}"
                )

        # R6 LIMIT 强制：无则注入 max_rows；已有则 clamp 到 min(原值, max_rows)
        root = self._enforce_limit(root)

        # 执行的是 AST 重渲染后的规范化 SQL，不是原文；comments=False 剥离全部注释
        normalized = root.sql(dialect=self.dialect, comments=False)
        return GuardResult(True, sql=normalized, tables=tables, reason="通过全部安全校验")

    # ------------------------------------------------------------------

    def _allowed_root_types(self) -> tuple[type, ...]:
        types: list[type] = []
        for stmt in self.security.allowed_statements:
            if stmt.upper() == "SELECT":
                for name in ("Select", "Union", "SetOperation", "Subquery"):
                    if hasattr(exp, name):
                        types.append(getattr(exp, name))
        return tuple(types) or (exp.Select,)

    def _enforce_limit(self, root: exp.Expression) -> exp.Expression:
        max_rows = self.security.max_rows
        limit_node = root.args.get("limit")
        if isinstance(limit_node, exp.Limit):
            lit = limit_node.expression
            if isinstance(lit, exp.Literal) and lit.is_int:
                if int(lit.this) > max_rows:
                    limit_node.set("expression", exp.Literal.number(max_rows))
            else:
                # LIMIT 后不是整数字面量（表达式/占位符），直接替换为上限
                limit_node.set("expression", exp.Literal.number(max_rows))
            return root
        try:
            return root.limit(max_rows)
        except AttributeError:
            # 极端兜底：根节点不支持 .limit() 时包一层子查询
            return exp.select("*").from_(root.subquery("q")).limit(max_rows)
