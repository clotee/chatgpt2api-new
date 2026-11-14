"""T-00 contract baseline — Phase 4 重构前的稳定契约网。

tests/ 按 backend.md 规则不入库，仅本地验证。覆盖 Phase 4 结构性拆分
（account_service / api 路由 / protocol）的回归面：

- 路由表面（method, path）全量快照：拆分不得增删改路由
- 认证语义：admin key 通过、错误 key 401、失败限流 429、成功清零
- 文档开关：/docs 默认关闭、env opt-in 开启
- 日志脱敏契约：password/api_key/cookie/token 字段被 mask
- 请求边界：messages/modalities/tools/base64_images/n 校验
"""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.ai import ChatCompletionRequest
from api.app import create_app
from api.support import require_identity
from services.auth_throttle import AuthFailureThrottle, auth_throttle
from utils.diagnostics import sanitize_log_value
from utils.helper import parse_image_count

_ADMIN_KEY = "test-auth-key"  # conftest.py 注入的 CHATGPT2API_AUTH_KEY


@pytest.fixture(autouse=True)
def _reset_auth_throttle():
    auth_throttle._buckets.clear()
    yield
    auth_throttle._buckets.clear()


# ---------------------------------------------------------------- routes

_EXPECTED_ROUTES = frozenset({
    ('DELETE', '/api/account-groups/{group_id}'),
    ('DELETE', '/api/accounts'),
    ('DELETE', '/api/auth/users/{key_id}'),
    ('DELETE', '/api/cpa/pools/{pool_id}'),
    ('DELETE', '/api/proxy/groups/{group_id}'),
    ('DELETE', '/api/sub2api/servers/{server_id}'),
    ('DELETE', '/v1/editable-file-tasks/{task_id}'),
    ('GET', '/api/account-groups'),
    ('GET', '/api/accounts'),
    ('GET', '/api/accounts/operations/{progress_id}'),
    ('GET', '/api/accounts/refresh/progress/{progress_id}'),
    ('GET', '/api/accounts/{account_id}'),
    ('GET', '/api/accounts/{account_id}/access-token'),
    ('GET', '/api/accounts/{account_id}/refresh-token'),
    ('GET', '/api/auth/users'),
    ('GET', '/api/backups'),
    ('GET', '/api/cpa/pools'),
    ('GET', '/api/cpa/pools/{pool_id}/files'),
    ('GET', '/api/cpa/pools/{pool_id}/import'),
    ('GET', '/api/dashboard'),
    ('GET', '/api/image-tasks'),
    ('GET', '/api/images'),
    ('GET', '/api/images/download/{image_path:path}'),
    ('GET', '/api/images/storage'),
    ('GET', '/api/logs'),
    ('GET', '/api/logs/{log_id}'),
    ('GET', '/api/model-catalog'),
    ('GET', '/api/monitor/realtime'),
    ('GET', '/api/monitor/realtime/{call_id}'),
    ('GET', '/api/prompts'),
    ('GET', '/api/proxy/runtime'),
    ('GET', '/api/proxy/view'),
    ('GET', '/api/settings'),
    ('GET', '/api/sub2api/servers'),
    ('GET', '/api/sub2api/servers/{server_id}/accounts'),
    ('GET', '/api/sub2api/servers/{server_id}/groups'),
    ('GET', '/api/sub2api/servers/{server_id}/import'),
    ('GET', '/api/system/update-status'),
    ('GET', '/api/system/update-task'),
    ('GET', '/api/third-party-apps'),
    ('GET', '/auth/status'),
    ('GET', '/files/{file_path:path}'),
    ('GET', '/image-thumbnails/{image_path:path}'),
    ('GET', '/images/{image_path:path}'),
    ('GET', '/v1/editable-file-tasks'),
    ('GET', '/v1/models'),
    ('GET', '/version'),
    ('GET', '/{full_path:path}'),
    ('PATCH', '/api/settings'),
    ('POST', '/api/account-groups'),
    ('POST', '/api/accounts'),
    ('POST', '/api/accounts/batch-update'),
    ('POST', '/api/accounts/export'),
    ('POST', '/api/accounts/group'),
    ('POST', '/api/accounts/import-cleanup'),
    ('POST', '/api/accounts/memory'),
    ('POST', '/api/accounts/oauth/finish'),
    ('POST', '/api/accounts/oauth/start'),
    ('POST', '/api/accounts/refresh'),
    ('POST', '/api/accounts/refresh-access-token'),
    ('POST', '/api/accounts/selection-preview'),
    ('POST', '/api/accounts/sync'),
    ('POST', '/api/accounts/update'),
    ('POST', '/api/accounts/{account_id}/test'),
    ('POST', '/api/admin/prompt-sources/refresh'),
    ('POST', '/api/admin/prompt-sources/{source_id}'),
    ('POST', '/api/admin/prompt-sources/{source_id}/refresh'),
    ('POST', '/api/auth/users'),
    ('POST', '/api/auth/users/{key_id}'),
    ('POST', '/api/backup/test'),
    ('POST', '/api/backups/delete'),
    ('POST', '/api/backups/run'),
    ('POST', '/api/cpa/pools'),
    ('POST', '/api/cpa/pools/{pool_id}'),
    ('POST', '/api/cpa/pools/{pool_id}/import'),
    ('POST', '/api/image-storage/sync'),
    ('POST', '/api/image-storage/test'),
    ('POST', '/api/image-tasks/edits'),
    ('POST', '/api/image-tasks/generations'),
    ('POST', '/api/image-tasks/{task_id}/resume-poll'),
    ('POST', '/api/images/delete'),
    ('POST', '/api/images/download'),
    ('POST', '/api/images/genbox-push'),
    ('POST', '/api/images/retention-cleanup'),
    ('POST', '/api/images/storage/cleanup-to-target'),
    ('POST', '/api/images/storage/compress'),
    ('POST', '/api/images/tags'),
    ('POST', '/api/logs/delete'),
    ('POST', '/api/proxy/clearance/test'),
    ('POST', '/api/proxy/defaults'),
    ('POST', '/api/proxy/groups'),
    ('POST', '/api/proxy/groups/test'),
    ('POST', '/api/proxy/nodes/import'),
    ('POST', '/api/proxy/test'),
    ('POST', '/api/settings/account-cleanup/preview'),
    ('POST', '/api/settings/account-cleanup/run'),
    ('POST', '/api/settings/retention-cleanup/preview'),
    ('POST', '/api/settings/retention-cleanup/run'),
    ('POST', '/api/sub2api/servers'),
    ('POST', '/api/sub2api/servers/{server_id}'),
    ('POST', '/api/sub2api/servers/{server_id}/import'),
    ('POST', '/api/system/update'),
    ('POST', '/auth/login'),
    ('POST', '/v1/chat/completions'),
    ('POST', '/v1/editable-file-tasks'),
    ('POST', '/v1/images/edits'),
    ('POST', '/v1/images/generations'),
    ('POST', '/v1/messages'),
    ('POST', '/v1/ppt/generations'),
    ('POST', '/v1/psd/generations'),
    ('POST', '/v1/responses'),
    ('POST', '/v1/search'),
})


