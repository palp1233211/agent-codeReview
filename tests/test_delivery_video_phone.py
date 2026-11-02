import asyncio
import time
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.delivery_conversation.agent import ConversationEvidenceAgent
from src.delivery_conversation.schemas import ConversationEvidenceRequest, ConversationMediaResult, ConversationMediaType, PhoneObservation
from src.delivery_conversation.video_phone import MediaCommandError, VideoPhoneReader, compare_phones, normalize_phone


def observation(raw="+66 99-181-2244", **kwargs):
    return PhoneObservation(frame_index=1, time_seconds=0, raw_phone=raw,
                            is_call_counterparty=kwargs.get("counterparty", True),
                            readable=kwargs.get("readable", True))


@pytest.mark.parametrize("raw", ["0991812244", "+66 99-181-2244", "0066991812244", "๐๙๙๑๘๑๒๒๔๔"])
def test_thai_equivalent_numbers(raw):
    assert normalize_phone(raw, "TH") == "+66991812244"


@pytest.mark.parametrize("raw", ["+66 99-18??-2244", "991812244", "call 0991812244", "0991812244 ext 1"])
def test_partial_or_ambiguous_phone_is_not_completed(raw):
    assert normalize_phone(raw, "TH") is None


@pytest.mark.parametrize("items,target,expected", [
    ([observation()], "0991812244", "matched"),
    ([observation("0991812245")], "0991812244", "mismatch"),
    ([observation(), observation("0991812245")], "0991812244", "uncertain"),
    ([observation(counterparty=False)], "0991812244", "uncertain"),
    ([observation(readable=False)], "0991812244", "uncertain"),
    ([observation()], None, "uncertain"),
    ([], "0991812244", "uncertain"),
])
def test_comparison_requires_readable_counterparty(items, target, expected):
    assert compare_phones(items, target, "TH") == expected


def test_sampling_is_bounded_and_covers_start_and_end():
    times = VideoPhoneReader.sample_times(28.61)
    assert len(times) == 6
    assert times[0] == 0
    assert times[-1] == 28.51
    gaps = [round(b - a, 3) for a, b in zip(times, times[1:])]
    assert max(gaps) - min(gaps) < 0.01
    with pytest.raises(ValueError):
        VideoPhoneReader.sample_times(float("nan"))


@pytest.mark.parametrize("duration,expected_count", [(2, 3), (12, 3), (13, 4), (17, 5), (21, 6), (299, 6)])
def test_sampling_scales_with_duration_between_three_and_six_frames(duration, expected_count):
    times = VideoPhoneReader.sample_times(duration)
    assert len(times) == expected_count
    assert times == sorted(set(times))


@pytest.mark.parametrize("items,expected", [
    # 转场/遮挡帧：可见数字与完整号码不矛盾时不应拖垮整体结果。
    ([observation("063 768 6120"), observation("063 768 ??20", readable=False)], "matched"),
    ([observation("063 768 6120"), observation("063 768", readable=False)], "matched"),
    ([observation("063 768 6120"), observation("???", readable=False)], "matched"),
    ([observation("063 768 6120"), observation("+66 63 768 61??", readable=False)], "matched"),
    # 可见数字与完整号码矛盾，说明画面里还有别的号码，保持无法核验。
    ([observation("063 768 6120"), observation("099 181 ????", readable=False)], "uncertain"),
    ([observation("063 768 6120"), observation("12345")], "uncertain"),
    # 非本次通话对方的帧始终忽略。
    ([observation("063 768 6120"), observation("099 181 2244", counterparty=False)], "matched"),
    # 完整号码与目标不一致仍是 mismatch，部分号码不改变这一点。
    ([observation("063 768 6121"), observation("063 768 ??21", readable=False)], "mismatch"),
    # 只有不可读号码时没有可比对依据。
    ([observation("063 768 ??20", readable=False)], "uncertain"),
])
def test_unreadable_counterparty_frames_only_veto_when_contradicting(items, expected):
    assert compare_phones(items, "0637686120", "TH") == expected


def test_output_keeps_raw_phone_and_timestamp_and_survives_vision_error():
    async def run():
        config = SimpleNamespace(timeout_seconds=10)
        agent = ConversationEvidenceAgent(config, model_client=object())
        agent.phone_reader.read = AsyncMock(return_value=([observation()], [0]))
        request = ConversationEvidenceRequest(request_id="r", pno="THT1", country="TH",
                    platform="shopee", recipient_phone="0991812244", media_files=[
                    {"media_id": "m", "media_type": "video", "url": "https://example.com/a.mp4"}])
        result = ConversationMediaResult(media_id="m", media_type="video", transcription="preserved",
                    waybill_match="matched", customer_receipt_status="confirmed")
        await agent._append_phone_review(result, request, b"media", ".mp4", time.monotonic() + 10)
        payload = result.model_dump(mode="json")
        assert payload["detected_phone_numbers"] == ["+66 99-181-2244"]
        assert payload["phone_observations"][0]["time_seconds"] == 0
        assert payload["phone_match"] == "matched"
        agent.phone_reader.read = AsyncMock(side_effect=TimeoutError())
        await agent._append_phone_review(result, request, b"media", ".mp4", time.monotonic() + 10)
        assert result.transcription == "preserved"
        assert result.phone_match == "uncertain"
    asyncio.run(run())


