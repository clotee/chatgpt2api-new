from __future__ import annotations

import hashlib
import time

from services.openai_backend_api import OpenAIBackendAPI
from services.realtime_monitor_service import realtime_monitor_service
from typing import (
    Any,
    Callable,
)
from utils.diagnostics import diagnostic_excerpt
from utils.log import logger

from ._core import ConversationRequest

def _monitor_image_stage(request: "ConversationRequest", event: str, **data: Any) -> None:
    if request.trace_image_perf and request.call_id:
        if request.monitor_attempt > 0 and "attempt" not in data:
            data["attempt"] = request.monitor_attempt
        realtime_monitor_service.stage(request.call_id, event, model=request.model, **data)


def _capture_failed_image_attempts(request: "ConversationRequest", exc: Exception) -> None:
    if not request.trace_image_perf or not request.call_id:
        return
    realtime_monitor_service.capture_image_attempts(
        request.call_id,
        getattr(exc, "image_attempts", None),
    )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _proxy_hash(proxy_url: object) -> str:
    value = str(proxy_url or "").strip()
    if not value:
        return "direct"
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]


def _backend_egress_data(backend: OpenAIBackendAPI) -> dict[str, Any]:
    profile = getattr(backend, "proxy_profile", None)
    proxy_url = getattr(profile, "proxy_url", "") if profile else ""
    return {
        "proxy_source": str(getattr(profile, "proxy_source", "") or "direct"),
        "proxy_hash": _proxy_hash(proxy_url),
        "egress_key": str(getattr(profile, "egress_key", "") or "direct"),
        "egress_label": str(getattr(profile, "egress_label", "") or ""),
        "proxy_group_id": str(getattr(profile, "proxy_group_id", "") or ""),
        "proxy_node_id": str(getattr(profile, "proxy_node_id", "") or ""),
        "proxy_node_name": str(getattr(profile, "proxy_node_name", "") or ""),
        "image_egress_limit": int(getattr(profile, "image_concurrency_limit", 0) or 0),
        "has_proxy": bool(proxy_url),
        "egress_mode": str(getattr(profile, "egress_mode", "") or "direct"),
    }


def _backend_http_timing_data(backend: OpenAIBackendAPI | None, name: str = "image_generation_stream") -> dict[str, Any]:
    if backend is None or not hasattr(backend, "pop_http_timing"):
        return {}
    try:
        return backend.pop_http_timing(name)
    except Exception:
        return {}


def _backend_image_result_timing_data(
        backend: OpenAIBackendAPI | None,
        fallback_resolve_ms: int,
) -> dict[str, int]:
    if backend is not None and hasattr(backend, "pop_image_result_timing"):
        try:
            timing = backend.pop_image_result_timing()
        except Exception:
            timing = {}
        if isinstance(timing, dict):
            normalized = {
                str(key): max(0, int(value or 0))
                for key, value in timing.items()
                if str(key).endswith("_ms") and int(value or 0) > 0
            }
            if normalized:
                return normalized
    return {"resolve_ms": max(0, int(fallback_resolve_ms))}


_IMAGE_PROGRESS_STAGE_EVENTS = {
    "uploading": "image_uploading",
    "bootstrapping": "image_bootstrapping",
    "getting_token": "image_getting_token",
    "preparing_conversation": "image_preparing_conversation",
    "starting_generation": "image_starting_generation",
    "generating": "image_generating",
}

_IMAGE_PROGRESS_DURATION_KEYS = {
    "uploading": "upload_ms",
    "bootstrapping": "bootstrap_ms",
    "getting_token": "requirements_ms",
    "preparing_conversation": "prepare_conversation_ms",
    "starting_generation": "generation_start_ms",
}


def _image_progress_callback_with_monitor(
        request: "ConversationRequest",
        index: int,
        total: int,
        account_email_getter: Callable[[], str],
) -> Callable[[str], None]:
    original_callback = request.progress_callback
    last_step = ""
    last_step_started = time.perf_counter()

    def report(step: str) -> None:
        nonlocal last_step, last_step_started
        now = time.perf_counter()
        step_name = str(step or "").strip()
        data: dict[str, Any] = {
            "index": index,
            "total": total,
        }
        account_email = account_email_getter()
        if account_email:
            data["account_email"] = account_email
        duration_key = _IMAGE_PROGRESS_DURATION_KEYS.get(last_step)
        if duration_key:
            data[duration_key] = int((now - last_step_started) * 1000)
        stage_event = _IMAGE_PROGRESS_STAGE_EVENTS.get(step_name)
        if stage_event:
            _monitor_image_stage(request, stage_event, **data)
        last_step = step_name
        last_step_started = now
        if original_callback:
            original_callback(step_name)

    return report


