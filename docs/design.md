# NL2SQL Agent · 设计文档

> 题目二：自然语言转 SQL 助手
> 目标：业务同学用自然语言查数据 —— 理解问题 → 生成 SQL → 受控执行 → 页面展示结果。

---

## 1. 总体架构

```mermaid
flowchart LR
    subgraph Web["Web UI（web/）"]
        UI[单页应用<br/>提问 / SQL 预览 / 确认执行 / 结果表格 / 导出 CSV]
    end

    subgraph API["FastAPI（src/api/）"]
        R1[POST /api/query/generate]
        R2[POST /api/query/execute]
        R3[GET /api/schema]
        R4[POST /api/export/csv]
    end

    subgraph Agent["Agent 核心（src/agent/）"]
        SP[SchemaProvider<br/>DDL 文件 / 数据库自省]
        GEN[SQLGenerator<br/>LLM 生成 SQL + 解释]
        GUARD[SQLGuard<br/>sqlglot AST 安全校验]
        EXEC[Executor<br/>只读执行 / 超时 / 行数上限]
        REPAIR[自修复循环<br/>报错回喂重试 ≤ N 次]
    end

    subgraph LLM["LLM（OpenAI 兼容）"]
        NV[NVIDIA API<br/>GLM / DeepSeek 可配置]
    end

    subgraph DB["数据库"]
        SQLITE[(SQLite / MySQL / PostgreSQL)]
    end

    UI --> API
    R1 --> GEN
    GEN --> SP
    GEN --> NV
    GEN --> GUARD
    R2 --> GUARD
    GUARD --> EXEC
    EXEC --> SQLITE
    EXEC -. 执行报错 .-> REPAIR
    REPAIR --> GEN
    SP --> SQLITE
```

分层职责：

| 层 | 目录 | 职责 |
|---|---|---|
| Web UI | `web/` | 纯静态单页（FastAPI 托管），提问、SQL 预览与确认、结果表格、CSV 导出 |
| API | `src/api/` | HTTP 路由、请求校验、错误码规范 |
| Agent 核心 | `src/agent/` | Schema 感知、SQL 生成、AST 安全守卫、受控执行、自修复 |
| LLM 接入 | `src/llm/` | OpenAI 兼容客户端封装（NVIDIA endpoint），模型/参数可配置 |
| 配置 | `config/` | YAML 配置 + 环境变量（密钥只走环境变量） |

---

## 2. 核心交互：两段式「生成 → 确认 → 执行」

硬性要求「执行前展示 SQL 预览，用户确认后再执行」通过**两个独立接口**实现，而不是一个接口内部分步：

```mermaid
sequenceDiagram
    participant U as 用户
    participant W as Web UI
    participant A as API
    participant G as SQLGuard
    participant L as LLM
    participant D as 数据库

    U->>W: 输入自然语言问题
    W->>A: POST /api/query/generate
    A->>L: schema + 问题 + 方言 → 生成
    L-->>A: SQL + 生成说明
    A->>G: AST 校验（改写为规范化 SQL）
    G-->>A: 通过 / 拦截（含原因）
    A-->>W: {sql, explanation, guard_report}
    Note over W: 展示 SQL 预览 + 解释，等待用户确认
    U->>W: 点击「确认执行」
    W->>A: POST /api/query/execute {sql}
    A->>G: 再次完整校验（不信任客户端回传）
    G->>D: 只读连接执行（超时 + 行数上限）
    D-->>W: 列 + 行 + 是否截断 + 耗时
```

关键点：`execute` 接口对客户端回传的 SQL **重新做完整校验**。前端可以被绕过（直接 curl 调接口），服务端永远不信任回传内容。

---

## 3. Agent 设计

### 3.1 Schema 感知（SchemaProvider）

两种来源，配置切换：

1. **DDL 文件**：解析 `.sql` 文件（sqlglot 解析 CREATE TABLE），得到表/列/类型/注释；
2. **数据库自省**：SQLAlchemy Inspector 拉取元数据（表、列、主外键、注释）。

输出统一的 Schema Catalog（表 → 列 → 类型/注释），同时生成给 LLM 的紧凑文本表示。表数量多时只注入与问题相关的表（先用 LLM 做一轮表选择），避免上下文膨胀——demo 库表少，此路径默认关闭、可配置开启。

### 3.2 SQL 生成（SQLGenerator）

