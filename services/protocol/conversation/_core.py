from __future__ import annotations

import base64
import io
import json
import re
import tiktoken
import time

from PIL import (
    Image,
    ImageOps,
)
from dataclasses import (
    dataclass,
    field,
)
from fastapi import HTTPException
from services.config import config
from services.image_failure import (
    ImageDownloadError,
    ImageFailure,
    ImageFailureError,
    classify_image_exception,
    classify_message_facts,
    extract_message_facts,
    is_terminal_message_status,
    merge_message_failure,
)
from services.image_storage_service import (
    image_media_type,
    image_storage_service,
)
from services.image_upscale_service import upscale_image_if_needed
from services.openai_backend_api import OpenAIBackendAPI
from typing import (
    Any,
    Iterable,
    Iterator,
)
from utils.diagnostics import diagnostic_excerpt
from utils.helper import (
    extract_image_from_message_content,
    is_supported_image_model,
)
from utils.image_tokens import (
    count_image_content_tokens,
    image_size_from_bytes,
)
from utils.log import logger
from utils.svg import svg_dimensions

MISSING_IMAGE_OPTION = object()
SUPPORTED_IMAGE_OUTPUT_FORMATS = ("gif", "png", "jpeg", "webp", "svg")
_IMAGE_OUTPUT_FORMAT_ALIASES = {"jpg": "jpeg"}


def normalize_image_output_format(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    normalized = _IMAGE_OUTPUT_FORMAT_ALIASES.get(text, text)
    if normalized not in SUPPORTED_IMAGE_OUTPUT_FORMATS:
        raise HTTPException(
            status_code=400,
            detail={
                "error": f"unsupported output_format: {text}",
                "supported": [*SUPPORTED_IMAGE_OUTPUT_FORMATS, "jpg"],
            },
        )
    return normalized


def _prompt_option_value(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def validate_image_output_request(
        model: object,
        output_format: object = None,
        *,
        stream: bool = False,
        has_references: bool = False,
) -> None:
    """Validate raw image formats and their shared model/transport constraints."""
    normalized_output_format = normalize_image_output_format(output_format)
    if normalized_output_format not in {"gif", "svg"}:
        return
    if str(model or "").strip().lower() not in config.gif_models:
        raise HTTPException(
            status_code=400,
            detail={"error": f"{normalized_output_format.upper()} output is not enabled for this image model"},
        )
    if stream:
        raise HTTPException(
            status_code=400,
            detail={"error": f"{normalized_output_format.upper()} output does not support stream=true"},
        )
    if normalized_output_format == "svg" and has_references:
        raise HTTPException(
            status_code=400,
            detail={"error": "SVG output is supported only for image generations"},
        )


validate_gif_request = validate_image_output_request


def is_token_invalid_error(message: str) -> bool:
    text = str(message or "").lower()
    return (
        "token_invalidated" in text
        or "token_revoked" in text
        or "authentication token has been invalidated" in text
        or "invalidated oauth token" in text
    )


def encode_images(images: Iterable[tuple[bytes, str, str]]) -> list[str]:
    return [base64.b64encode(data).decode("ascii") for data, _, _ in images if data]


def save_image_bytes(
        image_data: bytes,
        base_url: str | None = None,
        *,
        mime_type: str | None = None,
        deadline_monotonic: float | None = None,
) -> str:
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            return image_storage_service.save(
                image_data,
                base_url,
                mime_type=mime_type,
                deadline_monotonic=deadline_monotonic,
            ).url
        except ImageFailureError as exc:
            if exc.failure.code == "task_interrupted":
                raise
            last_error = exc
        except Exception as exc:
            last_error = exc
        logger.warning({
            "event": "image_result_storage_retry" if attempt == 0 else "image_result_storage_failed",
            "attempt": attempt + 1,
            "error": diagnostic_excerpt(repr(last_error), 500),
        })
    raise ImageDownloadError(
        f"image result storage failed: {diagnostic_excerpt(last_error, 500)}"
    ) from last_error


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and str(item.get("type") or "") in {"text", "input_text", "output_text"}:
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return ""


def normalize_messages(messages: object, system: Any = None) -> list[dict[str, Any]]:
    normalized = []
    if config.global_system_prompt:
        normalized.append({"role": "system", "content": config.global_system_prompt})
    system_text = message_text(system)
    if system_text:
        normalized.append({"role": "system", "content": system_text})
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role", "user")
            content = message.get("content", "")
            text = message_text(content)
            images: list[tuple[bytes, str]] = []
            if role == "user":
                images.extend(extract_image_from_message_content(content))
                if isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict) or part.get("type") != "image":
                            continue
                        data = part.get("data")
                        if isinstance(data, (bytes, bytearray)) and all(existing[0] != bytes(data) for existing in images):
                            images.append((bytes(data), str(part.get("mime") or "image/png")))
            if images:
                parts: list[Any] = []
                if text:
                    parts.append({"type": "text", "text": text})
                for data, mime in images:
                    parts.append({"type": "image", "data": data, "mime": mime})
                normalized.append({"role": role, "content": parts})
            else:
                normalized.append({"role": role, "content": text})
    return normalized


def prompt_with_global_system(prompt: str) -> str:
    return f"{config.global_system_prompt}\n\n{prompt}" if config.global_system_prompt else prompt


def assistant_history_text(messages: list[dict[str, Any]]) -> str:
    return "".join(str(item.get("content") or "") for item in messages if item.get("role") == "assistant")


def assistant_history_messages(messages: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("content") or "") for item in messages if item.get("role") == "assistant" and item.get("content")]


