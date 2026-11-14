import re

from curl_cffi import CurlInfo
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Dict,
    Optional,
)

@dataclass
class ChatRequirements:
    """保存一次对话请求所需的 sentinel token。"""
    token: str
    proof_token: str = ""
    turnstile_token: str = ""
    so_token: str = ""
    raw_finalize: Optional[Dict[str, Any]] = None


class MemoryStateInvalidError(ValueError):
    """The upstream m3m field exists but does not contain a boolean state."""


DEFAULT_CLIENT_VERSION = "prod-a194cd50d4416d3c0b47c740f206b12ce60f5887"
DEFAULT_CLIENT_BUILD_NUMBER = "6708908"
DEFAULT_POW_SCRIPT = "https://chatgpt.com/backend-api/sentinel/sdk.js"
CODEX_IMAGE_MODEL = "codex-gpt-image-2"
CODEX_RESPONSES_MODEL = "gpt-5.5"
SEARCH_MODEL = "gpt-5-5"
SEARCH_POLL_INTERVAL_SECS = 3.0
SEARCH_DONE_STATUS = {"finished_successfully", "finished_partial_completion", "stop"}
SEARCH_CONVERSATION_ID_RE = re.compile(r'"conversation_id"\s*:\s*"([^"]+)"')
SEARCH_URL_RE = re.compile(r"https?://[^\s\"'<>）)\]}]+")
SEARCH_CITATION_RE = re.compile(r"\ue200cite\ue202([^\ue201]*)\ue201")
SEARCH_IMAGE_GROUP_RE = re.compile(r"\ue200image_group\ue202([^\ue201]*)\ue201")
SEARCH_INTERNAL_BLOCK_RE = re.compile(r"\ue200(?!cite\ue202|image_group\ue202)[a-zA-Z0-9_]+\ue202[^\ue201]*\ue201")
EDITABLE_FILE_MODEL = "gpt-5-5-thinking"
EDITABLE_FILE_THINKING_EFFORT = "extended"
EDITABLE_FILE_POLL_INTERVAL_SECS = 5.0
EDITABLE_FILE_CLIENT_VERSION = "prod-bede35f9dcd856d080e012478f0c1031faa2588e"
EDITABLE_FILE_CLIENT_BUILD_NUMBER = "6631702"
EDITABLE_FILE_PSD_OUTPUT_DIR = "data/files/psd"
EDITABLE_FILE_PPT_OUTPUT_DIR = "data/files/ppt"
EDITABLE_FILE_PPT_PROMPT = """我需要你根据用户的需求，来制作一个可以编辑的PPT，你可以使用Agent来做，你不要再继续询问用户问题，内容风格、版式、配色、内容结构和页面信息你可以自行补充并直接执行。整体的流程如下：
1. 用生图的方式，帮我生成一个精美的产品介绍ppt，5-6个页面
2. 帮我把以上涉及到的所有图像和形状素材拆分成单独png，每个素材单独一张图片，不要有遗漏，让我可以直接在ppt里拼接素材还原，不要文字
3. 利用以上所有图片和形状素材，帮我还原你第一次生成的展示ppt，我需要是可编辑的ppt格式，主要部分需要你单独还原插入，文字需要可以编辑
最后只需要给我生成一个PPT文件，以及生成中遇到的各种素材压缩包zip文件就行。"""
EDITABLE_FILE_PSD_PROMPT = "帮我生成这个图像，把这张海报分成若干图像，包括背景图，每个元素不要改位置，这样子我可以直接在 平时里无需拖动，底色为白色，不要伪透明底。再帮我将以上拆分的图像拼合成一个psd文件，去除白色底，不要改变每个图层的相应位置，保留每个元素所在图层的相应位置，保留每个元素的图层，最后只需要给我输出psd文件，以及每个图层的zip文件"
EDITABLE_ASSET_POINTER_RE = re.compile(r"(?:file-service|sediment)://([A-Za-z0-9_-]+)")

