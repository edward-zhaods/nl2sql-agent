"""配置加载与校验。

YAML 配置文件 + .env 环境变量（密钥只走环境变量，配置文件里只存变量名）。
字段说明见 config/example.yaml 与 README。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class LLMConfig(BaseModel):
    base_url: str = "https://integrate.api.nvidia.com/v1"
    model: str = "z-ai/glm-5.2"
    api_key_env: str = "NVIDIA_API_KEY"
    temperature: float = 0.2
    max_tokens: int = 2048

    @property
    def api_key(self) -> str:
        key = os.getenv(self.api_key_env, "")
        if not key:
            raise RuntimeError(
                f"环境变量 {self.api_key_env} 未设置，请复制 .env.example 为 .env 并填入密钥"
            )
        return key


class DatabaseConfig(BaseModel):
    type: Literal["sqlite", "mysql", "postgresql"] = "sqlite"
    path: str = "./data/demo.db"
    url: str | None = None

    @property
    def dialect(self) -> str:
        """sqlglot 方言名。"""
        return {"sqlite": "sqlite", "mysql": "mysql", "postgresql": "postgres"}[self.type]

    def sqlalchemy_url(self, read_only: bool = True) -> str:
        if self.type == "sqlite":
            p = Path(self.path).resolve()
            if not p.exists():
                raise FileNotFoundError(
                    f"SQLite 数据库不存在：{p}（先运行 python data/seed_demo_db.py 生成演示库）"
                )
            if read_only:
                return f"sqlite:///file:{p}?mode=ro&uri=true"
            return f"sqlite:///{p}"
        if not self.url:
            raise ValueError(f"database.type={self.type} 需要配置 database.url（务必使用只读账号）")
        return self.url


class SchemaSourceConfig(BaseModel):
    source: Literal["introspect", "ddl"] = "introspect"
    ddl_path: str = "./data/schema.sql"


class SecurityConfig(BaseModel):
    allowed_statements: list[str] = Field(default_factory=lambda: ["SELECT"])
    max_rows: int = 500
    blocked_tables: list[str] = Field(default_factory=list)
    allowed_tables: list[str] = Field(default_factory=list)  # 非空则启用白名单模式
    blocked_functions: list[str] = Field(default_factory=lambda: ["load_extension", "pg_sleep"])
    statement_timeout_ms: int = 5000


class AgentConfig(BaseModel):
    max_repair_attempts: int = 2
    schema_linking: bool = False
    enum_injection: bool = True          # Layer1：把低基数列的真实取值注入 Schema，喂给 LLM
    enum_max_cardinality: int = 30       # 去重取值数 ≤ 此阈值的列才注入（滤掉自由文本/主键）
    semantic_validation: bool = True     # Layer2a：确定性语义校验（枚举值是否合法）
    llm_critic: bool = False             # Layer2b：额外的 LLM 审查 agent（默认关，按需开）


class UIConfig(BaseModel):
    require_confirm_before_execute: bool = True


class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    schema_source: SchemaSourceConfig = Field(default_factory=SchemaSourceConfig, alias="schema")
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    ui: UIConfig = Field(default_factory=UIConfig)

    model_config = {"populate_by_name": True}


def load_config(path: str | Path | None = None) -> AppConfig:
    """加载 .env 与 YAML 配置。path 缺省时读环境变量 NL2SQL_CONFIG，再退回 config/example.yaml。"""
    load_dotenv()
    config_path = Path(path or os.getenv("NL2SQL_CONFIG", "config/example.yaml"))
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在：{config_path}")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return AppConfig(**data)