def build_image_prompt(
        prompt: str,
        size: str | None,
        quality: str = "auto",
        output_format: object = None,
        frame_count: object = MISSING_IMAGE_OPTION,
        frame_duration_ms: object = MISSING_IMAGE_OPTION,
) -> str:
    hints = []
    option_hints = []
    if size:
        hints.append(f"输出图片尺寸为 {size}。")
    if quality:
        hints.append(f"输出图片质量为 {quality}。")
    normalized_output_format = normalize_image_output_format(output_format)
    if normalized_output_format:
        option_hints.append(f"生成格式为 {normalized_output_format}。")
        if normalized_output_format == "svg":
            option_hints.append(
                "请直接返回完整的 SVG XML（从 <svg 开始到 </svg> 结束），禁止脚本、外部资源和嵌入式位图，并包含有效的 width/height 或 viewBox。"
            )
    if frame_count is not MISSING_IMAGE_OPTION:
        option_hints.append(f"frame_count: {_prompt_option_value(frame_count)}")
    if frame_duration_ms is not MISSING_IMAGE_OPTION:
        option_hints.append(f"frame_duration_ms: {_prompt_option_value(frame_duration_ms)}")
    suffix = "".join(hints)
    if option_hints:
        suffix += ("\n" if suffix else "") + "\n".join(option_hints)
    return f"{prompt.strip()}\n\n{suffix}" if suffix else prompt


def convert_image_bytes(image_data: bytes, output_format: object) -> bytes:
    """Convert a single raster image to the requested non-GIF format."""
    normalized_output_format = normalize_image_output_format(output_format)
    if not normalized_output_format or normalized_output_format in {"gif", "svg"}:
        return image_data
    try:
        with Image.open(io.BytesIO(image_data)) as source:
            source.seek(0)
            image = ImageOps.exif_transpose(source.copy())
        output = io.BytesIO()
        if normalized_output_format == "jpeg":
            if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            elif image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            image.save(output, format="JPEG", quality=95, subsampling=0)
        elif normalized_output_format == "webp":
            if image.mode == "P" and "transparency" in image.info:
                image = image.convert("RGBA")
            image.save(output, format="WEBP", quality=95, method=4)
        else:
            image.save(output, format="PNG", optimize=True)
        return output.getvalue()
    except Exception as exc:
        raise ImageDownloadError(
            f"image output format conversion failed: {normalized_output_format}"
        ) from exc


