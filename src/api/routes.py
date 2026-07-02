"""API 路由。

两段式交互（docs/design.md §2）：
- POST /query/generate  自然语言 → SQL 预览（不执行）
- POST /query/execute   确认后执行；服务端对回传 SQL 重新完整校验

守卫拦截不是 HTTP 错误：返回 200 + 结构化 {ok/success: false, ...}，
前端据此渲染拦截横幅；只有请求本身非法（缺字段等）才用 4xx。
"""

from __future__ import annotations

import csv
import io
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

router = APIRouter()


class GenerateRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class ExecuteRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=20000)
    question: str | None = None  # 提供则启用自修复


class ExportRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=20000)


@router.get("/schema")
def get_schema(request: Request):
    return request.app.state.catalog.to_dict()


@router.get("/config")
def get_config(request: Request):
    """脱敏后的运行配置（永不返回密钥）。"""
    cfg = request.app.state.cfg
    return {
        "model": cfg.llm.model,
        "database_type": cfg.database.type,
        "max_rows": cfg.security.max_rows,
        "allowed_statements": cfg.security.allowed_statements,
        "max_repair_attempts": cfg.agent.max_repair_attempts,
        "require_confirm_before_execute": cfg.ui.require_confirm_before_execute,
    }


@router.post("/query/generate")
def generate(request: Request, body: GenerateRequest):
    preview = request.app.state.pipeline.generate_preview(body.question)
    return asdict(preview)


@router.post("/query/execute")
def execute(request: Request, body: ExecuteRequest):
    outcome = request.app.state.pipeline.execute_confirmed(body.sql, question=body.question)
    return asdict(outcome)


@router.post("/export/csv")
def export_csv(request: Request, body: ExportRequest):
    outcome = request.app.state.pipeline.execute_confirmed(body.sql)
    if not outcome.success:
        raise HTTPException(status_code=400, detail=outcome.error)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(outcome.result.columns)
    writer.writerows(outcome.result.rows)
    return Response(
        content="\ufeff" + buf.getvalue(),  # BOM：让 Excel 正确识别 UTF-8 中文
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="query_result.csv"'},
    )
