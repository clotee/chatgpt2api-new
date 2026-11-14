from __future__ import annotations

import base64
import time

from services.account_service import account_service
from services.image_failure import (
    ImageFailure,
    ImageFailureError,
    ImageGenerationError,
    classify_image_exception,
    image_failure,
)
from services.openai_backend_api import OpenAIBackendAPI
from typing import Iterator
from utils.diagnostics import diagnostic_excerpt
from utils.log import logger
from utils.svg import svg_dimensions

from ._core import (
    ConversationRequest,
    ImageOutput,
    _is_account_auth_failure,
    conversation_events,
    format_image_result,
)

def _text_account_email(access_token: str) -> str:
    account = account_service.get_account(access_token)
    if not account:
        return ""
    return str(account.get("email") or "").strip()


def _remember_text_account(backend: OpenAIBackendAPI, access_token: str) -> str:
    email = _text_account_email(access_token)
    setattr(backend, "account_email", email)
    return email


def text_backend() -> OpenAIBackendAPI:
    access_token = account_service.get_text_access_token()
    backend = OpenAIBackendAPI(access_token=access_token)
    _remember_text_account(backend, access_token)
    return backend


def stream_text_deltas(backend: OpenAIBackendAPI, request: ConversationRequest) -> Iterator[str]:
    attempted_tokens: set[str] = set()
    token = getattr(backend, "access_token", "")
    emitted = False
    auth_failures = 0
    while True:
        if token and token in attempted_tokens:
            raise RuntimeError("no available text account")
        if token:
            attempted_tokens.add(token)
        account_snapshot = account_service.get_account(token) or {}
        active_backend: OpenAIBackendAPI | None = None
        try:
            _remember_text_account(backend, token)
            active_backend = OpenAIBackendAPI(access_token=token)
            _remember_text_account(active_backend, token)
            for event in conversation_events(
                active_backend,
                messages=request.messages,
                model=request.model,
                prompt=request.prompt,
                thinking_effort=request.thinking_effort,
            ):
                structured_failure = event.get("_image_failure")
                if isinstance(structured_failure, ImageFailure):
                    raise ImageFailureError(failure=structured_failure)
                if event.get("type") != "conversation.delta":
                    continue
                delta = str(event.get("delta") or "")
                if delta:
                    emitted = True
                    yield delta
            account_service.mark_text_used(token)
            return
        except Exception as exc:
            auth_failure = bool(token and _is_account_auth_failure(exc))
            if auth_failure:
                try:
                    account_service.schedule_auth_verification(
                        token,
                        "text_stream",
                        expected_access_token=token,
                        expected_refresh_token=str(
                            account_snapshot.get("refresh_token") or ""
                        ),
                        expected_last_token_refresh_at=account_snapshot.get(
                            "last_token_refresh_at"
                        ),
                    )
                except Exception as schedule_error:
                    logger.warning({
                        "event": "auth_verification_schedule_failed",
                        "source": "text_stream",
                        "error": diagnostic_excerpt(schedule_error, 300),
                    })
                auth_failures += 1
            if auth_failure and not emitted and auth_failures < 2:
                token = account_service.get_text_access_token(attempted_tokens)
                if token:
                    continue
            if token and not getattr(exc, "account_email", ""):
                setattr(exc, "account_email", _text_account_email(token))
            raise
        finally:
            if active_backend is not None:
                active_backend.close()


def collect_text(backend: OpenAIBackendAPI, request: ConversationRequest) -> str:
    return "".join(stream_text_deltas(backend, request))


def append_svg_prompt(prompt: str) -> str:
    """Build the private text-model instruction without mutating the caller prompt."""
    return (
        f"{str(prompt or '').rstrip()}\n\n"
        "生成格式为 svg。\n"
        "请在回复正文中直接包含完整的 SVG XML（从 <svg 开始到 </svg> 结束），"
        "不要只返回 sandbox:/mnt/data 下载链接、下载按钮或文字说明；禁止脚本、外部资源和嵌入式位图，"
        "必须包含有效的 width/height 或 viewBox。"
    )


def extract_svg_document_from_text(text: str) -> bytes | None:
    """Extract and validate the first complete static SVG document in text."""
    source = str(text or "")
    lower = source.lower()
    offset = 0
    while True:
        start = lower.find("<svg", offset)
        if start < 0:
            return None
        end_marker = lower.find("</svg>", start)
        if end_marker < 0:
            return None
        candidate = source[start:end_marker + len("</svg>")].strip().encode("utf-8")
        if svg_dimensions(candidate) is not None:
            return candidate
        offset = start + len("<svg")


def _generate_single_svg(request: "ConversationRequest", index: int, total: int) -> list[ImageOutput]:
    backend: OpenAIBackendAPI | None = None
    started = time.perf_counter()
    try:
        backend = text_backend()
        response = collect_text(
            backend,
            ConversationRequest(
                model="auto",
                prompt=append_svg_prompt(request.prompt),
                deadline_monotonic=request.deadline_monotonic,
            ),
        )
        document = extract_svg_document_from_text(response)
        if document is None:
            raise ImageGenerationError(
                "upstream did not return a valid SVG document",
                failure=image_failure("upstream_error", raw_detail=response[:2000]),
            )
        formatted = format_image_result(
            [{"b64_json": base64.b64encode(document).decode("ascii")}],
            request.prompt,
            request.response_format,
            request.base_url,
            int(time.time()),
            deadline_monotonic=request.deadline_monotonic or None,
            output_format="svg",
        )
        return [ImageOutput(
            kind="result",
            model=request.model,
            index=index,
            total=total,
            data=formatted["data"],
            image_urls=list(formatted.get("_image_urls") or []),
            account_email=str(getattr(backend, "account_email", "") or ""),
        )]
    except ImageGenerationError:
        raise
    except Exception as exc:
        raise ImageGenerationError(
            str(exc) or "SVG generation failed",
            failure=classify_image_exception(exc),
        ) from exc
    finally:
        if backend is not None:
            backend.close()
        logger.debug({
            "event": "svg_generation_slot_done",
            "model": request.model,
            "index": index,
            "total": total,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        })
