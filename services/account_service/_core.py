from __future__ import annotations

import time
from ._primitives import AccountPrimitivesMixin
from ._shared import _CredentialGeneration
from collections import deque
from concurrent.futures import Future
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from services.account_credentials import ACCESS_TOKEN_REFRESH_SKEW_SECONDS
from services.account_operation_events import ACCOUNT_OPERATION_EVENT_LIMIT
from services.storage.base import StorageBackend, StorageRevisionConflictError
from threading import Condition, Lock
from typing import Any, Callable
from utils.diagnostics import sanitize_diagnostic_text

class AccountServiceCore(AccountPrimitivesMixin):
    """账号池服务，使用 token -> account 的 dict 保存账号。"""

    _ACCESS_TOKEN_REFRESH_SKEW_SECONDS = ACCESS_TOKEN_REFRESH_SKEW_SECONDS
    _TOKEN_REFRESH_ERROR_BACKOFF_SECONDS = 5 * 60
    _POOL_HEALTH_REFRESH_BATCH_SIZE = 10
    _IMAGE_FAILURE_REFRESH_DEDUP_SECONDS = 30
    _ACCESS_TOKEN_FINGERPRINT_LIMIT = 8
    _REFRESH_PROGRESS_COMPLETED_TTL_SECONDS = 10 * 60
    _REFRESH_PROGRESS_ACTIVE_TTL_SECONDS = 60 * 60
    _REFRESH_PROGRESS_PRUNE_INTERVAL_SECONDS = 60
    _REFRESH_PROGRESS_EVENT_LIMIT = ACCOUNT_OPERATION_EVENT_LIMIT
    _STORAGE_MUTATION_MAX_ATTEMPTS = 4
    _ACCOUNT_SNAPSHOT_TTL_SECONDS = 5.0
    _GIT_ACCOUNT_SNAPSHOT_TTL_SECONDS = 60.0
    # Operational totals only; resettable state such as invalid_count stays LWW.
    _ADDITIVE_ACCOUNT_COUNTER_FIELDS = frozenset({"success", "fail"})
    _OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
    _OAUTH_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
    _OAUTH_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    )
    _TERMINAL_REFRESH_ERROR_CODES = frozenset({
        "invalid_grant",
        "invalid_refresh_token",
        "refresh_token_invalidated",
    })
    _TERMINAL_REFRESH_MESSAGE_FRAGMENTS = ("session has ended",)

    def __init__(
        self,
        storage_backend: StorageBackend,
        *,
        proxy_reference_mutation: Callable[
            [list[tuple[object, object]], Callable[[list[str]], Any]],
            Any,
        ] | None = None,
    ):
        self.storage = storage_backend
        self._proxy_reference_mutation = proxy_reference_mutation
        self._lock = Lock()
        self._oauth_refresh_flights_lock = Lock()
        self._oauth_refresh_flights: dict[_CredentialGeneration, Future[str]] = {}
        self._memory_active_account_ids: set[str] = set()
        self._image_slot_condition = Condition(self._lock)
        self._account_snapshot_refresh_lock = Lock()
        self._index = 0
        self._persisted_accounts: dict[str, dict] = {}
        self._accounts_revision = ""
        self._accounts = self._load_accounts()
        self._account_snapshot_checked_at = time.monotonic()
        self._image_inflight: dict[str, int] = {}
        self._image_failure_refresh_lock = Lock()
        self._image_failure_refresh_active: set[str] = set()
        self._image_failure_refresh_active_scopes: dict[str, str] = {}
        self._image_failure_refresh_rerun: set[str] = set()
        self._image_failure_refresh_pending: deque[str] = deque()
        self._image_failure_refresh_pending_set: set[str] = set()
        self._image_failure_refresh_pending_scopes: dict[str, str] = {}
        self._image_failure_refresh_started_at: dict[str, float] = {}
        self._token_aliases: dict[str, str] = {}
        self._cumulative_total = self._load_cumulative_total()

    def _get_cumulative_file(self) -> Path:
        storage_path = getattr(self.storage, "file_path", None)
        if isinstance(storage_path, Path):
            return storage_path.with_name(".cumulative_total")
        from services.config import DATA_DIR
        return DATA_DIR / ".cumulative_total"

    def _load_cumulative_total(self) -> int:
        try:
            f = self._get_cumulative_file()
            if f.exists():
                return int(f.read_text().strip())
        except Exception:
            pass
        return len(self._accounts)

    def _save_cumulative_total(self) -> None:
        try:
            self._get_cumulative_file().write_text(str(self._cumulative_total))
        except Exception:
            pass

    def _normalize_loaded_accounts(
        self,
        accounts: list[Any],
        *,
        recover_interrupted_checks: bool,
    ) -> tuple[dict[str, dict], bool]:
        loaded: dict[str, dict] = {}
        used_management_ids: set[str] = set()
        changed = False
        for item in accounts:
            normalized = self._normalize_account(item)
            if normalized is None:
                changed = True
                continue
            access_token = str(normalized.get("access_token") or "").strip()
            previous = loaded.get(access_token)
            if previous is not None:
                used_management_ids.discard(
                    str(previous.get("management_id") or "").strip().lower()
                )
            management_id = self._unique_management_id(
                access_token,
                normalized.get("management_id"),
                used_management_ids,
            )
            if management_id != normalized.get("management_id"):
                normalized["management_id"] = management_id
                changed = True
            if (
                recover_interrupted_checks
                and normalized.get("last_remote_check_result") == "pending"
                and normalized.get("pending_auth_scope") != "image"
            ):
                normalized["last_remote_check_result"] = "error"
                normalized["last_remote_check_error"] = (
                    normalized.get("last_remote_check_error")
                    or "Account verification was interrupted by a service restart."
                )
                normalized["last_remote_check_error_at"] = datetime.now(timezone.utc).isoformat()
                normalized["pending_auth_remove_invalid"] = None
                normalized["pending_auth_scope"] = None
                normalized.pop("pending_auth_verification_id", None)
                changed = True
            if normalized != item:
                changed = True
            used_management_ids.add(management_id)
            loaded[normalized["access_token"]] = normalized
        return loaded, changed

    def _load_accounts(self) -> dict[str, dict]:
        last_conflict: StorageRevisionConflictError | None = None
        for _ in range(self._STORAGE_MUTATION_MAX_ATTEMPTS):
            loaded, revision, changed = self._read_accounts_snapshot(
                recover_interrupted_checks=True,
            )
            if changed:
                try:
                    result = self.storage.replace_accounts(
                        list(loaded.values()),
                        expected_revision=revision,
                    )
                except StorageRevisionConflictError as exc:
                    last_conflict = exc
                    continue
                revision = result.revision
            self._persisted_accounts = deepcopy(loaded)
            self._accounts_revision = revision
            return loaded
        assert last_conflict is not None
        raise last_conflict

    def _read_accounts_snapshot(
        self,
        *,
        recover_interrupted_checks: bool = False,
    ) -> tuple[dict[str, dict], str, bool]:
        snapshot = self.storage.load_accounts_snapshot()
        loaded, changed = self._normalize_loaded_accounts(
            snapshot.items,
            recover_interrupted_checks=recover_interrupted_checks,
        )
        return loaded, snapshot.revision, changed

    def _account_snapshot_ttl_seconds(self) -> float:
        ttl = max(0.0, float(self._ACCOUNT_SNAPSHOT_TTL_SECONDS))
        try:
            backend_type = str(self.storage.get_backend_info().get("type") or "").strip().lower()
        except Exception:
            return ttl
        if backend_type == "git":
            return max(ttl, float(self._GIT_ACCOUNT_SNAPSHOT_TTL_SECONDS))
        return ttl

    def _apply_account_view_locked(
        self,
        accounts: dict[str, dict],
        *,
        persisted_accounts: dict[str, dict],
        revision: str,
        snapshot_checked: bool = True,
        preserve_index: bool = False,
    ) -> None:
        token_rotations = self._passive_token_rotations_locked(accounts)
        self._accounts = accounts
        self._persisted_accounts = deepcopy(persisted_accounts)
        self._accounts_revision = revision
        for new_token, alias_sources in token_rotations:
            self._move_account_runtime_token_locked(new_token, alias_sources)
        if not preserve_index:
            self._index = self._index % len(self._accounts) if self._accounts else 0
        if snapshot_checked:
            self._account_snapshot_checked_at = time.monotonic()
        self._image_slot_condition.notify_all()

    def _refresh_accounts_snapshot_if_stale(
        self,
        *,
        wait_for_refresh: bool = False,
    ) -> bool:
        now = time.monotonic()
        ttl = self._account_snapshot_ttl_seconds()
        with self._lock:
            if (
                now - self._account_snapshot_checked_at
                < ttl
            ):
                return False

        if not self._account_snapshot_refresh_lock.acquire(blocking=wait_for_refresh):
            return False
        try:
            with self._lock:
                if (
                    time.monotonic() - self._account_snapshot_checked_at
                    < ttl
                ):
                    return False
                expected_revision = self._accounts_revision

            try:
                loaded, revision, _ = self._read_accounts_snapshot()
            except Exception:
                with self._lock:
                    if self._accounts_revision == expected_revision:
                        self._account_snapshot_checked_at = time.monotonic()
                return False

            with self._image_slot_condition:
                if self._accounts_revision != expected_revision:
                    return False
                self._account_snapshot_checked_at = time.monotonic()
                if revision == expected_revision:
                    return False
                self._apply_account_view_locked(
                    loaded,
                    persisted_accounts=loaded,
                    revision=revision,
                )
                return True
        finally:
            self._account_snapshot_refresh_lock.release()

    def _prune_token_aliases_locked(self) -> None:
        aliases = dict(getattr(self, "_token_aliases", {}))
        compacted: dict[str, str] = {}
        for source in aliases:
            token = source
            seen: set[str] = set()
            while token not in self._accounts and token in aliases and token not in seen:
                seen.add(token)
                token = aliases[token]
            if token in self._accounts and token != source:
                compacted[source] = token
        self._token_aliases = compacted

    def _remove_account_runtime_state_locked(self, tokens: set[str]) -> None:
        removed_tokens = {str(token or "").strip() for token in tokens if token}
        if not removed_tokens:
            return
        for token in removed_tokens:
            self._image_inflight.pop(token, None)
        self._token_aliases = {
            source: target
            for source, target in self._token_aliases.items()
            if source not in removed_tokens and target not in removed_tokens
        }
        self._index = self._index % len(self._accounts) if self._accounts else 0

    def _restore_accounts_after_save_error(self) -> None:
        fallback = deepcopy(self._persisted_accounts)
        try:
            loaded, revision, _ = self._read_accounts_snapshot()
        except Exception:
            self._apply_account_view_locked(
                fallback,
                persisted_accounts=fallback,
                revision=self._accounts_revision,
                snapshot_checked=False,
                preserve_index=True,
            )
        else:
            self._apply_account_view_locked(
                loaded,
                persisted_accounts=loaded,
                revision=revision,
                preserve_index=True,
            )

    def _save_accounts(
        self,
        *,
        expected_credential_generation: _CredentialGeneration | None = None,
        expected_memory_operation: tuple[str, str] | None = None,
        conflict_existing_tokens: set[str] | None = None,
    ) -> bool:
        last_conflict: StorageRevisionConflictError | None = None
        for attempt in range(self._STORAGE_MUTATION_MAX_ATTEMPTS):
            mutation = self._account_mutation(
                self._persisted_accounts,
                self._accounts,
                self._accounts_revision,
            )
            if not mutation.upserts and not mutation.delete_keys:
                self._prune_token_aliases_locked()
                return True
            try:
                result = self.storage.mutate_accounts(mutation)
            except StorageRevisionConflictError as exc:
                last_conflict = exc
                if attempt + 1 >= self._STORAGE_MUTATION_MAX_ATTEMPTS:
                    self._restore_accounts_after_save_error()
                    raise
                try:
                    snapshot = self.storage.load_accounts_snapshot()
                    remote, _ = self._normalize_loaded_accounts(
                        snapshot.items,
                        recover_interrupted_checks=False,
                    )
                    if conflict_existing_tokens is not None:
                        conflict_existing_tokens.update(
                            token
                            for token in self._accounts
                            if token not in self._persisted_accounts and token in remote
                        )
                    if expected_credential_generation is not None:
                        expected_access_token = expected_credential_generation[0]
                        remote_account = remote.get(expected_access_token)
                        if (
                            remote_account is None
                            or self._credential_generation(
                                expected_access_token,
                                remote_account,
                            )
                            != expected_credential_generation
                        ):
                            self._apply_account_view_locked(
                                remote,
                                persisted_accounts=remote,
                                revision=snapshot.revision,
                            )
                            return False
                    if expected_memory_operation is not None:
                        memory_token, operation_id = expected_memory_operation
                        remote_account = remote.get(memory_token)
                        if (
                            remote_account is None
                            or str(remote_account.get("memory_operation_id") or "") != operation_id
                        ):
                            self._apply_account_view_locked(
                                remote,
                                persisted_accounts=remote,
                                revision=snapshot.revision,
                            )
                            return False
                    merged = self._merge_accounts_after_conflict(
                        self._persisted_accounts,
                        self._accounts,
                        remote,
                    )
                    self._apply_account_view_locked(
                        merged,
                        persisted_accounts=remote,
                        revision=snapshot.revision,
                    )
                except Exception:
                    self._restore_accounts_after_save_error()
                    raise
                continue
            except Exception:
                self._restore_accounts_after_save_error()
                raise
            self._persisted_accounts = deepcopy(self._accounts)
            self._accounts_revision = result.revision
            self._account_snapshot_checked_at = time.monotonic()
            self._prune_token_aliases_locked()
            return True
        assert last_conflict is not None
        raise last_conflict

    def _normalize_account(self, item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None
        access_token = item.get("access_token") or item.get("accessToken") or ""
        if not access_token:
            return None
        normalized = dict(item)
        normalized.pop("accessToken", None)
        normalized["access_token"] = access_token
        normalized["management_id"] = self._normalize_management_id(
            normalized.get("management_id"),
            access_token,
        )
        normalized["access_token_fingerprints"] = self._normalize_access_token_fingerprints(
            normalized.get("access_token_fingerprints"),
            access_token,
        )
        if str(normalized.get("type") or "").strip().lower() == "codex":
            normalized["export_type"] = "codex"
            normalized.pop("type", None)
        limits_progress = normalized.get("limits_progress")
        limits_progress = limits_progress if isinstance(limits_progress, list) else []
        derived_quota, derived_restore_at, derived_unknown = self._extract_image_quota_from_limits(limits_progress)
        has_explicit_quota = self._has_value(normalized.get("quota"))
        normalized["type"] = self._normalize_account_type(normalized.get("type"))
        normalized["status"] = self._normalize_account_status(normalized.get("status"), normalized)
        normalized.pop("enabled", None)
        normalized.pop("auto_disabled", None)
        normalized["email"] = normalized.get("email") or None
        normalized["user_id"] = normalized.get("user_id") or None
        normalized["proxy"] = str(normalized.get("proxy") or "").strip()
        source_type = normalized.get("source_type")
        if not source_type and str(normalized.get("export_type") or "").strip().lower() == "codex":
            source_type = "codex"
        normalized["source_type"] = self._normalize_source_type(source_type)
        if not has_explicit_quota and derived_quota is not None:
            normalized["quota"] = derived_quota
        normalized["quota"] = self._quota_value(normalized.get("quota"), 0)
        has_explicit_quota_state = self._has_value(normalized.get("image_quota_unknown"))
        if derived_unknown is not None and not has_explicit_quota_state:
            normalized["image_quota_unknown"] = derived_unknown
        elif not has_explicit_quota_state:
            normalized["image_quota_unknown"] = True
        normalized["image_quota_unknown"] = self._bool_value(normalized.get("image_quota_unknown"), True)
        has_confirmed_quota = (
            derived_unknown is False
            or (
                bool(normalized.get("last_remote_checked_at"))
                and not normalized["image_quota_unknown"]
            )
        )
        if normalized["status"] == "正常" and (not has_confirmed_quota or normalized["quota"] == 0):
            normalized["image_quota_unknown"] = True
        elif normalized["status"] == "限流":
            normalized["quota"] = 0
            normalized["image_quota_unknown"] = False
        normalized["limits_progress"] = limits_progress
        normalized["default_model_slug"] = normalized.get("default_model_slug") or None
        if derived_restore_at and not normalized.get("restore_at"):
            normalized["restore_at"] = derived_restore_at
        normalized["restore_at"] = normalized.get("restore_at") or None
        normalized["success"] = int(normalized.get("success") or 0)
        normalized["fail"] = int(normalized.get("fail") or 0)
        normalized["invalid_count"] = int(normalized.get("invalid_count") or 0)
        normalized["last_used_at"] = normalized.get("last_used_at")
        normalized["last_invalid_at"] = normalized.get("last_invalid_at") or None
        normalized["last_refresh_error"] = normalized.get("last_refresh_error") or None
        normalized["last_refresh_error_at"] = normalized.get("last_refresh_error_at") or None
        normalized["last_remote_checked_at"] = normalized.get("last_remote_checked_at") or None
        normalized["last_remote_check_attempt_at"] = normalized.get("last_remote_check_attempt_at") or None
        normalized["last_remote_check_error"] = normalized.get("last_remote_check_error") or None
        normalized["last_remote_check_error_at"] = normalized.get("last_remote_check_error_at") or None
        normalized["last_remote_check_event"] = normalized.get("last_remote_check_event") or None
        remote_check_result = str(normalized.get("last_remote_check_result") or "").strip().lower()
        normalized["last_remote_check_result"] = (
            remote_check_result
            if remote_check_result in {"pending", "ok", "error", "invalid"}
            else None
        )
        pending_remove = normalized.get("pending_auth_remove_invalid")
        normalized["pending_auth_remove_invalid"] = (
            self._bool_value(pending_remove)
            if normalized["last_remote_check_result"] == "pending" and pending_remove is not None
            else None
        )
        pending_scope = str(normalized.get("pending_auth_scope") or "").strip().lower()
        normalized["pending_auth_scope"] = (
            "image" if pending_scope == "image" else "account"
        ) if normalized["last_remote_check_result"] == "pending" else None
        verification_id = str(normalized.get("pending_auth_verification_id") or "").strip()
        if normalized["last_remote_check_result"] == "pending" and verification_id:
            normalized["pending_auth_verification_id"] = verification_id
        else:
            normalized.pop("pending_auth_verification_id", None)

        normalized["last_token_refresh_at"] = normalized.get("last_token_refresh_at") or None
        normalized["last_token_refresh_error"] = normalized.get("last_token_refresh_error") or None
        normalized["last_token_refresh_error_at"] = normalized.get("last_token_refresh_error_at") or None
        normalized["refresh_token_invalid_at"] = normalized.get("refresh_token_invalid_at") or None
        diagnostic_secrets = (
            normalized.get("access_token"),
            normalized.get("refresh_token"),
            normalized.get("id_token"),
        )
        diagnostic_proxies = (normalized.get("proxy"),)
        for key in (
            "last_remote_check_error",
            "last_refresh_error",
            "last_token_refresh_error",
        ):
            if normalized.get(key):
                normalized[key] = sanitize_diagnostic_text(
                    normalized[key],
                    sensitive_values=diagnostic_secrets,
                    proxy_values=diagnostic_proxies,
                )
        for key in (
            "capability_cooldowns",
            "capability_failure_counts",
            "capability_failure_codes",
            "capability_failed_at",
        ):
            normalized.pop(key, None)
        normalized["created_at"] = normalized.get("created_at") or AccountPrimitivesMixin._now()
        return normalized

    def _resolve_access_token_locked(self, access_token: str) -> str:
        token = str(access_token or "").strip()
        seen: set[str] = set()
        while token and token not in self._accounts and token in self._token_aliases and token not in seen:
            seen.add(token)
            token = self._token_aliases.get(token, token)
        return token

    def resolve_access_token(self, access_token: str) -> str:
        if not access_token:
            return ""
        with self._lock:
            return self._resolve_access_token_locked(access_token)

    def _move_account_runtime_token_locked(
        self,
        new_token: str,
        alias_sources: set[str],
    ) -> None:
        for source in alias_sources:
            if source != new_token:
                self._token_aliases[source] = new_token

        old_inflight = sum(
            int(self._image_inflight.pop(source, 0))
            for source in alias_sources
            if source != new_token
        )
        if old_inflight:
            self._image_inflight[new_token] = (
                int(self._image_inflight.get(new_token, 0)) + old_inflight
            )

        with self._image_failure_refresh_lock:
            old_scopes = [
                self._image_failure_refresh_pending_scopes.pop(source)
                for source in alias_sources
                if source in self._image_failure_refresh_pending_scopes
            ]
            pending = (
                new_token if token in alias_sources else token
                for token in self._image_failure_refresh_pending
            )
            self._image_failure_refresh_pending = deque(dict.fromkeys(pending))
            self._image_failure_refresh_pending_set = set(
                self._image_failure_refresh_pending
            )
            if old_scopes:
                current_scope = self._image_failure_refresh_pending_scopes.get(new_token)
                self._image_failure_refresh_pending_scopes[new_token] = (
                    "account"
                    if "account" in {*old_scopes, current_scope}
                    else "image"
                )
            old_started_at = max(
                (
                    self._image_failure_refresh_started_at.pop(source)
                    for source in alias_sources
                    if source in self._image_failure_refresh_started_at
                ),
                default=None,
            )
            if old_started_at is not None:
                self._image_failure_refresh_started_at[new_token] = max(
                    old_started_at,
                    self._image_failure_refresh_started_at.get(new_token, 0.0),
                )
            # Active workers keep their original key until their finally block.

    def _passive_token_rotations_locked(
        self,
        loaded: dict[str, dict],
    ) -> list[tuple[str, set[str]]]:
        loaded_by_management_id = {
            str(account.get("management_id") or "").strip(): token
            for token, account in loaded.items()
            if str(account.get("management_id") or "").strip()
        }
        rotations: list[tuple[str, set[str]]] = []
        for old_token, old_account in self._accounts.items():
            if old_token in loaded:
                continue
            management_id = str(old_account.get("management_id") or "").strip()
            new_token = loaded_by_management_id.get(management_id)
            if not new_token or new_token == old_token:
                continue
            alias_sources = {
                source
                for source in {old_token, *self._token_aliases}
                if self._resolve_access_token_locked(source) == old_token
            }
            rotations.append((new_token, alias_sources))
        return rotations

    def list_tokens(self) -> list[str]:
        self._refresh_accounts_snapshot_if_stale()
        with self._lock:
            return list(self._accounts)

    def get_account(self, access_token: str) -> dict | None:
        if not access_token:
            return None
        self._refresh_accounts_snapshot_if_stale()
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            account = self._accounts.get(access_token)
            return dict(account) if account else None

    def get_account_by_token_identity(self, access_token: str) -> dict | None:
        """Resolve a current account from either its current or a rotated access token."""
        token = str(access_token or "").strip()
        if not token:
            return None
        self._refresh_accounts_snapshot_if_stale()
        with self._lock:
            resolved = self._resolve_access_token_locked(token)
            account = self._accounts.get(resolved)
            if account is not None:
                return dict(account)

            fingerprint = self._access_token_fingerprint(token)
            for candidate in self._accounts.values():
                fingerprints = candidate.get("access_token_fingerprints")
                if isinstance(fingerprints, list) and fingerprint in fingerprints:
                    return dict(candidate)
        return None

    def get_account_by_id(self, account_id: str) -> dict | None:
        management_id = str(account_id or "").strip().lower()
        if not management_id:
            return None
        self._refresh_accounts_snapshot_if_stale()
        with self._lock:
            for account in self._accounts.values():
                if str(account.get("management_id") or "").strip().lower() == management_id:
                    result = dict(account)
                    access_token = str(result.get("access_token") or "").strip()
                    result["image_inflight"] = int(self._image_inflight.get(access_token, 0))
                    return result
        return None

    def resolve_account_ids(self, account_ids: list[str]) -> tuple[list[str], list[str]]:
        requested = list(dict.fromkeys(str(item or "").strip().lower() for item in account_ids if str(item or "").strip()))
        if not requested:
            return [], []
        self._refresh_accounts_snapshot_if_stale()
        with self._lock:
            token_by_id = {
                str(account.get("management_id") or "").strip().lower(): str(account.get("access_token") or "").strip()
                for account in self._accounts.values()
                if str(account.get("management_id") or "").strip()
                and str(account.get("access_token") or "").strip()
            }
        tokens = [token_by_id[item] for item in requested if item in token_by_id]
        missing = [item for item in requested if item not in token_by_id]
        return tokens, missing

    def list_accounts(self) -> list[dict]:
        """返回所有账号的副本，并为每个账号附加当前图片在途数 image_inflight。

        image_inflight 为内存态并发计数(账号正在生成、尚未结束的图片数)。号池空闲时
        若某账号该值持续 > 0，说明其并发槽位泄漏、已被静默排除出调度，可借此在 UI 上诊断。
        """
        self._refresh_accounts_snapshot_if_stale()
        with self._lock:
            result = []
            for item in self._accounts.values():
                account = dict(item)
                token = account.get("access_token") or ""
                account["image_inflight"] = int(self._image_inflight.get(token, 0))
                result.append(account)
            return result