def encoding_for_model(model: str):
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        try:
            return tiktoken.get_encoding("o200k_base")
        except KeyError:
            return tiktoken.get_encoding("cl100k_base")


def count_message_image_tokens(messages: list[dict[str, Any]], model: str) -> int:
    return sum(count_image_content_tokens(message.get("content"), model) for message in messages)


def count_message_text_tokens(messages: list[dict[str, Any]], model: str) -> int:
    encoding = encoding_for_model(model)
    total = 0
    for message in messages:
        total += 3
        for key, value in message.items():
            if key == "content" and isinstance(value, list):
                total += len(encoding.encode(message_text(value)))
            elif isinstance(value, str):
                total += len(encoding.encode(value))
            else:
                continue
            if key == "name":
                total += 1
    return total + 3


def count_message_tokens(messages: list[dict[str, Any]], model: str) -> int:
    return count_message_text_tokens(messages, model) + count_message_image_tokens(messages, model)


def count_text_tokens(text: str, model: str) -> int:
    return len(encoding_for_model(model).encode(text))


def format_image_result(
    items: list[dict[str, Any]],
    prompt: str,
    response_format: str,
    base_url: str | None = None,
    created: int | None = None,
    message: str = "",
    requested_size: str | None = None,
    deadline_monotonic: float | None = None,
    output_format: object = None,
) -> dict[str, Any]:
    data: list[dict[str, Any]] = []
    image_urls: list[str] = []
    normalized_output_format = normalize_image_output_format(output_format)
    for item in items:
        b64_json = str(item.get("b64_json") or "").strip()
        if not b64_json:
            continue
        revised_prompt = str(item.get("revised_prompt") or prompt).strip() or prompt
        image_bytes = base64.b64decode(b64_json)
        dimensions = None
        if normalized_output_format == "svg":
            dimensions = svg_dimensions(image_bytes)
            if dimensions is None:
                raise ImageDownloadError("upstream did not return a valid SVG document")
        elif normalized_output_format:
            image_bytes = convert_image_bytes(image_bytes, normalized_output_format)
        if normalized_output_format not in {"gif", "svg"}:
            image_bytes = upscale_image_if_needed(image_bytes, requested_size)
        mime_type = (
            "image/svg+xml"
            if normalized_output_format == "svg"
            else image_media_type(payload=image_bytes)
        )
        stored_url = save_image_bytes(
            image_bytes,
            base_url,
            mime_type=mime_type or None,
            deadline_monotonic=deadline_monotonic,
        )
        if stored_url:
            image_urls.append(stored_url)
        asset: dict[str, Any] = {"revised_prompt": revised_prompt}
        if normalized_output_format == "svg":
            asset["mime_type"] = "image/svg+xml"
        if response_format == "b64_json":
            asset["b64_json"] = base64.b64encode(image_bytes).decode("ascii")
        else:
            asset["url"] = stored_url
        dimensions = dimensions or image_size_from_bytes(image_bytes)
        if dimensions:
            asset["width"], asset["height"] = dimensions
        data.append(asset)
    result: dict[str, Any] = {"created": created or int(time.time()), "data": data}
    if image_urls:
        result["_image_urls"] = image_urls
    if message and not data:
        result["message"] = message
    return result


@dataclass
class ConversationRequest:
    model: str = "auto"
    prompt: str = ""
    messages: list[dict[str, Any]] | None = None
    thinking_effort: str = ""
    images: list[str] | None = None
    n: int = 1
    size: str | None = None
    quality: str = "auto"
    response_format: str = "b64_json"
    output_format: str | None = None
    frame_count: object = MISSING_IMAGE_OPTION
    frame_duration_ms: object = MISSING_IMAGE_OPTION
    base_url: str | None = None
    message_as_error: bool = False
    progress_callback: Any = None  # Callable[[str], None] | None
    call_id: str = ""
    trace_image_perf: bool = False
    monitor_attempt: int = 0
    deadline_monotonic: float = 0.0


