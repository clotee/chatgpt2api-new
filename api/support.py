from __future__ import annotations

import hmac
from contextvars import ContextVar
from pathlib import Path
from threading import Event, Thread

from fastapi import HTTPException, Request

from services.account_service import account_service
from services.auth_service import auth_service
from services.auth_throttle import auth_throttle
from services.config import config
from utils.log import logger

BASE_DIR = Path(__file__).resolve().parents[1]
WEB_DIST_DIR = BASE_DIR / "web_dist"

_client_ip_var: ContextVar[str] = ContextVar("chatgpt2api_client_ip", default="")


def current_client_ip() -> str:
    return _client_ip_var.get() or ""


def _resolve_client_ip(scope: dict) -> str:
    client = scope.get("client")
    host = str(client[0]) if client else ""
    if host not in {"127.0.0.1", "::1"}:
        return host
    xff = ""
    for name, value in scope.get("headers") or []:
        if name == b"x-forwarded-for":
            xff = value.decode("latin-1")
    entries = [part.strip() for part in xff.split(",") if part.strip()]
    return entries[-1] if entries else host


class ClientIPMiddleware:
    """纯 ASGI 中间件：把客户端 IP 写入 contextvar 供认证限流使用。

    仅当直接对端是 loopback（同机反向代理）时才采信 X-Forwarded-For
    最后一跳，避免客户端伪造头绕过限流。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        token = _client_ip_var.set(_resolve_client_ip(scope))
        try:
            await self.app(scope, receive, send)
        finally:
            _client_ip_var.reset(token)


def extract_bearer_token(authorization: str | None) -> str:
    scheme, _, value = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return ""
    return value.strip()


def _legacy_admin_identity(token: str) -> dict[str, object] | None:
    auth_key = str(config.auth_key or "").strip()
    if auth_key and hmac.compare_digest(token.encode("utf-8"), auth_key.encode("utf-8")):
        return {"id": "admin", "name": "管理员", "role": "admin"}
    return None


def require_identity(authorization: str | None) -> dict[str, object]:
    token = extract_bearer_token(authorization)
    client_ip = current_client_ip()
    retry_after = auth_throttle.retry_after(client_ip, token)
    if retry_after > 0:
        raise HTTPException(
            status_code=429,
            detail={"error": "认证尝试过于频繁，请稍后重试"},
            headers={"Retry-After": str(max(1, int(retry_after) + 1))},
        )
    identity = _legacy_admin_identity(token) or auth_service.authenticate(token)
    if identity is None:
        auth_throttle.record_failure(client_ip, token)
        raise HTTPException(status_code=401, detail={"error": "密钥无效或已失效，请重新登录"})
    auth_throttle.record_success(client_ip, token)
    return identity


def require_auth_key(authorization: str | None) -> None:
    require_identity(authorization)


def require_admin(authorization: str | None) -> dict[str, object]:
    identity = require_identity(authorization)
    if identity.get("role") != "admin":
        raise HTTPException(status_code=403, detail={"error": "需要管理员权限才能执行这个操作"})
    return identity


def resolve_image_base_url(request: Request) -> str:
    return config.base_url or f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"


def sanitize_cpa_pool(pool: dict | None) -> dict | None:
    if not isinstance(pool, dict):
        return None
    return {key: value for key, value in pool.items() if key != "secret_key"}


def sanitize_cpa_pools(pools: list[dict]) -> list[dict]:
    return [sanitized for pool in pools if (sanitized := sanitize_cpa_pool(pool)) is not None]


def sanitize_sub2api_server(server: dict | None) -> dict | None:
    if not isinstance(server, dict):
        return None
    sanitized = {key: value for key, value in server.items() if key not in {"password", "api_key"}}
    sanitized["has_api_key"] = bool(str(server.get("api_key") or "").strip())
    return sanitized


def sanitize_sub2api_servers(servers: list[dict]) -> list[dict]:
    return [sanitized for server in servers if (sanitized := sanitize_sub2api_server(server)) is not None]


def start_account_lifecycle_watcher(stop_event: Event) -> Thread:
    def worker() -> None:
        while not stop_event.is_set():
            try:
                pending_auth = account_service.list_pending_auth_verification_tokens()
                if pending_auth:
                    account_service.resume_pending_auth_verifications()
                expiring_tokens = account_service.list_expiring_access_tokens()
                if expiring_tokens:
                    logger.info({
                        "event": "account_watcher_renewing_expiring_access_tokens",
                        "count": len(expiring_tokens),
                    })
                    result = account_service.renew_expiring_access_tokens(expiring_tokens)
                    if result.get("errors"):
                        logger.warning({
                            "event": "account_watcher_renewal_errors",
                            "errors": result["errors"],
                        })

                limited_tokens = account_service.list_limited_tokens()
                normal_tokens = account_service.list_normal_tokens()
                if limited_tokens:
                    logger.info({
                        "event": "account_watcher_syncing_limited_accounts",
                        "limited": len(limited_tokens),
                        "skipped_healthy": len(normal_tokens),
                        "pending_auth": len(pending_auth),
                    })
                    account_service.sync_accounts_and_quota(limited_tokens)
            except Exception as exc:
                logger.error({
                    "event": "account_watcher_iteration_failed",
                    "error": str(exc),
                })
            stop_event.wait(config.refresh_account_interval_minute * 60)

    thread = Thread(target=worker, name="account-lifecycle-watcher", daemon=True)
    thread.start()
    return thread


def start_limited_account_watcher(stop_event: Event) -> Thread:
    """Compatibility alias for integrations importing the old watcher name."""
    return start_account_lifecycle_watcher(stop_event)


def resolve_web_asset(requested_path: str) -> Path | None:
    if not WEB_DIST_DIR.exists():
        return None
    clean_path = requested_path.strip("/")
    base_dir = WEB_DIST_DIR.resolve()
    candidates = [base_dir / "index.html"] if not clean_path else [
        base_dir / Path(clean_path),
        base_dir / clean_path / "index.html",
        base_dir / f"{clean_path}.html",
    ]
    for candidate in candidates:
        try:
            candidate.resolve().relative_to(base_dir)
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None
