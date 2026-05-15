"""BI 双周文档创建 Agent"""
import asyncio
from datetime import date, timedelta
from typing import Any

from ..lark.client import LarkClient, encode_url

# 固定配置
FOLDER_TOKEN = "UrcNfhwkalLeaKd1GRqcfKcSnvh"
SUMMARY_TEMPLATE = "Nzoxd1LrPoCwQaxFOAdcXMm4nMh"
COUNTRY_TEMPLATE = "Ou8tdObXMolH8VxQ0yzcd5K6nQh"
CHAT_ID = "oc_02b6d626dd23a36c13ab1e3c6d80d21f"
MENTION_USER_ID = "ou_59c70b3948d557b0edbf258050241b21"
FEISHU_DOCX_BASE = "https://flashexpress.feishu.cn/docx"


def _next_thursday() -> str:
    today = date.today()
    days_ahead = (3 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return (today + timedelta(days=days_ahead)).strftime("%Y%m%d")


def _extract_block_text(block: Any) -> str:
    content = getattr(block, "text", None) or getattr(block, "paragraph", None)
    if not content:
        return ""
    return "".join(
        getattr(getattr(elem, "text_run", None), "content", "") or ""
        for elem in (getattr(content, "elements", None) or [])
    )


def _find_block(blocks: list[Any], search: str) -> Any:
    for block in blocks:
        if search in _extract_block_text(block):
            return block
    raise ValueError(f"找不到包含 '{search}' 的文档块")


def _find_block_id(blocks: list[Any], search: str) -> str:
    return _find_block(blocks, search).block_id


def _elements_before(block: Any, marker: str) -> list[dict]:
    """提取 block 中 marker 文本之前的原始元素（用于保留模板中已有的内容）。"""
    content = getattr(block, "text", None) or getattr(block, "paragraph", None)
    if not content:
        return []
    result = []
    for elem in (getattr(content, "elements", None) or []):
        text_run = getattr(elem, "text_run", None)
        if text_run and marker in (getattr(text_run, "content", "") or ""):
            break
        result.append(_elem_to_dict(elem))
    return result


def _elem_to_dict(elem: Any) -> dict:
    """把 SDK 元素对象转成 dict（用于放回 update_text_elements）。"""
    if getattr(elem, "text_run", None):
        tr = elem.text_run
        style = getattr(tr, "text_element_style", None)
        d: dict[str, Any] = {"content": getattr(tr, "content", "")}
        if style:
            d["text_element_style"] = {
                k: v for k, v in vars(style).items() if v is not None
            }
        return {"text_run": d}
    if getattr(elem, "mention_user", None):
        mu = elem.mention_user
        return {"mention_user": {"user_id": getattr(mu, "user_id", ""), "text_element_style": {}}}
    if getattr(elem, "mention_doc", None):
        md = elem.mention_doc
        return {
            "mention_doc": {
                "token": getattr(md, "token", ""),
                "obj_type": getattr(md, "obj_type", 22),
                "url": getattr(md, "url", ""),
                "title": getattr(md, "title", ""),
            }
        }
    return {}


def _branch_update(block_id: str, date_str: str) -> dict:
    return {
        "block_id": block_id,
        "update_text_elements": {
            "elements": [
                {
                    "text_run": {
                        "content": f"1.上线分支：feature/{date_str}/common",
                        "text_element_style": {"bold": True},
                    }
                }
            ]
        },
    }


def _country_links_update(
    block: Any,
    date_str: str,
    thai_token: str,
    ph_token: str,
) -> dict:
    thai_url = encode_url(f"{FEISHU_DOCX_BASE}/{thai_token}")
    ph_url = encode_url(f"{FEISHU_DOCX_BASE}/{ph_token}")
    # 保留「泰国：」之前的原始元素（如模板中已有的 mention_user）
    prefix = _elements_before(block, "泰国：")
    return {
        "block_id": block.block_id,
        "update_text_elements": {
            "elements": prefix + [
                {"text_run": {"content": "    泰国："}},
                {
                    "mention_doc": {
                        "token": thai_token,
                        "obj_type": 22,
                        "url": thai_url,
                        "title": f"BI {date_str} 迭代上线SQL及任务 -- 泰国",
                    }
                },
                {"text_run": {"content": "\n"}},
                {
                    "mention_user": {
                        "user_id": MENTION_USER_ID,
                        "text_element_style": {},
                    }
                },
                {"text_run": {"content": "    菲律宾："}},
                {
                    "mention_doc": {
                        "token": ph_token,
                        "obj_type": 22,
                        "url": ph_url,
                        "title": f"BI {date_str} 迭代上线SQL及任务 -- 菲律宾",
                    }
                },
            ]
        },
    }


async def run_bi_weekly_doc(date_str: str | None = None) -> dict[str, Any]:
    """执行 BI 双周文档创建任务

    Args:
        date_str: 指定日期，格式 YYYYMMDD。不传则自动取本周四。
    """
    date_str = date_str or _next_thursday()
    client = LarkClient.from_env()

    # 步骤 2：顺序复制两个国家文档（同一模板并发复制会触发飞书 1061045 资源竞争）
    thai_token = await asyncio.to_thread(
        client.copy_file,
        COUNTRY_TEMPLATE,
        f"BI {date_str} 迭代上线SQL及任务 -- 泰国",
        FOLDER_TOKEN,
    )
    ph_token = await asyncio.to_thread(
        client.copy_file,
        COUNTRY_TEMPLATE,
        f"BI {date_str} 迭代上线SQL及任务 -- 菲律宾",
        FOLDER_TOKEN,
    )

    # 步骤 3：复制汇总文档
    summary_token = await asyncio.to_thread(
        client.copy_file,
        SUMMARY_TEMPLATE,
        f"BI {date_str} 迭代上线文档",
        FOLDER_TOKEN,
    )

    # 步骤 4：更新汇总文档
    blocks = await asyncio.to_thread(client.list_document_blocks, summary_token)
    branch_block_id = _find_block_id(blocks, "1.上线分支：")
    country_block = _find_block(blocks, "泰国：")

    await asyncio.to_thread(
        client.batch_update_blocks,
        summary_token,
        [
            _branch_update(branch_block_id, date_str),
            _country_links_update(country_block, date_str, thai_token, ph_token),
        ],
    )

    summary_url = f"{FEISHU_DOCX_BASE}/{summary_token}"

    # 步骤 6：发送飞书群消息（暂时注释，确认流程正确后再开启）
    await asyncio.to_thread(client.send_text_message, CHAT_ID, summary_url)

    return {
        "date": date_str,
        "summary_url": summary_url,
        "thai_url": f"{FEISHU_DOCX_BASE}/{thai_token}",
        "ph_url": f"{FEISHU_DOCX_BASE}/{ph_token}",
    }
