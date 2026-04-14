"""FastAPI 服务入口"""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from .models import (
    ReviewRequest,
    ReviewResult,
    ReviewIssue,
    ReviewDimension,
    IssueSeverity,
    StreamEvent,
    StreamMessage,
    SourceType,
    YunxiaoMRSource,
)
from .agents import CodeReviewAgent
from .hooks import get_audit_log

# 加载环境变量
load_dotenv(override=True)

# 全局 agent 实例
_review_agent: CodeReviewAgent | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时初始化 agent
    global _review_agent
    _review_agent = CodeReviewAgent()
    yield
    # 关闭时清理资源
    _review_agent = None


# 创建 FastAPI 应用
app = FastAPI(
    title="Code Review Agent Service",
    description="基于 Claude Agent SDK 的智能代码审查服务",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    """服务状态"""
    return {
        "status": "running",
        "service": "Code Review Agent",
        "version": "1.0.0",
    }


@app.get("/health")
async def health_check():
    """健康检查"""
    return {"healthy": True}


@app.post("/review", response_model=ReviewResult)
async def review_code(request: ReviewRequest):
    """代码审查接口"""
    if _review_agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    dimensions = [d.value for d in request.dimensions]
    if ReviewDimension.ALL.value in dimensions:
        dimensions = None  # None 表示全部维度

    try:
        if request.source.type == SourceType.GIT_DIFF:
            result = await _review_agent.review_git_diff(
                base_branch=request.source.base_branch,
                target_branch=request.source.target_branch,
                dimensions=dimensions,
            )
        elif request.source.type == SourceType.FILES:
            result = await _review_agent.review_files(
                file_paths=request.source.paths,
                dimensions=dimensions,
            )
        elif request.source.type == SourceType.CODE_SNIPPET:
            result = await _review_agent.review_code_snippet(
                code=request.source.code,
                language=request.source.language,
                filename=request.source.filename,
                dimensions=dimensions,
            )
        elif request.source.type == SourceType.YUNXIAO_MR:
            result = await _review_agent.review_yunxiao_mr(
                repository_id=request.source.repository_id,
                local_id=request.source.local_id,
                organization_id=request.source.organization_id,
                dimensions=dimensions,
                auto_comment=request.source.auto_comment,
            )
        else:
            raise HTTPException(status_code=400, detail="Unknown source type")

        # 解析结果为 ReviewResult 格式
        return _format_review_result(result, request)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/review/stream")
async def review_stream(request: ReviewRequest):
    """流式审查接口 (SSE)"""
    if _review_agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    async def generate_events():
        """生成 SSE 事件流"""
        # 开始事件
        yield _sse_event(
            StreamEvent.STARTED,
            {"source_type": request.source.type.value},
        )

        dimensions = [d.value for d in request.dimensions]
        if ReviewDimension.ALL.value in dimensions:
            dimensions = None

        try:
            # 获取文件列表事件
            if request.source.type == SourceType.FILES:
                for path in request.source.paths:
                    yield _sse_event(
                        StreamEvent.FILE_PROCESSING,
                        {"file": path},
                    )
                    await asyncio.sleep(0.1)  # 模拟处理延迟

            # 执行审查
            if request.source.type == SourceType.GIT_DIFF:
                result = await _review_agent.review_git_diff(
                    base_branch=request.source.base_branch,
                    target_branch=request.source.target_branch,
                    dimensions=dimensions,
                )
            elif request.source.type == SourceType.FILES:
                result = await _review_agent.review_files(
                    file_paths=request.source.paths,
                    dimensions=dimensions,
                )
            elif request.source.type == SourceType.YUNXIAO_MR:
                result = await _review_agent.review_yunxiao_mr(
                    repository_id=request.source.repository_id,
                    local_id=request.source.local_id,
                    organization_id=request.source.organization_id,
                    dimensions=dimensions,
                    auto_comment=request.source.auto_comment,
                )
            else:
                result = await _review_agent.review_code_snippet(
                    code=request.source.code,
                    language=request.source.language,
                    dimensions=dimensions,
                )

            # 维度完成事件
            for dim in request.dimensions:
                yield _sse_event(
                    StreamEvent.DIMENSION_COMPLETE,
                    {"dimension": dim.value},
                )

            # 完成事件
            yield _sse_event(
                StreamEvent.COMPLETE,
                {
                    "summary": result.get("summary", ""),
                    "tools_used": result.get("tools_used", []),
                },
            )

        except Exception as e:
            yield _sse_event(
                StreamEvent.ERROR,
                {"error": str(e)},
            )

    return StreamingResponse(
        generate_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@app.get("/audit-log")
async def get_audit():
    """获取审计日志"""
    return {"logs": get_audit_log()}


# 云效 MR 审查专用端点
@app.post("/review/yunxiao-mr")
async def review_yunxiao_mr(
    repository_id: str,
    local_id: str,
    organization_id: str = None,
    dimensions: list[ReviewDimension] = None,
    auto_comment: bool = True,
):
    """云效 MR 代码审查专用接口

    Args:
        repository_id: 代码库ID或路径（如: 2835387 或 org/repo-name）
        local_id: MR局部ID（代码库中第几个MR）
        organization_id: 组织ID（默认: 5ea86562f89c9700014a671f）
        dimensions: 审查维度列表（默认: 全部）
        auto_comment: 是否自动在MR上添加评论（默认: True）
    """
    if _review_agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    # 默认审查所有维度
    if dimensions is None:
        dimensions = [ReviewDimension.ALL]

    dim_values = [d.value for d in dimensions]
    if ReviewDimension.ALL.value in dim_values:
        dim_values = None

    try:
        result = await _review_agent.review_yunxiao_mr(
            repository_id=repository_id,
            local_id=local_id,
            organization_id=organization_id,
            dimensions=dim_values,
            auto_comment=auto_comment,
        )

        return {
            "success": True,
            "summary": result.get("summary", ""),
            "tools_used": result.get("tools_used", []),
            "metadata": result.get("metadata", {}),
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/review/yunxiao-mr/stream")
async def review_yunxiao_mr_stream(
    repository_id: str,
    local_id: str,
    organization_id: str = None,
    auto_comment: bool = True,
):
    """云效 MR 审查流式接口 (SSE)"""
    if _review_agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    async def generate_events():
        """生成 SSE 事件流"""
        # 开始事件
        yield _sse_event(
            StreamEvent.STARTED,
            {
                "repository_id": repository_id,
                "local_id": local_id,
                "auto_comment": auto_comment,
            },
        )

        try:
            # 获取 MR 信息事件
            yield _sse_event(
                StreamEvent.FILE_PROCESSING,
                {"action": "fetching_mr_details"},
            )

            result = await _review_agent.review_yunxiao_mr(
                repository_id=repository_id,
                local_id=local_id,
                organization_id=organization_id,
                dimensions=None,
                auto_comment=auto_comment,
            )

            # 维度完成事件
            dimensions_completed = ["security", "quality", "performance"]
            for dim in dimensions_completed:
                yield _sse_event(
                    StreamEvent.DIMENSION_COMPLETE,
                    {"dimension": dim},
                )

            # 完成事件
            yield _sse_event(
                StreamEvent.COMPLETE,
                {
                    "summary": result.get("summary", ""),
                    "tools_used": result.get("tools_used", []),
                    "comments_added": auto_comment,
                },
            )

        except Exception as e:
            yield _sse_event(
                StreamEvent.ERROR,
                {"error": str(e)},
            )

    return StreamingResponse(
        generate_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


def _sse_event(event: StreamEvent, data: dict[str, Any]) -> str:
    """格式化 SSE 事件"""
    message = StreamMessage(event=event, data=data)
    return f"data: {message.model_dump_json()}\n\n"


def _format_review_result(
    raw_result: dict[str, Any],
    request: ReviewRequest,
) -> ReviewResult:
    """格式化审查结果"""
    # 从 raw_messages 中提取问题列表
    issues: list[ReviewIssue] = []
    reviewed_files: list[str] = []

    # 解析 tools_used 判断审查的维度
    tools_used = raw_result.get("tools_used", [])
    if "security_scan" in tools_used:
        # 安全审查维度
        for msg in raw_result.get("raw_messages", []):
            if msg.get("type") == "tool_use" and msg.get("tool") == "security_scan":
                # 这里应该从工具结果中提取问题
                pass

    # 从 summary 中提取关键信息
    summary = raw_result.get("summary", "")

    # 获取审查的文件列表
    if request.source.type == SourceType.FILES:
        reviewed_files = request.source.paths
    elif request.source.type == SourceType.GIT_DIFF:
        # 从工具调用结果中提取
        reviewed_files = []
    elif request.source.type == SourceType.YUNXIAO_MR:
        reviewed_files = []
    else:
        reviewed_files = []

    return ReviewResult(
        issues=issues,
        summary=summary,
        reviewed_files=reviewed_files,
        metadata={
            "tools_used": tools_used,
            "result_type": raw_result.get("result_type"),
        },
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("SERVICE_PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)