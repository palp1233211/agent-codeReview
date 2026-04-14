"""云效 MCP 工具集成 - MR 代码审查"""
from typing import Any

from claude_agent_sdk import tool, create_sdk_mcp_server


@tool(
    name="get_yunxiao_mr",
    description="获取云效（Yunxiao）Merge Request 详情。包括标题、描述、源分支、目标分支、状态等。",
    input_schema={
        "organization_id": {
            "type": "string",
            "description": "组织ID",
        },
        "repository_id": {
            "type": "string",
            "description": "代码库ID或路径",
        },
        "local_id": {
            "type": "string",
            "description": "MR局部ID（代码库中第几个MR）",
        },
    },
)
async def get_yunxiao_mr(
    organization_id: str,
    repository_id: str,
    local_id: str,
) -> dict:
    """获取云效 MR 详情"""
    # 这个工具会调用 yunxiao MCP server
    # 在实际实现中，这里应该通过 MCP 协议调用
    return {
        "content": [
            {
                "type": "text",
                "text": f"获取 MR #{local_id} 详情，仓库: {repository_id}",
            }
        ],
        "action": "get_change_request",
        "params": {
            "organizationId": organization_id,
            "repositoryId": repository_id,
            "localId": local_id,
        },
    }


@tool(
    name="get_yunxiao_mr_diff",
    description="获取云效 MR 的代码差异。比较源分支和目标分支之间的变更。",
    input_schema={
        "organization_id": {
            "type": "string",
            "description": "组织ID",
        },
        "repository_id": {
            "type": "string",
            "description": "代码库ID或路径",
        },
        "source_branch": {
            "type": "string",
            "description": "源分支名称",
        },
        "target_branch": {
            "type": "string",
            "description": "目标分支名称",
        },
    },
)
async def get_yunxiao_mr_diff(
    organization_id: str,
    repository_id: str,
    source_branch: str,
    target_branch: str,
) -> dict:
    """获取云效 MR diff"""
    return {
        "content": [
            {
                "type": "text",
                "text": f"比较分支差异: {target_branch} <- {source_branch}",
            }
        ],
        "action": "compare",
        "params": {
            "organizationId": organization_id,
            "repositoryId": repository_id,
            "from": target_branch,
            "to": source_branch,
            "sourceType": "branch",
            "targetType": "branch",
        },
    }


@tool(
    name="comment_on_yunxiao_mr",
    description="在云效 MR 上添加审查评论。支持全局评论和行内评论。",
    input_schema={
        "organization_id": {
            "type": "string",
            "description": "组织ID",
        },
        "repository_id": {
            "type": "string",
            "description": "代码库ID或路径",
        },
        "local_id": {
            "type": "string",
            "description": "MR局部ID",
        },
        "comment_type": {
            "type": "string",
            "enum": ["GLOBAL_COMMENT", "INLINE_COMMENT"],
            "description": "评论类型",
        },
        "content": {
            "type": "string",
            "description": "评论内容",
        },
        "file_path": {
            "type": "string",
            "description": "文件路径（行内评论需要）",
        },
        "line_number": {
            "type": "integer",
            "description": "行号（行内评论需要）",
        },
    },
)
async def comment_on_yunxiao_mr(
    organization_id: str,
    repository_id: str,
    local_id: str,
    comment_type: str,
    content: str,
    file_path: str | None = None,
    line_number: int | None = None,
) -> dict:
    """在云效 MR 上添加评论"""
    return {
        "content": [
            {
                "type": "text",
                "text": f"添加评论到 MR #{local_id}",
            }
        ],
        "action": "create_change_request_comment",
        "params": {
            "organizationId": organization_id,
            "repositoryId": repository_id,
            "localId": local_id,
            "comment_type": comment_type,
            "content": content,
            "file_path": file_path,
            "line_number": line_number,
        },
    }


