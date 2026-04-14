"""API 数据模型定义"""
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SourceType(str, Enum):
    """审查来源类型"""
    GIT_DIFF = "git_diff"
    FILES = "files"
    PR_URL = "pr_url"
    CODE_SNIPPET = "code_snippet"
    YUNXIAO_MR = "yunxiao_mr"


class ReviewDimension(str, Enum):
    """审查维度"""
    SECURITY = "security"
    QUALITY = "quality"
    PERFORMANCE = "performance"
    ALL = "all"


class GitDiffSource(BaseModel):
    """Git diff 来源"""
    type: SourceType = SourceType.GIT_DIFF
    base_branch: str = Field(default="main", description="基准分支")
    target_branch: str = Field(..., description="目标分支")
    repo_path: str | None = Field(default=None, description="仓库路径")


class FilesSource(BaseModel):
    """文件列表来源"""
    type: SourceType = SourceType.FILES
    paths: list[str] = Field(..., description="文件路径列表")


class CodeSnippetSource(BaseModel):
    """代码片段来源"""
    type: SourceType = SourceType.CODE_SNIPPET
    code: str = Field(..., description="代码内容")
    language: str | None = Field(default=None, description="编程语言")
    filename: str | None = Field(default=None, description="文件名")


class YunxiaoMRSource(BaseModel):
    """云效 MR 来源"""
    type: SourceType = SourceType.YUNXIAO_MR
    repository_id: str = Field(..., description="代码库ID或路径")
    local_id: str = Field(..., description="MR局部ID")
    organization_id: str | None = Field(default=None, description="组织ID，默认读取 YUNXIAO_ORG_ID 环境变量")
    auto_comment: bool = Field(default=True, description="是否自动在MR上添加评论")
    business_type: str = Field(default="default", description="业务类型（default, frontend, backend）")


class ReviewRequest(BaseModel):
    """审查请求"""
    source: GitDiffSource | FilesSource | CodeSnippetSource | YunxiaoMRSource = Field(
        ..., description="审查来源"
    )
    dimensions: list[ReviewDimension] = Field(
        default=[ReviewDimension.ALL], description="审查维度"
    )
    options: dict[str, Any] | None = Field(
        default=None, description="额外选项"
    )


class IssueSeverity(str, Enum):
    """问题严重程度"""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class ReviewIssue(BaseModel):
    """审查发现的问题"""
    severity: IssueSeverity
    dimension: ReviewDimension
    file_path: str | None = None
    line_number: int | None = None
    description: str
    suggestion: str | None = None
    code_snippet: str | None = None


class ReviewResult(BaseModel):
    """审查结果"""
    issues: list[ReviewIssue]
    summary: str
    score: float | None = None  # 代码质量评分 0-100
    reviewed_files: list[str]
    metadata: dict[str, Any] | None = None


class StreamEvent(str, Enum):
    """流式事件类型"""
    STARTED = "started"
    FILE_PROCESSING = "file_processing"
    ISSUE_FOUND = "issue_found"
    DIMENSION_COMPLETE = "dimension_complete"
    COMPLETE = "complete"
    ERROR = "error"


class StreamMessage(BaseModel):
    """流式消息"""
    event: StreamEvent
    data: dict[str, Any]