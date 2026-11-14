"""OpenAI backend API client (package facade)."""

from ._constants import (
    CODEX_IMAGE_MODEL as CODEX_IMAGE_MODEL,
    CODEX_RESPONSES_INSTRUCTIONS as CODEX_RESPONSES_INSTRUCTIONS,
    CODEX_RESPONSES_MODEL as CODEX_RESPONSES_MODEL,
    ChatRequirements as ChatRequirements,
    DEFAULT_CLIENT_BUILD_NUMBER as DEFAULT_CLIENT_BUILD_NUMBER,
    DEFAULT_CLIENT_VERSION as DEFAULT_CLIENT_VERSION,
    DEFAULT_POW_SCRIPT as DEFAULT_POW_SCRIPT,
    EDITABLE_ASSET_POINTER_RE as EDITABLE_ASSET_POINTER_RE,
    EDITABLE_FILE_CLIENT_BUILD_NUMBER as EDITABLE_FILE_CLIENT_BUILD_NUMBER,
    EDITABLE_FILE_CLIENT_VERSION as EDITABLE_FILE_CLIENT_VERSION,
    EDITABLE_FILE_MODEL as EDITABLE_FILE_MODEL,
    EDITABLE_FILE_POLL_INTERVAL_SECS as EDITABLE_FILE_POLL_INTERVAL_SECS,
    EDITABLE_FILE_PPT_OUTPUT_DIR as EDITABLE_FILE_PPT_OUTPUT_DIR,
    EDITABLE_FILE_PPT_PROMPT as EDITABLE_FILE_PPT_PROMPT,
    EDITABLE_FILE_PSD_OUTPUT_DIR as EDITABLE_FILE_PSD_OUTPUT_DIR,
    EDITABLE_FILE_PSD_PROMPT as EDITABLE_FILE_PSD_PROMPT,
    EDITABLE_FILE_THINKING_EFFORT as EDITABLE_FILE_THINKING_EFFORT,
    EDITABLE_PPT_EXPORT_FILE_RE as EDITABLE_PPT_EXPORT_FILE_RE,
    EDITABLE_PPT_MIME_TYPES as EDITABLE_PPT_MIME_TYPES,
    EDITABLE_PSD_EXPORT_FILE_RE as EDITABLE_PSD_EXPORT_FILE_RE,
    EDITABLE_PSD_MIME_TYPES as EDITABLE_PSD_MIME_TYPES,
    EDITABLE_ZIP_MIME_TYPES as EDITABLE_ZIP_MIME_TYPES,
    EditableFileArtifact as EditableFileArtifact,
    EditableFileExportResult as EditableFileExportResult,
    FILE_ID_RE as FILE_ID_RE,
    FILE_SERVICE_ID_RE as FILE_SERVICE_ID_RE,
    HTTP_TIMING_INFOS as HTTP_TIMING_INFOS,
    IMAGE_POLL_SETTLE_SECS as IMAGE_POLL_SETTLE_SECS,
    MemoryStateInvalidError as MemoryStateInvalidError,
    REAL_IMAGE_FILE_ID_RE as REAL_IMAGE_FILE_ID_RE,
    SEARCH_CITATION_RE as SEARCH_CITATION_RE,
    SEARCH_CONVERSATION_ID_RE as SEARCH_CONVERSATION_ID_RE,
    SEARCH_DONE_STATUS as SEARCH_DONE_STATUS,
    SEARCH_IMAGE_GROUP_RE as SEARCH_IMAGE_GROUP_RE,
    SEARCH_INTERNAL_BLOCK_RE as SEARCH_INTERNAL_BLOCK_RE,
    SEARCH_MODEL as SEARCH_MODEL,
    SEARCH_POLL_INTERVAL_SECS as SEARCH_POLL_INTERVAL_SECS,
    SEARCH_URL_RE as SEARCH_URL_RE,
    SEDIMENT_ID_RE as SEDIMENT_ID_RE,
    _delta_ms as _delta_ms,
    _ms_from_seconds as _ms_from_seconds,
    _response_http_timing as _response_http_timing,
)
from ._core import CoreMixin
from ._memory import MemoryMixin
from ._conversation_mgmt import ConversationMgmtMixin
from ._search import SearchMixin
from ._editable import EditableMixin
from ._image_results import ImageResultsMixin
from ._codex import CodexMixin

import base64 as base64
import json as json
import mimetypes as mimetypes
import os as os
import random as random
import re as re
import threading as threading
import time as time
import urllib.error  # noqa: F401
import urllib.request  # noqa: F401
from concurrent.futures import ThreadPoolExecutor as ThreadPoolExecutor
from dataclasses import dataclass as dataclass
from io import BytesIO as BytesIO
from pathlib import Path as Path
from collections.abc import Callable as Callable
from typing import (
    Any as Any,
    Dict as Dict,
    Iterator as Iterator,
    Optional as Optional,
)
from urllib.parse import (
    unquote as unquote,
    urlparse as urlparse,
)
from curl_cffi import (
    CurlInfo as CurlInfo,
    CurlOpt as CurlOpt,
    requests as requests,
)
from PIL import Image as Image
from services.account_service import account_service as account_service
from services.config import config as config
from services.editable_file_failure import EditableFileFailureError as EditableFileFailureError
from services.image_failure import (
    ImageDownloadError as ImageDownloadError,
    ImageFailure as ImageFailure,
    ImageFailureError as ImageFailureError,
    ImagePollTimeoutError as ImagePollTimeoutError,
    InvalidAccessTokenError as InvalidAccessTokenError,
    classify_conversation_failure as classify_conversation_failure,
    classify_image_exception as classify_image_exception,
    classify_upstream_message as classify_upstream_message,
    classify_task_failure as classify_task_failure,
    image_failure as image_failure,
    is_terminal_message_status as is_terminal_message_status,
    merge_message_failure as merge_message_failure,
    structured_upstream_codes as structured_upstream_codes,
    terminal_assistant_text as terminal_assistant_text,
)
from services.protocol.reasoning import normalize_thinking_effort as normalize_thinking_effort
from services.proxy_service import (
    ProxyRuntimeProfile as ProxyRuntimeProfile,
    proxy_settings as proxy_settings,
)
from utils.file_names import sanitize_public_filename as sanitize_public_filename
from utils.helper import (
    UpstreamHTTPError as UpstreamHTTPError,
    ensure_ok as ensure_ok,
    iter_sse_payloads as iter_sse_payloads,
    new_uuid as new_uuid,
    split_image_model as split_image_model,
)
from utils.diagnostics import diagnostic_excerpt as diagnostic_excerpt
from utils.log import logger as logger
from utils.pow import (
    build_legacy_requirements_token as build_legacy_requirements_token,
    build_proof_token as build_proof_token,
    parse_pow_resources as parse_pow_resources,
)
from utils.turnstile import solve_turnstile_token as solve_turnstile_token

class OpenAIBackendAPI(
    CoreMixin,
    MemoryMixin,
    ConversationMgmtMixin,
    SearchMixin,
    EditableMixin,
    ImageResultsMixin,
    CodexMixin,
):
    """ChatGPT Web 后端封装。

    说明：
    - 传入 `access_token` 时，聊天和模型列表都会走已登录链路
      例如 `/backend-api/sentinel/chat-requirements`、`/backend-api/conversation`
    - 不传 `access_token` 时，会走未登录链路
      例如 `/backend-anon/sentinel/chat-requirements`、`/backend-anon/conversation`
    - `stream_conversation()` 是底层统一流式入口
    - 协议兼容转换放在 `services.protocol`
    """
