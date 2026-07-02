# NL2SQL Agent · 自然语言转 SQL 助手

> 基调听云 AI 研发工程师实操题 · **题目二**

让业务同学用自然语言查数据库：**理解问题 → 生成 SQL → AST 安全校验 → 用户确认 → 受控执行 → 表格展示/导出 CSV**。执行失败时 Agent 自动带报错重试修复。

![流程](https://img.shields.io/badge/%E6%B5%81%E7%A8%8B-%E7%94%9F%E6%88%90%E2%86%92%E9%A2%84%E8%A7%88%E7%A1%AE%E8%AE%A4%E2%86%92%E6%89%A7%E8%A1%8C-blue) ![测试](https://img.shields.io/badge/%E6%B5%8B%E8%AF%95-53%20passed-brightgreen) ![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab)

## 快速开始

```bash
# 1. 环境（Python 3.10+）
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. 生成演示数据库（电商场景 5 张表，可重复执行）
.venv/bin/python data/seed_demo_db.py

# 3. 配置 LLM 密钥（NVIDIA API Catalog，OpenAI 兼容）
cp .env.example .env          # 编辑 .env 填入 NVIDIA_API_KEY

# 4. 启动（仓库根目录）
.venv/bin/uvicorn src.main:app --port 8020
# 浏览器打开 http://127.0.0.1:8020
```

## 使用方式

1. 输入自然语言问题（或点示例问题），点「生成 SQL」；
2. 预览卡片展示 **SQL + 生成说明 + 假设 + Agent 步骤轨迹**；
3. 点「确认执行」——服务端会对 SQL **重新做完整安全校验**后才执行；
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

预期：10 个城市，共 50 名用户（北京 7 人居首）。

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

预期：5 行，首位为「27寸显示器」（总销售额约 49362 元）。

**③ 时间范围：最近 30 天每天的订单数和总金额**

```sql
SELECT DATE(created_at) AS day,
       COUNT(*)          AS order_count,
       SUM(total_amount) AS total_amount
FROM orders
WHERE created_at >= DATE('now', '-30 days')
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

## 配置说明（config/example.yaml）

| 字段 | 说明 | 默认 |
|---|---|---|
| `llm.base_url` | OpenAI 兼容接口地址 | NVIDIA API Catalog |
| `llm.model` | 模型 ID | `deepseek-ai/deepseek-v4-flash` |
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
| `agent.max_repair_attempts` | 执行失败自修复重试次数 | `2` |
| `ui.require_confirm_before_execute` | 执行前需用户确认 | `true` |

自定义配置：复制 `config/example.yaml` 后修改，用环境变量 `NL2SQL_CONFIG=path/to/your.yaml` 指定。

**切换数据库**：`database.type` 改为 `mysql`/`postgresql` 并配置 `database.url`（连接账号请只授予 SELECT 权限，这是第三层防御的前提）。
**切换模型**：改 `llm.model` 一行即可（如 `deepseek-ai/deepseek-v4-pro`、`qwen/qwen3.5-397b-a17b`）；任何 OpenAI 兼容供应商改 `base_url` + `model`。

## 测试

```bash
.venv/bin/pytest tests/ -v     # 53 个测试
```

- `test_guard_security.py`：22 类恶意输入拦截 + 正常查询放行 + LIMIT 强制 + 白名单模式
- `test_schema_and_executor.py`：Schema 双模式一致性、敏感表隐藏、只读连接拒写、截断
- `test_generator_and_pipeline.py`：JSON 容错解析、自修复循环、修复产物重过守卫、重试封顶（FakeLLM，不联网）

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
│   │   ├── pipeline.py        # 编排：生成→守卫→执行→自修复
│   │   ├── generator.py       # NL → {sql, explanation, assumptions}
│   │   ├── schema_provider.py # Schema 感知（自省/DDL 双模式）
│   │   └── executor.py        # 只读执行器
│   └── llm/client.py          # OpenAI 兼容客户端
├── web/index.html             # 结果展示页（静态单页，无构建步骤）
├── tests/                     # 53 个测试
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

- NVIDIA API Catalog 免费接口延迟波动较大（实测 5～45 秒）；
- SQLite 无服务端语句超时（依赖 LIMIT clamp + 只读兜底）；MySQL 的会话超时需在连接串/账号侧配置；
- 多轮追问澄清暂未实现（设计文档中已列为可扩展点）。

---

设计文档：[docs/design.md](docs/design.md) · 演示脚本：[docs/demo-script.md](docs/demo-script.md)
