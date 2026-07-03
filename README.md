# NL2SQL Agent · 自然语言转 SQL 助手

> 基调听云 AI 研发工程师实操题 · **题目二**

让业务同学用自然语言查数据库：**理解问题 → 生成 SQL → AST 安全校验 → 语义校验 → 用户确认 → 受控执行 → 表格展示/导出 CSV**。语义校验发现枚举值等错误会带反馈自动重生成；执行失败也会带报错重试修复。

![流程](https://img.shields.io/badge/%E6%B5%81%E7%A8%8B-%E7%94%9F%E6%88%90%E2%86%92%E9%A2%84%E8%A7%88%E7%A1%AE%E8%AE%A4%E2%86%92%E6%89%A7%E8%A1%8C-blue) ![测试](https://img.shields.io/badge/%E6%B5%8B%E8%AF%95-75%20passed-brightgreen) ![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab)

## 快速开始

```bash
# 1. 环境（Python 3.10+）
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. 生成演示数据库（电商场景 13 张表：12 业务表 + 1 敏感表，1200 笔订单，可重复执行）
.venv/bin/python data/seed_demo_db.py

# 3. 配置 LLM 密钥（NVIDIA API Catalog，OpenAI 兼容）
cp .env.example .env          # 编辑 .env 填入 NVIDIA_API_KEY

# 4. 启动（仓库根目录）
.venv/bin/uvicorn src.main:app --port 8020
# 浏览器打开 http://127.0.0.1:8020
```

## 使用方式

1. 输入自然语言问题（或点示例问题），点「查询」；
2. 预览卡片展示 **SQL + 生成说明 + 假设 + Agent 步骤轨迹**（此时尚未执行）；
3. 点「确认执行」——服务端会对 SQL **重新做完整安全校验**后才执行（`ui.require_confirm_before_execute: false` 可关闭确认步骤改为一键直查）；
4. 结果表格展示，可导出 CSV；执行失败时 Agent 自动带报错回喂重试（页面显示「经 N 次自修复」徽标）；
5. 页面上的「⚠ 注入攻击演示」按钮：模拟攻击者绕过前端直接调执行接口发 `SELECT 1; DROP TABLE users`，会看到守卫的拦截横幅。

## 三个示例问题与参考答案

演示数据锚定「运行 seed 脚本的日期」回溯 180 天生成，任何时候跑「最近 N 天」都有数据。

**① 简单聚合：各城市的用户数量是多少？按数量从多到少排**

```sql
SELECT city, COUNT(*) AS user_count
FROM users
GROUP BY city
ORDER BY user_count DESC
LIMIT 500;
```

预期：10 个城市，共 60 名用户（广州 8 人居首）。

**② JOIN：已支付订单中，销售额最高的 5 个商品是什么？**

```sql
SELECT p.name, SUM(oi.quantity * oi.unit_price) AS total_sales
FROM order_items oi
JOIN orders o ON oi.order_id = o.id
JOIN products p ON oi.product_id = p.id
WHERE o.status = 'paid'
GROUP BY p.id, p.name
ORDER BY total_sales DESC
LIMIT 5;
```

预期：5 行，首位为「27寸显示器」（总销售额 197448 元）。

**③ 时间范围：最近 30 天每天的订单数和总金额**

```sql
SELECT DATE(created_at) AS day,
       COUNT(*)          AS order_count,
       SUM(total_amount) AS total_amount
FROM orders
WHERE DATE(created_at) >= DATE('now', '-29 days')
GROUP BY day
ORDER BY day
LIMIT 500;
```

预期：约 20-30 行（每天 0-5 笔订单）。

## 安全设计（题目硬性要求）

三层纵深防御，**不依赖任何单层**：

| 层 | 机制 | 作用 |
|---|---|---|
| 1 | Prompt 约束 | 引导 LLM 只生成 SELECT（最弱，不作为安全依据） |
| 2 | **SQLGuard：sqlglot AST 校验** | 主防线（白名单思路），见下 |
| 3 | 只读连接 + 超时 + 行数上限 | 兜底：SQLite 以 `mode=ro` 打开，写操作在数据库层直接失败 |

SQLGuard 六条规则（任一失败即拦截并给出中文原因）：

1. 必须解析成功且**恰好一条语句**（拦多语句注入 `SELECT 1; DROP TABLE t`）
2. 根节点必须是 SELECT/UNION（`allowed_statements` 可配置）
3. **全树遍历**节点黑名单：INSERT/UPDATE/DELETE/DROP/CREATE/ALTER/TRUNCATE/PRAGMA/ATTACH/SELECT INTO 等——CTE 和子查询里嵌套的写操作同样被拦
4. 表黑/白名单（演示库内置敏感表 `users_password`：LLM 看不到它，手写 SQL 访问会被拦）
5. 危险函数黑名单（`load_extension`、`pg_sleep` 等）
6. LIMIT 强制：缺失则注入 `max_rows`，已有则 clamp 到上限

关键细节：**执行的是 AST 重渲染后的规范化 SQL，不是用户/LLM 原文**——`DR/**/OP` 之类注释混淆被结构性消除。正则黑名单方案无法做到这一点。

22 类恶意输入的可复现证据：

```bash
.venv/bin/pytest tests/test_guard_security.py -v
```

## 正确性校验（安全之外的一层）

安全守卫只解决「能不能执行」，不解决「答得对不对」。像 `WHERE status = '已支付'`——
语法合法、过守卫、能执行、**静默返回 0 行**——这类语义错误单靠守卫和「执行报错才重试」
都逮不住。为此在生成后补了一层正确性校验：

| 层 | 机制 | 作用 |
|---|---|---|
| 防（接地） | **Schema 枚举注入** | 自省时对低基数列采样真实取值，写进 Schema（如 `status TEXT -- 取值: paid, refunded`），让模型基于库里的实际枚举写 WHERE，而不是把中文描述当字段值 |
| 校（确定性） | **SemanticValidator** | 遍历 AST，把 `col = 'x'` / `IN (...)` 的字面量比对封闭枚举集，逮住幻觉值。零 LLM 调用、可单测；只对小枚举强校验，开放型分类（城市、商品名）仅接地不强卡，避免误报 |
| 校（可选） | **SQLCritic**（LLM 审查 agent） | 抓确定性规则抓不到的逻辑错（连错表、聚合口径、漏条件）。`agent.llm_critic` 开关，默认关 |

发现问题后**复用自修复循环**：把校验反馈当作 error 回喂生成器重生成，产物照样重过守卫；
用尽重试仍存疑则**不硬拦**（有人工确认兜底），改为在预览里给⚠️警告，避免误杀。

```bash
.venv/bin/pytest tests/test_validator.py -v          # 枚举注入 + 确定性校验
```

## 配置说明（config/example.yaml）

| 字段 | 说明 | 默认 |
|---|---|---|
| `llm.base_url` | OpenAI 兼容接口地址 | NVIDIA API Catalog |
| `llm.model` | 模型 ID | `z-ai/glm-5.2` |
| `llm.api_key_env` | 密钥所在**环境变量名**（配置文件不落明文） | `NVIDIA_API_KEY` |
| `llm.temperature` | 生成温度（SQL 要求稳定，宜低） | `0.2` |
| `database.type` | `sqlite` / `mysql` / `postgresql` | `sqlite` |
| `database.path` | SQLite 文件路径 | `./data/demo.db` |
| `database.url` | MySQL/PG 连接串（**务必用只读账号**） | — |
| `schema.source` | `introspect`（数据库自省）/ `ddl`（解析 DDL 文件） | `introspect` |
| `schema.ddl_path` | DDL 文件路径（ddl 模式） | `./data/schema.sql` |
| `security.allowed_statements` | 允许的语句类型 | `["SELECT"]` |
| `security.max_rows` | 强制 LIMIT 上限 | `500` |
| `security.blocked_tables` | 表黑名单 | `["users_password"]` |
| `security.allowed_tables` | 表白名单（非空则启用白名单模式） | `[]` |
| `security.blocked_functions` | 危险函数黑名单 | `load_extension` 等 |
| `security.statement_timeout_ms` | 语句超时（PG 会话级生效） | `5000` |
| `agent.max_repair_attempts` | 执行失败/语义存疑的自修复重试次数 | `2` |
| `agent.enum_injection` | 把低基数列真实取值注入 Schema（接地，防幻觉） | `true` |
| `agent.enum_max_cardinality` | 去重取值 ≤ 此值的列才注入 | `30` |
| `agent.semantic_validation` | 确定性枚举校验（逮住「已支付」这类幻觉值） | `true` |
| `agent.llm_critic` | 额外的 LLM 审查 agent（抓 join/聚合逻辑错，多一次调用） | `false` |
| `ui.require_confirm_before_execute` | 执行前需用户确认 | `true` |

自定义配置：复制 `config/example.yaml` 后修改，用环境变量 `NL2SQL_CONFIG=path/to/your.yaml` 指定。

**切换数据库**：`database.type` 改为 `mysql`/`postgresql` 并配置 `database.url`（连接账号请只授予 SELECT 权限，这是第三层防御的前提）。
**切换模型**：改 `llm.model` 一行即可（如 `stepfun-ai/step-3.7-flash`、`deepseek-ai/deepseek-v4-flash`、`mistralai/mistral-medium-3.5-128b`）；任何 OpenAI 兼容供应商改 `base_url` + `model`。

## 测试

```bash
.venv/bin/pytest tests/ -v     # 75 个测试
```

- `test_guard_security.py`：22 类恶意输入拦截 + 正常查询放行 + LIMIT 强制 + 白名单模式
- `test_schema_and_executor.py`：Schema 双模式一致性、敏感表隐藏、只读连接拒写、截断
- `test_generator_and_pipeline.py`：JSON 容错解析、自修复循环、修复产物重过守卫、重试封顶、语义校验自愈 + 用尽降级警告 + LLM 审查触发重生成（FakeLLM，不联网）
- `test_validator.py`：枚举注入（含时间戳/标识符排除）、确定性枚举校验（幻觉值拦截、别名/未限定列解析、歧义放过、IN 列表、误报防护）

## 项目结构

```
nl2sql-agent/
├── config/example.yaml        # 示例配置（可直接运行）
├── data/
│   ├── seed_demo_db.py        # 演示库生成脚本（可重复执行）
│   └── schema.sql             # DDL（ddl 模式输入）
├── src/
│   ├── main.py                # FastAPI 入口
│   ├── config.py              # 配置加载与校验
│   ├── api/routes.py          # 两段式接口 + 导出
│   ├── agent/
│   │   ├── guard.py           # ★ AST 安全守卫（六条规则）
│   │   ├── pipeline.py        # 编排：生成→守卫→语义校验→执行→自修复
│   │   ├── generator.py       # NL → {sql, explanation, assumptions}
│   │   ├── validator.py       # ★ 语义校验：确定性枚举校验 + 可选 LLM 审查
│   │   ├── schema_provider.py # Schema 感知（自省/DDL 双模式，含枚举注入）
│   │   └── executor.py        # 只读执行器
│   └── llm/client.py          # OpenAI 兼容客户端
├── web/index.html             # 结果展示页（静态单页，无构建步骤）
├── tests/                     # 75 个测试
└── docs/design.md             # 设计文档（架构/Agent 设计/关键取舍）
```

## AI 使用说明（开发方式）

本项目按题目要求全程使用 AI Coding 工具（**Claude Code**）开发：

- **架构先行**：先与 AI 讨论产出 `docs/design.md`（安全模型、两段式交互、关键取舍），再按文档实施；
- **测试驱动安全**：恶意语句测试套件先于守卫实现编写，开发中曾借此发现 sqlglot 重渲染默认保留注释的问题（修复为 `comments=False`）；
- **真实环境验证**：每个模块完成后跑真实测试（真实数据库、真实 LLM 调用、浏览器端到端点击验证），不以「代码写完」为完成标准；
- **人工把关**：安全相关代码（守卫规则、只读连接、服务端二次校验）经逐行审阅。

运行期 AI 的职责边界：LLM 只负责「理解问题、生成 SQL、解释思路、修复报错」；**能不能执行由确定性的 AST 守卫与只读连接决定**，LLM 无权也无法绕过。

## 已知限制

- NVIDIA API Catalog 免费接口延迟波动较大；若生成 SQL 慢，可优先切换 `llm.model` 为其他 Free Endpoint；
- SQLite 无服务端语句超时（依赖 LIMIT clamp + 只读兜底）；MySQL 的会话超时需在连接串/账号侧配置；
- 多轮追问澄清暂未实现（设计文档中已列为可扩展点）。

---

设计文档：[docs/design.md](docs/design.md) · 演示脚本：[docs/demo-script.md](docs/demo-script.md)
