from __future__ import annotations

import time

from concurrent.futures import (
    TimeoutError as FuturesTimeoutError,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from dataclasses import dataclass, field, replace
from services.account_service import (
    ImageAccountSelectionError,
    account_service,
)
from services.config import config
from services.image_failure import (
    ImageFailure,
    ImageGenerationError,
    classify_image_exception,
    image_failure,
    image_failure_priority,
    public_image_error_message,
)
from services.openai_backend_api import OpenAIBackendAPI
from services.proxy_service import (
    ImageEgressDeadlineError,
    proxy_settings,
)
from typing import (
    Any,
    Callable,
    Iterator,
)
from utils.diagnostics import diagnostic_excerpt
from utils.helper import (
    IMAGE_MODELS,
    UpstreamHTTPError,
    is_codex_image_model,
    is_supported_image_model,
    split_image_model,
)
from utils.image_tokens import image_output_metadata
from utils.log import logger

from ._core import (
    ConversationRequest,
    ImageOutput,
    normalize_image_output_format,
    validate_image_output_request,
)
from ._image_stream import (
    _cleanup_image_conversations_after_success,
    stream_codex_image_outputs,
    stream_image_outputs,
)
from ._monitor import (
    _backend_egress_data,
    _backend_http_timing_data,
    _capture_failed_image_attempts,
    _image_progress_callback_with_monitor,
    _monitor_image_stage,
)
from ._text import _generate_single_svg


@dataclass
class ImageSlotAttempt:
    """单个重试迭代的图片槽位尝试上下文。

    等价于原 ``finalize_image_slot`` 闭包在每个 ``while`` 迭代内重新
    定义并捕获的当轮局部变量；每轮迭代开始时重建为新实例。
    """

    token: str = ""
    access_token: str = ""
    refresh_token: str = ""
    last_token_refresh_at: Any = None
    started: float = 0.0
    stream_started: float = 0.0
    conversation_id: str = ""
    emitted_for_token: bool = False
    returned_message: bool = False
    returned_result: bool = False
    slot_finalized: bool = False
    backend: OpenAIBackendAPI | None = None
    egress_acquired: bool = False


@dataclass
class SingleImageRunner:
    """``_generate_single_image`` 的显式状态对象。

    ``request``/``index``/``total`` 为入参；其余字段对应原函数体内
    跨 ``while`` 迭代的持久可变状态；当轮迭代状态存放在 ``attempt``
    （``ImageSlotAttempt``）中，每轮迭代重建。
    """

    request: ConversationRequest
    index: int
    total: int
    account_email: str = ""
    retry_token: str = ""
    attempted_tokens: set[str] = field(default_factory=set)
    fallback_retry_pending: bool = False
    fallback_retry_used: bool = False
    fallback_from_egress: dict[str, Any] = field(default_factory=dict)
    image_attempts: list[dict[str, Any]] = field(default_factory=list)
    retry_error: ImageGenerationError | None = None
    pending_switch_attempt_index: int | None = None
    account_attempt_started: float = 0.0
    max_account_attempts: int = field(
        default_factory=lambda: (
            config.image_max_account_attempts
            if config.image_account_retry_enabled
            else 1
        )
    )
    single_started: float = field(default_factory=time.perf_counter)
    attempt: ImageSlotAttempt = field(default_factory=ImageSlotAttempt)

    def run(self) -> list[ImageOutput]:
        """执行单张图片生成（含重试/切号状态机），返回结果列表。"""
        while True:
            self.request.monitor_attempt = len(self.image_attempts) + 1
            account_wait_started = time.perf_counter()
            self.attempt = ImageSlotAttempt()
            try:
                self._acquire_token(account_wait_started)
            except ImageAccountSelectionError as exc:
                _monitor_image_stage(
                    self.request,
                    "image_local_rejected",
                    local_reason="account_pool",
                    status="failed",
                    index=self.index,
                    total=self.total,
                )
                if self.retry_error is not None:
                    raise self._attach_attempts(self.retry_error) from exc
                raise ImageGenerationError(
                    str(exc) or "image generation failed",
                    failure=image_failure(exc.code, raw_detail=str(exc)),
                    account_email=self.account_email,
                    image_attempts=self.image_attempts,
                ) from exc
            except RuntimeError as exc:
                _monitor_image_stage(
                    self.request,
                    "image_local_rejected",
                    local_reason="account_pool",
                    status="failed",
                    index=self.index,
                    total=self.total,
                )
                if self.retry_error is not None:
                    raise self._attach_attempts(self.retry_error) from exc
                raise ImageGenerationError(
                    str(exc) or "image generation failed",
                    failure=image_failure("no_available_account"),
                    account_email=self.account_email,
                    image_attempts=self.image_attempts,
                ) from exc

            # 每轮迭代重置尝试上下文（等价原每轮重建的 finalize_image_slot 捕获集）。
            self.attempt.emitted_for_token = False
            self.attempt.returned_message = False
            self.attempt.returned_result = False
            self.attempt.slot_finalized = False
            self.attempt.started = self.account_attempt_started or account_wait_started
            self.attempt.conversation_id = ""
            self.attempt.access_token = self.attempt.token
            self.attempt.refresh_token = ""
            self.attempt.last_token_refresh_at = None

            self._resolve_account(account_wait_started)
            self.attempt.backend = None
            self.attempt.egress_acquired = False
            try:
                stream_fn = self._open_backend_and_egress()
                outputs = self._consume_stream(stream_fn)
                if self.attempt.returned_message:
                    message_output = outputs[-1] if outputs else None
                    message_failure = (
                        message_output.failure
                        if message_output is not None and message_output.failure is not None
                        else image_failure(
                            "no_image_generated",
                            raw_detail=message_output.text if message_output is not None else "",
                        )
                    )
                    message_error = ImageGenerationError(
                        message_output.text if message_output is not None else "image generation failed",
                        failure=message_failure,
                        account_email=self.account_email,
                        conversation_id=self.attempt.conversation_id,
                        raw_error="",
                        raw_upstream_message=message_output.text if message_output is not None else "",
                        image_attempts=self.image_attempts,
                    )
                    self._finalize_image_slot(False, failure=message_failure, error=message_error)
                    self._attach_attempts_to_outputs(outputs)
                    if self._retry_with_different_account(message_failure, message_error):
                        continue
                    if self.request.trace_image_perf:
                        _monitor_image_stage(
                            self.request,
                            "image_single_done",
                            total_ms=int((time.perf_counter() - self.single_started) * 1000),
                            status="message",
                            account_email=self.account_email,
                            index=self.index,
                            total=self.total,
                        )
                        logger.info({
                            "event": "image_single_done",
                            "call_id": self.request.call_id,
                            "model": self.request.model,
                            "index": self.index,
                            "total": self.total,
                            "total_ms": int((time.perf_counter() - self.single_started) * 1000),
                            "status": "message",
                            "account_email": self.account_email,
                        })
                    return outputs
                if not self.attempt.returned_result:
                    missing_result_failure = image_failure("no_image_generated")
                    if self.attempt.emitted_for_token:
                        self.attempt.conversation_id = outputs[-1].conversation_id if outputs else ""
                    raise ImageGenerationError(
                        "upstream completed without generating images",
                        failure=missing_result_failure,
                        account_email=self.account_email,
                        conversation_id=self.attempt.conversation_id,
                        image_attempts=self.image_attempts,
                    )
                _cleanup_image_conversations_after_success(self.attempt.backend, outputs)
                self._finalize_image_slot(True)
                self._attach_attempts_to_outputs(outputs)
                if self.request.trace_image_perf:
                    result_images = image_output_metadata([
                        item for output in outputs if output.kind == "result" for item in output.data
                    ])
                    _monitor_image_stage(
                        self.request,
                        "image_single_done",
                        total_ms=int((time.perf_counter() - self.single_started) * 1000),
                        status="success",
                        account_email=self.account_email,
                        index=self.index,
                        total=self.total,
                    )
                    logger.info({
                        "event": "image_single_done",
                        "call_id": self.request.call_id,
                        "model": self.request.model,
                        "index": self.index,
                        "total": self.total,
                        "total_ms": int((time.perf_counter() - self.single_started) * 1000),
                        "status": "success",
                        "account_email": self.account_email,
                        "requested_size": self.request.size or "auto",
                        "quality": self.request.quality,
                        "response_format": self.request.response_format,
                        "image_count": len(result_images),
                        "result_images": result_images,
                    })
                return outputs
            except Exception as exc:
                failure = (
                    image_failure("task_interrupted", raw_detail=str(exc))
                    if isinstance(exc, ImageEgressDeadlineError)
                    else classify_image_exception(exc)
                )
                self.attempt.conversation_id = str(
                    getattr(exc, "conversation_id", "") or self.attempt.conversation_id
                )
                stream_error_ms = (
                    int((time.perf_counter() - self.attempt.stream_started) * 1000)
                    if self.attempt.stream_started > 0
                    else 0
                )
                http_timing = _backend_http_timing_data(self.attempt.backend)
                quick_timeout_retry_ms = min(
                    30000,
                    max(5000, int(config.image_stream_timeout_secs * 1000 * 0.2)),
                )
                early_connection_failure = (
                    not self.attempt.emitted_for_token
                    and (stream_error_ms == 0 or stream_error_ms <= quick_timeout_retry_ms)
                    and failure.code in {
                        "upstream_connection_failed",
                        "upstream_connection_timeout",
                        "upstream_unavailable",
                    }
                )
                fallback_reference = proxy_settings.get_fallback_proxy_reference()
                if early_connection_failure and fallback_reference and not self.fallback_retry_used:
                    self.fallback_retry_used = True
                    self.fallback_retry_pending = True
                    self.retry_token = self.attempt.token
                    self.fallback_from_egress = (
                        _backend_egress_data(self.attempt.backend)
                        if self.attempt.backend is not None
                        else {}
                    )
                    logger.warning({
                        "event": "image_stream_fallback_retry",
                        "request_token": self.attempt.token,
                        "account_email": self.account_email,
                        "index": self.index,
                        "fallback_proxy_configured": True,
                        "fallback_from_egress_key": self.fallback_from_egress.get("egress_key", ""),
                        "fallback_from_egress_label": self.fallback_from_egress.get("egress_label", ""),
                        "stream_error_ms": stream_error_ms,
                        "error": str(exc)[:200],
                    })
                    if self.request.trace_image_perf:
                        _monitor_image_stage(
                            self.request,
                            "image_egress_fallback_retry",
                            account_email=self.account_email,
                            index=self.index,
                            total=self.total,
                            status="retrying",
                            fallback_from_egress_key=self.fallback_from_egress.get("egress_key", ""),
                            fallback_from_egress_label=self.fallback_from_egress.get("egress_label", ""),
                        )
                    continue
                image_error = self._normalize_image_error(exc, failure)
                logger.warning({
                    "event": "image_attempt_failed",
                    "request_token": self.attempt.token,
                    "account_email": self.account_email,
                    "error": diagnostic_excerpt(image_error.raw_error or str(image_error), 1000),
                    "public_error": diagnostic_excerpt(image_error.public_error, 1000),
                    "failure_code": failure.code,
                    "status_code": failure.status_code,
                    "account_failure": failure.verify_account,
                    "stream_error_ms": stream_error_ms,
                    "index": self.index,
                    "total": self.total,
                    **http_timing,
                })
                self._finalize_image_slot(
                    False,
                    failure=failure,
                    error=image_error,
                    quota_consumed=(failure.code == "image_download_failed"),
                )
                self._attach_attempts(image_error)
                if self._retry_with_different_account(failure, image_error):
                    continue
                if image_error is exc:
                    raise
                raise image_error from exc
            finally:
                if self.attempt.egress_acquired and self.attempt.backend is not None:
                    proxy_settings.release_image_egress(self.attempt.backend.proxy_profile)
                if self.attempt.backend is not None:
                    self.attempt.backend.close()

    def _acquire_token(self, account_wait_started: float) -> None:
        """阶段 A：取号——``retry_token`` 复用或向账号池申请新 token。"""
        if self.retry_token:
            self.attempt.token = self.retry_token
            self.retry_token = ""
        else:
            if self.request.progress_callback:
                self.request.progress_callback("getting_account")
            _monitor_image_stage(
                self.request,
                "image_getting_account",
                max_account_attempts=self.max_account_attempts,
                index=self.index,
                total=self.total,
            )
            plan_type, _ = split_image_model(self.request.model)
            codex_model = is_codex_image_model(self.request.model)
            self.attempt.token = account_service.get_available_access_token(
                plan_type=plan_type,
                source_type="codex" if codex_model else None,
                plan_types=("plus", "team", "pro") if codex_model and not plan_type else None,
                excluded_tokens=self.attempted_tokens,
                deadline_monotonic=self.request.deadline_monotonic or None,
            )
            self.attempted_tokens.add(self.attempt.token)
            self.account_attempt_started = account_wait_started
            self.fallback_retry_pending = False
            self.fallback_retry_used = False
            self.fallback_from_egress = {}

    def _resolve_account(self, account_wait_started: float) -> None:
        """阶段 C：账号解析、``pending_switch_attempt_index`` 回写与查找监控。"""
        account_wait_ms = int((time.perf_counter() - account_wait_started) * 1000)
        account = account_service.get_account(self.attempt.token) or {}
        self.attempt.refresh_token = str(account.get("refresh_token") or "").strip()
        self.attempt.last_token_refresh_at = account.get("last_token_refresh_at")
        self.account_email = str(account.get("email") or "").strip()
        if self.pending_switch_attempt_index is not None:
            previous_attempt = self.image_attempts[self.pending_switch_attempt_index]
            previous_attempt["switched_account"] = True
            logger.warning({
                "event": "image_cross_account_retry",
                "call_id": self.request.call_id,
                "failure_code": previous_attempt.get("failure_code", ""),
                "account_email": previous_attempt.get("account_email", ""),
                "next_account_email": self.account_email,
                "attempted_account_count": len(self.image_attempts) + 1,
                "max_account_attempts": self.max_account_attempts,
                "index": self.index,
            })
            if self.request.trace_image_perf:
                _monitor_image_stage(
                    self.request,
                    "image_cross_account_retry",
                    status="retrying",
                    failure_code=previous_attempt.get("failure_code", ""),
                    account_email=self.account_email,
                    previous_account_email=previous_attempt.get("account_email", ""),
                    account_switch_count=len(self.image_attempts),
                    max_account_attempts=self.max_account_attempts,
                    index=self.index,
                    total=self.total,
                )
            self.pending_switch_attempt_index = None
        self.retry_error = None
        _monitor_image_stage(
            self.request,
            "image_account_lookup",
            account_wait_ms=account_wait_ms,
            account_email=self.account_email,
            account_found=bool(account),
            max_account_attempts=self.max_account_attempts,
            index=self.index,
            total=self.total,
        )
        if account_wait_ms >= 5000:
            logger.warning({
                "event": "image_account_wait_slow",
                "call_id": self.request.call_id,
                "account_wait_ms": account_wait_ms,
                "account_email": self.account_email,
                "index": self.index,
            })
        logger.debug({
            "event": "image_account_lookup",
            "call_id": self.request.call_id,
            "token_prefix": self.attempt.token[:12] + "..." if len(self.attempt.token) > 12 else self.attempt.token,
            "account_email": self.account_email,
            "account_found": bool(account),
            "account_wait_ms": account_wait_ms,
            "index": self.index,
        })

    def _open_backend_and_egress(self) -> Callable[..., Iterator[ImageOutput]]:
        """阶段 D 前半：fallback profile 判定、backend 构造与 egress 获取，返回 stream_fn。"""
        egress_started = time.perf_counter()
        fallback_profile = None
        using_fallback_profile = self.fallback_retry_pending
        self.fallback_retry_pending = False
        if using_fallback_profile:
            fallback_profile = proxy_settings.get_fallback_profile(
                upstream=True,
                reserve_image_egress=True,
                deadline_monotonic=self.request.deadline_monotonic or None,
            )
            if fallback_profile is None:
                raise ImageGenerationError(
                    "fallback proxy is not configured",
                    failure=image_failure("upstream_connection_failed"),
                    account_email=self.account_email,
                )
        self.attempt.backend = OpenAIBackendAPI(
            access_token=self.attempt.token,
            proxy_profile=fallback_profile,
            reserve_image_egress=fallback_profile is None,
            deadline_monotonic=self.request.deadline_monotonic or None,
        )
        if self.request.trace_image_perf:
            egress_data = _backend_egress_data(self.attempt.backend)
            if using_fallback_profile:
                egress_data.update({
                    "fallback_retry": True,
                    "fallback_from_egress_key": self.fallback_from_egress.get("egress_key", ""),
                    "fallback_from_egress_label": self.fallback_from_egress.get("egress_label", ""),
                })
            _monitor_image_stage(
                self.request,
                "image_egress_waiting",
                account_email=self.account_email,
                index=self.index,
                total=self.total,
                **egress_data,
            )
        self.attempt.egress_acquired = bool(
            getattr(self.attempt.backend.proxy_profile, "image_egress_reserved", False)
        )
        egress_acquire_ms = proxy_settings.acquire_image_egress(
            self.attempt.backend.proxy_profile,
            deadline_monotonic=self.request.deadline_monotonic or None,
        )
        self.attempt.egress_acquired = (
            int(getattr(self.attempt.backend.proxy_profile, "image_concurrency_limit", 0) or 0) > 0
        )
        egress_wait_ms = int((time.perf_counter() - egress_started) * 1000)
        if self.request.trace_image_perf:
            egress_data = _backend_egress_data(self.attempt.backend)
            if using_fallback_profile:
                egress_data.update({
                    "fallback_retry": True,
                    "fallback_from_egress_key": self.fallback_from_egress.get("egress_key", ""),
                    "fallback_from_egress_label": self.fallback_from_egress.get("egress_label", ""),
                })
            _monitor_image_stage(
                self.request,
                "image_egress_ready",
                egress_wait_ms=egress_wait_ms,
                egress_acquire_ms=egress_acquire_ms,
                account_email=self.account_email,
                index=self.index,
                total=self.total,
                **egress_data,
            )
            logger.debug({
                "event": "image_egress_ready",
                "call_id": self.request.call_id,
                "model": self.request.model,
                "index": self.index,
                "total": self.total,
                "account_email": self.account_email,
                "egress_wait_ms": egress_wait_ms,
                "egress_acquire_ms": egress_acquire_ms,
                **egress_data,
            })
        if self.request.progress_callback or self.request.trace_image_perf:
            self.attempt.backend.progress_callback = _image_progress_callback_with_monitor(
                self.request,
                self.index,
                self.total,
                lambda: self.account_email,
            )
        stream_fn = stream_codex_image_outputs if is_codex_image_model(self.request.model) else stream_image_outputs
        return stream_fn

    def _consume_stream(
        self,
        stream_fn: Callable[..., Iterator[ImageOutput]],
    ) -> list[ImageOutput]:
        """阶段 E：消费流式输出，回填账号/会话信息并收集 ``outputs``。"""
        outputs: list[ImageOutput] = []
        self.attempt.stream_started = time.perf_counter()
        for output in stream_fn(self.attempt.backend, self.request, self.index, self.total):
            if self.account_email and not output.account_email:
                output.account_email = self.account_email
            if output.conversation_id:
                self.attempt.conversation_id = output.conversation_id
            if output.kind == "message" and self.request.message_as_error:
                failure = output.failure or image_failure(
                    "no_image_generated",
                    raw_detail=output.text,
                )
                raise ImageGenerationError(
                    output.text,
                    failure=failure,
                    account_email=self.account_email,
                    conversation_id=output.conversation_id,
                    raw_error="",
                    raw_upstream_message=output.text or "",
                )
            self.attempt.emitted_for_token = True
            self.attempt.returned_message = output.kind == "message"
            self.attempt.returned_result = self.attempt.returned_result or output.kind == "result"
            outputs.append(output)
        stream_ms = int((time.perf_counter() - self.attempt.stream_started) * 1000)
        if self.request.trace_image_perf:
            _monitor_image_stage(
                self.request,
                "image_single_stream_done",
                stream_ms=stream_ms,
                returned_message=self.attempt.returned_message,
                returned_result=self.attempt.returned_result,
                account_email=self.account_email,
                index=self.index,
                total=self.total,
            )
            logger.info({
                "event": "image_single_stream_done",
                "call_id": self.request.call_id,
                "model": self.request.model,
                "index": self.index,
                "total": self.total,
                "stream_ms": stream_ms,
                "returned_message": self.attempt.returned_message,
                "returned_result": self.attempt.returned_result,
                "account_email": self.account_email,
            })
        return outputs

    def _normalize_image_error(
        self,
        exc: Exception,
        failure: ImageFailure,
    ) -> ImageGenerationError:
        """阶段 G：把统一 except 捕获的异常归一化为 ``ImageGenerationError``。"""
        if isinstance(exc, ImageGenerationError):
            image_error = exc
            if self.account_email and not image_error.account_email:
                image_error.account_email = self.account_email
        else:
            upstream_http_error = isinstance(exc, UpstreamHTTPError)
            explicit_raw_error = getattr(exc, "raw_error", None)
            raw_error = (
                ""
                if upstream_http_error
                else str(explicit_raw_error or "").strip()
                if explicit_raw_error is not None
                else (
                    ""
                    if failure.code == "image_poll_timeout"
                    else str(exc).strip() or "image generation failed"
                )
            )
            upstream_error = str(
                getattr(exc, "upstream_error", "")
                or getattr(exc, "last_task_error", "")
                or (str(exc) if upstream_http_error else "")
            )
            raw_upstream_message = str(
                getattr(exc, "raw_upstream_message", "")
                or ""
            )
            image_error = ImageGenerationError(
                raw_error,
                failure=failure,
                account_email=self.account_email,
                conversation_id=self.attempt.conversation_id,
                raw_error=raw_error,
                upstream_error=upstream_error,
                raw_upstream_message=raw_upstream_message,
                image_attempts=self.image_attempts,
            )
            for attr in (
                "conversation_id",
                "poll_attempts",
                "poll_timeout_secs",
                "last_task_error",
                "last_conversation_snapshot",
                "last_assistant_text",
                "stream_timeout_secs",
                "stream_timeout_followup",
            ):
                if hasattr(exc, attr):
                    setattr(image_error, attr, getattr(exc, attr))
        return image_error

    def _finalize_image_slot(
        self,
        success: bool,
        *,
        failure: ImageFailure | None = None,
        error: ImageGenerationError | None = None,
        quota_consumed: bool | None = None,
    ) -> None:
        """阶段 B/F/G 的 finalize 等价物：结算图片槽位并记录当轮 attempt。"""
        if self.attempt.slot_finalized:
            return
        self.attempt.slot_finalized = True
        try:
            if failure is not None and failure.code == "task_interrupted":
                account_service.release_image_slot(self.attempt.token)
            else:
                account_service.mark_image_result(
                    self.attempt.token,
                    success,
                    failure=failure,
                    quota_consumed=quota_consumed,
                    capabilities={"auth", "image_generation"}
                    | ({"file_upload"} if self.request.images else set()),
                    expected_access_token=self.attempt.access_token,
                    expected_refresh_token=self.attempt.refresh_token,
                    expected_last_token_refresh_at=self.attempt.last_token_refresh_at,
                )
        except Exception as exc:
            logger.warning({
                "event": "image_account_result_update_failed",
                "account_email": self.account_email,
                "success": success,
                "failure_code": failure.code if failure is not None else "",
                "error": diagnostic_excerpt(exc, 500),
            })
            try:
                account_service.release_image_slot(self.attempt.token)
            except Exception as release_exc:
                logger.warning({
                    "event": "image_account_slot_release_failed",
                    "account_email": self.account_email,
                    "error": diagnostic_excerpt(release_exc, 500),
                })
        attempt: dict[str, Any] = {
            "slot": self.index,
            "attempt": len(self.image_attempts) + 1,
            "account_email": self.account_email,
            "status": (
                "success"
                if success
                else "text_review"
                if failure is not None and failure.outcome == "text"
                else "failed"
            ),
            "duration_ms": max(0, int((time.perf_counter() - self.attempt.started) * 1000)),
        }
        if self.attempt.conversation_id:
            attempt["conversation_id"] = self.attempt.conversation_id
        if failure is not None:
            raw_error = str(getattr(error, "raw_error", "") or "").strip()
            if failure.code == "image_poll_timeout":
                raw_error = ""
            upstream_error = str(
                getattr(error, "upstream_error", "")
                or getattr(error, "last_task_error", "")
                or ""
            ).strip()
            raw_upstream_message = str(
                getattr(error, "raw_upstream_message", "")
                or getattr(error, "upstream_message_preview", "")
                or getattr(error, "last_assistant_text", "")
                or ""
            ).strip()
            public_error = (
                error.public_error
                if error is not None
                else public_image_error_message(failure)
            )
            failure_fields = {
                **failure.diagnostic_fields(),
                "public_error": public_error,
                "account_failure": failure.verify_account,
                "switched_account": False,
            }
            if raw_error:
                failure_fields["raw_error"] = raw_error
            if upstream_error:
                failure_fields["upstream_error"] = upstream_error
            if raw_upstream_message:
                failure_fields["raw_upstream_message"] = raw_upstream_message
            attempt.update(failure_fields)
        self.image_attempts.append(attempt)
        if failure is not None and self.request.trace_image_perf:
            _monitor_image_stage(
                self.request,
                "image_attempt_failed",
                status="text_review" if failure.outcome == "text" else "failed",
                **failure.diagnostic_fields(),
                public_error=attempt.get("public_error", ""),
                raw_error=attempt.get("raw_error", ""),
                account_failure=failure.verify_account,
                account_email=self.account_email,
                conversation_id=self.attempt.conversation_id,
                stream_error_ms=(
                    int((time.perf_counter() - self.attempt.stream_started) * 1000)
                    if self.attempt.stream_started > 0 else 0
                ),
                index=self.index,
                total=self.total,
            )

    def _attach_attempts(self, value: Any) -> Any:
        """把 ``image_attempts`` 快照拷贝赋给 ``value.image_attempts`` 并返回 ``value``。"""
        value.image_attempts = [dict(item) for item in self.image_attempts]
        return value

    def _attach_attempts_to_outputs(self, outputs: list[ImageOutput]) -> None:
        """把同一份 ``image_attempts`` 快照赋给每个 output（共享同一 list 对象）。"""
        attempts = [dict(item) for item in self.image_attempts]
        for output in outputs:
            output.image_attempts = attempts

    def _retry_with_different_account(
        self,
        failure: ImageFailure,
        error: ImageGenerationError | None = None,
    ) -> bool:
        """判定是否换账号重试；是则写入 ``retry_token``/``retry_error`` 等状态。"""
        if (
            failure.code == "task_interrupted"
            or (
                self.request.deadline_monotonic > 0
                and time.monotonic() >= self.request.deadline_monotonic
            )
            or not failure.switch_account
            or len(self.image_attempts) >= self.max_account_attempts
        ):
            return False
        self.retry_token = ""
        self.fallback_retry_pending = False
        self.retry_error = self._attach_attempts(error or ImageGenerationError(
            public_image_error_message(failure),
            failure=failure,
            account_email=self.account_email,
            raw_error="",
        ))
        self.pending_switch_attempt_index = len(self.image_attempts) - 1
        return True


def _generate_single_image(
        request: ConversationRequest,
        index: int,
        total: int,
) -> list[ImageOutput]:
    """为单张图片执行生成逻辑（含重试），返回结果列表。

    该函数在独立线程中运行，每个线程使用不同的账号，
    实现并行生图，避免串行超时阻塞。
    """
    return SingleImageRunner(request, index, total).run()


def _select_image_pool_error(errors: dict[int, Exception]) -> Exception | None:
    """Select one deterministic failure and retain every slot's attempts."""
    if not errors:
        return None

    def score(item: tuple[int, Exception]) -> tuple[int, int]:
        slot, exc = item
        failure = classify_image_exception(exc)
        return (
            image_failure_priority(failure),
            -slot,
        )

    _, selected = max(errors.items(), key=score)
    selected_failure = classify_image_exception(selected)
    setattr(selected, "failure", selected_failure)

    attempts: list[dict[str, Any]] = []
    for slot, exc in sorted(errors.items()):
        raw_attempts = getattr(exc, "image_attempts", None)
        if not isinstance(raw_attempts, (list, tuple)):
            continue
        for attempt_number, raw_attempt in enumerate(raw_attempts, start=1):
            if not isinstance(raw_attempt, dict):
                continue
            attempt = dict(raw_attempt)
            attempt.setdefault("slot", slot)
            attempt.setdefault("attempt", attempt_number)
            if attempt not in attempts:
                attempts.append(attempt)

    attempts.sort(
        key=lambda item: (
            int(item.get("slot") or 0),
            int(item.get("attempt") or 0),
        )
    )
    if attempts:
        setattr(selected, "image_attempts", attempts)
    return selected


def stream_image_outputs_with_pool(request: ConversationRequest) -> Iterator[ImageOutput]:
    """并行生成多张图片，每张图片使用独立线程和账号，互不阻塞。"""
    normalized_output_format = normalize_image_output_format(request.output_format)
    validate_image_output_request(
        request.model,
        normalized_output_format,
        has_references=bool(request.images),
    )
    if not is_supported_image_model(request.model):
        _monitor_image_stage(
            request,
            "image_local_rejected",
            local_reason="unsupported_model",
            status="failed",
        )
        raise ImageGenerationError(
            "unsupported image model,supported models: " + ", ".join(sorted(IMAGE_MODELS)),
            failure=image_failure("unsupported_model"),
        )

    if request.deadline_monotonic <= 0:
        request.deadline_monotonic = (
            time.monotonic() + max(0.001, float(config.image_request_timeout_secs))
        )

    if normalized_output_format == "svg":
        errors: dict[int, Exception] = {}
        succeeded = False
        for index in range(1, request.n + 1):
            try:
                outputs = _generate_single_svg(replace(request), index, request.n)
            except Exception as exc:
                errors[index] = exc
                logger.warning({
                    "event": "svg_generation_slot_error",
                    "index": index,
                    "error": diagnostic_excerpt(exc, 300),
                })
                continue
            if any(output.kind == "result" and output.data for output in outputs):
                succeeded = True
            for output in outputs:
                yield output
        if succeeded:
            return
        if selected_error := _select_image_pool_error(errors):
            raise selected_error
        raise ImageGenerationError(
            "no text account could generate SVG output",
            failure=image_failure("no_available_account"),
        )

    if request.n <= 1:
        # 单张图片，直接执行（无需线程池开销）
        outputs = _generate_single_image(replace(request), 1, 1)
        if not outputs:
            raise ImageGenerationError(
                "image generation completed without output",
                failure=image_failure("no_image_generated"),
            )
        for output in outputs:
            yield output
        return

    # 多张图片：根据配置选择并行或串行执行
    if not config.image_parallel_generation:
        logger.info({
            "event": "image_serial_generation_start",
            "n": request.n,
            "model": request.model,
        })
        successful_slots: set[int] = set()
        errors: dict[int, Exception] = {}
        for index in range(1, request.n + 1):
            try:
                outputs = _generate_single_image(replace(request), index, request.n)
            except Exception as exc:
                _capture_failed_image_attempts(request, exc)
                errors[index] = exc
                logger.warning({
                    "event": "image_serial_generation_error",
                    "index": index,
                    "error": str(exc)[:300],
                })
                if time.monotonic() >= request.deadline_monotonic:
                    break
                continue
            if any(output.kind == "result" and output.data for output in outputs):
                successful_slots.add(index)
            for output in outputs:
                yield output
        if successful_slots:
            for index, exc in errors.items():
                logger.warning({
                    "event": "image_serial_partial_failure",
                    "failed_index": index,
                    "error": str(exc)[:200],
                })
            return
        if selected_error := _select_image_pool_error(errors):
            raise selected_error
        raise ImageGenerationError(
            "no account in the pool could generate images",
            failure=image_failure("no_available_account"),
        )

    logger.info({
        "event": "image_parallel_generation_start",
        "n": request.n,
        "model": request.model,
    })
    # 每张图片一个线程，同时启动
    futures = {}
    errors: dict[int, Exception] = {}
    successful_slots: set[int] = set()
    processed_futures = set()
    executor = ThreadPoolExecutor(max_workers=request.n)

    def consume_future(future: Any) -> list[ImageOutput]:
        index = futures[future]
        processed_futures.add(future)
        try:
            outputs = future.result()
        except Exception as exc:
            _capture_failed_image_attempts(request, exc)
            errors[index] = exc
            error_text = str(exc)
            logger.warning({
                "event": "image_parallel_generation_error",
                "index": index,
                "error": error_text[:300],
            })
            if not successful_slots:
                logger.warning({
                    "event": "image_parallel_failure_before_success",
                    "failed_index": index,
                    "error": error_text[:200],
                })
            return []
        if any(output.kind == "result" and output.data for output in outputs):
            successful_slots.add(index)
        return outputs

    try:
        for index in range(1, request.n + 1):
            future = executor.submit(_generate_single_image, replace(request), index, request.n)
            futures[future] = index

        # yield 结果：按完成顺序立即输出，不再等所有图片都结束后才返回成功结果。
        try:
            remaining = max(0.0, request.deadline_monotonic - time.monotonic())
            for future in as_completed(futures, timeout=remaining):
                for output in consume_future(future):
                    yield output
        except FuturesTimeoutError:
            pending = [future for future in futures if future not in processed_futures]
            done, _ = wait(pending, timeout=0.05)
            for future in done:
                for output in consume_future(future):
                    yield output
            for future in pending:
                if future in processed_futures:
                    continue
                index = futures[future]
                future.cancel()
                errors[index] = ImageGenerationError(
                    f"image slot {index} exceeded the request deadline",
                    failure=image_failure("task_interrupted"),
                )
    finally:
        for future in futures:
            if not future.done():
                future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)

    # 如果有失败但也有成功，记录警告
    if successful_slots:
        for index in range(1, request.n + 1):
            if index in errors:
                logger.warning({
                    "event": "image_parallel_partial_failure",
                    "failed_index": index,
                    "error": str(errors[index])[:200],
                })
        return

    if selected_error := _select_image_pool_error(errors):
        raise selected_error
    raise ImageGenerationError(
        "no account in the pool could generate images",
        failure=image_failure("no_available_account"),
    )
