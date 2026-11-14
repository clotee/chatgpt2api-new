import base64
import json
import mimetypes
import re
import time

from PIL import Image
from io import BytesIO
from pathlib import Path
from services.editable_file_failure import EditableFileFailureError
from services.image_failure import (
    ImageFailure,
    classify_conversation_failure,
    classify_image_exception,
    classify_upstream_message,
    image_failure,
)
from typing import (
    Any,
    Dict,
)
from urllib.parse import (
    unquote,
    urlparse,
)
from utils.file_names import sanitize_public_filename
from utils.helper import (
    UpstreamHTTPError,
    ensure_ok,
    iter_sse_payloads,
    new_uuid,
)

from ._constants import (
    EDITABLE_ASSET_POINTER_RE,
    EDITABLE_FILE_CLIENT_BUILD_NUMBER,
    EDITABLE_FILE_CLIENT_VERSION,
    EDITABLE_FILE_MODEL,
    EDITABLE_FILE_POLL_INTERVAL_SECS,
    EDITABLE_FILE_PPT_OUTPUT_DIR,
    EDITABLE_FILE_PPT_PROMPT,
    EDITABLE_FILE_PSD_OUTPUT_DIR,
    EDITABLE_FILE_PSD_PROMPT,
    EDITABLE_FILE_THINKING_EFFORT,
    EDITABLE_PPT_EXPORT_FILE_RE,
    EDITABLE_PPT_MIME_TYPES,
    EDITABLE_PSD_EXPORT_FILE_RE,
    EDITABLE_PSD_MIME_TYPES,
    EDITABLE_ZIP_MIME_TYPES,
    EditableFileArtifact,
    EditableFileExportResult,
    FILE_ID_RE,
    SEARCH_CONVERSATION_ID_RE,
)

