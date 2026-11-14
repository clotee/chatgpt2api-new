import json

from services.image_failure import InvalidAccessTokenError
from typing import Any
from utils.helper import UpstreamHTTPError

from ._constants import MemoryStateInvalidError

class MemoryMixin:
    def _request_memory_settings(
        self,
        method: str,
        path: str,
        account_id: str,
        *,
        timeout_secs: float = 20.0,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """Use the account session without exposing upstream settings in errors."""
        account_id = str(account_id or "").strip()
        if not account_id:
            raise ValueError("缺少上游账号 ID，无法操作记忆")
        extra = {"Accept": "application/json", "ChatGPT-Account-ID": account_id}
        request_options: dict[str, Any] = {}
        if json_body is not None:
            extra["Content-Type"] = "application/json"
            request_options["json"] = json_body
        if method == "GET":
            extra["Oai-Ep-Cache-Bypass"] = "true"
        response = self.session.request(
            method,
            self.base_url + path,
            headers=self._headers(path, extra),
            timeout=timeout_secs,
            allow_redirects=False,
            stream=True,
            **request_options,
        )
        try:
            if response.status_code == 401:
                raise InvalidAccessTokenError("记忆接口认证失败 (HTTP 401)")
            if not 200 <= response.status_code < 300:
                raise UpstreamHTTPError(path, int(response.status_code), "memory settings request failed")
            if method != "GET":
                return None
            chunks = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                chunks.extend(chunk)
                if len(chunks) > 1024 * 1024:
                    raise ValueError("上游记忆设置响应过大")
            try:
                payload = json.loads(chunks)
            except (ValueError, UnicodeDecodeError, RecursionError) as exc:
                raise ValueError("上游记忆设置响应无效") from exc
            if not isinstance(payload, dict):
                raise ValueError("上游记忆设置响应无效")
            return payload
        finally:
            # Closing a successful mutation cannot undo its acceptance.
            try:
                response.close()
            except Exception:
                pass

    def get_memory_enabled(self, account_id: str, *, timeout_secs: float = 20.0) -> bool | None:
        """Read m3m only; absence is unknown rather than disabled."""
        payload = self._request_memory_settings(
            "GET", "/backend-api/settings/user", account_id, timeout_secs=timeout_secs,
        )
        pending = [payload]
        while pending:
            current = pending.pop()
            if isinstance(current, dict):
                for key, value in current.items():
                    if str(key).casefold() != "m3m":
                        continue
                    while isinstance(value, dict):
                        wrapped = {str(key).casefold(): item for key, item in value.items()}
                        field = next((key for key in ("enabled", "value", "is_enabled") if key in wrapped), None)
                        if field is None:
                            break
                        value = wrapped[field]
                    if isinstance(value, bool):
                        return value
                    if isinstance(value, str) and value.strip().casefold() in {"true", "false"}:
                        return value.strip().casefold() == "true"
                    raise MemoryStateInvalidError("上游记忆状态格式无效")
                pending.extend(reversed(list(current.values())))
            elif isinstance(current, list):
                pending.extend(reversed(current))
        return None

    def set_memory_enabled(self, account_id: str, enabled: bool, *, timeout_secs: float = 20.0) -> None:
        """Set the current memory switch, leaving existing saved memories intact."""
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        path = "/backend-api/settings/account_user_setting?feature=m3m&value=" + str(enabled).lower()
        self._request_memory_settings("PATCH", path, account_id, timeout_secs=timeout_secs)

    def clear_saved_memories(self, account_id: str, *, timeout_secs: float = 20.0) -> None:
        """Clear account-level saved memories without changing the m3m switch."""
        self._request_memory_settings(
            "DELETE", "/backend-api/settings/clear_account_user_memory", account_id,
            timeout_secs=timeout_secs, json_body={},
        )

    def saved_memories_empty(self, account_id: str, *, timeout_secs: float = 20.0) -> bool | None:
        """Verify emptiness without persisting or exposing saved memory contents."""
        payload = self._request_memory_settings(
            "GET", "/backend-api/memories?include_memory_entries=true", account_id,
            timeout_secs=timeout_secs,
        )
        entries = payload.get("memories")
        tokens = payload.get("memory_num_tokens")
        valid_tokens = isinstance(tokens, (int, float)) and not isinstance(tokens, bool) and tokens >= 0
        if isinstance(entries, list) and entries:
            return False
        if valid_tokens and tokens > 0:
            return False
        if isinstance(entries, list) and valid_tokens and tokens == 0:
            return True
        return None