@dataclass
class ConversationState:
    text: str = ""
    raw_text: str = ""
    conversation_id: str = ""
    file_ids: list[str] = field(default_factory=list)
    sediment_ids: list[str] = field(default_factory=list)
    blocked: bool = False
    tool_invoked: bool | None = None
    turn_use_case: str = ""
    async_task_type: str = ""
    message_type: str = ""
    message_facts: dict[str, Any] = field(default_factory=dict)
    terminal_tool_arguments: dict[str, Any] | None = None
    failure: ImageFailure | None = None


@dataclass
class ImageOutput:
    kind: str
    model: str
    index: int
    total: int
    created: int = field(default_factory=lambda: int(time.time()))
    text: str = ""
    upstream_event_type: str = ""
    data: list[dict[str, Any]] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    image_attempts: list[dict[str, Any]] = field(default_factory=list)
    account_email: str = ""
    conversation_id: str = ""
    failure: ImageFailure | None = field(default=None, repr=False)

    def to_chunk(self) -> dict[str, Any]:
        chunk: dict[str, Any] = {
            "object": "image.generation.chunk",
            "created": self.created,
            "model": self.model,
            "index": self.index,
            "total": self.total,
            "progress_text": self.text,
            "upstream_event_type": self.upstream_event_type,
            "data": [],
        }
        if self.account_email:
            chunk["_account_email"] = self.account_email
        if self.conversation_id:
            chunk["_conversation_id"] = self.conversation_id
        if self.image_urls:
            chunk["_image_urls"] = list(self.image_urls)
        if self.image_attempts:
            chunk["_image_attempts"] = [dict(item) for item in self.image_attempts]
        if self.kind == "message":
            chunk.update({
                "object": "image.generation.message",
                "message": self.text,
            })
            chunk.pop("progress_text", None)
            chunk.pop("upstream_event_type", None)
        elif self.kind == "result":
            chunk.update({
                "object": "image.generation.result",
                "data": self.data,
            })
            chunk.pop("progress_text", None)
            chunk.pop("upstream_event_type", None)
        return chunk


def assistant_message_text(message: dict[str, Any]) -> str:
    content = message.get("content") or {}
    parts = content.get("parts") or []
    if isinstance(parts, list) and parts:
        text = "".join(part for part in parts if isinstance(part, str))
        if text:
            return text
    # Fallback: content_type "code" stores text in the "text" field instead of "parts"
    text_field = str(content.get("text") or "")
    if text_field:
        return text_field
    return ""


def is_image_generation_arguments(
        text: str,
        *,
        role: str = "",
        content_type: str = "",
) -> bool:
    """Return whether an assistant code message contains image tool arguments."""
    if str(role or "").strip().lower() != "assistant":
        return False
    if str(content_type or "").strip().lower() != "code":
        return False
    try:
        arguments = json.loads(str(text or "").strip())
    except (TypeError, ValueError):
        return False
    if not isinstance(arguments, dict):
        return False
    prompt = arguments.get("prompt")
    instructions = arguments.get("instructions")
    has_prompt = any(
        isinstance(value, str) and bool(value.strip())
        for value in (prompt, instructions)
    )
    has_image_options = any(
        field in arguments
        for field in (
            "size",
            "n",
            "transparent_background",
            "is_style_transfer",
            "referenced_image_ids",
        )
    )
    # Some upstream responses omit the prompt and only retain these two tool arguments.
    return bool(
        (has_prompt and has_image_options)
        or ("size" in arguments and "n" in arguments)
    )


def strip_history(text: str, history_text: str = "") -> str:
    text = str(text or "")
    history_text = str(history_text or "")
    while history_text and text.startswith(history_text):
        text = text[len(history_text):]
    return text