- Prompt 组成：系统约束（只写 SELECT、目标方言、禁止编造表/列）+ Schema Catalog + 用户问题 + 当前日期（时间范围类问题需要）。
- 输出结构化 JSON：`{sql, explanation, assumptions}`，`explanation` 直接展示在预览页，满足「展示生成说明」要求。
- 温度默认 0.2：SQL 生成要稳定，不要发散。

### 3.3 安全守卫（SQLGuard）—— 本项目的核心

**为什么不用正则黑名单**：`DR/**/OP`、`SELECT 1; DROP TABLE x`、CTE 里藏 `UPDATE`、`SELECT ... INTO` 等都能绕过关键词匹配。守卫必须工作在**语法树层面**。

基于 sqlglot AST 的校验规则（按顺序执行，任一失败即拦截并返回具体原因）：

| # | 规则 | 拦截对象 |
|---|---|---|
| 1 | 必须成功解析且**恰好一条语句** | 多语句注入 `SELECT 1; DROP TABLE t` |
| 2 | 根节点类型 ∈ `allowed_statements`（默认仅 SELECT/UNION-of-SELECT） | 一切非查询语句 |
| 3 | 遍历全树，节点类型黑名单：Insert / Update / Delete / Drop / Create / Alter / Truncate / Grant / Merge / Set / Use / Command / Pragma / Attach / Into | CTE/子查询里嵌套的写操作、`SELECT INTO`、`PRAGMA`、`ATTACH` |
| 4 | 提取全部表名，校验白名单/黑名单 | 越权访问敏感表（如 `users_password`） |
| 5 | 危险函数黑名单（`load_extension`、`pg_sleep` 等，可配置） | 通过函数逃逸/拖慢数据库 |
| 6 | LIMIT 强制：无 LIMIT 则注入 `max_rows`；已有则 clamp 到 `min(原值, max_rows)` | 全表大结果集拖垮服务 |

**执行的是 AST 重新渲染出的规范化 SQL，不是用户/LLM 的原文**。注释、大小写混淆、多余空白在重渲染时全部消失，注释混淆类绕过被结构性消除。

### 3.4 受控执行（Executor）

守卫之外的**纵深防御**（假设守卫有未知漏洞，执行层仍然兜底）：

- SQLite：以 `file:...?mode=ro` 只读模式打开；
- MySQL / PostgreSQL：README 明确要求使用只读账号连接，会话级设置执行超时（PG `statement_timeout` / MySQL `max_execution_time`）；
- 应用层 `fetchmany(max_rows + 1)`，超出即标记「结果已截断」；
- 单查询总超时可配置。

### 3.5 自修复循环

执行失败（列名写错、方言函数不存在等）时，把**原问题 + 出错 SQL + 数据库报错**回喂给 LLM 重新生成，重试上限 `max_repair_attempts`（默认 2）。每次重试产物**重新过一遍守卫**——修复不能成为绕过安全校验的旁路。修复过程在 UI 上以步骤形式可见（体现 Agent 循环，也方便演示）。

### 3.6 安全模型总结：三层防御

```
第 1 层  Prompt 约束        （最弱，只是引导，不作为安全依据）
第 2 层  SQLGuard AST 校验  （主防线，白名单思路：只放行已知安全的结构）
第 3 层  只读连接 + 超时 + 行数上限（兜底：即使前两层全破，也写不了数据）
```

---

## 4. LLM 接入

通过 OpenAI 兼容协议接入 NVIDIA API Catalog（`https://integrate.api.nvidia.com/v1`），模型可配置：

- `z-ai/glm-5.2`（默认）
- `stepfun-ai/step-3.7-flash` / `deepseek-ai/deepseek-v4-flash` / `mistralai/mistral-medium-3.5-128b`（备选，改 `llm.model` 一行切换）

密钥只从环境变量 `NVIDIA_API_KEY` 读取（`.env` 已 gitignore），配置文件里只写环境变量名，仓库中永不出现明文密钥。客户端封装为独立模块，换任何 OpenAI 兼容供应商（OpenAI、SiliconFlow、Ollama 本地）只需改 `base_url` + `model`。

---

## 5. 配置设计（config/example.yaml）

