import random
import time

from collections.abc import Callable
from curl_cffi import requests
from services.config import config
from services.image_failure import (
    ImageDownloadError,
    ImageFailure,
    ImageFailureError,
    ImagePollTimeoutError,
    classify_conversation_failure,
    classify_image_exception,
    classify_task_failure,
    image_failure,
    merge_message_failure,
    structured_upstream_codes,
    terminal_assistant_text,
)
from typing import (
    Any,
    Dict,
)
from urllib.parse import urlparse
from utils.diagnostics import diagnostic_excerpt
from utils.helper import (
    UpstreamHTTPError,
    ensure_ok,
)
from utils.log import logger

from ._constants import (
    FILE_SERVICE_ID_RE,
    REAL_IMAGE_FILE_ID_RE,
    SEDIMENT_ID_RE,
)

class ImageResultsMixin:
    @staticmethod
    def _add_unique(values: list[str], candidates: list[str]) -> None:
        for candidate in candidates:
            if candidate and candidate not in values:
                values.append(candidate)

    @classmethod
    def _extract_image_reference_ids(cls, payload: Any) -> tuple[list[str], list[str]]:
        file_ids: list[str] = []
        sediment_ids: list[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, str):
                # 只提取真正的图片文件 ID（file_00000000... 格式）和 file-service:// URI
                cls._add_unique(file_ids, FILE_SERVICE_ID_RE.findall(value))
                cls._add_unique(file_ids, REAL_IMAGE_FILE_ID_RE.findall(value))
                cls._add_unique(sediment_ids, SEDIMENT_ID_RE.findall(value))
                return
            if isinstance(value, dict):
                for item in value.values():
                    walk(item)
                return
            if isinstance(value, list):
                for item in value:
                    walk(item)

        walk(payload)
        return file_ids, sediment_ids

    @classmethod
    def _extract_image_asset_pointer_ids(
        cls,
        payload: Any,
    ) -> tuple[list[str], list[str]]:
        file_ids: list[str] = []
        sediment_ids: list[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                asset_pointer = value.get("asset_pointer")
                if isinstance(asset_pointer, str):
                    cls._add_unique(file_ids, FILE_SERVICE_ID_RE.findall(asset_pointer))
                    cls._add_unique(file_ids, REAL_IMAGE_FILE_ID_RE.findall(asset_pointer))
                    cls._add_unique(sediment_ids, SEDIMENT_ID_RE.findall(asset_pointer))
                for item in value.values():
                    if isinstance(item, (dict, list)):
                        walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(payload)
        return file_ids, sediment_ids

    @classmethod
    def _has_image_asset_pointer(cls, payload: Any) -> bool:
        if isinstance(payload, dict):
            if str(payload.get("content_type") or "") == "image_asset_pointer":
                return True
            asset_pointer = str(payload.get("asset_pointer") or "")
            if asset_pointer.startswith(("file-service://", "sediment://")):
                return True
            return any(cls._has_image_asset_pointer(item) for item in payload.values())
        if isinstance(payload, list):
            return any(cls._has_image_asset_pointer(item) for item in payload)
        return False

    def _extract_image_tool_records(self, data: Dict[str, Any]) -> list[Dict[str, Any]]:
        """从 conversation 明细里提取图片工具输出记录。"""
        mapping_value = data.get("mapping") or {}
        mapping = mapping_value if isinstance(mapping_value, dict) else {}
        records = []
        for message_id, node in mapping.items():
            message = (node or {}).get("message") or {}
            author = message.get("author") or {}
            metadata = message.get("metadata") or {}
            content = message.get("content") or {}
            role = str(author.get("role") or "").strip().lower()
            if role not in {"tool", "assistant"}:
                continue
            is_image_gen = metadata.get("async_task_type") == "image_gen"
            has_asset_pointer = self._has_image_asset_pointer(content) or self._has_image_asset_pointer(metadata)
            if role == "assistant":
                if not has_asset_pointer:
                    continue
                file_ids, sediment_ids = self._extract_image_asset_pointer_ids(
                    {"content": content, "metadata": metadata}
                )
            else:
                file_ids, sediment_ids = self._extract_image_reference_ids(
                    {"content": content, "metadata": metadata}
                )
            if not is_image_gen and not has_asset_pointer and not file_ids and not sediment_ids:
                continue
            records.append(
                {"message_id": message_id, "create_time": message.get("create_time") or 0, "file_ids": file_ids,
                 "sediment_ids": sediment_ids})
        return sorted(records, key=lambda item: item["create_time"])

    def _conversation_poll_snapshot(self, data: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
        """Small last-state snapshot for poll timeout diagnostics."""
        mapping_value = data.get("mapping") or {}
        mapping = mapping_value if isinstance(mapping_value, dict) else {}
        messages: list[Dict[str, Any]] = []
        for message_id, node in mapping.items():
            message = (node or {}).get("message") or {}
            if not isinstance(message, dict):
                continue
            author = message.get("author") or {}
            metadata = message.get("metadata") or {}
            content = message.get("content") or {}
            role = str(author.get("role") or "").strip().lower()
            if role not in {"user", "assistant", "tool", "system"}:
                continue
            create_time = float(message.get("create_time") or 0.0)
            text = self._editable_message_text(message)
            file_ids, sediment_ids = self._extract_image_reference_ids({"content": content, "metadata": metadata})
            item: Dict[str, Any] = {
                "message_id": str(message.get("id") or message_id or "")[:80],
                "role": role,
                "create_time": create_time,
                "content_type": str(content.get("content_type") or "") if isinstance(content, dict) else type(content).__name__,
            }
            for key in ("async_task_type", "status", "message_type", "model_slug"):
                value = message.get(key) if key == "status" else None
                if value in (None, "") and isinstance(metadata, dict):
                    value = metadata.get(key)
                if value not in (None, ""):
                    item[key] = value
            if file_ids:
                item["file_ids"] = file_ids[:5]
            if sediment_ids:
                item["sediment_ids"] = sediment_ids[:5]
            if text:
                item["text_preview"] = diagnostic_excerpt(text, 500)
            messages.append(item)
        messages.sort(key=lambda item: float(item.get("create_time") or 0.0))
        snapshot = {
            "mapping_count": len(mapping) if isinstance(mapping, dict) else 0,
            "messages": messages[-8:],
        }
        return snapshot, diagnostic_excerpt(terminal_assistant_text(data), 2000)

    def _poll_image_results(
            self,
            conversation_id: str,
            timeout_secs: float = 120.0,
            initial_file_ids: list[str] | None = None,
            initial_sediment_ids: list[str] | None = None,
    ) -> tuple[list[str], list[str]]:
        """Poll the conversation document until image file ids appear or budget runs out.

        - Sleeps image_poll_initial_wait_secs first (default 5s, +jitter). ChatGPT
          image generation takes ~30s; polling immediately wastes requests and trips
          a transient 429 the upstream returns within ~200ms of the SSE stream
          closing (the conversation document is not yet committed).
        - Subsequent polls are image_poll_interval_secs apart (default 5s).
        - Failures marked retryable by the canonical image-failure policy back
          off exponentially (capped at 16s, +jitter), honoring Retry-After.
        - All sleeps stay within timeout_secs; on exhaustion raises ImagePollTimeoutError.
        """
        self._reset_image_result_timing()
        started_at = time.monotonic()
        attempt = 0
        interval = float(config.image_poll_interval_secs)
        initial_wait = float(config.image_poll_initial_wait_secs)
        file_ids: list[str] = []
        sediment_ids: list[str] = []
        self._add_unique(file_ids, initial_file_ids or [])
        self._add_unique(sediment_ids, initial_sediment_ids or [])
        has_initial_ids = bool(file_ids or sediment_ids)
        last_hit_key: tuple[tuple[str, ...], tuple[str, ...]] | None = (
            (tuple(file_ids), tuple(sediment_ids)) if has_initial_ids else None
        )
        logger.info({
            "event": "image_poll_start",
            "conversation_id": conversation_id,
            "timeout_secs": timeout_secs,
            "initial_wait_secs": initial_wait,
            "interval_secs": interval,
            "initial_file_ids": file_ids,
            "initial_sediment_ids": sediment_ids,
        })

        def _remaining() -> float:
            return timeout_secs - (time.monotonic() - started_at)

        if has_initial_ids and config.image_settle_enabled:
            settle_for = min(config.image_settle_secs, max(0.0, _remaining()))
            if settle_for > 0:
                self._sleep_for_image_poll(settle_for)
        elif initial_wait > 0:
            jitter = random.uniform(0, min(2.0, initial_wait * 0.2))
            sleep_for = min(initial_wait + jitter, max(0.0, _remaining()))
            if sleep_for > 0:
                self._sleep_for_image_poll(sleep_for)

        def _retry_sleep(reason: str, status_code: int | None, error: str | None, retry_after: int | None) -> bool:
            # retry_after=0 means "retry immediately" — must not be coerced via falsy check.
            base = retry_after if retry_after is not None else min(2 ** min(attempt, 4), 16)
            backoff = base + random.uniform(0, 0.5)
            remaining = _remaining()
            if remaining <= 0:
                return False
            sleep_for = min(backoff, remaining)
            log_payload: Dict[str, Any] = {
                "event": "image_poll_retry",
                "conversation_id": conversation_id,
                "attempt": attempt,
                "reason": reason,
                "sleep_secs": round(sleep_for, 2),
            }
            if status_code is not None:
                log_payload["status_code"] = status_code
            if error is not None:
                log_payload["error"] = error
            logger.warning(log_payload)
            self._sleep_for_image_poll(sleep_for)
            return True

        last_task_error = ""
        pending_task_failure: ImageFailure | None = None
        task_probe_failure: ImageFailure | None = None
        task_probe_error: Exception | None = None
        last_conversation_snapshot: Dict[str, Any] = {}
        last_assistant_text = ""
        conversation_transport_failure: ImageFailure | None = None
        conversation_transport_error: Exception | None = None

        def _raise_final_failure(
            failure: ImageFailure,
            *,
            raw_detail: str = "",
            source_error: Exception | None = None,
            raw_error: str = "",
            upstream_error: str = "",
            raw_upstream_message: str = "",
        ) -> None:
            resolved_detail: Any = raw_detail or failure.raw_detail
            resolved = failure.with_raw_detail(resolved_detail)
            if source_error is not None:
                exc = source_error
                setattr(exc, "failure", resolved)
            else:
                error_class = (
                    ImagePollTimeoutError
                    if resolved.code == "image_poll_timeout"
                    else ImageFailureError
                )
                exc = error_class(raw_detail, failure=resolved)
            setattr(exc, "conversation_id", conversation_id or "")
            setattr(exc, "poll_attempts", attempt)
            setattr(exc, "poll_timeout_secs", timeout_secs)
            setattr(exc, "last_assistant_text", last_assistant_text)
            setattr(exc, "last_conversation_snapshot", last_conversation_snapshot or {})
            setattr(exc, "raw_error", diagnostic_excerpt(raw_error, 4000))
            setattr(exc, "upstream_error", diagnostic_excerpt(upstream_error, 4000))
            setattr(exc, "raw_upstream_message", diagnostic_excerpt(raw_upstream_message, 4000))
            if last_task_error:
                setattr(exc, "task_error", last_task_error)
                setattr(exc, "last_task_error", last_task_error)
            raise exc

        while _remaining() > 0:
            attempt += 1
            task_count = 0
            task_check_ok = False
            task_query_started = time.perf_counter()
            try:
                tasks = self._query_backend_tasks(conversation_id=conversation_id, timeout_secs=5.0)
                task_count = len(tasks)
                task_check_ok = True
                task_probe_failure = None
                task_probe_error = None
                for task in tasks:
                    candidate_failure = classify_task_failure(task)
                    if candidate_failure is None:
                        continue
                    error_msg, metadata = self.image_task_diagnostics(task)
                    candidate_error = error_msg or (
                        candidate_failure.raw_detail
                        if isinstance(candidate_failure.raw_detail, str)
                        else ""
                    )
                    if candidate_error:
                        candidate_failure = candidate_failure.with_raw_detail(candidate_error)
                    pending_task_failure = merge_message_failure(
                        pending_task_failure,
                        candidate_failure,
                    )
                    if pending_task_failure is not None:
                        detail = pending_task_failure.raw_detail
                        last_task_error = detail if isinstance(detail, str) else last_task_error
                    logger.info({
                        "event": "image_poll_task_failure",
                        "conversation_id": conversation_id,
                        "attempt": attempt,
                        "failure_code": candidate_failure.code,
                        "error_msg": diagnostic_excerpt(candidate_error, 1000),
                        "metadata": metadata,
                    })
            except (UpstreamHTTPError, requests.exceptions.RequestException) as exc:
                task_probe_failure = classify_image_exception(exc)
                task_probe_error = exc
                logger.warning({
                    "event": "image_poll_task_check_failed",
                    "conversation_id": conversation_id,
                    "attempt": attempt,
                    "failure_code": task_probe_failure.code,
                    "status_code": task_probe_failure.status_code,
                    "error": diagnostic_excerpt(exc, 300),
                })
                if task_probe_failure.capability == "auth":
                    setattr(exc, "failure", task_probe_failure)
                    if file_ids or sediment_ids:
                        try:
                            self._schedule_auth_recovery("image_task_probe_after_success")
                        except Exception as recovery_exc:
                            logger.warning({
                                "event": "image_auth_recovery_schedule_failed",
                                "source": "image_task_probe_after_success",
                                "conversation_id": conversation_id,
                                "error": diagnostic_excerpt(recovery_exc, 300),
                            })
                        return file_ids, sediment_ids
                    _raise_final_failure(
                        task_probe_failure,
                        raw_detail=str(exc),
                        source_error=exc,
                        upstream_error=str(exc),
                    )
            except Exception as exc:
                # tasks 查询失败不影响正常轮询流程
                logger.debug({
                    "event": "image_poll_task_check_failed",
                    "conversation_id": conversation_id,
                    "attempt": attempt,
                    "error": diagnostic_excerpt(exc, 300),
                })
            finally:
                self._add_image_result_timing(
                    "poll_request_ms",
                    (time.perf_counter() - task_query_started) * 1000,
                )

            conversation_query_started = time.perf_counter()
            try:
                try:
                    conversation = self._get_conversation(conversation_id)
                finally:
                    self._add_image_result_timing(
                        "poll_request_ms",
                        (time.perf_counter() - conversation_query_started) * 1000,
                    )
            except UpstreamHTTPError as exc:
                failure = classify_image_exception(exc)
                setattr(exc, "failure", failure)
                if failure.retryable:
                    conversation_transport_failure = failure
                    conversation_transport_error = exc
                    if _retry_sleep("upstream_status", exc.status_code, None, exc.retry_after):
                        continue
                    break
                final_failure = merge_message_failure(pending_task_failure, task_probe_failure)
                final_failure = merge_message_failure(final_failure, failure) or failure
                _raise_final_failure(
                    final_failure,
                    raw_detail=str(exc),
                    source_error=exc,
                    upstream_error=str(exc),
                )
            except requests.exceptions.RequestException as exc:
                failure = classify_image_exception(exc)
                setattr(exc, "failure", failure)
                if failure.retryable:
                    conversation_transport_failure = failure
                    conversation_transport_error = exc
                    if _retry_sleep("network", None, str(exc), None):
                        continue
                    break
                final_failure = merge_message_failure(pending_task_failure, task_probe_failure)
                final_failure = merge_message_failure(final_failure, failure) or failure
                _raise_final_failure(
                    final_failure,
                    raw_detail=str(exc),
                    source_error=exc,
                    raw_error=str(exc),
                )
            conversation_transport_failure = None
            conversation_transport_error = None
            last_conversation_snapshot, last_assistant_text = self._conversation_poll_snapshot(conversation)

            for record in self._extract_image_tool_records(conversation):
                for file_id in record["file_ids"]:
                    if file_id not in file_ids:
                        file_ids.append(file_id)
                for sediment_id in record["sediment_ids"]:
                    if sediment_id not in sediment_ids:
                        sediment_ids.append(sediment_id)

            if not file_ids and not sediment_ids:
                conversation_failure = classify_conversation_failure(conversation)
                failure = merge_message_failure(pending_task_failure, conversation_failure)
                if conversation_failure is not None or (
                    pending_task_failure is not None
                    and pending_task_failure.code != "upstream_error"
                ):
                    failure = failure or conversation_failure or pending_task_failure
                    assert failure is not None
                    raw_detail = (
                        failure.raw_detail
                        if isinstance(failure.raw_detail, str)
                        else last_assistant_text or last_task_error
                    )
                    if conversation_failure is not None:
                        logger.info({
                            "event": "image_poll_conversation_failure",
                            "conversation_id": conversation_id,
                            "attempt": attempt,
                            "failure_code": failure.code,
                            "task_count": task_count if task_check_ok else None,
                            "message_preview": diagnostic_excerpt(raw_detail, 1000),
                        })
                    _raise_final_failure(
                        failure,
                        raw_detail=raw_detail,
                        upstream_error=(
                            "" if failure.code == "upstream_text_reply" else raw_detail
                        ),
                        raw_upstream_message=(
                            last_assistant_text if conversation_failure is not None else ""
                        ),
                    )

            logger.debug({"event": "image_poll_check", "conversation_id": conversation_id, "attempt": attempt,
                          "file_ids": file_ids, "sediment_ids": sediment_ids})
            if file_ids or sediment_ids:
                if not config.image_check_before_hit_enabled:
                    # 先check再hit 机制关闭：直接返回首次发现的 file_ids
                    logger.info({"event": "image_poll_hit_no_settle", "conversation_id": conversation_id,
                                 "file_ids": file_ids, "sediment_ids": sediment_ids})
                    return file_ids, sediment_ids
                hit_key = (tuple(file_ids), tuple(sediment_ids))
                if last_hit_key == hit_key:
                    logger.info({"event": "image_poll_hit", "conversation_id": conversation_id, "file_ids": file_ids,
                                 "sediment_ids": sediment_ids})
                    return file_ids, sediment_ids
                last_hit_key = hit_key
                if not config.image_settle_enabled:
                    # 二次确认机制关闭：直接返回首次发现的 file_ids
                    logger.info({"event": "image_poll_hit_settle_disabled", "conversation_id": conversation_id,
                                 "file_ids": file_ids, "sediment_ids": sediment_ids})
                    return file_ids, sediment_ids
                logger.info({"event": "image_poll_hit_pending_settle", "conversation_id": conversation_id,
                             "file_ids": file_ids, "sediment_ids": sediment_ids,
                             "settle_secs": config.image_settle_secs})
                wait = min(config.image_settle_secs, max(0.0, _remaining()))
                if wait > 0:
                    self._sleep_for_image_poll(wait)
                    continue
                return file_ids, sediment_ids
            logger.debug({"event": "image_poll_wait", "conversation_id": conversation_id,
                          "elapsed_secs": round(time.monotonic() - started_at, 1)})
            wait = min(interval, max(0.0, _remaining()))
            if wait > 0:
                self._sleep_for_image_poll(wait)
        timeout_failure = image_failure("image_poll_timeout")
        final_failure = merge_message_failure(pending_task_failure, task_probe_failure)
        final_failure = merge_message_failure(final_failure, conversation_transport_failure)
        final_failure = merge_message_failure(final_failure, timeout_failure)
        assert final_failure is not None
        logger.info({
            "event": "image_poll_terminal_failure",
            "conversation_id": conversation_id,
            "timeout_secs": timeout_secs,
            "attempts_made": attempt,
            "failure_code": final_failure.code,
            # attempts_made == 0 means the initial_wait consumed the entire budget — no HTTP attempted.
            "initial_wait_exhausted_budget": attempt == 0,
            "last_task_error": last_task_error if last_task_error else None,
            "last_assistant_text": last_assistant_text or None,
            "last_conversation_snapshot": last_conversation_snapshot or None,
        })
        raw_detail = "" if final_failure.code == "image_poll_timeout" else (
            final_failure.raw_detail
            if isinstance(final_failure.raw_detail, str)
            else last_assistant_text or last_task_error
        )
        source_error: Exception | None = None
        if (
            conversation_transport_failure is not None
            and final_failure.code == conversation_transport_failure.code
        ):
            source_error = conversation_transport_error
        elif task_probe_failure is not None and final_failure.code == task_probe_failure.code:
            source_error = task_probe_error
        if source_error is not None:
            source_raw_error = "" if isinstance(source_error, UpstreamHTTPError) else raw_detail
            source_upstream_error = (
                str(source_error)
                if isinstance(source_error, UpstreamHTTPError)
                else last_task_error
            )
        else:
            source_raw_error = ""
            source_upstream_error = last_task_error or (
                "" if final_failure.code == "image_poll_timeout" else raw_detail
            )
        _raise_final_failure(
            final_failure,
            raw_detail=raw_detail,
            source_error=source_error,
            raw_error=source_raw_error,
            upstream_error=source_upstream_error,
            raw_upstream_message=last_assistant_text,
        )

    def _get_file_download_url(self, file_id: str) -> str:
        """获取文件下载地址。"""
        path = f"/backend-api/files/{file_id}/download"
        response = self.session.get(self.base_url + path, headers=self._headers(path, {"Accept": "application/json"}),
                                    timeout=60)
        ensure_ok(response, path)
        data = response.json()
        return data.get("download_url") or data.get("url") or ""

    def _get_attachment_download_url(self, conversation_id: str, attachment_id: str) -> str:
        """通过 conversation 附件接口获取下载地址。"""
        path = f"/backend-api/conversation/{conversation_id}/attachment/{attachment_id}/download"
        response = self.session.get(self.base_url + path, headers=self._headers(path, {"Accept": "application/json"}),
                                    timeout=60)
        ensure_ok(response, path)
        data = response.json()
        return data.get("download_url") or data.get("url") or ""

    def _query_backend_tasks(
        self,
        conversation_id: str = "",
        task_id: str = "",
        timeout_secs: float = 30.0,
    ) -> list[Dict[str, Any]]:
        """查询 /backend-api/tasks/ 接口获取异步任务状态和错误信息。

        参数：
        - `conversation_id`：可选。按 conversation_id 过滤任务。
        - `task_id`：可选。按 task_id 过滤任务。
        - `timeout_secs`：请求超时秒数。

        返回：
        - 任务列表，每个任务包含 image_gen_message 等字段。
        """
        path = "/backend-api/tasks"
        response = self.session.get(
            self.base_url + path,
            headers=self._headers(path, {"Accept": "application/json"}),
            timeout=timeout_secs,
        )
        ensure_ok(response, path)
        data = response.json()
        tasks = data.get("tasks", [])
        if not isinstance(tasks, list):
            return []

        # 按 conversation_id 或 task_id 过滤
        if conversation_id:
            tasks = [
                t for t in tasks
                if isinstance(t, dict) and (
                    t.get("conversation_id") == conversation_id
                    or t.get("original_conversation_id") == conversation_id
                )
            ]
        if task_id:
            tasks = [t for t in tasks if isinstance(t, dict) and t.get("task_id") == task_id]
        return tasks

    def image_task_diagnostics(self, task: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        """提取任务原始诊断信息，不在这里重复判断失败类型。"""
        img_msg = task.get("image_gen_message") or {}
        if not isinstance(img_msg, dict):
            return "", {}

        metadata = img_msg.get("metadata") or {}
        content = img_msg.get("content") or {}
        metadata = metadata if isinstance(metadata, dict) else {}
        content = content if isinstance(content, dict) else {}
        parts = content.get("parts", [])
        if isinstance(parts, str):
            detail = parts
        elif isinstance(parts, list):
            detail = "".join(part for part in parts if isinstance(part, str))
        else:
            detail = ""
        return detail, metadata

    def _resolve_image_urls(self, conversation_id: str, file_ids: list[str], sediment_ids: list[str]) -> list[str]:
        """把图片结果 id 解析成可下载 URL。"""
        urls: list[str] = []
        resolution_errors: list[tuple[ImageFailure, Exception]] = []
        skip_patterns = {"file_upload"}

        def resolve_candidate(
                source: str,
                asset_id: str,
                resolver: Callable[[], str],
        ) -> str:
            for attempt in range(2):
                try:
                    url = str(resolver() or "").strip()
                except Exception as exc:
                    failure = classify_image_exception(exc)
                    missing_candidate = (
                        isinstance(exc, UpstreamHTTPError)
                        and exc.status_code == 404
                        and "file_not_ready" in structured_upstream_codes(exc.body)
                    )
                    logger.warning({
                        "event": (
                            "image_download_url_missing"
                            if missing_candidate
                            else "image_download_url_retry"
                            if attempt == 0 and failure.capability != "auth"
                            else "image_download_url_failed"
                        ),
                        "source": source,
                        "conversation_id": conversation_id,
                        "id": asset_id,
                        "attempt": attempt + 1,
                        "failure_code": failure.code,
                        "error": diagnostic_excerpt(repr(exc), 300),
                    })
                    if missing_candidate:
                        if attempt == 0:
                            continue
                        delivery_error = ImageDownloadError(
                            "image download URL resolution failed: "
                            f"{diagnostic_excerpt(exc, 500)}"
                        )
                        resolution_errors.append(
                            (delivery_error.failure, delivery_error)
                        )
                        return ""
                    if attempt == 0 and failure.capability != "auth":
                        continue
                    resolution_errors.append((failure, exc))
                    return ""
                if url:
                    return url
                logger.warning({
                    "event": (
                        "image_download_url_retry"
                        if attempt == 0
                        else "image_download_url_failed"
                    ),
                    "source": source,
                    "conversation_id": conversation_id,
                    "id": asset_id,
                    "attempt": attempt + 1,
                    "failure_code": "empty_download_url",
                })
                if attempt == 0:
                    continue
                delivery_error = ImageDownloadError(
                    f"empty download URL for {source} result {asset_id}"
                )
                resolution_errors.append((delivery_error.failure, delivery_error))
                return ""
            return ""

        for file_id in file_ids:
            if file_id in skip_patterns:
                logger.debug({
                    "event": "image_file_id_skipped",
                    "source": "file",
                    "conversation_id": conversation_id,
                    "id": file_id,
                })
                continue
            url = resolve_candidate(
                "file",
                file_id,
                lambda file_id=file_id: self._get_file_download_url(file_id),
            )
            if url:
                if url not in urls:
                    urls.append(url)
        if conversation_id:
            for sediment_id in sediment_ids:
                url = resolve_candidate(
                    "sediment",
                    sediment_id,
                    lambda sediment_id=sediment_id: self._get_attachment_download_url(
                        conversation_id,
                        sediment_id,
                    ),
                )
                if url and url not in urls:
                    urls.append(url)
        logger.debug({
            "event": "image_urls_resolved",
            "conversation_id": conversation_id,
            "file_ids": file_ids,
            "sediment_ids": sediment_ids,
            "url_count": len(urls),
            "url_hosts": sorted({urlparse(url).netloc for url in urls if urlparse(url).netloc}),
        })
        auth_failed = any(failure.capability == "auth" for failure, _ in resolution_errors)
        if urls:
            if auth_failed:
                self._schedule_auth_recovery("image_url_resolution_after_success")
            return urls
        if resolution_errors:
            _, error = max(
                resolution_errors,
                key=lambda item: (
                    item[0].capability == "auth",
                    item[0].switch_account,
                    not item[0].retryable,
                ),
            )
            setattr(error, "conversation_id", conversation_id or "")
            raise error
        return urls

    def _resolve_image_urls_with_timing(
            self,
            conversation_id: str,
            file_ids: list[str],
            sediment_ids: list[str],
    ) -> list[str]:
        started = time.perf_counter()
        try:
            return self._resolve_image_urls(conversation_id, file_ids, sediment_ids)
        finally:
            self._add_image_result_timing(
                "resolve_ms",
                (time.perf_counter() - started) * 1000,
            )

    def resolve_conversation_image_urls(
            self,
            conversation_id: str,
            file_ids: list[str],
            sediment_ids: list[str],
            poll: bool = True,
            poll_timeout_secs: float | None = None,
    ) -> list[str]:
        self._reset_image_result_timing()
        file_ids = [item for item in file_ids if item != "file_upload"]
        sediment_ids = list(sediment_ids)
        timeout = poll_timeout_secs if poll_timeout_secs is not None else config.image_poll_timeout_secs
        # 当 check-before-hit 和 settle 均已关闭，且 SSE 已给出 file_ids 时，
        # 跳过轮询直接解析 URL，省去 initial_wait + 轮询耗时。
        if poll and conversation_id and (file_ids or sediment_ids):
            if not config.image_check_before_hit_enabled and not config.image_settle_enabled:
                logger.info({
                    "event": "image_resolve_skip_poll_direct_resolve",
                    "conversation_id": conversation_id,
                    "file_ids": file_ids,
                    "sediment_ids": sediment_ids,
                })
                return self._resolve_image_urls_with_timing(conversation_id, file_ids, sediment_ids)
        if poll and conversation_id:
            logger.info({
                "event": "image_resolve_poll_needed",
                "conversation_id": conversation_id,
                "initial_file_ids": file_ids,
                "initial_sediment_ids": sediment_ids,
                "poll_timeout_secs": timeout,
            })
            try:
                polled_file_ids, polled_sediment_ids = self._poll_image_results(
                    conversation_id,
                    timeout,
                    file_ids,
                    sediment_ids,
                )
            except ImagePollTimeoutError:
                if not file_ids and not sediment_ids:
                    raise
                logger.warning({
                    "event": "image_resolve_poll_partial_timeout",
                    "conversation_id": conversation_id,
                    "file_ids": file_ids,
                    "sediment_ids": sediment_ids,
                })
            except Exception as exc:
                if not file_ids and not sediment_ids:
                    raise
                failure = classify_image_exception(exc)
                if failure.capability == "auth":
                    self._schedule_auth_recovery("image_result_probe_after_success")
                logger.warning({
                    "event": "image_resolve_poll_partial_error",
                    "conversation_id": conversation_id,
                    "file_ids": file_ids,
                    "sediment_ids": sediment_ids,
                    "failure_code": failure.code,
                    "error": diagnostic_excerpt(repr(exc), 300),
                })
            else:
                file_ids.extend(item for item in polled_file_ids if item and item not in file_ids)
                sediment_ids.extend(item for item in polled_sediment_ids if item and item not in sediment_ids)
        return self._resolve_image_urls_with_timing(conversation_id, file_ids, sediment_ids)

    def download_image_bytes(self, urls: list[str]) -> list[bytes]:
        images: list[bytes] = []
        for url in urls:
            parsed_url = urlparse(url)
            same_origin = (
                not parsed_url.netloc
                or parsed_url.netloc.lower() == urlparse(self.base_url).netloc.lower()
            )
            download_headers = (
                self._headers(parsed_url.path or "/")
                if same_origin
                else self._signed_asset_headers()
            )
            for attempt in range(2):
                try:
                    response = self.session.get(
                        url,
                        headers=download_headers,
                        timeout=120,
                    )
                    ensure_ok(
                        response,
                        "image_download",
                        credential_scope="account" if same_origin else "signed_asset",
                    )
                    content = bytes(response.content or b"")
                    if not content:
                        if attempt == 0:
                            logger.warning({
                                "event": "image_download_retry",
                                "reason": "empty_response",
                                "url_host": urlparse(url).netloc,
                            })
                            continue
                        raise ImageDownloadError("image download returned an empty response")
                    if content not in images:
                        images.append(content)
                    break
                except ImageDownloadError:
                    raise
                except Exception as exc:
                    failure = classify_image_exception(exc)
                    if attempt == 0:
                        logger.warning({
                            "event": "image_download_retry",
                            "reason": failure.code,
                            "url_host": urlparse(url).netloc,
                            "error": diagnostic_excerpt(repr(exc), 300),
                        })
                        continue
                    raise ImageDownloadError(
                        f"image download failed: {diagnostic_excerpt(exc, 500)}",
                        failure=failure,
                    ) from exc
        return images
