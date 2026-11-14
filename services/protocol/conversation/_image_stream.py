from __future__ import annotations

import base64
import json
import time

from curl_cffi.requests import exceptions as curl_exceptions
from services.config import config
from services.image_failure import (
    ImageDownloadError,
    ImageFailure,
    ImageGenerationError,
    classify_conversation_failure,
    classify_task_failure,
    classify_upstream_message,
    image_failure,
    merge_message_failure,
    public_image_error_message,
    terminal_assistant_text,
)
from services.openai_backend_api import OpenAIBackendAPI
from typing import (
    Any,
    Iterable,
    Iterator,
)
from utils.diagnostics import diagnostic_excerpt
from utils.log import logger

from ._core import (
    ConversationRequest,
    ImageOutput,
    _is_account_auth_failure,
    add_unique,
    conversation_events,
    format_image_result,
    is_image_generation_arguments,
)
from ._monitor import (
    _backend_http_timing_data,
    _download_image_bytes_with_monitor,
    _monitor_image_stage,
    _resolve_image_urls_with_monitor,
)

def _failure_raw_text(failure: ImageFailure | None, fallback: str = "") -> str:
    if failure is None:
        return str(fallback or "").strip()
    raw_detail = failure.raw_detail
    if isinstance(raw_detail, str):
        return raw_detail.strip() or str(fallback or "").strip()
    if raw_detail not in (None, ""):
        try:
            return json.dumps(raw_detail, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return repr(raw_detail)
    return str(fallback or "").strip()


def _get_detailed_failure_from_tasks(
    backend: OpenAIBackendAPI,
    conversation_id: str,
    timeout_secs: float = 10.0,
    wait_secs: float = 2.0,
) -> tuple[ImageFailure | None, str]:
    """Return the most specific structured task failure and its diagnostic text."""
    import time as _time
    try:
        if wait_secs > 0:
            _time.sleep(wait_secs)
        tasks = backend._query_backend_tasks(conversation_id=conversation_id, timeout_secs=timeout_secs)
        if not tasks:
            return None, ""

        selected_failure: ImageFailure | None = None
        for task in tasks:
            candidate = classify_task_failure(task)
            if candidate is None:
                continue
            error_msg, metadata = backend.image_task_diagnostics(task)
            raw_error = error_msg or _failure_raw_text(candidate)
            if raw_error:
                candidate = candidate.with_raw_detail(raw_error)
            logger.info({
                "event": "image_task_structured_error",
                "conversation_id": conversation_id,
                "failure_code": candidate.code,
                "error_msg": diagnostic_excerpt(raw_error, 1000),
                "metadata": metadata,
            })
            selected_failure = merge_message_failure(selected_failure, candidate)
        if selected_failure is None:
            return None, ""
        selected_error = _failure_raw_text(selected_failure)
        return (
            selected_failure.with_raw_detail(selected_error or selected_failure.raw_detail),
            selected_error,
        )
    except Exception as exc:
        if _is_account_auth_failure(exc):
            raise
        logger.warning({
            "event": "image_task_error_query_failed",
            "conversation_id": conversation_id,
            "error": diagnostic_excerpt(exc, 300),
        })
        return None, ""


def _recover_image_conversation_id(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        *,
        reason: str,
        message: str = "",
        started_at: float | None = None,
) -> str:
    """从最近对话中补救 conversation_id；只做一次，失败不影响主流程。"""
    if not request.prompt:
        return ""
    try:
        recovered_id = backend.find_conversation_by_prompt(
            request.prompt,
            started_at or time.time(),
            timeout_secs=5.0,
        )
        if recovered_id:
            logger.info({
                "event": "image_conversation_id_recovered",
                "reason": reason,
                "conversation_id": recovered_id,
                "message_preview": message[:200],
            })
            return recovered_id
    except Exception as exc:
        if _is_account_auth_failure(exc):
            raise
        logger.warning({
            "event": "image_conversation_id_recovery_failed",
            "reason": reason,
            "error": repr(exc)[:300],
        })
    return ""


def _image_stream_timeout_task_diagnostics(
        backend: OpenAIBackendAPI,
        conversation_id: str,
) -> tuple[ImageFailure | None, str, list[dict[str, Any]], str]:
    """Collect a small task summary after an upstream SSE timeout."""
    try:
        tasks = backend._query_backend_tasks(conversation_id=conversation_id, timeout_secs=5.0)
    except Exception as exc:
        if _is_account_auth_failure(exc):
            raise
        return None, "", [], diagnostic_excerpt(repr(exc), 1000)

    summaries: list[dict[str, Any]] = []
    task_failure: ImageFailure | None = None
    task_error = ""
    for task in tasks[:5]:
        if not isinstance(task, dict):
            continue
        error_msg, metadata = backend.image_task_diagnostics(task)
        failure = classify_task_failure(task)
        if failure is not None:
            if error_msg:
                failure = failure.with_raw_detail(error_msg)
            task_failure = merge_message_failure(task_failure, failure)
            task_error = _failure_raw_text(task_failure, task_error)
        summary: dict[str, Any] = {
            "id": str(task.get("id") or task.get("task_id") or "")[:80],
            "status": str(task.get("status") or metadata.get("status") or "")[:80],
            "type": str(task.get("type") or metadata.get("async_task_type") or "")[:80],
            "is_error": failure is not None,
        }
        for key in ("conversation_id", "original_conversation_id"):
            value = task.get(key)
            if value not in (None, ""):
                summary[key] = str(value)[:80]
        if failure is not None and error_msg:
            summary["error_preview"] = diagnostic_excerpt(error_msg, 500)
        summaries.append({key: value for key, value in summary.items() if value not in (None, "")})
    return task_failure, task_error, summaries, ""


def _image_stream_timeout_error(
        raw_error: str,
        conversation_id: str,
        followup: dict[str, Any],
        conversation_snapshot: dict[str, Any] | None = None,
        upstream_error: str = "",
        raw_upstream_message: str = "",
        failure_code: str = "image_stream_timeout",
) -> ImageGenerationError:
    exc = ImageGenerationError(
        raw_error,
        code=failure_code,
        conversation_id=conversation_id,
        raw_error=raw_error,
        upstream_error=diagnostic_excerpt(upstream_error, 4000),
        raw_upstream_message=diagnostic_excerpt(raw_upstream_message, 4000),
    )
    if failure_code == "image_stream_timeout":
        setattr(exc, "stream_timeout_secs", config.image_stream_timeout_secs)
    setattr(exc, "stream_timeout_followup", followup)
    if followup.get("task_error"):
        setattr(exc, "last_task_error", followup["task_error"])
    if conversation_snapshot:
        setattr(exc, "last_conversation_snapshot", conversation_snapshot)
    return exc


def _recover_after_image_stream_timeout(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        last: dict[str, Any],
        timeout_error: Exception,
        index: int,
        total: int,
        stream_started_at: float,
        *,
        failure_code: str = "image_stream_timeout",
) -> ImageOutput:
    is_timeout = failure_code == "image_stream_timeout"
    recovery_reason = "stream_timeout" if is_timeout else "stream_interrupted"
    recovery_path = f"{recovery_reason}_followup"
    followup_reason = "sse_timeout" if is_timeout else "sse_interrupted"
    raw_error = str(timeout_error) or (
        f"SSE stream exceeded {config.image_stream_timeout_secs}s"
        if is_timeout else "SSE stream interrupted"
    )
    conversation_id = str(last.get("conversation_id") or "")
    file_ids = [str(item) for item in last.get("file_ids") or []]
    sediment_ids = [str(item) for item in last.get("sediment_ids") or []]
    message = str(last.get("text") or "").strip()

    if not conversation_id and not file_ids:
        conversation_id = _recover_image_conversation_id(
            backend,
            request,
            reason=recovery_reason,
            message=message or raw_error,
            started_at=stream_started_at,
        )

    followup: dict[str, Any] = {
        "reason": followup_reason,
        "conversation_id": conversation_id,
        "stream_error": raw_error,
        "last_stream_state": {
            "conversation_id": str(last.get("conversation_id") or ""),
            "file_ids": file_ids,
            "sediment_ids": sediment_ids,
            "blocked": bool(last.get("blocked")),
            "tool_invoked": last.get("tool_invoked"),
            "turn_use_case": str(last.get("turn_use_case") or ""),
            "async_task_type": str(last.get("async_task_type") or ""),
            "message_type": str(last.get("message_type") or ""),
            "text_preview": diagnostic_excerpt(message, 1000),
        },
    }
    if is_timeout:
        followup["timeout_secs"] = config.image_stream_timeout_secs
    stream_failure = last.get("_image_failure")
    stream_failure = stream_failure if isinstance(stream_failure, ImageFailure) else None
    task_failure: ImageFailure | None = None
    task_error = ""
    task_summaries: list[dict[str, Any]] = []
    task_probe_error = ""
    conversation_snapshot: dict[str, Any] = {}
    latest_assistant_text = ""
    conversation_failure: ImageFailure | None = None
    conversation_probe_error = ""

    if conversation_id and not (file_ids or sediment_ids):
        task_failure, task_error, task_summaries, task_probe_error = _image_stream_timeout_task_diagnostics(
            backend,
            conversation_id,
        )
        try:
            conversation = backend._get_conversation(conversation_id, timeout_secs=10)
            conversation_snapshot, snapshot_assistant_text = backend._conversation_poll_snapshot(conversation)
            latest_assistant_text = terminal_assistant_text(conversation) or snapshot_assistant_text
            for record in backend._extract_image_tool_records(conversation):
                add_unique(file_ids, [str(item) for item in record.get("file_ids") or []])
                add_unique(sediment_ids, [str(item) for item in record.get("sediment_ids") or []])
            conversation_failure = classify_conversation_failure(conversation)
            terminal_message = next(
                (
                    item
                    for item in reversed(conversation_snapshot.get("messages") or [])
                    if (
                        isinstance(item, dict)
                        and str(item.get("role") or "").strip().lower() == "assistant"
                        and str(item.get("content_type") or "").strip().lower() in {"text", "code"}
                        and bool(item.get("text_preview"))
                    )
                ),
                None,
            )
            if (
                terminal_message is not None
                and str(terminal_message.get("content_type") or "").strip().lower() == "code"
                and is_image_generation_arguments(
                    latest_assistant_text,
                    role="assistant",
                    content_type="code",
                )
            ):
                conversation_failure = merge_message_failure(
                    conversation_failure,
                    image_failure("image_tool_error", raw_detail=latest_assistant_text),
                )
        except Exception as exc:
            if _is_account_auth_failure(exc):
                raise
            conversation_probe_error = diagnostic_excerpt(repr(exc), 1000)

    terminal_failure: ImageFailure | None = None
    for failure in (stream_failure, task_failure, conversation_failure):
        terminal_failure = merge_message_failure(terminal_failure, failure)
    failure_detail = _failure_raw_text(terminal_failure, message)

    followup.update({
        "failure_code": terminal_failure.code if terminal_failure else "",
        "task_error": diagnostic_excerpt(task_error, 2000),
        "task_count": len(task_summaries),
        "tasks": task_summaries,
        "task_probe_error": task_probe_error,
        "conversation_probe_error": conversation_probe_error,
        "conversation_message_count": len(conversation_snapshot.get("messages") or []) if conversation_snapshot else 0,
        "latest_assistant_text": diagnostic_excerpt(latest_assistant_text, 2000),
        "conversation_failure": diagnostic_excerpt(_failure_raw_text(conversation_failure), 2000),
        "recovered_file_ids": file_ids,
        "recovered_sediment_ids": sediment_ids,
    })

    if file_ids or sediment_ids:
        try:
            image_urls = _resolve_image_urls_with_monitor(
                backend,
                request,
                conversation_id,
                file_ids,
                sediment_ids,
                index=index,
                total=total,
                path=recovery_path,
                poll=False,
            )
            result_output = _image_result_output_from_urls(
                backend,
                request,
                conversation_id,
                image_urls,
                index,
                total,
                path=recovery_path,
            )
            if result_output:
                logger.info({
                    "event": f"image_{recovery_reason}_recovered_result",
                    "call_id": request.call_id,
                    "conversation_id": conversation_id,
                    "file_ids": file_ids,
                    "sediment_ids": sediment_ids,
                    "url_count": len(image_urls),
                })
                return result_output
        except ImageDownloadError:
            raise
        except Exception as exc:
            followup["result_recovery_error"] = diagnostic_excerpt(repr(exc), 1000)
            raise

    if terminal_failure is not None:
        terminal_upstream_text = latest_assistant_text
        if (
            not terminal_upstream_text
            and stream_failure is not None
            and str(last.get("message_role") or "").strip().lower() == "assistant"
            and str(last.get("content_type") or "").strip().lower() in {"text", "code"}
        ):
            terminal_upstream_text = message
        terminal_exc = ImageGenerationError(
            failure_detail or raw_error,
            failure=terminal_failure.with_raw_detail(failure_detail or terminal_failure.raw_detail),
            conversation_id=conversation_id,
            raw_error=raw_error,
            upstream_error=(
                "" if terminal_failure.code == "upstream_text_reply" else failure_detail
            ),
            raw_upstream_message=terminal_upstream_text,
        )
        if is_timeout:
            setattr(terminal_exc, "stream_timeout_secs", config.image_stream_timeout_secs)
        setattr(terminal_exc, "stream_timeout_followup", followup)
        if conversation_snapshot:
            setattr(terminal_exc, "last_conversation_snapshot", conversation_snapshot)
        raise terminal_exc

    logger.warning({
        "event": f"image_{recovery_reason}_followup",
        "call_id": request.call_id,
        "conversation_id": conversation_id,
        "task_error": diagnostic_excerpt(task_error, 500),
        "task_count": len(task_summaries),
        "file_ids": file_ids,
        "sediment_ids": sediment_ids,
        "conversation_probe_error": conversation_probe_error,
        "task_probe_error": task_probe_error,
    })
    timeout_upstream_text = latest_assistant_text
    if (
        not timeout_upstream_text
        and str(last.get("message_role") or "").strip().lower() == "assistant"
        and str(last.get("content_type") or "").strip().lower() in {"text", "code"}
    ):
        timeout_upstream_text = message
    raise _image_stream_timeout_error(
        raw_error,
        conversation_id,
        followup,
        conversation_snapshot,
        upstream_error=task_error,
        raw_upstream_message=timeout_upstream_text,
        failure_code=failure_code,
    )


def _cleanup_image_conversations_after_success(backend: OpenAIBackendAPI, outputs: Iterable[ImageOutput]) -> None:
    if not config.image_remove_conversation_after_result:
        return
    conversation_ids: list[str] = []
    seen: set[str] = set()
    for output in outputs:
        conversation_id = str(getattr(output, "conversation_id", "") or "").strip()
        if output.kind != "result" or not conversation_id or conversation_id in seen:
            continue
        seen.add(conversation_id)
        conversation_ids.append(conversation_id)
    for conversation_id in conversation_ids:
        try:
            backend.delete_conversation(conversation_id)
            logger.info({"event": "image_conversation_removed", "conversation_id": conversation_id})
        except Exception as exc:
            logger.warning({
                "event": "image_conversation_remove_failed",
                "conversation_id": conversation_id,
                "error": diagnostic_excerpt(exc, 500),
            })


def _image_result_output_from_urls(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        conversation_id: str,
        image_urls: list[str],
        index: int,
        total: int,
        *,
        path: str = "",
) -> ImageOutput | None:
    if not image_urls:
        return None
    if request.progress_callback:
        request.progress_callback("receiving_image")
    downloaded_images = _download_image_bytes_with_monitor(
        backend,
        request,
        conversation_id,
        image_urls,
        index,
        total,
        path=path,
    )
    image_items = [
        {"b64_json": base64.b64encode(image_data).decode("ascii")}
        for image_data in downloaded_images
    ]
    formatted = format_image_result(
        image_items,
        request.prompt,
        request.response_format,
        request.base_url,
        int(time.time()),
        requested_size=request.size,
        deadline_monotonic=request.deadline_monotonic or None,
        output_format=request.output_format,
    )
    data = formatted["data"]
    if not data:
        return None
    return ImageOutput(
        kind="result",
        model=request.model,
        index=index,
        total=total,
        data=data,
        image_urls=list(formatted.get("_image_urls") or []),
        conversation_id=conversation_id,
    )


def stream_image_outputs(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        index: int = 1,
        total: int = 1,
) -> Iterator[ImageOutput]:
    """执行一张 ChatGPT 图片任务。

    统一原则：上游 SSE 只负责启动/生成阶段；SSE 结束后只进入一次结果解析/轮询。
    不再在文本回复、空结果、轮询超时后叠加多轮长重试，避免一个配置的 300 秒被隐式放大到十几分钟。
    """
    last: dict[str, Any] = {}
    conversation_stream_started = time.perf_counter()
    conversation_wall_started = time.time()
    try:
        for event in conversation_events(
                backend,
                prompt=request.prompt,
                model=request.model,
                images=request.images or [],
                size=request.size,
                quality=request.quality,
                output_format=request.output_format,
                frame_count=request.frame_count,
                frame_duration_ms=request.frame_duration_ms,
        ):
            last = event
            if event.get("type") == "conversation.delta":
                yield ImageOutput(
                    kind="progress",
                    model=request.model,
                    index=index,
                    total=total,
                    text=str(event.get("delta") or ""),
                    upstream_event_type="conversation.delta",
                )
                continue
            if event.get("type") == "conversation.event":
                raw = event.get("raw")
                raw_type = str(raw.get("type") or "") if isinstance(raw, dict) else ""
                yield ImageOutput(
                    kind="progress",
                    model=request.model,
                    index=index,
                    total=total,
                    upstream_event_type=raw_type,
                )
    except (TimeoutError, curl_exceptions.Timeout) as exc:
        yield _recover_after_image_stream_timeout(
            backend,
            request,
            last,
            exc,
            index,
            total,
            conversation_wall_started,
        )
        return
    except curl_exceptions.RequestException as exc:
        if not any((
            last.get("conversation_id"),
            last.get("file_ids"),
            last.get("sediment_ids"),
        )):
            raise
        yield _recover_after_image_stream_timeout(
            backend,
            request,
            last,
            exc,
            index,
            total,
            conversation_wall_started,
            failure_code="image_stream_interrupted",
        )
        return

    conversation_id = str(last.get("conversation_id") or "")
    file_ids = [str(item) for item in last.get("file_ids") or []]
    sediment_ids = [str(item) for item in last.get("sediment_ids") or []]
    message = str(last.get("text") or "").strip()
    stream_failure = last.get("_image_failure")
    stream_failure = stream_failure if isinstance(stream_failure, ImageFailure) else None
    terminal_tool_arguments = last.get("_terminal_tool_arguments")
    terminal_tool_arguments = (
        terminal_tool_arguments if isinstance(terminal_tool_arguments, dict) else None
    )
    turn_use_case = str(last.get("turn_use_case") or "").strip().lower()
    async_task_type = str(last.get("async_task_type") or "").strip().lower()
    message_type = str(last.get("message_type") or "").strip().lower()
    has_image_arguments = is_image_generation_arguments(
        message,
        role=str(last.get("message_role") or ""),
        content_type=str(last.get("content_type") or ""),
    )
    should_poll_for_image = bool(
        (
            request.images
            or turn_use_case == "image gen"
            or async_task_type == "image_gen"
            or message_type in {"image_gen", "image_generation"}
            or has_image_arguments
        )
        and stream_failure is None
    )
    is_text_reply = bool(
        stream_failure is not None
        and stream_failure.code == "upstream_text_reply"
    )
    conversation_stream_ms = int((time.perf_counter() - conversation_stream_started) * 1000)
    http_timing = _backend_http_timing_data(backend)
    _monitor_image_stage(
        request,
        "image_stream_resolve_start",
        conversation_id=conversation_id,
        conversation_stream_ms=conversation_stream_ms,
        index=index,
        total=total,
        **http_timing,
    )
    logger.info({
        "event": "image_stream_resolve_start",
        "call_id": request.call_id,
        "conversation_id": conversation_id,
        "file_ids": file_ids,
        "sediment_ids": sediment_ids,
        "tool_invoked": last.get("tool_invoked"),
        "turn_use_case": turn_use_case,
        "async_task_type": async_task_type,
        "message_type": message_type,
        "has_image_arguments": has_image_arguments,
        "has_terminal_tool_arguments": terminal_tool_arguments is not None,
        "is_text_reply": is_text_reply,
        "should_poll_for_image": should_poll_for_image,
        "conversation_stream_ms": conversation_stream_ms,
        **http_timing,
    })
    if request.progress_callback:
        request.progress_callback("image_stream_resolve_start")

    if is_text_reply:
        logger.info({
            "event": "image_stream_text_reply_detected",
            "conversation_id": conversation_id,
            "message_preview": message[:200],
        })

    if terminal_tool_arguments is not None and not file_ids and not sediment_ids and stream_failure is None:
        argument_text = str(terminal_tool_arguments.get("text") or message).strip()
        terminal_failure = image_failure(
            "image_tool_error",
            raw_detail=argument_text,
        )
        yield ImageOutput(
            kind="message",
            model=request.model,
            index=index,
            total=total,
            text=argument_text,
            conversation_id=conversation_id,
            failure=terminal_failure,
        )
        return
    if not conversation_id and not file_ids and (sediment_ids or should_poll_for_image or stream_failure is not None):
        conversation_id = _recover_image_conversation_id(
            backend,
            request,
            reason="stream_result",
            message=message,
            started_at=conversation_wall_started,
        )

    if not file_ids and not sediment_ids and last.get("blocked"):
        _task_failure, detailed_error = _get_detailed_failure_from_tasks(backend, conversation_id)
        policy_failure = image_failure(
            "content_policy_violation",
            raw_detail=detailed_error or message,
        )
        yield ImageOutput(
            kind="message",
            model=request.model,
            index=index,
            total=total,
            text=detailed_error or message or public_image_error_message(policy_failure),
            conversation_id=conversation_id,
            failure=policy_failure,
        )
        return

    if is_text_reply and conversation_id and not file_ids and not sediment_ids:
        task_failure, task_error = _get_detailed_failure_from_tasks(
            backend,
            conversation_id,
            timeout_secs=5.0,
            wait_secs=0.0,
        )
        previous_code = stream_failure.code
        merged_failure = merge_message_failure(stream_failure, task_failure)
        stream_failure = merged_failure
        if task_failure is not None and merged_failure.code != previous_code:
            stream_failure = merged_failure.with_raw_detail(
                task_error or merged_failure.raw_detail or message
            )
            message = task_error or message
            logger.info({
                "event": "image_text_reply_structured_failure",
                "conversation_id": conversation_id,
                "failure_code": stream_failure.code,
                "error": diagnostic_excerpt(message, 1000),
            })

    if stream_failure is not None and not file_ids and not sediment_ids:
        yield ImageOutput(
            kind="message",
            model=request.model,
            index=index,
            total=total,
            text=_failure_raw_text(stream_failure, message),
            conversation_id=conversation_id,
            failure=stream_failure,
        )
        return

    if message and not file_ids and not sediment_ids and not should_poll_for_image and not is_text_reply:
        yield ImageOutput(
            kind="message",
            model=request.model,
            index=index,
            total=total,
            text=message,
            conversation_id=conversation_id,
            failure=image_failure("no_image_generated", raw_detail=message),
        )
        return

    image_urls = _resolve_image_urls_with_monitor(
        backend,
        request,
        conversation_id,
        file_ids,
        sediment_ids,
        poll_timeout_secs=config.image_poll_timeout_secs,
        index=index,
        total=total,
    )
    result_output = _image_result_output_from_urls(
        backend,
        request,
        conversation_id,
        image_urls,
        index,
        total,
    )
    if result_output:
        yield result_output
        return

    if message and not should_poll_for_image:
        yield ImageOutput(
            kind="message",
            model=request.model,
            index=index,
            total=total,
            text=message,
            conversation_id=conversation_id,
            failure=stream_failure or image_failure("no_image_generated", raw_detail=message),
        )
        return

    if conversation_id:
        failure = image_failure("no_image_generated")
        yield ImageOutput(
            kind="message",
            model=request.model,
            index=index,
            total=total,
            text=public_image_error_message(failure),
            conversation_id=conversation_id,
            failure=failure,
        )
        return

    failure = image_failure("no_image_generated")
    yield ImageOutput(
        kind="message",
        model=request.model,
        index=index,
        total=total,
        text=public_image_error_message(failure),
        conversation_id=conversation_id,
        failure=failure,
    )

def _codex_response_images(value: Any) -> list[str]:
    if isinstance(value, dict):
        if value.get("type") == "image_generation_call" and isinstance(value.get("result"), str):
            result = value["result"].strip()
            if result:
                return [result.split(",", 1)[1] if result.startswith("data:image/") else result]
        images: list[str] = []
        for item in value.values():
            images.extend(_codex_response_images(item))
        return images
    if isinstance(value, list):
        images: list[str] = []
        for item in value:
            images.extend(_codex_response_images(item))
        return images
    return []


def stream_codex_image_outputs(
        backend: OpenAIBackendAPI,
        request: ConversationRequest,
        index: int = 1,
        total: int = 1,
) -> Iterator[ImageOutput]:
    codex_started = time.perf_counter()
    events = list(backend.iter_codex_image_response_events(
        prompt=request.prompt,
        images=request.images or [],
        size=request.size,
        quality=request.quality,
        output_format=request.output_format,
    ))
    images = _codex_response_images(events)
    failure: ImageFailure | None = None
    for event in events:
        candidate = classify_upstream_message(event)
        failure = merge_message_failure(
            failure,
            candidate,
        )
    if images and failure is not None and failure.capability == "auth":
        backend._schedule_auth_recovery("codex_image_response_after_success")
    if request.trace_image_perf:
        _monitor_image_stage(
            request,
            "image_codex_response_done",
            response_ms=int((time.perf_counter() - codex_started) * 1000),
            image_count=len(images),
            index=index,
            total=total,
        )
        logger.info({
            "event": "image_codex_response_done",
            "call_id": request.call_id,
            "model": request.model,
            "index": index,
            "total": total,
            "response_ms": int((time.perf_counter() - codex_started) * 1000),
            "image_count": len(images),
        })
    if not images:
        if failure is not None:
            raise ImageGenerationError(
                "",
                failure=failure,
                raw_error="",
                upstream_error=diagnostic_excerpt(
                    _failure_raw_text(failure), 4000
                ),
                raw_upstream_message=diagnostic_excerpt(failure.public_detail, 4000),
            )
        raise ImageGenerationError(
            "No image result found in response",
            failure=image_failure("no_image_generated"),
        )
    formatted = format_image_result(
        [{"b64_json": item, "revised_prompt": request.prompt} for item in images],
        request.prompt,
        request.response_format,
        request.base_url,
        int(time.time()),
        requested_size=request.size,
        deadline_monotonic=request.deadline_monotonic or None,
        output_format=request.output_format,
    )
    data = formatted["data"]
    if data:
        yield ImageOutput(
            kind="result",
            model=request.model,
            index=index,
            total=total,
            data=data,
            image_urls=list(formatted.get("_image_urls") or []),
        )
        return
    raise ImageGenerationError(
        "No image result found in response",
        failure=image_failure("no_image_generated"),
    )
