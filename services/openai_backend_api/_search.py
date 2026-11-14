import json
import re
import time

from typing import (
    Any,
    Dict,
)
from utils.helper import (
    UpstreamHTTPError,
    ensure_ok,
    iter_sse_payloads,
    new_uuid,
)

from ._constants import (
    SEARCH_CITATION_RE,
    SEARCH_CONVERSATION_ID_RE,
    SEARCH_DONE_STATUS,
    SEARCH_IMAGE_GROUP_RE,
    SEARCH_INTERNAL_BLOCK_RE,
    SEARCH_MODEL,
    SEARCH_POLL_INTERVAL_SECS,
    SEARCH_URL_RE,
)

class SearchMixin:
    def search(
            self,
            prompt: str,
            model: str = SEARCH_MODEL,
            *,
            timeout_secs: float,
            poll_interval_secs: float = SEARCH_POLL_INTERVAL_SECS,
    ) -> Dict[str, Any]:
        if not self.access_token:
            raise RuntimeError("access_token is required for search")
        deadline = time.monotonic() + max(0.001, float(timeout_secs))
        conduit_token = self._prepare_search_conversation(
            prompt,
            model,
            self._remaining_search_timeout(deadline),
        )
        self._bootstrap(timeout_secs=self._remaining_search_timeout(deadline))
        conversation_id = self._run_search_conversation(
            prompt,
            conduit_token,
            model,
            deadline,
        )
        return self._wait_search_result(conversation_id, deadline, poll_interval_secs)

    @staticmethod
    def _remaining_timeout(deadline: float, message: str) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(message)
        return max(0.001, remaining)

    @classmethod
    def _remaining_search_timeout(cls, deadline: float) -> float:
        return cls._remaining_timeout(deadline, "web search timed out")

    def _prepare_search_conversation(
            self,
            prompt: str,
            model: str,
            timeout_secs: float,
    ) -> str:
        path = "/backend-api/f/conversation/prepare"
        response = self.session.post(
            self.base_url + path,
            headers=self._headers(path, {"Accept": "*/*", "Content-Type": "application/json", "X-Conduit-Token": "no-token"}),
            json={
                "action": "next",
                "fork_from_shared_post": False,
                "parent_message_id": "client-created-root",
                "model": model,
                "client_prepare_state": "success",
                "timezone_offset_min": -480,
                "timezone": "Asia/Shanghai",
                "conversation_mode": {"kind": "primary_assistant"},
                "system_hints": ["search"],
                "partial_query": {"id": new_uuid(), "author": {"role": "user"}, "content": {"content_type": "text", "parts": [prompt]}},
                "supports_buffering": True,
                "supported_encodings": ["v1"],
                "client_contextual_info": {"app_name": "chatgpt.com"},
            },
            timeout=timeout_secs,
        )
        ensure_ok(response, path)
        token = str(response.json().get("conduit_token") or "")
        if not token:
            raise RuntimeError("missing conduit_token")
        return token

    def _run_search_conversation(
            self,
            prompt: str,
            conduit_token: str,
            model: str,
            deadline: float,
    ) -> str:
        requirements = self._get_chat_requirements(
            timeout_secs=self._remaining_search_timeout(deadline),
            deadline=deadline,
        )
        path = "/backend-api/f/conversation"
        response = self.session.post(
            self.base_url + path,
            headers=self._image_headers(path, requirements, conduit_token, "text/event-stream"),
            json={
                "action": "next",
                "messages": [{
                    "id": new_uuid(),
                    "author": {"role": "user"},
                    "create_time": time.time(),
                    "content": {"content_type": "text", "parts": [prompt]},
                    "metadata": {
                        "developer_mode_connector_ids": [],
                        "selected_github_repos": [],
                        "selected_all_github_repos": False,
                        "system_hints": ["search"],
                        "serialization_metadata": {"custom_symbol_offsets": []},
                    },
                }],
                "parent_message_id": "client-created-root",
                "model": model,
                "client_prepare_state": "success",
                "timezone_offset_min": -480,
                "timezone": "Asia/Shanghai",
                "conversation_mode": {"kind": "primary_assistant"},
                "enable_message_followups": True,
                "system_hints": [],
                "supports_buffering": True,
                "supported_encodings": ["v1"],
                "force_use_search": True,
                "client_reported_search_source": "conversation_composer_web_icon",
                "client_contextual_info": {"is_dark_mode": False, "time_since_loaded": 36, "page_height": 925, "page_width": 886, "pixel_ratio": 2, "screen_height": 1440, "screen_width": 2560, "app_name": "chatgpt.com"},
                "paragen_cot_summary_display_override": "allow",
                "force_parallel_switch": "auto",
            },
            timeout=self._remaining_search_timeout(deadline),
            stream=True,
        )
        ensure_ok(response, path)
        conversation_id = ""
        try:
            for payload in iter_sse_payloads(response):
                self._remaining_search_timeout(deadline)
                conversation_id = conversation_id or self._find_search_value(payload, "conversation_id")
                if payload == "[DONE]":
                    break
        finally:
            response.close()
        if not conversation_id:
            raise RuntimeError("conversation_id not found in stream")
        return conversation_id

    def _wait_search_result(self, conversation_id: str, deadline: float, poll_interval_secs: float) -> Dict[str, Any]:
        last_result: Dict[str, Any] | None = None
        last_answer = ""
        stable_hits = 0
        while time.monotonic() < deadline:
            try:
                last_result = self._extract_search_result(
                    conversation_id,
                    self._get_search_conversation(
                        conversation_id,
                        self._remaining_search_timeout(deadline),
                    ),
                )
            except UpstreamHTTPError as exc:
                if exc.status_code not in {404, 409, 423, 429, 500, 502, 503, 504}:
                    raise
            if last_result and last_result.get("answer"):
                if last_result.get("status") in SEARCH_DONE_STATUS:
                    return last_result
                answer = str(last_result.get("answer") or "")
                stable_hits = stable_hits + 1 if answer == last_answer else 0
                last_answer = answer
                if stable_hits >= 2:
                    return last_result
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(poll_interval_secs, remaining))
        if last_result and last_result.get("answer"):
            return last_result
        raise TimeoutError("web search timed out")

    def _get_search_conversation(
            self,
            conversation_id: str,
            timeout_secs: float,
    ) -> Dict[str, Any]:
        path = f"/backend-api/conversation/{conversation_id}"
        headers = self._headers(path, {"Accept": "*/*"})
        headers["Referer"] = f"{self.base_url}/c/{conversation_id}"
        headers["X-OpenAI-Target-Route"] = "/backend-api/conversation/{conversation_id}"
        response = self.session.get(
            self.base_url + path,
            headers=headers,
            timeout=timeout_secs,
        )
        ensure_ok(response, path)
        return response.json()

    def _extract_search_result(self, conversation_id: str, conversation: Dict[str, Any]) -> Dict[str, Any]:
        candidates: list[tuple[bool, float, Dict[str, Any], str]] = []
        for node in (conversation.get("mapping") or {}).values():
            message = (node or {}).get("message") or {}
            if ((message.get("author") or {}).get("role") or "") != "assistant":
                continue
            content = message.get("content") if isinstance(message.get("content"), dict) else {}
            content_type = str(content.get("content_type") or "").strip().lower()
            recipient = str(message.get("recipient") or "").strip().lower()
            if content_type in {"code", "thoughts", "reasoning_recap"} or recipient not in {"", "all"}:
                continue
            answer = self._search_message_text(message)
            if not answer:
                continue
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            finish_details = metadata.get("finish_details") if isinstance(metadata.get("finish_details"), dict) else {}
            terminal = (
                message.get("end_turn") is True
                or metadata.get("is_complete") is True
                or str(finish_details.get("type") or "").strip() in SEARCH_DONE_STATUS
            )
            candidates.append((terminal, float(message.get("create_time") or 0.0), message, answer))
        if candidates:
            _, _, message, answer = max(candidates, key=lambda item: (item[0], item[1]))
        else:
            message = {}
            answer = ""
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        finish_details = metadata.get("finish_details") if isinstance(metadata.get("finish_details"), dict) else {}
        image_groups = self._collect_search_image_groups(message)
        sources = self._extract_search_sources(message)
        for url in SEARCH_URL_RE.findall(answer):
            url = self._clean_search_url(url)
            if url and all(item["url"] != url for item in sources):
                sources.append({"title": "", "url": url, "snippet": "", "source_type": ""})
        return {
            "conversation_id": conversation_id,
            "status": str(finish_details.get("type") or metadata.get("status") or self._find_search_value(message, "status") or "").strip(),
            "answer": answer,
            "sources": sources,
            "image_groups": image_groups,
            "assistant_message_id": str(message.get("id") or ""),
            "create_time": float(message.get("create_time") or 0.0),
        }

    def _extract_search_sources(self, payload: Any) -> list[Dict[str, str]]:
        sources: list[Dict[str, str]] = []
        for obj in self._walk_search_dicts(payload):
            metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
            url = self._clean_search_url(obj.get("url") or obj.get("link") or obj.get("source_url") or metadata.get("url"))
            if url and all(item["url"] != url for item in sources):
                sources.append({
                    "title": str(obj.get("title") or obj.get("name") or obj.get("source") or "").strip(),
                    "url": url,
                    "snippet": str(obj.get("snippet") or obj.get("text") or obj.get("description") or "").strip(),
                    "source_type": str(obj.get("type") or obj.get("source_type") or "").strip(),
                })
        return sources

    def _search_message_text(self, message: Any) -> str:
        content = message.get("content") if isinstance(message, dict) else {}
        parts = []
        if isinstance(content, dict):
            if isinstance(content.get("text"), str):
                parts.append(content["text"])
            for part in content.get("parts") or []:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    parts.extend(str(part.get(key) or "") for key in ("text", "summary", "content") if part.get(key))
        elif isinstance(content, str):
            parts.append(content)
        return self._clean_search_answer("\n".join(part.strip() for part in parts if str(part).strip()))

    def _clean_search_answer(self, answer: str) -> str:
        def replace_citation(match: re.Match[str]) -> str:
            citation_id = match.group(1) or ""
            number_match = re.search(r"search(\d+)", citation_id)
            return f"[{int(number_match.group(1)) + 1}]" if number_match else ""

        answer = SEARCH_CITATION_RE.sub(replace_citation, str(answer or ""))
        answer = SEARCH_IMAGE_GROUP_RE.sub("", answer)
        answer = SEARCH_INTERNAL_BLOCK_RE.sub("", answer)
        answer = re.sub(r"[ \t]+\n", "\n", answer)
        answer = re.sub(r"\n{3,}", "\n\n", answer)
        return answer.strip()

    def _collect_search_image_groups(self, payload: Any) -> list[Dict[str, Any]]:
        groups: list[Dict[str, Any]] = []
        seen: set[str] = set()

        def add(value: Any) -> None:
            group = self._normalize_search_image_group(value)
            if not group:
                return
            key = json.dumps(group, ensure_ascii=False, sort_keys=True)
            if key in seen:
                return
            seen.add(key)
            groups.append(group)

        def walk(value: Any) -> None:
            if isinstance(value, str):
                for match in SEARCH_IMAGE_GROUP_RE.finditer(value):
                    add(match.group(1))
                return
            if isinstance(value, list):
                for item in value:
                    walk(item)
                return
            if not isinstance(value, dict):
                return

            if "image_group" in value:
                image_group = value.get("image_group")
                if isinstance(image_group, (dict, str)):
                    add(image_group)
                walk(image_group)

            block_type = str(value.get("type") or value.get("name") or value.get("block_type") or "").strip().lower()
            if block_type == "image_group" or ("query" in value and ("aspect_ratio" in value or "num_per_query" in value)):
                add(value)

            for item in value.values():
                walk(item)

        walk(payload)
        return groups

    def _normalize_search_image_group(self, payload: Any) -> Dict[str, Any] | None:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload or "{}")
            except Exception:
                return None
        if not isinstance(payload, dict):
            return None
        raw_queries = payload.get("query") or payload.get("queries")
        if isinstance(raw_queries, str):
            query_values = [raw_queries]
        elif isinstance(raw_queries, list):
            query_values = raw_queries
        else:
            query_values = []
        queries = [str(item).strip() for item in query_values if str(item).strip()][:6]
        if not queries:
            return None
        group: Dict[str, Any] = {"queries": queries}
        aspect_ratio = str(payload.get("aspect_ratio") or "").strip()
        if aspect_ratio:
            group["aspect_ratio"] = aspect_ratio
        try:
            num_per_query = int(payload.get("num_per_query") or 0)
        except Exception:
            num_per_query = 0
        if num_per_query > 0:
            group["num_per_query"] = num_per_query
        return group

    def _find_search_value(self, payload: Any, key: str) -> str:
        if isinstance(payload, str):
            match = SEARCH_CONVERSATION_ID_RE.search(payload) if key == "conversation_id" else None
            if match:
                return match.group(1)
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return ""
        if isinstance(payload, dict):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
            return next((found for item in payload.values() if (found := self._find_search_value(item, key))), "")
        if isinstance(payload, list):
            return next((found for item in payload if (found := self._find_search_value(item, key))), "")
        return ""

    def _walk_search_dicts(self, payload: Any) -> list[Dict[str, Any]]:
        if isinstance(payload, dict):
            return [payload, *(item for value in payload.values() for item in self._walk_search_dicts(value))]
        if isinstance(payload, list):
            return [item for value in payload for item in self._walk_search_dicts(value)]
        return []

    def _clean_search_url(self, value: Any) -> str:
        return str(value or "").strip().rstrip(".,;，。；")