@tool(
    name="get_yunxiao_mr_files",
    description="获取云效 MR 变更的文件列表。",
    input_schema={
        "organization_id": {
            "type": "string",
            "description": "组织ID",
        },
        "repository_id": {
            "type": "string",
            "description": "代码库ID或路径",
        },
        "ref": {
            "type": "string",
            "description": "分支名称",
        },
        "path": {
            "type": "string",
            "description": "路径前缀，可选",
        },
    },
)
async def get_yunxiao_mr_files(
    organization_id: str,
    repository_id: str,
    ref: str,
    path: str | None = None,
) -> dict:
    """获取云效仓库文件列表"""
    return {
        "content": [
            {
                "type": "text",
                "text": f"获取分支 {ref} 的文件列表",
            }
        ],
        "action": "list_files",
        "params": {
            "organizationId": organization_id,
            "repositoryId": repository_id,
            "ref": ref,
            "path": path,
            "type": "RECURSIVE",
        },
    }


@tool(
    name="get_yunxiao_file_content",
    description="读取云效仓库中的文件内容。",
    input_schema={
        "organization_id": {
            "type": "string",
            "description": "组织ID",
        },
        "repository_id": {
            "type": "string",
            "description": "代码库ID或路径",
        },
        "file_path": {
            "type": "string",
            "description": "文件路径",
        },
        "ref": {
            "type": "string",
            "description": "分支名称",
        },
    },
)
async def get_yunxiao_file_content(
    organization_id: str,
    repository_id: str,
    file_path: str,
    ref: str,
) -> dict:
    """读取云效仓库文件"""
    return {
        "content": [
            {
                "type": "text",
                "text": f"读取文件: {file_path} (分支: {ref})",
            }
        ],
        "action": "get_file_blobs",
        "params": {
            "organizationId": organization_id,
            "repositoryId": repository_id,
            "filePath": file_path,
            "ref": ref,
        },
    }


# 创建云效 MCP Server
yunxiao_server = create_sdk_mcp_server(
    name="yunxiao-mr-tools",
    version="1.0.0",
    tools=[
        get_yunxiao_mr,
        get_yunxiao_mr_diff,
        comment_on_yunxiao_mr,
        get_yunxiao_mr_files,
        get_yunxiao_file_content,
    ],
)


class YunxiaoMRReviewHelper:
    """云效 MR 审查辅助类"""

    DEFAULT_ORGANIZATION_ID = "5ea86562f89c9700014a671f"

    def __init__(
        self,
        organization_id: str = DEFAULT_ORGANIZATION_ID,
    ):
        self.organization_id = organization_id

    async def get_mr_info(
        self,
        repository_id: str,
        local_id: str,
    ) -> dict[str, Any]:
        """获取 MR 信息"""
        return await get_yunxiao_mr(
            organization_id=self.organization_id,
            repository_id=repository_id,
            local_id=local_id,
        )

    async def get_mr_diff(
        self,
        repository_id: str,
        source_branch: str,
        target_branch: str,
    ) -> dict[str, Any]:
        """获取 MR diff"""
        return await get_yunxiao_mr_diff(
            organization_id=self.organization_id,
            repository_id=repository_id,
            source_branch=source_branch,
            target_branch=target_branch,
        )

    async def add_review_comment(
        self,
        repository_id: str,
        local_id: str,
        comment_type: str,
        content: str,
        file_path: str | None = None,
        line_number: int | None = None,
    ) -> dict[str, Any]:
        """添加审查评论"""
        return await comment_on_yunxiao_mr(
            organization_id=self.organization_id,
            repository_id=repository_id,
            local_id=local_id,
            comment_type=comment_type,
            content=content,
            file_path=file_path,
            line_number=line_number,
        )

    async def read_file(
        self,
        repository_id: str,
        file_path: str,
        ref: str,
    ) -> dict[str, Any]:
        """读取仓库文件"""
        return await get_yunxiao_file_content(
            organization_id=self.organization_id,
            repository_id=repository_id,
            file_path=file_path,
            ref=ref,
        )