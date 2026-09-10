"""飞书 API 客户端封装"""
import json
import os
import urllib.parse
from typing import Any

import lark_oapi as lark
import requests
from lark_oapi.api.drive.v1 import CopyFileRequest, CopyFileRequestBody
from lark_oapi.api.docx.v1 import ListDocumentBlockRequest
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

FEISHU_API = "https://open.feishu.cn/open-apis"


class LarkClient:
    def __init__(self, app_id: str, app_secret: str):
        self._app_id = app_id
        self._app_secret = app_secret
        self._sdk = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .build()
        )

    @classmethod
    def from_env(cls, env_prefix: str = "LARK_") -> "LarkClient":
        """按机器人专属环境变量创建客户端。

        ``env_prefix`` 例如 ``LARK_DIFY_`` 或 ``LARK_CODE_REVIEW_``，分别读取
        ``<prefix>APP_ID`` 和 ``<prefix>APP_SECRET``。不在这里回退到另一套
        凭证，避免两个机器人误以同一飞书应用身份运行。
        """
        return cls(
            app_id=os.environ[f"{env_prefix}APP_ID"],
            app_secret=os.environ[f"{env_prefix}APP_SECRET"],
        )

    @property
    def app_id(self) -> str:
        return self._app_id

    @property
    def app_secret(self) -> str:
        return self._app_secret

    def _get_token(self) -> str:
        resp = requests.post(
            f"{FEISHU_API}/auth/v3/tenant_access_token/internal",
            json={"app_id": self._app_id, "app_secret": self._app_secret},
            timeout=(3, 15),
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"get_token failed: {data.get('msg')}")
        return data["tenant_access_token"]

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }

    def copy_file(
        self,
        file_token: str,
        name: str,
        folder_token: str,
        file_type: str = "docx",
    ) -> str:
        req = (
            CopyFileRequest.builder()
            .file_token(file_token)
            .request_body(
                CopyFileRequestBody.builder()
                .name(name)
                .folder_token(folder_token)
                .type(file_type)
                .build()
            )
            .build()
        )
        resp = self._sdk.drive.v1.file.copy(req)
        if not resp.success():
            raise RuntimeError(f"copy_file failed [{resp.code}]: {resp.msg}")
        return resp.data.file.token

    def list_document_blocks(self, doc_token: str) -> list[Any]:
        req = ListDocumentBlockRequest.builder().document_id(doc_token).build()
        resp = self._sdk.docx.v1.document_block.list(req)
        if not resp.success():
            raise RuntimeError(f"list_document_blocks failed [{resp.code}]: {resp.msg}")
        return resp.data.items or []

    def batch_update_blocks(
        self,
        doc_token: str,
        updates: list[dict[str, Any]],
    ) -> None:
        # 使用 raw HTTP 调用，确保 update_text_elements 结构完全可控
        resp = requests.patch(
            f"{FEISHU_API}/docx/v1/documents/{doc_token}/blocks/batch_update",
            headers=self._headers(),
            json={"requests": updates},
            timeout=(3, 15),
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"batch_update_blocks failed [{data.get('code')}]: {data.get('msg')}")

    def get_bot_open_id(self) -> str:
        resp = requests.get(f"{FEISHU_API}/bot/v3/info", headers=self._headers(), timeout=(3, 15))
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"get_bot_open_id failed [{data.get('code')}]: {data.get('msg')}")
        return data["bot"]["open_id"]

    def get_user_name(self, open_id: str) -> str:
        """按发送者 open_id 获取飞书用户姓名。"""
        resp = requests.get(
            f"{FEISHU_API}/contact/v3/users/{urllib.parse.quote(open_id, safe='')}",
            headers=self._headers(),
            params={"user_id_type": "open_id"},
            timeout=(3, 15),
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"get_user_name failed [{data.get('code')}]: {data.get('msg')}")
        return data.get("data", {}).get("user", {}).get("name", "")

    def get_user_open_id_by_email(self, email: str) -> str:
        """通过企业邮箱解析飞书 open_id。"""
        resp = requests.post(
            f"{FEISHU_API}/contact/v3/users/batch_get_id",
            headers=self._headers(),
            params={"user_id_type": "open_id"},
            json={"emails": [email], "include_resigned": False},
            timeout=(3, 15),
        )
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if not resp.ok or data.get("code") != 0:
            raise RuntimeError(
                "get_user_open_id_by_email failed: "
                f"http_status={resp.status_code} "
                f"code={data.get('code')} msg={data.get('msg')} "
                f"request_id={resp.headers.get('X-Tt-Logid', '')}"
            )
        users = data.get("data", {}).get("user_list") or []
        return str(users[0].get("user_id") or "") if users else ""

    def send_text_message(self, chat_id: str, text: str) -> str:
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            )
            .build()
        )
        resp = self._sdk.im.v1.message.create(req)
        if not resp.success():
            raise RuntimeError(f"send_text_message failed [{resp.code}]: {resp.msg}")
        return resp.data.message_id

    def reply_text_message(
        self,
        message_id: str,
        text: str,
        idempotency_key: str,
    ) -> str:
        """以当前机器人身份回复指定消息，并返回实际消息 ID。"""
        resp = requests.post(
            f"{FEISHU_API}/im/v1/messages/{urllib.parse.quote(message_id, safe='')}/reply",
            headers=self._headers(),
            json={
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "uuid": idempotency_key,
            },
            timeout=(3, 15),
        )
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if not resp.ok or data.get("code") != 0:
            raise RuntimeError(
                "reply_text_message failed: "
                f"http_status={resp.status_code} "
                f"code={data.get('code')} msg={data.get('msg')} "
                f"request_id={resp.headers.get('X-Tt-Logid', '')}"
            )
        return data["data"]["message_id"]

    def send_review_message(
        self,
        chat_id: str,
        text: str,
        idempotency_key: str,
        open_id: str = "",
        user_name: str = "合并人",
        content_rows: list[list[dict[str, Any]]] | None = None,
    ) -> str:
        """幂等发送 Review 结果，并返回真实消息 ID。"""
        if content_rows is not None:
            msg_type = "post"
            content: dict[str, Any] = {
                "zh_cn": {
                    "title": "📋 代码审查完成",
                    "content": content_rows,
                },
            }
        else:
            msg_type = "text"
            content = {"text": text}
        resp = requests.post(
            f"{FEISHU_API}/im/v1/messages",
            headers=self._headers(),
            params={"receive_id_type": "chat_id"},
            json={
                "receive_id": chat_id,
                "msg_type": msg_type,
                "content": json.dumps(content, ensure_ascii=False),
                "uuid": idempotency_key,
            },
            timeout=(3, 15),
        )
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if not resp.ok or data.get("code") != 0:
            raise RuntimeError(
                "send_review_message failed: "
                f"http_status={resp.status_code} "
                f"code={data.get('code')} msg={data.get('msg')} "
                f"request_id={resp.headers.get('X-Tt-Logid', '')}"
            )
        return data["data"]["message_id"]


def encode_url(url: str) -> str:
    return urllib.parse.quote(url, safe="")