def _route_surface(app) -> frozenset:
    pairs = set()
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            pairs.add((method, getattr(route, "path", str(route))))
    return frozenset(pairs)


def test_route_surface_is_stable():
    assert _route_surface(create_app()) == _EXPECTED_ROUTES


# ------------------------------------------------------------------ docs


def test_docs_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CHATGPT2API_ENABLE_DOCS", raising=False)
    client = TestClient(create_app())
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_docs_enabled_by_env_opt_in(monkeypatch):
    monkeypatch.setenv("CHATGPT2API_ENABLE_DOCS", "1")
    client = TestClient(create_app())
    assert client.get("/docs").status_code == 200


# ------------------------------------------------------------------ auth


def test_require_identity_accepts_admin_key():
    identity = require_identity(f"Bearer {_ADMIN_KEY}")
    assert identity["role"] == "admin"


def test_require_identity_rejects_wrong_key():
    with pytest.raises(HTTPException) as exc_info:
        require_identity("Bearer definitely-wrong-key")
    assert exc_info.value.status_code == 401


def test_require_identity_rejects_missing_key():
    with pytest.raises(HTTPException) as exc_info:
        require_identity(None)
    assert exc_info.value.status_code == 401


def test_auth_throttle_blocks_after_threshold():
    throttle = AuthFailureThrottle()
    for _ in range(5):
        assert throttle.retry_after("1.2.3.4", "bad-token") == 0.0
        throttle.record_failure("1.2.3.4", "bad-token")
    assert throttle.retry_after("1.2.3.4", "bad-token") > 0.0


def test_auth_throttle_success_clears_bucket():
    throttle = AuthFailureThrottle()
    for _ in range(4):
        throttle.record_failure("1.2.3.4", "bad-token")
    throttle.record_success("1.2.3.4", "bad-token")
    for _ in range(4):
        throttle.record_failure("1.2.3.4", "bad-token")
    assert throttle.retry_after("1.2.3.4", "bad-token") == 0.0


def test_auth_throttle_does_not_block_other_tokens():
    throttle = AuthFailureThrottle()
    for _ in range(6):
        throttle.record_failure("1.2.3.4", "bad-token")
    assert throttle.retry_after("1.2.3.4", "other-token") == 0.0
    assert throttle.retry_after("5.6.7.8", "bad-token") == 0.0


def test_http_auth_flow_401_then_429():
    client = TestClient(create_app())
    token = "http-wrong-token"
    codes = [
        client.get("/api/accounts", headers={"Authorization": f"Bearer {token}"}).status_code
        for _ in range(6)
    ]
    assert codes[:5] == [401] * 5
    assert codes[5] == 429


def test_http_admin_key_passes_auth_gate():
    client = TestClient(create_app())
    response = client.get("/api/accounts", headers={"Authorization": f"Bearer {_ADMIN_KEY}"})
    assert response.status_code == 200


# ------------------------------------------------------------- sanitizing


def test_log_sanitization_masks_secret_fields():
    sanitized = sanitize_log_value(
        {
            "password": "hunter2",
            "api_key": "sk-test",
            "cookie": "session=abc",
            "nested": {"access_token": "tok-123", "safe": "visible"},
        }
    )
    text = str(sanitized)
    assert "hunter2" not in text
    assert "sk-test" not in text
    assert "session=abc" not in text
    assert "tok-123" not in text
    assert "visible" in text


# ------------------------------------------------------------ input bounds


def test_chat_completion_rejects_too_many_messages():
    with pytest.raises(ValidationError):
        ChatCompletionRequest(messages=[{"role": "user", "content": "x"}] * 1001)


def test_chat_completion_accepts_boundary_messages():
    request = ChatCompletionRequest(messages=[{"role": "user", "content": "x"}] * 1000)
    assert len(request.messages) == 1000


def test_chat_completion_rejects_too_many_modalities():
    with pytest.raises(ValidationError):
        ChatCompletionRequest(modalities=["text"] * 17)


def test_parse_image_count_bounds():
    assert parse_image_count(None, maximum=4) == 1
    assert parse_image_count("3", maximum=4) == 3
    with pytest.raises(HTTPException):
        parse_image_count("0", maximum=4)
    with pytest.raises(HTTPException):
        parse_image_count("5", maximum=4)
    with pytest.raises(HTTPException):
        parse_image_count("abc", maximum=4)