```yaml
llm:
  base_url: "https://integrate.api.nvidia.com/v1"
  model: "z-ai/glm-5.2"
  api_key_env: "NVIDIA_API_KEY"   # 只存环境变量名，不存密钥本体
  temperature: 0.2
  max_tokens: 2048

database:
  type: sqlite                     # sqlite | mysql | postgresql
  path: "./data/demo.db"
  # url: "postgresql://readonly_user:***@host/db"   # MySQL/PG 用连接串 + 只读账号

schema:
  source: introspect               # introspect（自省）| ddl（文件）
  ddl_path: "./data/schema.sql"

security:
  allowed_statements: ["SELECT"]
  max_rows: 500
  blocked_tables: ["users_password"]
  allowed_tables: []               # 非空则启用白名单模式
  blocked_functions: ["load_extension", "pg_sleep"]
  statement_timeout_ms: 5000

agent:
  max_repair_attempts: 2
  schema_linking: false            # 大库时开启：先选相关表再生成

ui:
  require_confirm_before_execute: true
```

---

## 6. 项目结构

```
nl2sql-agent/
├── README.md
├── requirements.txt
├── config/
│   └── example.yaml
├── data/
│   ├── seed_demo_db.py        # 生成演示库（可重复执行）
│   └── schema.sql             # DDL 示例（schema.source=ddl 时使用）
├── src/
│   ├── main.py                # FastAPI 入口
│   ├── config.py              # 配置加载与校验
│   ├── api/                   # 路由层
│   ├── agent/
│   │   ├── schema_provider.py
│   │   ├── generator.py
│   │   ├── guard.py           # ★ AST 安全守卫
│   │   ├── executor.py
│   │   └── pipeline.py        # 生成→校验→执行→修复 编排
│   └── llm/client.py
├── web/                       # 静态单页 UI
├── tests/
│   ├── test_guard_security.py # ★ 恶意语句测试套件
│   └── test_pipeline.py
└── docs/
    └── design.md
```

演示库为小型电商场景（`users` / `products` / `orders` / `order_items`，含时间字段），刚好覆盖验收要求的三类问题：简单聚合、JOIN、时间范围。

---

## 7. 关键取舍

| 决策 | 选择 | 放弃的方案与原因 |
|---|---|---|
| SQL 安全校验 | sqlglot AST 白名单 | 正则/关键词黑名单——注释混淆、多语句、CTE 嵌套写操作均可绕过；黑名单永远列不全，白名单只放行已知安全结构 |
| 执行的 SQL | AST 重渲染的规范化 SQL | 执行 LLM 原文——原文可能带注释/混淆，重渲染结构性消除这类风险 |
| 执行确认 | 两个独立接口 + 服务端二次校验 | 单接口内部确认——前端确认可被绕过，服务端必须独立设防 |
| Agent 框架 | 手写编排（~200 行 pipeline） | LangChain——本任务链路短且清晰，手写更透明、依赖更少、安全路径完全可控可审计 |
| 写权限防护 | 三层防御（Prompt + AST + 只读连接） | 仅靠 AST 校验——安全设计假设任何单层都可能被突破 |
| LLM 温度 | 0.2 | 高温度——SQL 生成要求确定性，创造性是负资产 |
| 前端 | 静态单页（无构建步骤） | React/Vue 工程——评审可 `pip install` 后直接跑，不需要 Node 环境 |
| 自修复 | 报错回喂重试 ≤2 次，每次重过守卫 | 无限重试——成本失控；跳过守卫的重试——安全旁路 |

---

## 8. 需求对照表（验收自查）

| PDF 要求 | 实现位置 |
|---|---|
| Schema 感知：DDL 导入 / 数据库拉取 | `agent/schema_provider.py`，两种模式配置切换 |
| NL→SQL + 生成说明 | `agent/generator.py`，结构化输出 `sql + explanation` |
| 默认仅允许 SELECT | Guard 规则 2（`allowed_statements` 配置） |
| 拦截 DROP/DELETE/UPDATE/INSERT | Guard 规则 1–3（AST 级，含嵌套与多语句），`tests/test_guard_security.py` 全覆盖 |
| 强制 LIMIT（上限可配置） | Guard 规则 6（注入 + clamp） |
| 执行前预览、确认后执行 | 两段式接口（§2），服务端二次校验 |
| SQLite/MySQL/PG 任一 + 表格展示 + 导出 CSV | Executor + Web UI + `/api/export/csv` |
| 可配置项 | `config/example.yaml`（§5，字段说明见 README） |
| 3 个示例问题 + 参考答案 | README（聚合 / JOIN / 时间范围），演示库按此设计 |
| 恶意语句明确提示 | Guard 拦截返回具体规则与原因，UI 红色横幅展示 |