def sanitize_output_text(text: str) -> str:
    text = str(text or "")

    def is_internal_annotation_part(part: str) -> bool:
        value = part.strip()
        if not value:
            return True
        lower = value.lower()
        return bool(
            re.fullmatch(r"turn\d+[a-z]*\d*", lower)
            or re.fullmatch(r"turn\d+\w*", lower)
            or lower.startswith(("turn", "source", "sources"))
        )

    def readable_annotation_part(parts: list[str]) -> str:
        for part in parts:
            value = part.strip()
            if value and not is_internal_annotation_part(value):
                return value
        return ""

    def replace_annotation(match: re.Match[str]) -> str:
        payload = match.group(1)
        parts = [part.strip() for part in payload.split("\ue202")]
        kind = (parts[0] if parts else "").lower()
        data = parts[1:]
        if kind == "url":
            label = data[0] if data else ""
            url = data[1] if len(data) > 1 else ""
            if label and url.startswith(("http://", "https://")):
                return f"{label} ({url})"
            return label or url
        if kind == "cite":
            return readable_annotation_part(data)
        return readable_annotation_part(data)

    # ChatGPT web sometimes returns rich annotation markers using private-use
    # characters. API clients cannot render those. Preserve readable labels
    # from entity/link annotations, while removing internal citation pointers.
    text = re.sub(r"\ue200([^\ue201]*)\ue201", replace_annotation, text)
    text = re.sub(r"\ue200[^\ue201]*$", "", text)
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    return text