def _resolve_image_urls_with_monitor(
        backend: OpenAIBackendAPI,
        request: "ConversationRequest",
        conversation_id: str,
        file_ids: list[str],
        sediment_ids: list[str],
        index: int,
        total: int,
        path: str = "",
        **kwargs: Any,
) -> list[str]:
    resolve_started = time.perf_counter()
    try:
        image_urls = backend.resolve_conversation_image_urls(
            conversation_id,
            file_ids,
            sediment_ids,
            **kwargs,
        )
    except Exception as exc:
        if request.trace_image_perf:
            result_timing = _backend_image_result_timing_data(
                backend,
                _elapsed_ms(resolve_started),
            )
            _monitor_image_stage(
                request,
                "image_resolve_failed",
                conversation_id=conversation_id,
                index=index,
                total=total,
                status="failed",
                upstream_error=diagnostic_excerpt(repr(exc), 1000),
                **result_timing,
            )
            log_payload: dict[str, Any] = {
                "event": "image_resolve_failed",
                "call_id": request.call_id,
                "conversation_id": conversation_id,
                "error": repr(exc)[:300],
                **result_timing,
            }
            if path:
                log_payload["path"] = path
            logger.warning(log_payload)
        raise
    if request.trace_image_perf:
        result_timing = _backend_image_result_timing_data(
            backend,
            _elapsed_ms(resolve_started),
        )
        _monitor_image_stage(
            request,
            "image_resolve_done",
            conversation_id=conversation_id,
            url_count=len(image_urls),
            index=index,
            total=total,
            **result_timing,
        )
        log_payload = {
            "event": "image_resolve_done",
            "call_id": request.call_id,
            "conversation_id": conversation_id,
            "url_count": len(image_urls),
            **result_timing,
        }
        if path:
            log_payload["path"] = path
        logger.info(log_payload)
    return image_urls


def _download_image_bytes_with_monitor(
        backend: OpenAIBackendAPI,
        request: "ConversationRequest",
        conversation_id: str,
        image_urls: list[str],
        index: int,
        total: int,
        path: str = "",
) -> list[bytes]:
    download_started = time.perf_counter()
    try:
        downloaded_images = backend.download_image_bytes(image_urls)
    except Exception as exc:
        if not getattr(exc, "conversation_id", ""):
            setattr(exc, "conversation_id", conversation_id)
        if request.trace_image_perf:
            download_ms = _elapsed_ms(download_started)
            _monitor_image_stage(
                request,
                "image_download_failed",
                conversation_id=conversation_id,
                download_ms=download_ms,
                url_count=len(image_urls),
                index=index,
                total=total,
                status="failed",
                upstream_error=diagnostic_excerpt(repr(exc), 1000),
            )
            log_payload: dict[str, Any] = {
                "event": "image_download_failed",
                "call_id": request.call_id,
                "conversation_id": conversation_id,
                "download_ms": download_ms,
                "url_count": len(image_urls),
                "error": repr(exc)[:300],
            }
            if path:
                log_payload["path"] = path
            logger.warning(log_payload)
        raise
    if request.trace_image_perf:
        download_ms = _elapsed_ms(download_started)
        download_bytes = sum(len(image) for image in downloaded_images)
        download_kbps = int((download_bytes / 1024) / (download_ms / 1000)) if download_ms > 0 else 0
        _monitor_image_stage(
            request,
            "image_download_done",
            conversation_id=conversation_id,
            download_ms=download_ms,
            download_bytes=download_bytes,
            download_kbps=download_kbps,
            url_count=len(image_urls),
            image_count=len(downloaded_images),
            index=index,
            total=total,
        )
        log_payload = {
            "event": "image_download_done",
            "call_id": request.call_id,
            "conversation_id": conversation_id,
            "download_ms": download_ms,
            "download_bytes": download_bytes,
            "download_kbps": download_kbps,
            "url_count": len(image_urls),
            "image_count": len(downloaded_images),
        }
        if path:
            log_payload["path"] = path
        logger.info(log_payload)
    return downloaded_images