class EditableMixin:
    def _raise_editable_failure(self, failure: ImageFailure, event: str) -> None:
        if failure.code == "auth_invalid":
            self._schedule_auth_recovery(event)
        raise EditableFileFailureError(failure=failure)

    def _raise_editable_http_failure(
        self,
        exc: UpstreamHTTPError,
        event: str,
    ) -> None:
        self._raise_editable_failure(classify_image_exception(exc), event)

    @staticmethod
    def _editable_prompt(fixed_prompt: str, user_prompt_text: str) -> str:
        extra = str(user_prompt_text or "").strip()
        return fixed_prompt if not extra else fixed_prompt + "\n\n以下是用户补充需求，请直接结合执行：\n" + extra

    def export_ppt_zip(
            self,
            base64_images: list[str] | None,
            prompt: str,
            output_dir: str | Path = EDITABLE_FILE_PPT_OUTPUT_DIR,
            *,
            timeout_secs: float,
            poll_interval_secs: float = EDITABLE_FILE_POLL_INTERVAL_SECS,
    ) -> EditableFileExportResult:
        return self._export_editable_file_zip(
            base64_images or [],
            self._editable_prompt(EDITABLE_FILE_PPT_PROMPT, prompt),
            output_dir,
            primary_label="ppt",
            primary_suffixes=(".ppt", ".pptx"),
            primary_mime_types=EDITABLE_PPT_MIME_TYPES,
            primary_mime_keywords=("presentationml.presentation", "ms-powerpoint"),
            primary_default_extension=".pptx",
            export_file_re=EDITABLE_PPT_EXPORT_FILE_RE,
            timeout_secs=timeout_secs,
            poll_interval_secs=poll_interval_secs,
        )

    def export_psd_zip(
            self,
            base64_images: list[str],
            prompt: str,
            output_dir: str | Path = EDITABLE_FILE_PSD_OUTPUT_DIR,
            *,
            timeout_secs: float,
            poll_interval_secs: float = EDITABLE_FILE_POLL_INTERVAL_SECS,
    ) -> EditableFileExportResult:
        if not base64_images:
            raise ValueError("base64_images is empty")
        return self._export_editable_file_zip(
            base64_images,
            self._editable_prompt(EDITABLE_FILE_PSD_PROMPT, prompt),
            output_dir,
            primary_label="psd",
            primary_suffixes=(".psd",),
            primary_mime_types=EDITABLE_PSD_MIME_TYPES,
            primary_mime_keywords=("photoshop",),
            primary_default_extension=".psd",
            export_file_re=EDITABLE_PSD_EXPORT_FILE_RE,
            timeout_secs=timeout_secs,
            poll_interval_secs=poll_interval_secs,
        )

    def _export_editable_file_zip(
            self,
            base64_images: list[str],
            prompt: str,
            output_dir: str | Path,
            *,
            primary_label: str,
            primary_suffixes: tuple[str, ...],
            primary_mime_types: set[str],
            primary_mime_keywords: tuple[str, ...],
            primary_default_extension: str,
            export_file_re: re.Pattern[str],
            timeout_secs: float,
            poll_interval_secs: float,
    ) -> EditableFileExportResult:
        if not self.access_token:
            raise RuntimeError("access_token is required for editable file export")
        self.client_version = EDITABLE_FILE_CLIENT_VERSION
        self.client_build_number = EDITABLE_FILE_CLIENT_BUILD_NUMBER
        self.session.headers["OAI-Client-Version"] = EDITABLE_FILE_CLIENT_VERSION
        self.session.headers["OAI-Client-Build-Number"] = EDITABLE_FILE_CLIENT_BUILD_NUMBER
        output_path = Path(output_dir).expanduser().resolve()
        output_path.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(0.001, float(timeout_secs))
        try:
            uploaded = [
                self._upload_editable_base64_image(
                    item,
                    index,
                    deadline=deadline,
                )
                for index, item in enumerate(base64_images, start=1)
            ]
            conduit_token = self._prepare_editable_conversation(
                prompt,
                [item["mime_type"] for item in uploaded],
                deadline=deadline,
            )
            conversation_id = self._run_editable_conversation(
                prompt,
                uploaded,
                conduit_token,
                deadline=deadline,
            )
            artifacts = self._wait_editable_output_artifacts(
                conversation_id,
                primary_label,
                primary_suffixes,
                primary_mime_types,
                primary_mime_keywords,
                export_file_re,
                timeout_secs,
                poll_interval_secs,
                deadline=deadline,
            )
            downloaded = [
                self._download_editable_artifact(
                    conversation_id,
                    item,
                    output_path,
                    primary_mime_types,
                    primary_mime_keywords,
                    primary_default_extension,
                    deadline=deadline,
                )
                for item in artifacts
            ]
            primary_path = next((item for item in downloaded if item.suffix.lower() in primary_suffixes), None)
            zip_path = next((item for item in downloaded if item.suffix.lower() == ".zip"), None)
            if not primary_path or not zip_path:
                self._raise_editable_failure(
                    image_failure("image_download_failed"),
                    "editable_download_incomplete",
                )
            return EditableFileExportResult(conversation_id=conversation_id, primary_path=primary_path, zip_path=zip_path)
        except UpstreamHTTPError as exc:
            self._raise_editable_http_failure(exc, "editable_export_http_failure")

    @classmethod
    def _editable_request_timeout(
            cls,
            deadline: float | None,
            maximum: float,
    ) -> float:
        if deadline is None:
            return maximum
        return min(maximum, cls._remaining_timeout(deadline, "editable file task timed out"))

    def _upload_editable_base64_image(
            self,
            base64_image: str,
            index: int,
            *,
            deadline: float | None = None,
    ) -> Dict[str, Any]:
        data, file_name, mime_type, width, height = self._decode_editable_base64_image(base64_image, index)
        path = "/backend-api/files"
        response = self.session.post(
            self.base_url + path,
            headers=self._headers(path, {"Accept": "*/*", "Content-Type": "application/json"}),
            json={
                "file_name": file_name,
                "file_size": len(data),
                "use_case": "multimodal",
                "timezone_offset_min": -480,
                "reset_rate_limits": False,
                "store_in_library": True,
                "library_persistence_mode": "opportunistic",
            },
            timeout=self._editable_request_timeout(deadline, 60),
        )
        ensure_ok(response, path)
        payload = response.json()
        upload_url = str(payload.get("upload_url") or "")
        file_id = str(payload.get("file_id") or "")
        if not upload_url or not file_id:
            raise RuntimeError(f"invalid upload response: {payload}")
        response = self.session.put(
            upload_url,
            headers=self._signed_asset_headers({
                "Content-Type": mime_type,
                "x-ms-blob-type": "BlockBlob",
                "x-ms-version": "2020-04-08",
                "Origin": self.base_url,
                "Referer": self.base_url + "/",
                "User-Agent": self.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.8",
            }),
            data=data,
            timeout=self._editable_request_timeout(deadline, 120),
        )
        ensure_ok(response, "image_upload", credential_scope="signed_asset")
        path = f"/backend-api/files/{file_id}/uploaded"
        response = self.session.post(
            self.base_url + path,
            headers=self._headers(path, {"Accept": "*/*", "Content-Type": "application/json"}),
            data="{}",
            timeout=self._editable_request_timeout(deadline, 60),
        )
        ensure_ok(response, path)
        return {
            "file_id": file_id,
            "library_file_id": str(payload.get("library_file_id") or ""),
            "file_name": file_name,
            "file_size": len(data),
            "mime_type": mime_type,
            "width": width,
            "height": height,
        }

    def _decode_editable_base64_image(self, base64_image: str, index: int) -> tuple[bytes, str, str, int, int]:
        raw = str(base64_image or "").strip()
        if not raw:
            raise ValueError("base64 image is empty")
        mime_type = ""
        payload = raw
        match = re.match(r"^data:([^;]+);base64,(.*)$", raw, re.IGNORECASE | re.DOTALL)
        if match:
            mime_type = str(match.group(1) or "").strip().lower()
            payload = str(match.group(2) or "").strip()
        data = base64.b64decode(payload)
        image = Image.open(BytesIO(data))
        image.load()
        width, height = image.size
        mime_type = Image.MIME.get(image.format, mime_type or "image/png")
        extension = mimetypes.guess_extension(mime_type) or ".png"
        return data, f"image_{index}{extension}", mime_type, width, height

    def _prepare_editable_conversation(
            self,
            prompt: str,
            attachment_mime_types: list[str],
            *,
            deadline: float | None = None,
    ) -> str:
        path = "/backend-api/f/conversation/prepare"
        payload: Dict[str, Any] = {
            "action": "next",
            "fork_from_shared_post": False,
            "parent_message_id": "client-created-root",
            "model": EDITABLE_FILE_MODEL,
            "client_prepare_state": "success",
            "timezone_offset_min": -480,
            "timezone": "Asia/Shanghai",
            "conversation_mode": {"kind": "primary_assistant"},
            "system_hints": [],
            "partial_query": {"id": new_uuid(), "author": {"role": "user"}, "content": {"content_type": "text", "parts": [prompt]}},
            "supports_buffering": True,
            "supported_encodings": ["v1"],
            "client_contextual_info": {"app_name": "chatgpt.com"},
            "thinking_effort": EDITABLE_FILE_THINKING_EFFORT,
        }
        if attachment_mime_types:
            payload["attachment_mime_types"] = attachment_mime_types
        response = self.session.post(
            self.base_url + path,
            headers=self._headers(path, {"Accept": "*/*", "Content-Type": "application/json", "X-Conduit-Token": "no-token"}),
            json=payload,
            timeout=self._editable_request_timeout(deadline, 60),
        )
        ensure_ok(response, path)
        conduit_token = str(response.json().get("conduit_token") or "")
        if not conduit_token:
            raise RuntimeError(f"missing conduit_token: {response.text}")
        return conduit_token

    def _run_editable_conversation(
            self,
            prompt: str,
            uploaded: list[Dict[str, Any]],
            conduit_token: str,
            *,
            deadline: float | None = None,
    ) -> str:
        self._bootstrap(timeout_secs=self._editable_request_timeout(deadline, 30))
        requirements = self._get_chat_requirements(
            timeout_secs=self._editable_request_timeout(deadline, 30),
            deadline=deadline,
            timeout_message="editable file task timed out",
        )
        message: Dict[str, Any] = {"id": new_uuid(), "author": {"role": "user"}, "create_time": time.time()}
        if uploaded:
            parts = [{
                "content_type": "image_asset_pointer",
                "asset_pointer": f"sediment://{item['file_id']}",
                "size_bytes": item["file_size"],
                "width": item["width"],
                "height": item["height"],
            } for item in uploaded]
            parts.append(prompt)
            message["content"] = {"content_type": "multimodal_text", "parts": parts}
            message["metadata"] = {
                "attachments": [{
                    "id": item["file_id"],
                    "size": item["file_size"],
                    "name": item["file_name"],
                    "mime_type": item["mime_type"],
                    "width": item["width"],
                    "height": item["height"],
                    "source": "library",
                    "library_file_id": item["library_file_id"],
                    "is_big_paste": False,
                } for item in uploaded],
                "developer_mode_connector_ids": [],
                "selected_sources": [],
                "selected_github_repos": [],
                "selected_all_github_repos": False,
                "serialization_metadata": {"custom_symbol_offsets": []},
            }
        else:
            message["content"] = {"content_type": "text", "parts": [prompt]}
        path = "/backend-api/f/conversation"
        response = self.session.post(
            self.base_url + path,
            headers=self._image_headers(path, requirements, conduit_token, "text/event-stream"),
            json={
                "action": "next",
                "messages": [message],
                "parent_message_id": "client-created-root",
                "model": EDITABLE_FILE_MODEL,
                "client_prepare_state": "sent",
                "timezone_offset_min": -480,
                "timezone": "Asia/Shanghai",
                "conversation_mode": {"kind": "primary_assistant"},
                "enable_message_followups": True,
                "system_hints": [],
                "supports_buffering": True,
                "supported_encodings": ["v1"],
                "client_contextual_info": {
                    "is_dark_mode": False,
                    "time_since_loaded": 401,
                    "page_height": 1138,
                    "page_width": 803,
                    "pixel_ratio": 2,
                    "screen_height": 1440,
                    "screen_width": 2560,
                    "app_name": "chatgpt.com",
                },
                "paragen_cot_summary_display_override": "allow",
                "force_parallel_switch": "auto",
                "thinking_effort": EDITABLE_FILE_THINKING_EFFORT,
            },
            timeout=self._editable_request_timeout(deadline, 300),
            stream=True,
        )
        try:
            ensure_ok(response, path)
        except UpstreamHTTPError as exc:
            response.close()
            self._raise_editable_http_failure(exc, "editable_stream_http_failure")
        conversation_id = ""
        try:
            for payload in iter_sse_payloads(response):
                self._editable_request_timeout(deadline, 300)
                if payload == "[DONE]":
                    break
                try:
                    parsed_payload = json.loads(payload)
                except (TypeError, json.JSONDecodeError):
                    parsed_payload = payload
                failure = classify_upstream_message(parsed_payload)
                if failure is not None and failure.code != "upstream_text_reply":
                    self._raise_editable_failure(failure, "editable_stream_failure")
                conversation_id = conversation_id or self._find_editable_value(payload, "conversation_id")
        finally:
            response.close()
        if not conversation_id:
            raise RuntimeError("conversation_id not found in stream")
        return conversation_id

    def _wait_editable_output_artifacts(
            self,
            conversation_id: str,
            primary_label: str,
            primary_suffixes: tuple[str, ...],
            primary_mime_types: set[str],
            primary_mime_keywords: tuple[str, ...],
            export_file_re: re.Pattern[str],
            timeout_secs: float,
            poll_interval_secs: float,
            *,
            deadline: float | None = None,
    ) -> list[EditableFileArtifact]:
        poll_deadline = deadline or (time.monotonic() + timeout_secs)
        while time.monotonic() < poll_deadline:
            try:
                conversation = self._get_editable_conversation_detail(
                    conversation_id,
                    deadline=poll_deadline,
                )
            except UpstreamHTTPError as exc:
                if exc.status_code in {404, 409, 423, 429, 500, 502, 503, 504}:
                    remaining = poll_deadline - time.monotonic()
                    if remaining > 0:
                        time.sleep(min(poll_interval_secs, remaining))
                    continue
                self._raise_editable_http_failure(exc, "editable_poll_http_failure")
            targeted = self._pick_editable_target_artifacts(
                self._extract_editable_artifacts(conversation, export_file_re),
                primary_suffixes,
                primary_mime_types,
                primary_mime_keywords,
            )
            failure = classify_conversation_failure(conversation)
            if targeted:
                if failure is not None and failure.code == "auth_invalid":
                    self._schedule_auth_recovery("editable_artifact_after_success")
                return targeted
            if failure is not None and failure.code != "upstream_text_reply":
                self._raise_editable_failure(failure, "editable_poll_failure")
            remaining = poll_deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(poll_interval_secs, remaining))
        self._raise_editable_failure(
            image_failure("image_poll_timeout"),
            "editable_poll_timeout",
        )

    def _get_editable_conversation_detail(
            self,
            conversation_id: str,
            *,
            deadline: float | None = None,
    ) -> Dict[str, Any]:
        path = f"/backend-api/conversation/{conversation_id}"
        response = self.session.get(
            self.base_url + path,
            headers=self._editable_conversation_document_headers(path, conversation_id),
            timeout=self._editable_request_timeout(deadline, 60),
        )
        ensure_ok(response, path)
        return response.json()

    def _editable_browser_headers(self, path: str, conversation_id: str) -> Dict[str, str]:
        headers = self._headers(path, {"Accept": "*/*"})
        headers["Referer"] = f"{self.base_url}/c/{conversation_id}"
        return headers

    def _editable_conversation_document_headers(self, path: str, conversation_id: str) -> Dict[str, str]:
        headers = self._editable_browser_headers(path, conversation_id)
        headers["X-OpenAI-Target-Route"] = "/backend-api/conversation/{conversation_id}"
        return headers

    def _extract_editable_artifacts(self, conversation: Dict[str, Any], export_file_re: re.Pattern[str]) -> list[EditableFileArtifact]:
        artifacts: dict[str, EditableFileArtifact] = {}
        for node in sorted((conversation.get("mapping") or {}).values(), key=lambda item: float(((item or {}).get("message") or {}).get("create_time") or 0.0)):
            message = (node or {}).get("message") or {}
            message_id = str(message.get("id") or "")
            author_role = str(((message.get("author") or {}).get("role") or "")).strip()
            if author_role not in {"assistant", "tool"}:
                continue
            create_time = float(message.get("create_time") or 0.0)
            message_text = self._editable_message_text(message)
            for artifact in self._extract_editable_message_artifacts(message, message_id, author_role, create_time, export_file_re):
                key = artifact.attachment_id or artifact.file_id or artifact.name or artifact.sandbox_path
                if key:
                    artifacts[key] = self._merge_editable_artifact(artifacts.get(key), artifact)
            for export_path in self._extract_editable_export_paths(message_text, export_file_re):
                inferred = EditableFileArtifact(name=Path(export_path).name, create_time=create_time, author_role=author_role, sandbox_path=export_path, message_id=message_id)
                artifacts[export_path] = self._merge_editable_artifact(artifacts.get(export_path), inferred)
        return sorted(artifacts.values(), key=lambda item: item.create_time)

    def _extract_editable_message_artifacts(
            self,
            message: Dict[str, Any],
            message_id: str,
            author_role: str,
            create_time: float,
            export_file_re: re.Pattern[str],
    ) -> list[EditableFileArtifact]:
        artifacts: list[EditableFileArtifact] = []
        for item in (message.get("metadata") or {}).get("attachments") or []:
            artifact = self._editable_artifact_from_dict(item, message_id, author_role, create_time, export_file_re)
            if artifact:
                artifacts.append(artifact)
        for obj in self._walk_search_dicts(message):
            artifact = self._editable_artifact_from_dict(obj, message_id, author_role, create_time, export_file_re)
            if artifact:
                artifacts.append(artifact)
        return artifacts

    def _editable_artifact_from_dict(
            self,
            payload: Dict[str, Any],
            message_id: str,
            author_role: str,
            create_time: float,
            export_file_re: re.Pattern[str],
    ) -> EditableFileArtifact | None:
        if not ({"id", "file_id", "asset_pointer", "name", "file_name", "filename", "mime_type", "mimeType"} & set(payload.keys())):
            return None
        attachment_id = self._match_editable_file_id(str(payload.get("id") or ""))
        file_id = self._match_editable_file_id(str(payload.get("file_id") or ""))
        name = self._sanitize_editable_filename(str(payload.get("name") or payload.get("file_name") or payload.get("filename") or payload.get("title") or "").strip())
        mime_type = self._clean_editable_mime_type(payload.get("mime_type") or payload.get("mimeType") or "")
        for asset_id in EDITABLE_ASSET_POINTER_RE.findall(str(payload.get("asset_pointer") or "")):
            attachment_id = attachment_id or asset_id
            file_id = file_id or asset_id
        if not attachment_id or not file_id:
            ids = self._extract_editable_file_ids(json.dumps(payload, ensure_ascii=False))
            attachment_id = attachment_id or (ids[0] if ids else "")
            file_id = file_id or (ids[0] if ids else "")
        if not attachment_id and not file_id:
            return None
        return EditableFileArtifact(
            attachment_id=attachment_id,
            file_id=file_id,
            name=name,
            mime_type=mime_type,
            create_time=create_time,
            author_role=author_role,
            sandbox_path=(self._extract_editable_export_paths(payload, export_file_re) or [""])[0],
            message_id=message_id,
        )

    def _pick_editable_target_artifacts(
            self,
            artifacts: list[EditableFileArtifact],
            primary_suffixes: tuple[str, ...],
            primary_mime_types: set[str],
            primary_mime_keywords: tuple[str, ...],
    ) -> list[EditableFileArtifact]:
        primary = next((item for item in reversed(artifacts) if self._looks_like_editable_primary(item, primary_suffixes, primary_mime_types, primary_mime_keywords)), None)
        zip_item = next((item for item in reversed(artifacts) if self._looks_like_editable_zip(item)), None)
        return [primary, zip_item] if primary and zip_item else []

    def _download_editable_artifact(
            self,
            conversation_id: str,
            artifact: EditableFileArtifact,
            output_dir: Path,
            primary_mime_types: set[str],
            primary_mime_keywords: tuple[str, ...],
            primary_default_extension: str,
            *,
            deadline: float | None = None,
    ) -> Path:
        download_url = self._resolve_editable_download_url(
            conversation_id,
            artifact,
            deadline=deadline,
        )
        if not download_url:
            self._raise_editable_failure(
                image_failure("image_download_failed"),
                "editable_download_url_missing",
            )
        response = self.session.get(
            download_url,
            timeout=self._editable_request_timeout(deadline, 300),
        )
        try:
            ensure_ok(response, "artifact_download", credential_scope="signed_asset")
        except UpstreamHTTPError as exc:
            self._raise_editable_http_failure(
                exc,
                "editable_asset_download_http_failure",
            )
        content_type = self._clean_editable_mime_type(response.headers.get("Content-Type") or artifact.mime_type)
        file_name = self._resolve_editable_output_name(artifact, response.url, response.headers.get("Content-Disposition"), content_type, primary_mime_types, primary_mime_keywords, primary_default_extension)
        target_path = self._unique_editable_path(output_dir / file_name)
        target_path.write_bytes(response.content)
        return target_path

    def _resolve_editable_download_url(
            self,
            conversation_id: str,
            artifact: EditableFileArtifact,
            *,
            deadline: float | None = None,
    ) -> str:
        ids: list[str] = []
        for item in (artifact.attachment_id, artifact.file_id):
            if item and item not in ids:
                ids.append(item)
        if artifact.sandbox_path and artifact.message_id:
            path = f"/backend-api/conversation/{conversation_id}/interpreter/download"
            response = self.session.get(
                self.base_url + path,
                headers=self._editable_download_headers(path, conversation_id, "/backend-api/conversation/{conversation_id}/interpreter/download"),
                params={"message_id": artifact.message_id, "sandbox_path": artifact.sandbox_path},
                timeout=self._editable_request_timeout(deadline, 60),
            )
            if url := self._editable_download_url_candidate(response, path):
                return url
        for attachment_id in ids:
            path = f"/backend-api/conversation/{conversation_id}/attachment/{attachment_id}/download"
            response = self.session.get(
                self.base_url + path,
                headers=self._editable_download_headers(path, conversation_id, "/backend-api/conversation/{conversation_id}/attachment/{attachment_id}/download"),
                timeout=self._editable_request_timeout(deadline, 60),
            )
            if url := self._editable_download_url_candidate(response, path):
                return url
        for file_id in ids:
            path = f"/backend-api/files/download/{file_id}"
            response = self.session.get(
                self.base_url + path,
                headers=self._editable_download_headers(path, conversation_id, "/backend-api/files/download/{file_id}"),
                params={"post_id": "", "inline": "false"},
                timeout=self._editable_request_timeout(deadline, 60),
            )
            if url := self._editable_download_url_candidate(response, path):
                return url
        for file_id in ids:
            path = f"/backend-api/files/{file_id}/download"
            response = self.session.get(
                self.base_url + path,
                headers=self._editable_download_headers(path, conversation_id, "/backend-api/files/download/{file_id}"),
                timeout=self._editable_request_timeout(deadline, 60),
            )
            if url := self._editable_download_url_candidate(response, path):
                return url
        return ""

    def _editable_download_url_candidate(self, response: Any, path: str) -> str:
        if 200 <= response.status_code < 300:
            return self._download_url_from_response(response)
        if response.status_code == 404:
            return ""
        try:
            ensure_ok(response, path)
        except UpstreamHTTPError as exc:
            self._raise_editable_http_failure(
                exc,
                "editable_download_url_http_failure",
            )
        return ""

    def _editable_download_headers(self, path: str, conversation_id: str, route: str) -> Dict[str, str]:
        headers = self._editable_browser_headers(path, conversation_id)
        headers["X-OpenAI-Target-Route"] = route
        return headers

    @staticmethod
    def _download_url_from_response(response: Any) -> str:
        try:
            payload = response.json()
        except Exception:
            payload = {}
        return str(payload.get("download_url") or payload.get("url") or "")

    def _resolve_editable_output_name(
            self,
            artifact: EditableFileArtifact,
            final_url: str,
            content_disposition: str | None,
            content_type: str,
            primary_mime_types: set[str],
            primary_mime_keywords: tuple[str, ...],
            primary_default_extension: str,
    ) -> str:
        file_name = self._sanitize_editable_filename(artifact.name)
        if not file_name and artifact.sandbox_path:
            file_name = self._sanitize_editable_filename(Path(artifact.sandbox_path).name)
        if not file_name:
            file_name = self._sanitize_editable_filename(self._editable_filename_from_content_disposition(content_disposition or ""))
        if not file_name:
            file_name = self._sanitize_editable_filename(Path(urlparse(final_url).path).name)
        extension = self._editable_extension_from_mime_type(content_type, primary_mime_types, primary_mime_keywords, primary_default_extension)
        return f"artifact{extension}" if not file_name else (file_name if Path(file_name).suffix else file_name + extension)

    def _find_editable_value(self, payload: Any, key: str) -> str:
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
            return next((found for item in payload.values() if (found := self._find_editable_value(item, key))), "")
        if isinstance(payload, list):
            return next((found for item in payload if (found := self._find_editable_value(item, key))), "")
        return ""

    def _extract_editable_file_ids(self, text: str) -> list[str]:
        values: list[str] = []
        for item in EDITABLE_ASSET_POINTER_RE.findall(text):
            if item not in values:
                values.append(item)
        for item in FILE_ID_RE.findall(text):
            if item not in values:
                values.append(item)
        return values

    @staticmethod
    def _match_editable_file_id(value: str) -> str:
        match = FILE_ID_RE.search(value)
        return match.group(1) if match else ""

    @staticmethod
    def _clean_editable_mime_type(value: Any) -> str:
        text = str(value or "").strip().lower()
        return text.split(";", 1)[0] if "/" in text else ""

    def _looks_like_editable_primary(
            self,
            artifact: EditableFileArtifact,
            primary_suffixes: tuple[str, ...],
            primary_mime_types: set[str],
            primary_mime_keywords: tuple[str, ...],
    ) -> bool:
        path, name, mime = artifact.sandbox_path.lower(), artifact.name.lower(), artifact.mime_type
        return name.endswith(primary_suffixes) or path.endswith(primary_suffixes) or mime in primary_mime_types or any(keyword in mime for keyword in primary_mime_keywords)

    @staticmethod
    def _looks_like_editable_zip(artifact: EditableFileArtifact) -> bool:
        path, name, mime = artifact.sandbox_path.lower(), artifact.name.lower(), artifact.mime_type
        return name.endswith(".zip") or path.endswith(".zip") or mime in EDITABLE_ZIP_MIME_TYPES or mime.endswith("/zip")

    @staticmethod
    def _editable_extension_from_mime_type(
            mime_type: str,
            primary_mime_types: set[str],
            primary_mime_keywords: tuple[str, ...],
            primary_default_extension: str,
    ) -> str:
        if mime_type in primary_mime_types or any(keyword in mime_type for keyword in primary_mime_keywords):
            return primary_default_extension
        if mime_type in EDITABLE_ZIP_MIME_TYPES or mime_type.endswith("/zip"):
            return ".zip"
        return mimetypes.guess_extension(mime_type) or ""

    @staticmethod
    def _editable_filename_from_content_disposition(content_disposition: str) -> str:
        extended_match = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, re.IGNORECASE)
        if extended_match:
            return unquote(extended_match.group(1)).strip()
        plain_match = re.search(r'filename="([^"]+)"', content_disposition, re.IGNORECASE)
        return plain_match.group(1).strip() if plain_match else ""

    @staticmethod
    def _sanitize_editable_filename(value: str) -> str:
        return sanitize_public_filename(value)

    @staticmethod
    def _unique_editable_path(path: Path) -> Path:
        if not path.exists():
            return path
        for index in range(1, 1000):
            candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
            if not candidate.exists():
                return candidate
        raise RuntimeError(f"failed to allocate output path for {path}")

    @staticmethod
    def _merge_editable_artifact(current: EditableFileArtifact | None, latest: EditableFileArtifact) -> EditableFileArtifact:
        if current is None:
            return latest
        return EditableFileArtifact(
            attachment_id=latest.attachment_id or current.attachment_id,
            file_id=latest.file_id or current.file_id,
            name=latest.name or current.name,
            mime_type=latest.mime_type or current.mime_type,
            create_time=max(current.create_time, latest.create_time),
            author_role=latest.author_role or current.author_role,
            sandbox_path=latest.sandbox_path or current.sandbox_path,
            message_id=latest.message_id or current.message_id,
        )

    @staticmethod
    def _editable_message_text(message: Any) -> str:
        if not isinstance(message, dict):
            return ""
        content = message.get("content") or {}
        parts: list[str] = []
        if isinstance(content, dict):
            if isinstance(content.get("text"), str):
                parts.append(content["text"])
            for part in content.get("parts") or []:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    parts.extend(str(part.get(key) or "") for key in ("text", "asset_pointer", "model_set_context") if part.get(key))
        if isinstance(message.get("content"), str):
            parts.append(str(message["content"]))
        return "\n".join(part for part in parts if part).strip()

    @staticmethod
    def _payload_status_values(payload: Any | None) -> list[str]:
        """Extract status/state values from ChatGPT message payloads and SSE patches."""
        values: list[str] = []

        def add(value: Any) -> None:
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized:
                    values.append(normalized)

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                patch_path = str(value.get("p") or "").strip().lower()
                if patch_path.endswith("/message/status") or patch_path == "/message/status":
                    add(value.get("v"))
                for key, item in value.items():
                    lowered_key = str(key).strip().lower()
                    if lowered_key in {"status", "state"}:
                        add(item)
                    if isinstance(item, (dict, list, tuple)):
                        visit(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    visit(item)

        visit(payload)
        return values

    @staticmethod
    def _payload_has_completion_marker(payload: Any | None) -> bool:
        """Return True when payload has structured completion metadata."""
        found = False

        def visit(value: Any) -> None:
            nonlocal found
            if found:
                return
            if isinstance(value, dict):
                for key, item in value.items():
                    lowered_key = str(key).strip().lower()
                    if lowered_key in {"is_complete", "complete"} and item is True:
                        found = True
                        return
                    if lowered_key == "finish_details" and isinstance(item, dict) and item:
                        found = True
                        return
                    if isinstance(item, (dict, list, tuple)):
                        visit(item)
                        if found:
                            return
            elif isinstance(value, (list, tuple)):
                for item in value:
                    visit(item)
                    if found:
                        return

        visit(payload)
        return found

    @staticmethod
    def _extract_editable_export_paths(payload: Any, export_file_re: re.Pattern[str]) -> list[str]:
        if isinstance(payload, str):
            text = payload
        else:
            try:
                text = json.dumps(payload, ensure_ascii=False)
            except Exception:
                text = str(payload)
        values: list[str] = []
        for item in export_file_re.findall(text):
            path = str(item or "").strip()
            if path and path not in values:
                values.append(path)
        return values
