"""飞书消息引用回复客户端测试。"""
import json
from unittest.mock import MagicMock, patch

from src.lark.client import LarkClient


def test_reply_text_message_uses_reply_endpoint_and_uuid():
    client = object.__new__(LarkClient)
    client._headers = MagicMock(return_value={"Authorization": "Bearer test"})
    response = MagicMock(
        ok=True,
        status_code=200,
        headers={},
    )
    response.json.return_value = {"code": 0, "data": {"message_id": "om_reply"}}

    with patch("src.lark.client.requests.post", return_value=response) as post:
        message_id = client.reply_text_message("om_feedback", "判断结果", "uuid-1")

    assert message_id == "om_reply"
    url = post.call_args.args[0]
    assert url.endswith("/im/v1/messages/om_feedback/reply")
    payload = post.call_args.kwargs["json"]
    assert payload["msg_type"] == "text"
    assert json.loads(payload["content"]) == {"text": "判断结果"}
    assert payload["uuid"] == "uuid-1"
