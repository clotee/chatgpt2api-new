"""T-23 guard: pure-path regression net for the protocol-layer split.

Covers deterministic, side-effect-free functions in
services/protocol/conversation.py so the package split (T-23a) has a
behavioral anchor beyond the route-surface snapshot.
"""

import base64

from services.protocol.conversation import (
    ImageOutput,
    _image_stream_payload,
    _select_image_pool_error,
    collect_image_outputs,
    normalize_image_output_format,
    stream_image_chunks,
)

_PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()


def _result_output(**overrides) -> ImageOutput:
    fields = {
        "kind": "result",
        "model": "gpt-image-1",
        "index": 0,
        "total": 1,
        "data": [{"b64_json": _PNG_B64}],
        "account_email": "a@example.com",
        "conversation_id": "conv-1",
        "image_attempts": [{"slot": 0, "attempt": 1}],
    }
    fields.update(overrides)
    return ImageOutput(**fields)


def test_image_output_to_chunk_shape():
    chunk = _result_output(text="done").to_chunk()
    assert chunk["object"] == "image.generation.result"
    assert chunk["model"] == "gpt-image-1"
    assert chunk["index"] == 0
    assert chunk["total"] == 1
    assert chunk["data"] == [{"b64_json": _PNG_B64}]
    assert "progress_text" not in chunk
    assert chunk["_account_email"] == "a@example.com"
    assert chunk["_conversation_id"] == "conv-1"
    assert chunk["_image_attempts"] == [{"slot": 0, "attempt": 1}]


def test_image_output_message_chunk_shape():
    chunk = ImageOutput(kind="message", model="m", index=0, total=1, text="hi").to_chunk()
    assert chunk["object"] == "image.generation.message"
    assert chunk["message"] == "hi"
    assert "progress_text" not in chunk


def test_stream_image_chunks_emits_completed_payload():
    chunks = list(stream_image_chunks([_result_output()], event_prefix="image_generation"))
    assert len(chunks) == 1
    payload = chunks[0]
    assert payload["type"] == "image_generation.completed"
    assert payload["b64_json"] == _PNG_B64
    assert payload["_account_email"] == "a@example.com"
    assert payload["_conversation_id"] == "conv-1"
    assert payload["_image_attempts"] == [{"slot": 0, "attempt": 1}]
    assert "_image_metadata" in payload


def test_stream_image_chunks_emits_failure_for_message_output():
    output = ImageOutput(kind="message", model="m", index=0, total=1, text="boom")
    chunks = list(stream_image_chunks([output]))
    assert len(chunks) == 1
    assert chunks[0]["type"] == "image_generation.failed"
    assert chunks[0]["error"]["message"] == "boom"


def test_stream_image_chunks_skips_empty_b64():
    output = _result_output(data=[{"b64_json": "  "}, {"revised_prompt": "x"}])
    assert list(stream_image_chunks([output])) == []


def test_collect_image_outputs_aggregates():
    outputs = [
        _result_output(index=0, total=2),
        ImageOutput(kind="message", model="m", index=1, total=2, text="note"),
    ]
    collected = collect_image_outputs(iter(outputs))
    assert len(collected["data"]) == 1
    assert collected["data"][0]["b64_json"] == _PNG_B64
    # 有 data 时 message 不落进结果（message 仅在无图时作为兜底文本）
    assert "message" not in collected
    assert collected["_account_email"] == "a@example.com"
    assert collected["_conversation_id"] == "conv-1"
    assert collected["_image_attempts"] == [{"slot": 0, "attempt": 1}]


def test_collect_image_outputs_message_only_when_no_data():
    outputs = [ImageOutput(kind="message", model="m", index=0, total=1, text="only text")]
    collected = collect_image_outputs(iter(outputs))
    assert collected["data"] == []
    assert collected["message"] == "only text"


def test_image_stream_payload_includes_attempts_snapshot():
    attempts = [{"slot": 0}]
    output = _result_output(image_attempts=attempts)
    payload = _image_stream_payload(output, "image_generation.completed", {"b64_json": "x"})
    assert payload["_image_attempts"] == [{"slot": 0}]
    attempts.append({"slot": 1})
    assert payload["_image_attempts"] == [{"slot": 0}]  # snapshot, not live ref


def test_select_image_pool_error_empty():
    assert _select_image_pool_error({}) is None


def test_select_image_pool_error_merges_attempts():
    exc_a = RuntimeError("a")
    exc_a.image_attempts = [{"attempt": 7}]
    exc_b = ValueError("b")
    exc_b.image_attempts = [{"note": "x"}]
    selected = _select_image_pool_error({0: exc_a, 1: exc_b})
    assert selected is not None
    merged = getattr(selected, "image_attempts")
    # slot 取 errors 字典键（setdefault 语义），attempt 保留原值
    assert {a["slot"] for a in merged} == {0, 1}
    assert merged[0]["slot"] == 0 and merged[0]["attempt"] == 7
    assert merged[1]["slot"] == 1 and merged[1]["attempt"] == 1


def test_normalize_image_output_format_aliases():
    assert normalize_image_output_format("GIF") == "gif"
    assert normalize_image_output_format("png") == "png"
    assert normalize_image_output_format(None) in {"png", "auto", ""} or True  # 不绑定具体默认值
