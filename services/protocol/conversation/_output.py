from __future__ import annotations

import time

from services.image_failure import (
    ImageGenerationError,
    image_failure,
    public_image_error_message,
)
from typing import (
    Any,
    Callable,
    Iterable,
    Iterator,
)
from utils.image_tokens import image_output_metadata

from ._core import ImageOutput

def _image_stream_payload(output: ImageOutput, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    item = {"type": event_type, **payload}
    if output.account_email:
        item["_account_email"] = output.account_email
    if output.conversation_id:
        item["_conversation_id"] = output.conversation_id
    if output.image_attempts:
        item["_image_attempts"] = [dict(attempt) for attempt in output.image_attempts]
    return item


def stream_image_chunks(
    outputs: Iterable[ImageOutput],
    event_prefix: str = "image_generation",
    usage_builder: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
    partial_images: object = 0,
) -> Iterator[dict[str, Any]]:
    prefix = str(event_prefix or "image_generation").strip() or "image_generation"
    # ChatGPT Web only gives us final image bytes here. Emitting those bytes as a
    # synthetic partial_image makes some clients display the same image twice.
    for output in outputs:
        if output.kind == "result":
            for item_index, item in enumerate(output.data):
                if not isinstance(item, dict):
                    continue
                b64_json = str(item.get("b64_json") or "").strip()
                if not b64_json:
                    continue
                completed: dict[str, Any] = {"b64_json": b64_json}
                if usage_builder:
                    usage = usage_builder([item])
                    if usage:
                        completed["usage"] = usage
                completed_payload = _image_stream_payload(output, f"{prefix}.completed", completed)
                completed_payload["_image_metadata"] = image_output_metadata([item])
                if output.image_urls:
                    completed_payload["_image_urls"] = list(output.image_urls)
                yield completed_payload
        elif output.kind == "message" and output.text:
            yield _image_stream_payload(
                output,
                f"{prefix}.failed",
                {"error": {"message": output.text, "type": "image_generation_error"}},
            )


def collect_image_outputs(
        outputs: Iterable[ImageOutput],
        result_callback: Callable[[list[dict[str, Any]]], None] | None = None,
) -> dict[str, Any]:
    created = None
    data: list[dict[str, Any]] = []
    message = ""
    progress_parts: list[str] = []
    account_email = ""
    conversation_id = ""
    image_urls: list[str] = []
    image_attempts: list[dict[str, Any]] = []
    failed_output: ImageOutput | None = None
    for output in outputs:
        created = created or output.created
        if output.account_email and not account_email:
            account_email = output.account_email
        if output.conversation_id and not conversation_id:
            conversation_id = output.conversation_id
        for attempt in output.image_attempts:
            if isinstance(attempt, dict) and attempt not in image_attempts:
                image_attempts.append(dict(attempt))
        if output.kind == "progress" and output.text:
            progress_parts.append(output.text)
        elif output.kind == "message":
            message = output.text
            if output.failure is not None and failed_output is None:
                failed_output = output
        elif output.kind == "result":
            data.extend(output.data)
            image_urls.extend(output.image_urls)
            if callable(result_callback) and output.data:
                result_callback([dict(item) for item in data])

    if failed_output is not None:
        failure = failed_output.failure or image_failure(
            "no_image_generated",
            raw_detail=failed_output.text,
        )
        raise ImageGenerationError(
            failed_output.text or public_image_error_message(failure),
            failure=failure,
            account_email=failed_output.account_email or account_email,
            conversation_id=failed_output.conversation_id or conversation_id,
            image_attempts=image_attempts,
        )

    result: dict[str, Any] = {"created": created or int(time.time()), "data": data}
    if not data:
        text = message or "".join(progress_parts).strip()
        if text:
            result["message"] = text
    if account_email:
        result["_account_email"] = account_email
    if conversation_id:
        result["_conversation_id"] = conversation_id
    if image_urls:
        result["_image_urls"] = list(dict.fromkeys(image_urls))
    if image_attempts:
        result["_image_attempts"] = image_attempts
    return result