def test_reader_supplies_images_but_not_target_phone_and_uses_actual_timestamps():
    async def run():
        client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(
            output_text='{"observations":[{"frame_index":2,"raw_phone":"0991812244","is_call_counterparty":true,"readable":true}]}'
        ))))
        runner = AsyncMock(side_effect=[b"video\n2.0\n"] + [b"\xff\xd8jpeg"] * len(VideoPhoneReader.sample_times(2)))
        reader = VideoPhoneReader(SimpleNamespace(api_mode="responses", model="vision"), runner, client)
        items, times = await reader.read(b"video", ".mp4", 10)
        assert items[0].time_seconds == times[1]
        parts = client.responses.create.call_args.kwargs["input"][0]["content"]
        assert sum(p["type"] == "input_image" for p in parts) == len(times)
        assert "0991812244" not in parts[0]["text"]
    asyncio.run(run())


def test_frame_extraction_allows_non_full_range_yuv_as_separate_ffmpeg_args():
    """手机视频常见的非全范围 YUV，新版 ffmpeg 的 mjpeg 默认拒绝（退出码 234），要显式放宽。"""
    runner = AsyncMock(return_value=b"\xff\xd8jpeg")
    reader = VideoPhoneReader(SimpleNamespace(), runner, SimpleNamespace())
    asyncio.run(reader._extract_frame("ffmpeg", "clip.mp4", 1.5, 10))
    command = runner.call_args.args[0]
    index = command.index("-strict")
    assert command[index + 1] == "unofficial"
    assert command.index("-vcodec") < index < command.index("-q:v")
    assert runner.await_count == 1


def test_frame_extraction_falls_back_to_full_range_pixel_format_once():
    runner = AsyncMock(side_effect=[MediaCommandError("音视频预处理失败"), b"\xff\xd8jpeg"])
    reader = VideoPhoneReader(SimpleNamespace(), runner, SimpleNamespace())
    assert asyncio.run(reader._extract_frame("ffmpeg", "clip.mp4", 0, 10)) == b"\xff\xd8jpeg"
    fallback = runner.await_args_list[1].args[0]
    assert "-strict" not in fallback
    assert any("format=yuvj420p" in part for part in fallback)


def test_frame_extraction_reports_error_when_fallback_also_fails():
    runner = AsyncMock(side_effect=MediaCommandError("坏文件"))
    reader = VideoPhoneReader(SimpleNamespace(), runner, SimpleNamespace())
    with pytest.raises(MediaCommandError):
        asyncio.run(reader._extract_frame("ffmpeg", "clip.mp4", 0, 10))
    assert runner.await_count == 2


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="requires ffmpeg")
def test_real_ffmpeg_extracts_local_video_frames(tmp_path):
    source = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=white:s=320x240:d=1",
                    "-c:v", "mpeg4", str(source)], check=True, timeout=10)

    async def run():
        client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(
            return_value=SimpleNamespace(output_text='{"observations":[]}'))))
        reader = VideoPhoneReader(SimpleNamespace(api_mode="responses", model="vision"),
                                  ConversationEvidenceAgent._run_media_command, client)
        observations, times = await asyncio.wait_for(reader.read(source.read_bytes(), ".mp4", 10), 20)
        assert not observations
        assert times[0] == 0
        assert len(times) > 1
    asyncio.run(run())


def test_sync_diarization_request_and_speaker_labels(tmp_path):
    async def run():
        wav = tmp_path / 'mono.wav'
        wav.write_bytes(b'RIFF-audio')
        response = SimpleNamespace(status_code=200, json=lambda: {'output': {'sentences': [
            {'speaker_id': 0, 'text': 'question', 'begin_time': 0, 'end_time': 1000},
            {'speaker_id': 1, 'text': 'answer', 'begin_time': 1000, 'end_time': 2000},
        ]}})
        client = SimpleNamespace(post=AsyncMock(return_value=response))
        config = SimpleNamespace(audio_api_token='test-token', api_token='fallback',
                    audio_model='qwen-audio-3.1-asr-flash', audio_base_url='https://example.com/api/v1')
        agent = ConversationEvidenceAgent(config, asr_client=client, model_client=object())
        segments = await agent._transcribe_with_diarization(str(wav), request_id='r', media_id='m', pno='THT1', timeout=10)
        assert [s['speaker_id'] for s in segments] == ['0', '1']
        assert client.post.await_count == 1
        args = client.post.call_args
        assert args.args[0].endswith('/services/aigc/multimodal-generation/generation')
        body = args.kwargs['json']
        assert body['model'] == 'qwen-audio-3.1-asr-flash'
        assert body['parameters']['speaker_diarization_enabled'] is True
        assert body['parameters']['sample_rate'] == 16000
        assert body['input']['messages'][0]['content'][0]['input_audio']['data'].startswith('data:audio/wav;base64,')
        assert 'X-DashScope-Async' not in args.kwargs['headers']
    asyncio.run(run())


