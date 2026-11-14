"""Protocol conversation pipeline (package facade)."""

from ._core import (
    ConversationRequest as ConversationRequest,
    ConversationState as ConversationState,
    FILE_ID_RE as FILE_ID_RE,
    FILE_SERVICE_ID_RE as FILE_SERVICE_ID_RE,
    ImageOutput as ImageOutput,
    MISSING_IMAGE_OPTION as MISSING_IMAGE_OPTION,
    REAL_IMAGE_FILE_ID_RE as REAL_IMAGE_FILE_ID_RE,
    SEDIMENT_ID_RE as SEDIMENT_ID_RE,
    SUPPORTED_IMAGE_OUTPUT_FORMATS as SUPPORTED_IMAGE_OUTPUT_FORMATS,
    _IMAGE_OUTPUT_FORMAT_ALIASES as _IMAGE_OUTPUT_FORMAT_ALIASES,
    _is_account_auth_failure as _is_account_auth_failure,
    _is_user_message_event as _is_user_message_event,
    _prompt_option_value as _prompt_option_value,
    add_unique as add_unique,
    apply_patch_op as apply_patch_op,
    apply_text_patch as apply_text_patch,
    assistant_history_messages as assistant_history_messages,
    assistant_history_text as assistant_history_text,
    assistant_message_text as assistant_message_text,
    assistant_raw_text as assistant_raw_text,
    assistant_text as assistant_text,
    build_image_prompt as build_image_prompt,
    conversation_base_event as conversation_base_event,
    conversation_events as conversation_events,
    convert_image_bytes as convert_image_bytes,
    count_message_image_tokens as count_message_image_tokens,
    count_message_text_tokens as count_message_text_tokens,
    count_message_tokens as count_message_tokens,
    count_text_tokens as count_text_tokens,
    encode_images as encode_images,
    encoding_for_model as encoding_for_model,
    event_assistant_text as event_assistant_text,
    extract_conversation_ids as extract_conversation_ids,
    format_image_result as format_image_result,
    is_image_generation_arguments as is_image_generation_arguments,
    is_image_tool_event as is_image_tool_event,
    is_token_invalid_error as is_token_invalid_error,
    iter_conversation_payloads as iter_conversation_payloads,
    message_text as message_text,
    normalize_image_output_format as normalize_image_output_format,
    normalize_messages as normalize_messages,
    prompt_with_global_system as prompt_with_global_system,
    sanitize_output_text as sanitize_output_text,
    save_image_bytes as save_image_bytes,
    strip_history as strip_history,
    update_conversation_state as update_conversation_state,
    validate_gif_request as validate_gif_request,
    validate_image_output_request as validate_image_output_request,
)
from ._monitor import (
    _IMAGE_PROGRESS_DURATION_KEYS as _IMAGE_PROGRESS_DURATION_KEYS,
    _IMAGE_PROGRESS_STAGE_EVENTS as _IMAGE_PROGRESS_STAGE_EVENTS,
    _backend_egress_data as _backend_egress_data,
    _backend_http_timing_data as _backend_http_timing_data,
    _backend_image_result_timing_data as _backend_image_result_timing_data,
    _capture_failed_image_attempts as _capture_failed_image_attempts,
    _download_image_bytes_with_monitor as _download_image_bytes_with_monitor,
    _elapsed_ms as _elapsed_ms,
    _image_progress_callback_with_monitor as _image_progress_callback_with_monitor,
    _monitor_image_stage as _monitor_image_stage,
    _proxy_hash as _proxy_hash,
    _resolve_image_urls_with_monitor as _resolve_image_urls_with_monitor,
)
from ._text import (
    _generate_single_svg as _generate_single_svg,
    _remember_text_account as _remember_text_account,
    _text_account_email as _text_account_email,
    append_svg_prompt as append_svg_prompt,
    collect_text as collect_text,
    extract_svg_document_from_text as extract_svg_document_from_text,
    stream_text_deltas as stream_text_deltas,
    text_backend as text_backend,
)
from ._image_stream import (
    _cleanup_image_conversations_after_success as _cleanup_image_conversations_after_success,
    _codex_response_images as _codex_response_images,
    _failure_raw_text as _failure_raw_text,
    _get_detailed_failure_from_tasks as _get_detailed_failure_from_tasks,
    _image_result_output_from_urls as _image_result_output_from_urls,
    _image_stream_timeout_error as _image_stream_timeout_error,
    _image_stream_timeout_task_diagnostics as _image_stream_timeout_task_diagnostics,
    _recover_after_image_stream_timeout as _recover_after_image_stream_timeout,
    _recover_image_conversation_id as _recover_image_conversation_id,
    stream_codex_image_outputs as stream_codex_image_outputs,
    stream_image_outputs as stream_image_outputs,
)
from ._runner import (
    _generate_single_image as _generate_single_image,
    _select_image_pool_error as _select_image_pool_error,
    stream_image_outputs_with_pool as stream_image_outputs_with_pool,
)
from ._output import (
    _image_stream_payload as _image_stream_payload,
    collect_image_outputs as collect_image_outputs,
    stream_image_chunks as stream_image_chunks,
)