def assistant_raw_text(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    for candidate in (event, event.get("v")):
        if not isinstance(candidate, dict):
            continue
        message = candidate.get("message")
        if not isinstance(message, dict):
            continue
        role = str((message.get("author") or {}).get("role") or "").strip().lower()
        if role != "assistant":
            continue
        text = assistant_message_text(message)
        if text:
            return strip_history(text, history_text)
    return apply_text_patch(event, current_text, history_text)


def assistant_text(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    return sanitize_output_text(assistant_raw_text(event, current_text, history_text))


def event_assistant_text(event: dict[str, Any], history_text: str = "") -> str:
    for candidate in (event, event.get("v")):
        if not isinstance(candidate, dict):
            continue
        message = candidate.get("message")
        if isinstance(message, dict) and (message.get("author") or {}).get("role") == "assistant":
            return strip_history(assistant_message_text(message), history_text)
    return ""


def apply_text_patch(event: dict[str, Any], current_text: str = "", history_text: str = "") -> str:
    if event.get("p") in {"/message/content/parts/0", "/message/content/text"}:
        return apply_patch_op(event, current_text, history_text)

    operations = event.get("v")
    if isinstance(operations, str) and current_text and not event.get("p") and not event.get("o"):
        return current_text + operations

    if event.get("o") == "patch" and isinstance(operations, list):
        text = current_text
        for item in operations:
            if isinstance(item, dict):
                text = apply_text_patch(item, text, history_text)
        return text

    if not isinstance(operations, list):
        return current_text

    text = current_text
    for item in operations:
        if isinstance(item, dict):
            text = apply_text_patch(item, text, history_text)
    return text


def apply_patch_op(operation: dict[str, Any], current_text: str, history_text: str = "") -> str:
    op = operation.get("o")
    value = str(operation.get("v") or "")
    if op == "append":
        return current_text + value
    if op == "replace":
        return strip_history(value, history_text)
    return current_text


def add_unique(values: list[str], candidates: list[str]) -> None:
    for candidate in candidates:
        if candidate and candidate not in values:
            values.append(candidate)


FILE_SERVICE_ID_RE = re.compile(r"file-service://([A-Za-z0-9_-]+)")
FILE_ID_RE = re.compile(r"\b(file[-_](?!service\b)[A-Za-z0-9_-]+)\b")
# 真正的图片文件 ID 格式：file_00000000 + 24位十六进制字符（共32字符）
# 用于过滤非图片文件 ID（如 file_upload_business_upsell）
REAL_IMAGE_FILE_ID_RE = re.compile(r"\bfile_00000000[a-f0-9]{24}\b")
SEDIMENT_ID_RE = re.compile(r"sediment://([A-Za-z0-9_-]+)")


def extract_conversation_ids(payload: str) -> tuple[str, list[str], list[str]]:
    conversation_match = re.search(r'"conversation_id"\s*:\s*"([^"]+)"', payload)
    conversation_id = conversation_match.group(1) if conversation_match else ""
    file_ids: list[str] = []
    # Negative lookahead excludes "file-service" (URI prefix, not a real id).
    add_unique(file_ids, FILE_SERVICE_ID_RE.findall(payload))
    # 只提取真正的图片文件 ID（file_00000000... 格式），过滤非图片文件 ID（如 file_upload_business_upsell）
    add_unique(file_ids, REAL_IMAGE_FILE_ID_RE.findall(payload))
    sediment_ids = SEDIMENT_ID_RE.findall(payload)
    return conversation_id, file_ids, sediment_ids


def is_image_tool_event(event: dict[str, Any]) -> bool:
    value = event.get("v")
    message = event.get("message") or (value.get("message") if isinstance(value, dict) else None)
    if not isinstance(message, dict):
        return False
    metadata = message.get("metadata") or {}
    author = message.get("author") or {}
    content = message.get("content") or {}
    if author.get("role") != "tool":
        return False
    if metadata.get("async_task_type") == "image_gen":
        return True
    if content.get("content_type") != "multimodal_text":
        return False
    return any(
        isinstance(part, dict) and (
                part.get("content_type") == "image_asset_pointer"
                or str(part.get("asset_pointer") or "").startswith(("file-service://", "sediment://"))
        )
        for part in content.get("parts") or []
    )


def _is_user_message_event(event: dict[str, Any]) -> bool:
    """检查事件是否来自 user 角色消息。"""
    value = event.get("v")
    message = event.get("message") or (value.get("message") if isinstance(value, dict) else None)
    if isinstance(message, dict):
        author = message.get("author") or {}
        if str(author.get("role") or "").strip().lower() == "user":
            return True
    return False


def update_conversation_state(
        state: ConversationState,
        payload: str,
        event: dict[str, Any] | None = None,
        message_text: str = "",
        classify_terminal_text_as_image_failure: bool = True,
) -> None:
    conversation_id, file_ids, sediment_ids = extract_conversation_ids(payload)
    if conversation_id and not state.conversation_id:
        state.conversation_id = conversation_id
    # Generated ids must come from an explicit tool message or a tool patch.
    # tool_invoked also covers later assistant references, so it is not an
    # output boundary by itself.
    is_patch_event = isinstance(event, dict) and event.get("o") == "patch"
    is_user_msg = isinstance(event, dict) and _is_user_message_event(event)
    event_facts = extract_message_facts(event) if isinstance(event, dict) else {}
    event_role = str(event_facts.get("role") or state.message_facts.get("role") or "").strip().lower()
    has_asset_pointer = (
        "asset_pointer" in payload
        or "file-service://" in payload
        or "sediment://" in payload
    )
    image_context = (
        (isinstance(event, dict) and is_image_tool_event(event))
        or (is_patch_event and not is_user_msg and event_role == "tool" and has_asset_pointer)
    )
    if image_context:
        add_unique(state.file_ids, file_ids)
        add_unique(state.sediment_ids, sediment_ids)
    if not isinstance(event, dict):
        return
    state.conversation_id = str(event.get("conversation_id") or state.conversation_id)
    value = event.get("v")
    if isinstance(value, dict):
        state.conversation_id = str(value.get("conversation_id") or state.conversation_id)
    if event.get("type") == "moderation":
        moderation = event.get("moderation_response")
        if isinstance(moderation, dict) and moderation.get("blocked") is True:
            state.blocked = True
    if event.get("type") == "server_ste_metadata":
        metadata = event.get("metadata")
        if isinstance(metadata, dict):
            if isinstance(metadata.get("tool_invoked"), bool):
                state.tool_invoked = metadata["tool_invoked"]
            state.turn_use_case = str(metadata.get("turn_use_case") or state.turn_use_case)

    facts = event_facts
    if facts.get("blocked") is True:
        state.blocked = True
    if facts.get("turn_use_case"):
        state.turn_use_case = str(facts["turn_use_case"])
    if facts.get("async_task_type"):
        state.async_task_type = str(facts["async_task_type"])
    if facts.get("message_type"):
        state.message_type = str(facts["message_type"])
    role = str(facts.get("role") or "").strip().lower()
    current_role = str(state.message_facts.get("role") or "").strip().lower()
    message_id = str(facts.get("message_id") or "").strip()
    current_message_id = str(state.message_facts.get("message_id") or "").strip()
    should_update_facts = False
    if role in {"assistant", "tool"}:
        if (
            (current_role and current_role != role)
            or (message_id and current_message_id and message_id != current_message_id)
        ):
            state.message_facts = {}
        should_update_facts = True
    elif not role and current_role in {"assistant", "tool"}:
        should_update_facts = True
    if should_update_facts:
        accumulated_codes = set(state.message_facts.get("codes") or ())
        state.message_facts.update(facts)
        incoming_codes = set(facts.get("codes") or ())
        if accumulated_codes or incoming_codes:
            state.message_facts["codes"] = accumulated_codes.union(incoming_codes)

    current_role = str(state.message_facts.get("role") or "").strip().lower()
    current_content_type = str(state.message_facts.get("content_type") or "").strip().lower()
    current_status = str(state.message_facts.get("status") or "").strip().lower()
    current_end_turn = state.message_facts.get("end_turn")
    is_terminal = current_end_turn is True or (
        current_end_turn is None and is_terminal_message_status(current_status)
    )
    if is_terminal and is_image_generation_arguments(
        message_text,
        role=current_role,
        content_type=current_content_type,
    ):
        state.terminal_tool_arguments = {
            "text": str(message_text or "").strip(),
            "content_type": current_content_type,
            "status": current_status,
            "end_turn": current_end_turn is True,
        }

    if image_context or state.tool_invoked is True:
        state.terminal_tool_arguments = None

    has_image_output = bool(state.file_ids or state.sediment_ids)
    if has_image_output:
        state.failure = None
        state.terminal_tool_arguments = None
        return

    candidate_failure = classify_message_facts(
        role=str(state.message_facts.get("role") or ""),
        content_type=str(state.message_facts.get("content_type") or ""),
        status=str(state.message_facts.get("status") or ""),
        end_turn=bool(state.message_facts.get("end_turn")),
        is_error=bool(state.message_facts.get("is_error")),
        blocked=state.blocked,
        has_image_output=False,
        has_text=bool(state.message_facts.get("has_text")),
        turn_use_case=str(state.message_facts.get("turn_use_case") or state.turn_use_case),
        async_task_type=str(state.message_facts.get("async_task_type") or ""),
        message_type=str(state.message_facts.get("message_type") or ""),
        codes=state.message_facts.get("codes") or (),
        raw_detail=message_text or state.raw_text,
    )
    if (
        candidate_failure is not None
        and candidate_failure.code == "upstream_text_reply"
        and not classify_terminal_text_as_image_failure
    ):
        candidate_failure = None
    if candidate_failure is not None:
        # Explicit codes outrank terminal text; terminal text only replaces a
        # generic tool error from the same turn.
        state.failure = merge_message_failure(state.failure, candidate_failure)


def conversation_base_event(event_type: str, state: ConversationState, **extra: Any) -> dict[str, Any]:
    return {
        "type": event_type,
        "text": state.text,
        "conversation_id": state.conversation_id,
        "file_ids": list(state.file_ids),
        "sediment_ids": list(state.sediment_ids),
        "blocked": state.blocked,
        "tool_invoked": state.tool_invoked,
        "turn_use_case": state.turn_use_case,
        "async_task_type": state.async_task_type,
        "message_type": state.message_type,
        "message_role": str(state.message_facts.get("role") or ""),
        "content_type": str(state.message_facts.get("content_type") or ""),
        "_terminal_tool_arguments": (
            dict(state.terminal_tool_arguments)
            if state.terminal_tool_arguments is not None else None
        ),
        "_image_failure": state.failure,
        **extra,
    }


def iter_conversation_payloads(
        payloads: Iterator[str],
        history_text: str = "",
        history_messages: list[str] | None = None,
        *,
        classify_terminal_text_as_image_failure: bool = True,
) -> Iterator[dict[str, Any]]:
    state = ConversationState()
    history_messages = history_messages or []
    history_index = 0
    for payload in payloads:
        # print(f"[upstream_sse] {payload}", flush=True)
        if not payload:
            continue
        if payload == "[DONE]":
            yield conversation_base_event("conversation.done", state, done=True)
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            update_conversation_state(state, payload)
            yield conversation_base_event("conversation.raw", state, payload=payload)
            continue
        if not isinstance(event, dict):
            yield conversation_base_event("conversation.event", state, raw=event)
            continue
        next_raw_text = assistant_raw_text(event, state.raw_text, history_text)
        update_conversation_state(
            state,
            payload,
            event,
            next_raw_text,
            classify_terminal_text_as_image_failure=classify_terminal_text_as_image_failure,
        )
        if history_index < len(history_messages) and event_assistant_text(event, history_text) == history_messages[history_index]:
            history_index += 1
            state.raw_text = ""
            state.text = ""
            continue
        next_text = sanitize_output_text(next_raw_text)
        state.raw_text = next_raw_text
        if next_text != state.text:
            delta = next_text[len(state.text):] if next_text.startswith(state.text) else next_text
            state.text = next_text
            yield conversation_base_event("conversation.delta", state, raw=event, delta=delta)
            continue
        yield conversation_base_event("conversation.event", state, raw=event)


def conversation_events(
    backend: OpenAIBackendAPI,
    messages: list[dict[str, Any]] | None = None,
    model: str = "auto",
    prompt: str = "",
    images: list[str] | None = None,
    size: str | None = None,
    quality: str = "auto",
    output_format: object = None,
    frame_count: object = MISSING_IMAGE_OPTION,
    frame_duration_ms: object = MISSING_IMAGE_OPTION,
    thinking_effort: str = "",
) -> Iterator[dict[str, Any]]:
    normalized = normalize_messages(messages or ([{"role": "user", "content": prompt}] if prompt else []))
    image_model = is_supported_image_model(model)
    history_text = "" if image_model else assistant_history_text(normalized)
    history_messages = [] if image_model else assistant_history_messages(normalized)
    final_prompt = prompt_with_global_system(
        build_image_prompt(
            prompt,
            size,
            quality,
            output_format,
            frame_count,
            frame_duration_ms,
        )
    ) if image_model else prompt
    payloads = backend.stream_conversation(
        messages=normalized,
        model=model,
        prompt=final_prompt,
        images=images if image_model else None,
        system_hints=["picture_v2"] if image_model else None,
        thinking_effort=thinking_effort if not image_model else "",
    )
    yield from iter_conversation_payloads(
        payloads,
        history_text,
        history_messages,
        classify_terminal_text_as_image_failure=image_model,
    )


def _is_account_auth_failure(exc: Exception) -> bool:
    failure = classify_image_exception(exc)
    if failure.capability != "auth":
        return False
    setattr(exc, "failure", failure)
    return True