HTTP_TIMING_INFOS = (
    CurlInfo.NAMELOOKUP_TIME,
    CurlInfo.CONNECT_TIME,
    CurlInfo.APPCONNECT_TIME,
    CurlInfo.PRETRANSFER_TIME,
    CurlInfo.STARTTRANSFER_TIME,
    CurlInfo.TOTAL_TIME,
)
EDITABLE_ZIP_MIME_TYPES = {"application/zip", "application/x-zip-compressed"}
EDITABLE_PSD_MIME_TYPES = {"image/vnd.adobe.photoshop", "application/vnd.adobe.photoshop"}
EDITABLE_PPT_MIME_TYPES = {
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-powerpoint",
}
EDITABLE_PSD_EXPORT_FILE_RE = re.compile(r"(?:sandbox:)?(/mnt/data/[^\s\"'\)\]]+\.(?:psd|zip))", re.IGNORECASE)
EDITABLE_PPT_EXPORT_FILE_RE = re.compile(r"(?:sandbox:)?(/mnt/data/[^\s\"'\)\]]+\.(?:pptx?|zip))", re.IGNORECASE)
FILE_SERVICE_ID_RE = re.compile(r"file-service://([A-Za-z0-9_-]+)")
FILE_ID_RE = re.compile(r"\b(file[-_](?!service\b)[A-Za-z0-9_-]+)\b")
# 真正的图片文件 ID 格式：file_00000000 + 24位十六进制字符（共32字符）
REAL_IMAGE_FILE_ID_RE = re.compile(r"\bfile_00000000[a-f0-9]{24}\b")
SEDIMENT_ID_RE = re.compile(r"sediment://([A-Za-z0-9_-]+)")
IMAGE_POLL_SETTLE_SECS = 2.0
CODEX_RESPONSES_INSTRUCTIONS = (
    "Use the image_generation tool to create exactly one image for the user's request. "
    "Return the generated image result."
)

def _ms_from_seconds(value: Any) -> int:
    try:
        seconds = float(value or 0)
    except (TypeError, ValueError):
        return 0
    if seconds <= 0:
        return 0
    return max(0, int(round(seconds * 1000)))


def _delta_ms(end_seconds: Any, start_seconds: Any) -> int:
    try:
        end_value = float(end_seconds or 0)
        start_value = float(start_seconds or 0)
    except (TypeError, ValueError):
        return 0
    if end_value <= 0 or end_value < start_value:
        return 0
    return _ms_from_seconds(end_value - start_value)


def _response_http_timing(response: Any) -> dict[str, Any]:
    infos = getattr(response, "infos", {}) or {}

    def info(name: CurlInfo) -> float:
        try:
            return float(infos.get(name) or 0)
        except Exception:
            return 0.0

    dns = info(CurlInfo.NAMELOOKUP_TIME)
    connect = info(CurlInfo.CONNECT_TIME)
    tls = info(CurlInfo.APPCONNECT_TIME)
    pretransfer = info(CurlInfo.PRETRANSFER_TIME)
    first_byte = info(CurlInfo.STARTTRANSFER_TIME)
    total = info(CurlInfo.TOTAL_TIME)
    timing: dict[str, Any] = {
        "http_dns_ms": _ms_from_seconds(dns),
        "http_tcp_ms": _delta_ms(connect, dns),
        "http_tls_ms": _delta_ms(tls, connect) if tls else 0,
        "http_wait_ms": _delta_ms(first_byte, pretransfer),
        "http_ttfb_ms": _ms_from_seconds(first_byte),
        "http_total_ms": _ms_from_seconds(total),
    }
    status_code = getattr(response, "status_code", None)
    if status_code:
        timing["http_status"] = int(status_code)
    for attr, key in (
        ("primary_ip", "http_primary_ip"),
        ("local_ip", "http_local_ip"),
        ("http_version", "http_version"),
        ("download_size", "http_download_bytes"),
        ("upload_size", "http_upload_bytes"),
    ):
        value = getattr(response, attr, None)
        if value not in (None, ""):
            timing[key] = value
    return timing


@dataclass
class EditableFileArtifact:
    attachment_id: str = ""
    file_id: str = ""
    name: str = ""
    mime_type: str = ""
    create_time: float = 0.0
    author_role: str = ""
    sandbox_path: str = ""
    message_id: str = ""


@dataclass
class EditableFileExportResult:
    conversation_id: str
    primary_path: Path
    zip_path: Path