import base64 as base64
import hashlib as hashlib
import io as io
import json as json
import re as re
import tiktoken as tiktoken
import time as time

from PIL import (
    Image as Image,
    ImageOps as ImageOps,
)
from concurrent.futures import (
    TimeoutError as FuturesTimeoutError,  # noqa: F401
    ThreadPoolExecutor as ThreadPoolExecutor,
    as_completed as as_completed,
    wait as wait,
)
from curl_cffi.requests import (
    exceptions as curl_exceptions,  # noqa: F401
)
from dataclasses import (
    dataclass as dataclass,
    field as field,
    replace as replace,
)
from fastapi import (
    HTTPException as HTTPException,
)
from services.account_service import (
    ImageAccountSelectionError as ImageAccountSelectionError,
    account_service as account_service,
)
from services.config import (
    config as config,
)
from services.image_failure import (
    ImageDownloadError as ImageDownloadError,
    ImageFailure as ImageFailure,
    ImageFailureError as ImageFailureError,
    ImageGenerationError as ImageGenerationError,
    classify_conversation_failure as classify_conversation_failure,
    classify_image_exception as classify_image_exception,
    classify_message_facts as classify_message_facts,
    classify_task_failure as classify_task_failure,
    classify_upstream_message as classify_upstream_message,
    extract_message_facts as extract_message_facts,
    image_failure as image_failure,
    image_failure_priority as image_failure_priority,
    is_terminal_message_status as is_terminal_message_status,
    merge_message_failure as merge_message_failure,
    public_image_error_message as public_image_error_message,
    terminal_assistant_text as terminal_assistant_text,
)
from services.image_storage_service import (
    image_media_type as image_media_type,
    image_storage_service as image_storage_service,
)
from services.image_upscale_service import (
    upscale_image_if_needed as upscale_image_if_needed,
)
from services.openai_backend_api import (
    OpenAIBackendAPI as OpenAIBackendAPI,
)
from services.proxy_service import (
    ImageEgressDeadlineError as ImageEgressDeadlineError,
    proxy_settings as proxy_settings,
)
from services.realtime_monitor_service import (
    realtime_monitor_service as realtime_monitor_service,
)
from typing import (
    Any as Any,
    Callable as Callable,
    Iterable as Iterable,
    Iterator as Iterator,
)
from utils.diagnostics import (
    diagnostic_excerpt as diagnostic_excerpt,
)
from utils.helper import (
    IMAGE_MODELS as IMAGE_MODELS,
    UpstreamHTTPError as UpstreamHTTPError,
    extract_image_from_message_content as extract_image_from_message_content,
    is_codex_image_model as is_codex_image_model,
    is_supported_image_model as is_supported_image_model,
    split_image_model as split_image_model,
)
from utils.image_tokens import (
    count_image_content_tokens as count_image_content_tokens,
    image_output_metadata as image_output_metadata,
    image_size_from_bytes as image_size_from_bytes,
)
from utils.log import (
    logger as logger,
)
from utils.svg import (
    svg_dimensions as svg_dimensions,
)
