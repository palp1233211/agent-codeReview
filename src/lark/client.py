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
    def from_env(cls) -> "LarkClient":
        return cls(
            app_id=os.environ["LARK_APP_ID"],
            app_secret=os.environ["LARK_APP_SECRET"],
        )

    def _get_token(self) -> str:
        resp = requests.post(
            f"{FEISHU_API}/auth/v3/tenant_access_token/internal",
            json={"app_id": self._app_id, "app_secret": self._app_secret},
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
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"batch_update_blocks failed [{data.get('code')}]: {data.get('msg')}")

    def send_text_message(self, chat_id: str, text: str) -> None:
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


def encode_url(url: str) -> str:
    return urllib.parse.quote(url, safe="")
