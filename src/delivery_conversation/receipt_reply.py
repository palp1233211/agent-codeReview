"""客户只用肯定词/礼貌词（如 ค่ะ）回应时的收货确认：只有它是对“是否已收到包裹”这个问题的直接回复才算。

“ค่ะ”这类词单独出现不能证明收到包裹；但客户紧接着对一句“您已经收到包裹了吧”回答“ค่ะ”，在语义上就是确认。
是否真的是这样的问题由模型给出逐字原文，这里不判断语义，只核对：这句话确实出现在转写里、是另一位说话人说的、紧挨在回复之前。

“好的/OK/明白/谢谢”（โอเค、เข้าใจ、ขอบคุณ）表示听懂了或同意安排，不是“是的，收到了”，
整句话只由这类词组成时（is_acknowledgement_only）不能当作收货确认，即使它回在“收到了吗”之后。
"""

import re
from typing import Any

# 肯定词、应声词、语气词；整句话只由这些词组成才算“纯肯定回应”。长的放前面，避免“ครับผม”被拆成“ครับ”+“ผม”
_PARTICLES = sorted([
    "ครับผม", "ค่ะ", "คะ", "ค่า", "คับ", "ครับ", "ใช่", "จ้ะ", "จ้า", "อืม", "อื้อ", "อ่ะ", "เออ", "นะคะ", "นะครับ", "นะ",
    "yes", "yeah", "yep", "uh-huh", "mm", "mhm", "hmm",
    "是的", "是", "对的", "对", "嗯",
], key=len, reverse=True)
# 听懂了/同意安排/道谢：不是“已收到”的肯定。空白和标点会先被去掉，所以 “thank you” 写成 “thankyou”
_ACKNOWLEDGEMENTS = sorted([
    "โอเค", "โอเก", "เข้าใจแล้ว", "เข้าใจ", "รับทราบ", "ขอบคุณ", "ขอบใจ",
    "okay", "ok", "understood", "thankyou", "thanks", "thank",
    "好的", "好", "行", "明白", "知道了", "谢谢", "多谢",
], key=len, reverse=True)
# 明确的“是的”：和应声词同时出现时（“ใช่ครับ เข้าใจ”），是肯定回答加上听懂了，仍交给提问核对处理
_STRONG_YES = ["ใช่", "yes", "yeah", "yep", "是的", "对的"]
_PARTICLE_PATTERN = re.compile("|".join(re.escape(particle) for particle in _PARTICLES), re.IGNORECASE)
_STRONG_YES_PATTERN = re.compile("|".join(re.escape(word) for word in _STRONG_YES), re.IGNORECASE)
_ACKNOWLEDGEMENT_PATTERN = re.compile("|".join(re.escape(word) for word in _ACKNOWLEDGEMENTS), re.IGNORECASE)
# “ๆ”是泰语的重复符号（“โอเคๆ”= 好好），不算内容
_PUNCTUATION_PATTERN = re.compile(r"[\s,.;:!?，。；：！？、…\-~'\"“”‘’()（）ๆ]+")

# 问题所在发言与回复所在发言之间最多隔几段（允许中间有一句很短的插话）
MAX_SEGMENT_DISTANCE = 2


def is_bare_affirmation(text: str) -> bool:
    """整句话只由肯定词、应声词、语气词组成（如“ค่ะ”“ใช่ค่ะ”“嗯，是的”），没有其他实际内容。

    只有“好的/OK/明白”这类词的不算（见 is_acknowledgement_only）；“ใช่ครับ เข้าใจ”这样肯定词加听懂了的算。
    """
    stripped = _PUNCTUATION_PATTERN.sub("", text or "")
    if not stripped or is_acknowledgement_only(text):
        return False
    remainder = _ACKNOWLEDGEMENT_PATTERN.sub("", stripped)
    return _PARTICLE_PATTERN.sub("", remainder) == ""


def is_acknowledgement_only(text: str) -> bool:
    """整句话只由“好的/OK/明白/谢谢”和语气词组成，且至少有一个这样的词（如“โอเค โอเค”“โอเคครับ”“เข้าใจ ขอบคุณครับ”）。

    只要还有别的实际内容（“ติ๊กต็อก โอเค”“โอเค ได้รับแล้ว”）或者同时有“ใช่”这类明确的“是的”，就不属于这一类。
    """
    stripped = _PUNCTUATION_PATTERN.sub("", text or "")
    if not stripped or not _ACKNOWLEDGEMENT_PATTERN.search(stripped):
        return False
    remainder = _ACKNOWLEDGEMENT_PATTERN.sub("", stripped)
    return not _STRONG_YES_PATTERN.search(remainder) and _PARTICLE_PATTERN.sub("", remainder) == ""


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().casefold()


def question_precedes_reply(
    segments: list[dict[str, Any]],
    reply_segment: dict[str, Any],
    reply_speaker_id: str,
    question_speaker_id: str,
    question: str,
) -> bool:
    """question 的原文确实出现在另一位说话人、紧挨在 reply_segment 之前的发言里。"""
    wanted = _normalize(question)
    if not wanted or not question_speaker_id or question_speaker_id == reply_speaker_id:
        return False
    index = next((i for i, segment in enumerate(segments) if segment is reply_segment), None)
    if index is None:
        return False
    return any(
        segment["speaker_id"] == question_speaker_id and wanted in _normalize(segment["text"])
        for segment in segments[max(0, index - MAX_SEGMENT_DISTANCE):index]
    )