@pytest.mark.parametrize('payload', [{'output': {}}, {'output': {'sentences': []}}, {}])
def test_sync_asr_invalid_output_fails_without_invented_speakers(tmp_path, payload):
    async def run():
        wav = tmp_path / 'mono.wav'
        wav.write_bytes(b'audio')
        client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: payload)))
        agent = ConversationEvidenceAgent(SimpleNamespace(audio_api_token='test', api_token='test',
            audio_model='qwen-audio-3.1-asr-flash', audio_base_url='https://example.com'), asr_client=client, model_client=object())
        with pytest.raises(ValueError):
            await agent._transcribe_with_diarization(str(wav), request_id='r', media_id='m', pno=None, timeout=10)
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['responses', 'chat_completions'])
def test_analysis_uses_image_model_and_protocol(mode):
    async def run():
        responses = AsyncMock(return_value=SimpleNamespace(output_text='{"customer_received":"confirmed"}'))
        chat = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"customer_received":"confirmed"}'))]))
        config = SimpleNamespace(model='image-model', api_mode=mode, timeout_seconds=20)
        agent = ConversationEvidenceAgent(config, model_client=SimpleNamespace(
            responses=SimpleNamespace(create=responses), chat=SimpleNamespace(completions=SimpleNamespace(create=chat))))
        request = ConversationEvidenceRequest(request_id='r', pno='THT1', country='TH', platform='TT',
            media_files=[{'media_id':'m','media_type':'video','url':'https://example.com/a.mp4'}])
        result = await agent._analyze_transcript(request, '{platform} {pno} {transcript}', 'text')
        chosen, other = (responses, chat) if mode=='responses' else (chat, responses)
        assert chosen.call_args.kwargs['model'] == 'image-model'
        assert chosen.await_count == 1 and other.await_count == 0
        assert result['customer_received'] == 'confirmed'
    asyncio.run(run())


def test_analysis_client_uses_image_credentials(monkeypatch):
    import sys
    from unittest.mock import Mock
    constructor = Mock()
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=constructor))
    ConversationEvidenceAgent(SimpleNamespace(api_token='image-token', base_url='https://image.example/v1'))
    assert constructor.call_args.kwargs['api_key'] == 'image-token'
    assert constructor.call_args.kwargs['base_url'] == 'https://image.example/v1'


@pytest.mark.parametrize('error_name', ['ReadTimeout', 'ConnectError', 'ConnectTimeout', 'ReadError'])
def test_download_retries_transient_errors_with_shared_budget(error_name):
    import httpx
    async def run():
        agent = ConversationEvidenceAgent(SimpleNamespace(), model_client=object())
        error = getattr(httpx, error_name)('transient')
        agent._download_media_once = AsyncMock(side_effect=[error, error, (b'video', '.mp4')])
        result = await agent._download_media_async(SimpleNamespace(media_id='m'), timeout=5)
        assert result == (b'video', '.mp4')
        calls = agent._download_media_once.call_args_list
        assert len(calls) == 3
        budgets = [call.args[1] for call in calls]
        assert 0 < budgets[2] < budgets[1] < budgets[0] <= 5
    asyncio.run(run())


def test_download_total_deadline_cancels_slow_read():
    async def run():
        agent = ConversationEvidenceAgent(SimpleNamespace(), model_client=object())
        async def slow(*args):
            await asyncio.sleep(10)
        agent._download_media_once = AsyncMock(side_effect=slow)
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            await agent._download_media_async(SimpleNamespace(media_id='m'), timeout=0.02)
        assert time.monotonic() - start < 1
        assert agent._download_media_once.await_count == 1
    asyncio.run(run())


def test_download_validation_errors_are_not_retried():
    async def run():
        agent = ConversationEvidenceAgent(SimpleNamespace(), model_client=object())
        agent._download_media_once = AsyncMock(side_effect=ValueError('invalid media'))
        with pytest.raises(ValueError):
            await agent._download_media_async(SimpleNamespace(media_id='m'), timeout=10)
        assert agent._download_media_once.await_count == 1
    asyncio.run(run())


