import json
import threading
import time
import urllib.error
import urllib.request

from services.account_service import account_service
from services.config import config
from typing import (
    Any,
    Dict,
    Iterator,
)
from utils.helper import UpstreamHTTPError
from utils.log import logger

from ._constants import (
    CODEX_RESPONSES_INSTRUCTIONS,
    CODEX_RESPONSES_MODEL,
)

class CodexMixin:
    def _codex_responses_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def _ensure_codex_source_account(self) -> None:
        account = account_service.get_account(self.access_token)
        source_type = str((account or {}).get("source_type") or "web").strip().lower()
        if source_type != "codex":
            raise RuntimeError("codex responses endpoint requires a codex source account")

    @staticmethod
    def _codex_image_input(prompt: str, images: list[str]) -> list[Dict[str, Any]]:
        content: list[Dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        for image in images:
            payload = image if image.startswith("data:image/") else f"data:image/png;base64,{image}"
            content.append({"type": "input_image", "image_url": payload})
        return [{"role": "user", "content": content}]

    @staticmethod
    def _codex_body_preview(body: Any, limit: int = 4000) -> str:
        if isinstance(body, (dict, list)):
            try:
                text = json.dumps(body, ensure_ascii=False)
            except Exception:
                text = repr(body)
        else:
            text = str(body or "")
        return text if len(text) <= limit else text[:limit] + "...[truncated]"

    @classmethod
    def _codex_event_image_result_lengths(cls, value: Any) -> list[int]:
        if isinstance(value, dict):
            lengths: list[int] = []
            if value.get("type") == "image_generation_call" and isinstance(value.get("result"), str):
                lengths.append(len(value["result"]))
            for item in value.values():
                lengths.extend(cls._codex_event_image_result_lengths(item))
            return lengths
        if isinstance(value, list):
            lengths: list[int] = []
            for item in value:
                lengths.extend(cls._codex_event_image_result_lengths(item))
            return lengths
        return []

    @classmethod
    def _codex_event_summary(cls, event: Dict[str, Any]) -> Dict[str, Any]:
        summary: Dict[str, Any] = {
            "type": str(event.get("type") or ""),
            "keys": list(event.keys())[:30],
        }
        for key in ("id", "status", "sequence_number", "response_id", "item_id", "output_index", "content_index"):
            value = event.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                summary[key] = value
        for key in ("response", "item", "output"):
            value = event.get(key)
            if isinstance(value, dict):
                summary[f"{key}_type"] = value.get("type")
                summary[f"{key}_status"] = value.get("status")
                summary[f"{key}_keys"] = list(value.keys())[:30]
            elif isinstance(value, list):
                summary[f"{key}_len"] = len(value)
                summary[f"{key}_types"] = [
                    item.get("type") for item in value[:10] if isinstance(item, dict)
                ]
        error = event.get("error")
        if isinstance(error, dict):
            summary["error"] = {
                key: error.get(key)
                for key in ("type", "code", "message")
                if error.get(key) is not None
            }
        delta = event.get("delta")
        if isinstance(delta, str):
            summary["delta_len"] = len(delta)
            summary["delta_preview"] = delta[:200]
        result_lengths = cls._codex_event_image_result_lengths(event)
        if result_lengths:
            summary["image_result_lengths"] = result_lengths[:10]
        return summary

    def _log_codex_response_failure(
            self,
            path: str,
            status_code: int,
            headers: Any,
            payload: Dict[str, Any],
            body: Any,
    ) -> None:
        request_headers = self._codex_responses_headers()
        safe_request_headers = {
            key: value for key, value in request_headers.items() if key.lower() != "authorization"
        }
        response_headers = dict(headers.items()) if hasattr(headers, "items") else dict(headers or {})
        tool = ((payload.get("tools") or [{}])[0]) if isinstance(payload.get("tools"), list) else {}
        logger.warning({
            "event": "codex_responses_http_error",
            "path": path,
            "status_code": status_code,
            "request": {
                "model": payload.get("model"),
                "tool_model": tool.get("model"),
                "tool_action": tool.get("action"),
                "size": tool.get("size"),
                "quality": tool.get("quality"),
                "image_input_count": max(len((payload.get("input") or [{}])[0].get("content") or []) - 1, 0),
                "prompt_preview": self._codex_body_preview(
                    (((payload.get("input") or [{}])[0].get("content") or [{}])[0].get("text") or ""),
                    500,
                ),
                "headers": safe_request_headers,
            },
            "response": {
                "headers": response_headers,
                "body_preview": self._codex_body_preview(body),
            },
        })

    @staticmethod
    def _stream_timeout_message(timeout_secs: float) -> str:
        return f"SSE stream exceeded {timeout_secs:.0f}s" if timeout_secs >= 1 else f"SSE stream exceeded {timeout_secs:.3f}s"

    @classmethod
    def _iter_codex_response_events(cls, raw: Any, max_duration_secs: float | None = None) -> Iterator[Dict[str, Any]]:
        content_type = str(raw.headers.get("content-type") or "").lower()
        status_code = getattr(raw, "status", None)
        timeout_secs = float(max_duration_secs or 0)
        started_at = time.monotonic()
        timed_out = False
        timer: threading.Timer | None = None
        parse_errors: list[str] = []
        events: list[Dict[str, Any]] = []
        body_parts: list[str] = []
        body_preview_len = 0

        def _abort_stream() -> None:
            nonlocal timed_out
            timed_out = True
            try:
                raw.close()
            except Exception:
                pass

        def _raise_if_timeout() -> None:
            if timeout_secs > 0 and (timed_out or time.monotonic() - started_at > timeout_secs):
                raise TimeoutError(cls._stream_timeout_message(timeout_secs))

        def _append_body(text: str) -> None:
            nonlocal body_preview_len
            if body_preview_len >= 4000:
                return
            chunk = text[: max(0, 4000 - body_preview_len)]
            body_parts.append(chunk)
            body_preview_len += len(chunk)

        def _flush_sse_event(lines: list[str]) -> bool:
            if not lines:
                return False
            payload_text = "\n".join(lines).strip()
            lines.clear()
            if not payload_text:
                return False
            if payload_text == "[DONE]":
                return True
            try:
                data = json.loads(payload_text)
            except Exception as exc:
                parse_errors.append(str(exc))
                return False
            if isinstance(data, dict):
                events.append(data)
                return str(data.get("type") or "") in {
                    "response.completed",
                    "response.failed",
                    "response.incomplete",
                }
            return False

        if timeout_secs > 0:
            timer = threading.Timer(timeout_secs, _abort_stream)
            timer.daemon = True
            timer.start()
        try:
            if "application/json" in content_type:
                _raise_if_timeout()
                text = raw.read().decode("utf-8", "replace")
                _raise_if_timeout()
                _append_body(text)
                try:
                    data = json.loads(text)
                    if isinstance(data, dict):
                        events.append(data)
                except Exception as exc:
                    parse_errors.append(str(exc))
            else:
                lines: list[str] = []
                while True:
                    _raise_if_timeout()
                    raw_line = raw.readline()
                    _raise_if_timeout()
                    if not raw_line:
                        break
                    line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
                    _append_body(line + "\n")
                    if not line:
                        if _flush_sse_event(lines):
                            break
                    elif line.startswith("data:"):
                        data_line = line[5:].lstrip()
                        if data_line == "[DONE]":
                            lines.clear()
                            break
                        lines.append(data_line)
                _flush_sse_event(lines)
                _raise_if_timeout()
        except Exception as exc:
            if timed_out and timeout_secs > 0 and not isinstance(exc, TimeoutError):
                raise TimeoutError(cls._stream_timeout_message(timeout_secs)) from exc
            raise
        finally:
            if timer is not None:
                timer.cancel()

        event_types: Dict[str, int] = {}
        image_result_lengths: list[int] = []
        for event in events:
            event_type = str(event.get("type") or "<missing>")
            event_types[event_type] = event_types.get(event_type, 0) + 1
            image_result_lengths.extend(cls._codex_event_image_result_lengths(event))
        logger.info({
            "event": "codex_responses_response_debug",
            "status_code": status_code,
            "content_type": content_type,
            "response_text_len": body_preview_len,
            "event_count": len(events),
            "event_types": event_types,
            "image_result_lengths": image_result_lengths[:10],
            "parse_error_count": len(parse_errors),
            "parse_errors": parse_errors[:5],
            "event_summaries": [cls._codex_event_summary(event) for event in events[:30]],
            "event_previews": [
                cls._codex_body_preview(event, 1500)
                for event in events[:10]
            ] if not image_result_lengths else [],
            "body_preview": "".join(body_parts)[:1000] if not events else "",
        })
        for event in events:
            yield event

    def iter_codex_image_response_events(
            self,
            prompt: str,
            images: list[str] | None = None,
            size: str | None = None,
            quality: str = "auto",
            output_format: object = None,
    ) -> Iterator[Dict[str, Any]]:
        if not self.access_token:
            raise RuntimeError("access_token is required for codex image endpoints")
        self._ensure_codex_source_account()
        path = "/backend-api/codex/responses"
        requested_output_format = {
            "gif": "gif",
            "png": "png",
            "jpg": "jpeg",
            "jpeg": "jpeg",
            "webp": "webp",
        }.get(str(output_format or "").strip().lower(), "png")
        payload = {
            "model": CODEX_RESPONSES_MODEL,
            "instructions": CODEX_RESPONSES_INSTRUCTIONS,
            "store": False,
            "input": self._codex_image_input(prompt, images or []),
            "tools": [{
                "type": "image_generation",
                "model": "gpt-image-2",
                "action": "edit" if images else "generate",
                "size": str(size or "1024x1024"),
                "quality": str(quality or "auto"),
                "output_format": requested_output_format,
            }],
            "tool_choice": {"type": "image_generation"},
            "stream": True,
        }
        request = urllib.request.Request(
            self.base_url + path,
            json.dumps(payload).encode(),
            self._codex_responses_headers(),
            method="POST",
        )
        account = account_service.get_account(self.access_token) or {}
        token_payload = account_service._decode_jwt_payload(self.access_token)
        auth_claim = token_payload.get("https://api.openai.com/auth")
        auth_claim = auth_claim if isinstance(auth_claim, dict) else {}
        tool = payload["tools"][0]
        logger.info({
            "event": "codex_responses_request_debug",
            "url": self.base_url + path,
            "transport": "urllib.request",
            "timeout_secs": config.image_stream_timeout_secs,
            "account_email": str(account.get("email") or "").strip(),
            "source_type": str(account.get("source_type") or "").strip(),
            "account_type": str(account.get("type") or "").strip(),
            "token_claims": {
                "jti": token_payload.get("jti"),
                "iat": token_payload.get("iat"),
                "exp": token_payload.get("exp"),
                "client_id": token_payload.get("client_id"),
                "chatgpt_account_id": auth_claim.get("chatgpt_account_id"),
                "chatgpt_plan_type": auth_claim.get("chatgpt_plan_type"),
                "localhost": auth_claim.get("localhost"),
            },
            "request": {
                "model": payload.get("model"),
                "tool_model": tool.get("model"),
                "tool_action": tool.get("action"),
                "size": tool.get("size"),
                "quality": tool.get("quality"),
                "output_format": tool.get("output_format"),
                "stream": payload.get("stream"),
                "image_input_count": max(len((payload.get("input") or [{}])[0].get("content") or []) - 1, 0),
                "prompt_preview": self._codex_body_preview(
                    (((payload.get("input") or [{}])[0].get("content") or [{}])[0].get("text") or ""),
                    500,
                ),
            },
            "headers": {
                key: value for key, value in self._codex_responses_headers().items()
                if key.lower() != "authorization"
            },
        })
        stream_timeout = config.image_stream_timeout_secs
        try:
            with urllib.request.urlopen(request, timeout=stream_timeout) as raw:
                yield from self._iter_codex_response_events(raw, max_duration_secs=stream_timeout)
        except urllib.error.HTTPError as error:
            body_text = error.read().decode("utf-8", "replace")
            body: Any = body_text
            try:
                body = json.loads(body_text)
            except Exception:
                pass
            self._log_codex_response_failure(path, error.code, error.headers, payload, body)
            retry_after_header = error.headers.get("Retry-After") if error.headers else None
            retry_after = int(retry_after_header) if str(retry_after_header or "").isdigit() else None
            raise UpstreamHTTPError(path, error.code, body, retry_after=retry_after) from error
