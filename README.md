# NL2SQL Agent · 自然语言转 SQL 助手

> 基调听云 AI 研发工程师实操题 · 题目二

一个让业务同学用自然语言查询数据库的 Agent：**理解用户问题 → 生成 SQL → 在受控环境下执行 → 在页面上展示结果**。

## 核心特性（规划中）

- **Schema 感知**：支持从 DDL 文件或数据库连接导入库表结构
- **NL → SQL**：输入中文或英文问题，生成 SQL 并展示生成说明
- **安全约束（硬性）**：基于 AST 的语句校验，默认仅允许 `SELECT`，拦截 `DROP/DELETE/UPDATE/INSERT` 等危险语句，强制注入 `LIMIT`
- **执行前确认**：展示 SQL 预览，用户确认后再执行
- **多数据库**：SQLite / MySQL / PostgreSQL
- **结果展示**：表格展示 + 导出 CSV

## 状态

🚧 开发中 — 项目脚手架初始化。

## 技术栈（暂定）

- 后端：Python 3.10+ / FastAPI
- SQL 解析与安全校验：sqlglot（AST 级）
- 前端：结果展示页（Web UI）

---

详细设计见 [`docs/design.md`](docs/design.md)（编写中）。