def test_download_gives_up_after_three_attempts():
    import httpx
    async def run():
        agent = ConversationEvidenceAgent(SimpleNamespace(), model_client=object())
        agent._download_media_once = AsyncMock(side_effect=httpx.ReadTimeout('transient'))
        with pytest.raises(httpx.ReadTimeout):
            await agent._download_media_async(SimpleNamespace(media_id='m'), timeout=5)
        assert agent._download_media_once.await_count == 3
    asyncio.run(run())


def test_model_speaker_prefix_maps_to_actual_asr_speaker_id():
    config = SimpleNamespace(model="vision", api_mode="responses", timeout_seconds=20)
    agent = ConversationEvidenceAgent(config, model_client=object())
    request = ConversationEvidenceRequest(request_id="r", pno="THT24065F0WB50Z", country="TH", platform="TT",
        media_files=[{"media_id":"m", "media_type":"video", "url":"https://example.com/a.mp4"}])
    result = agent._build_media_result(
        SimpleNamespace(media_id="m", media_type=ConversationMediaType.VIDEO), request,
        "[7.84s] 1: เอ่อ ได้ ได้รับแล้วครับผม", [{"speaker_id":"1", "start":7.84, "end":9.28, "text":"เอ่อ ได้ ได้รับแล้วครับผม"}],
        {"speaker_roles":[{"speaker_id":"speaker_1", "role":"customer"}],
         "waybill_number":"THT24065F0WB50Z", "waybill_mentioned":True,
         "customer_received":"confirmed", "receipt_speaker_id":"speaker_1",
         "receipt_quote":"ได้ ได้รับแล้วครับผม", "confidence":0.95},
    )
    assert result.receipt_speaker_role == "customer"
    assert result.customer_receipt_status.value == "confirmed"
    assert result.receipt_quote == "ได้ ได้รับแล้วครับผม"
    assert result.receipt_time_seconds == 7.84


def test_prompt_speaker_example_matches_transcript_label_convention():
    from pathlib import Path
    prompt = (Path(__file__).parents[1] / "src/prompts/delivery_conversation.yaml").read_text()
    assert '"speaker_id": "0"' in prompt
    assert '"speaker_0"' not in prompt


def _jpeg() -> bytes:
    return b"\xff\xd8jpeg"


def test_empty_frame_at_the_end_of_the_video_is_retried_slightly_earlier_and_reports_the_actual_time():
    """容器时长比视频流略长时，最后一帧的取帧位置超出视频流结尾，ffmpeg 只输出 0 字节。"""
    async def run():
        count = len(VideoPhoneReader.sample_times(10))
        last = VideoPhoneReader.sample_times(10)[-1]
        client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(
            output_text='{"observations":[{"frame_index":%d,"raw_phone":"0991812244","is_call_counterparty":true,"readable":true}]}' % count
        ))))
        runner = AsyncMock(side_effect=[b"video\n10.0\n"] + [_jpeg()] * (count - 1) + [b"", _jpeg()])
        reader = VideoPhoneReader(SimpleNamespace(api_mode="responses", model="vision"), runner, client)
        items, times = await reader.read(b"video", ".mp4", 10)
        assert times[-1] == round(last - 1.0, 3)  # 往前退 1 秒取到
        assert items[0].time_seconds == times[-1]
        seek_args = [call.args[0][call.args[0].index("-ss") + 1] for call in runner.await_args_list[1:]]
        assert seek_args[-2:] == [str(last), str(round(last - 1.0, 3))]
    asyncio.run(run())


def test_frame_backoff_keeps_going_earlier_and_gives_up_with_the_original_position():
    async def run():
        times = VideoPhoneReader.sample_times(10)
        runner = AsyncMock(side_effect=[b"video\n10.0\n"] + [_jpeg()] * (len(times) - 1) + [b""] * 4)
        reader = VideoPhoneReader(SimpleNamespace(api_mode="responses", model="vision"), runner, SimpleNamespace())
        with pytest.raises(Exception, match=r"返回 0 字节") as exc_info:
            await reader.read(b"video", ".mp4", 10)
        assert f"第 {len(times)} 帧，{times[-1]}s" in str(exc_info.value)
        assert runner.await_count == 1 + len(times) + 3  # 原位置加 3 次回退
    asyncio.run(run())


def test_backoff_never_goes_before_the_start_and_skips_duplicate_positions():
    async def run():
        runner = AsyncMock(side_effect=[b""] * 4)
        reader = VideoPhoneReader(SimpleNamespace(), runner, SimpleNamespace())
        frame, actual = await reader._frame_near("ffmpeg", "clip.mp4", 0.5, 10)
        assert (frame, actual) == (b"", 0.5)
        seeks = [call.args[0][call.args[0].index("-ss") + 1] for call in runner.await_args_list]
        assert seeks == ["0.5", "0.0"]  # 退到开头后不再重复取同一个位置
    asyncio.run(run())
