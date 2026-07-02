"""FastAPI 入口：组装 Agent 组件，托管 API 与静态 Web UI。

启动（仓库根目录）：
    .venv/bin/uvicorn src.main:app --port 8020
配置文件默认 config/example.yaml，可用环境变量 NL2SQL_CONFIG 覆盖。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.agent.executor import Executor
from src.agent.generator import SQLGenerator
from src.agent.guard import SQLGuard
from src.agent.pipeline import Pipeline
from src.agent.schema_provider import load_catalog
from src.api.routes import router
from src.config import load_config
from src.llm.client import LLMClient

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config(os.getenv("NL2SQL_CONFIG"))
    catalog = load_catalog(cfg.database, cfg.schema_source, cfg.security)
    executor = Executor(cfg.database, cfg.security)
    app.state.cfg = cfg
    app.state.catalog = catalog
    app.state.pipeline = Pipeline(
        generator=SQLGenerator(LLMClient(cfg.llm), cfg.database.dialect),
        guard=SQLGuard(cfg.security, cfg.database.dialect),
        executor=executor,
        catalog=catalog,
        agent_cfg=cfg.agent,
    )
    yield
    executor.dispose()


app = FastAPI(title="NL2SQL Agent", lifespan=lifespan)
app.include_router(router, prefix="/api")
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
