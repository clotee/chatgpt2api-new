from __future__ import annotations

import time
from ._shared import RefreshCredentialsChangedError, TerminalRefreshTokenError, _RemoteCheckMarker
from datetime import datetime, timezone
from services.account_processing import account_processing_slot, account_processing_worker_count
from services.config import config
from services.log_service import LOG_TYPE_ACCOUNT, log_service
from threading import Thread
from typing import Any
from utils.helper import anonymize_token
from uuid import uuid4

class RemoteCheckMixin:
    """remote check, invalid-token state machine and failure scheduling（T-21 Mixin 拆分，方法块自 _all.py 原样搬迁）。"""

    def remove_invalid_token(
        self,
        access_token: str,
        event: str,
        quiet: bool = False,
        remove: bool | None = None,
        error: str | None = None,
    ) -> bool:
        return self.handle_invalid_token(
            access_token,
            event,
            error=error,
            quiet=quiet,
            remove=remove,
            expected_access_token=access_token,
        )

    def handle_invalid_token(
        self,
        access_token: str,
        event: str,
        error: str | None = None,
        quiet: bool = False,
        remove: bool | None = None,
        expected_access_token: str | None = None,
        expected_refresh_token: str | None = None,
        expected_remote_check_marker: _RemoteCheckMarker | None = None,
        token_refresh_error: str | None = None,
        refresh_token_terminal: bool = False,
    ) -> bool:
        """统一处理鉴权异常账号。

        口径固定为：先记录异常，再按“自动移除异常账号”配置删除或保留异常状态。
        """
        should_remove = config.auto_remove_invalid_accounts if remove is None else remove
        return self._apply_invalid_token_state(
            access_token,
            event,
            str(error or "invalid access token"),
            remove=bool(should_remove),
            expected_access_token=expected_access_token,
            expected_refresh_token=expected_refresh_token,
            expected_remote_check_marker=expected_remote_check_marker,
            token_refresh_error=token_refresh_error,
            refresh_token_terminal=refresh_token_terminal,
            quiet=quiet,
        )

    def list_pending_auth_verification_tokens(self) -> list[str]:
        self._refresh_accounts_snapshot_if_stale()
        with self._lock:
            return [
                token
                for item in self._accounts.values()
                if item.get("last_remote_check_result") == "pending"
                and item.get("pending_auth_scope") == "image"
                and (token := str(item.get("access_token") or "").strip())
            ]

    def resume_pending_auth_verifications(self) -> int:
        tokens = self.list_pending_auth_verification_tokens()
        scheduled = 0
        for token in tokens:
            if self._schedule_account_refresh_after_image_failure(token):
                scheduled += 1
        return scheduled

    def _record_refresh_success(
        self,
        access_token: str,
        updates: dict,
        event: str = "fetch_remote_info",
        *,
        expected_access_token: str,
        expected_refresh_token: str,
        expected_remote_check_marker: _RemoteCheckMarker,
    ) -> dict | None:
        now = datetime.now(timezone.utc).isoformat()
        return self.update_account(
            access_token,
            {
                **updates,
                "invalid_count": 0,
                "last_invalid_at": None,
                "last_refresh_error": None,
                "last_refresh_error_at": None,
                "last_remote_checked_at": now,
                "last_remote_check_attempt_at": now,
                "last_remote_check_error": None,
                "last_remote_check_error_at": None,
                "last_remote_check_event": event,
                "last_remote_check_result": "ok",
                "pending_auth_remove_invalid": None,
                "pending_auth_scope": None,
            },
            quiet=True,
            expected_access_token=expected_access_token,
            expected_refresh_token=expected_refresh_token,
            expected_remote_check_marker=expected_remote_check_marker,
            preserve_disabled=True,
        )

    def _record_remote_check_error(
        self,
        access_token: str,
        event: str,
        error: str,
        *,
        expected_access_token: str | None = None,
        expected_refresh_token: str | None = None,
        expected_remote_check_marker: _RemoteCheckMarker | None = None,
    ) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return False
            if expected_access_token is not None and (
                access_token != str(expected_access_token or "").strip()
            ):
                return False
            if expected_refresh_token is not None and (
                str(current.get("refresh_token") or "").strip()
                != str(expected_refresh_token or "").strip()
            ):
                return False
            if (
                expected_remote_check_marker is not None
                and self._remote_check_marker(current) != expected_remote_check_marker
            ):
                return False
            expected_generation = None
            if expected_access_token is not None and expected_refresh_token is not None:
                expected_generation = (
                    str(expected_access_token or "").strip(),
                    str(expected_refresh_token or "").strip(),
                    (
                        expected_remote_check_marker[0]
                        if expected_remote_check_marker is not None
                        else str(current.get("last_token_refresh_at") or "").strip()
                    ),
                )
            next_item = dict(current)
            next_item["last_remote_check_attempt_at"] = now
            next_item["last_remote_check_error"] = str(error or "remote check failed")
            next_item["last_remote_check_error_at"] = now
            next_item["last_remote_check_event"] = event
            current_result = str(current.get("last_remote_check_result") or "")
            next_item["last_remote_check_result"] = (
                "invalid" if current_result == "invalid" else "error"
            )
            next_item["pending_auth_remove_invalid"] = None
            next_item["pending_auth_scope"] = None
            account = self._normalize_account(next_item)
            if account is not None:
                self._accounts[access_token] = account
                saved = self._save_accounts(
                    expected_credential_generation=expected_generation,
                )
                if not saved:
                    return False
                access_token = self._resolve_access_token_locked(access_token)
                persisted = self._accounts.get(access_token)
                return bool(
                    persisted is not None
                    and persisted.get("last_remote_check_error_at") == now
                )
        return False

    def _apply_invalid_token_state(
        self,
        access_token: str,
        event: str,
        error: str,
        *,
        remove: bool,
        expected_access_token: str | None = None,
        expected_refresh_token: str | None = None,
        expected_remote_check_marker: _RemoteCheckMarker | None = None,
        token_refresh_error: str | None = None,
        refresh_token_terminal: bool = False,
        quiet: bool = False,
    ) -> bool:
        _ = quiet
        now = datetime.now(timezone.utc)
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return False
            if expected_access_token is not None and (
                access_token != str(expected_access_token or "").strip()
            ):
                return False
            if expected_refresh_token is not None and (
                str(current.get("refresh_token") or "").strip()
                != str(expected_refresh_token or "").strip()
            ):
                return False
            if (
                expected_remote_check_marker is not None
                and self._remote_check_marker(current) != expected_remote_check_marker
            ):
                return False
            expected_generation = None
            if expected_access_token is not None and expected_refresh_token is not None:
                expected_generation = (
                    str(expected_access_token or "").strip(),
                    str(expected_refresh_token or "").strip(),
                    (
                        expected_remote_check_marker[0]
                        if expected_remote_check_marker is not None
                        else str(current.get("last_token_refresh_at") or "").strip()
                    ),
                )

            terminal_already_recorded = bool(
                refresh_token_terminal
                and token_refresh_error
                and current.get("refresh_token_invalid_at")
                and current.get("status") in {"异常", "禁用"}
                and current.get("last_remote_check_result") == "invalid"
                and str(current.get("last_token_refresh_error") or "")
                == str(token_refresh_error)
            )
            if terminal_already_recorded:
                account = dict(current)
                if not (remove and account.get("status") == "异常"):
                    return False
            else:
                next_item = dict(current)
                next_item["status"] = "禁用" if current.get("status") == "禁用" else "异常"
                next_item["quota"] = 0
                next_item["image_quota_unknown"] = True
                next_item["invalid_count"] = int(next_item.get("invalid_count") or 0) + 1
                next_item["last_invalid_at"] = now.isoformat()
                next_item["last_refresh_error"] = str(error or "invalid access token")
                next_item["last_refresh_error_at"] = now.isoformat()
                next_item["last_remote_checked_at"] = now.isoformat()
                next_item["last_remote_check_attempt_at"] = now.isoformat()
                next_item["last_remote_check_error"] = str(error or "invalid access token")
                next_item["last_remote_check_error_at"] = now.isoformat()
                next_item["last_remote_check_event"] = event
                next_item["last_remote_check_result"] = "invalid"
                next_item["pending_auth_remove_invalid"] = None
                next_item["pending_auth_scope"] = None
                if refresh_token_terminal and token_refresh_error:
                    next_item["last_token_refresh_error"] = str(token_refresh_error)
                    next_item["last_token_refresh_error_at"] = now.isoformat()
                    next_item["refresh_token_invalid_at"] = now.isoformat()
                account = self._normalize_account(next_item)
                if account is None:
                    return False

            final_status = str(account.get("status") or "异常")
            removed = bool(remove and final_status == "异常")
            if removed:
                self._accounts.pop(access_token, None)
            else:
                self._accounts[access_token] = account
            saved = self._save_accounts(
                expected_credential_generation=expected_generation,
            )
            if not saved:
                return False
            access_token = self._resolve_access_token_locked(access_token)
            persisted = self._accounts.get(access_token)
            if persisted is None:
                self._remove_account_runtime_state_locked({access_token})
                if not removed:
                    return False
            elif removed:
                return False
            else:
                account = dict(persisted)
                final_status = str(account.get("status") or "异常")

            diagnostic_error = self._credential_error_text(
                error,
                account,
                access_token=access_token,
            )
            if not terminal_already_recorded:
                log_service.add(
                    LOG_TYPE_ACCOUNT,
                    "账号鉴权确认失效" if final_status == "异常" else "已禁用账号鉴权确认失效",
                    {
                        "source": event,
                        "token": anonymize_token(access_token),
                        "status": final_status,
                        "error": diagnostic_error,
                    },
                )
            if removed:
                log_service.add(LOG_TYPE_ACCOUNT, "删除 1 个账号", {"removed": 1})
                log_service.add(
                    LOG_TYPE_ACCOUNT,
                    "自动移除异常账号",
                    {
                        "source": event,
                        "token": anonymize_token(access_token),
                        "error": diagnostic_error,
                    },
                )
        return removed

    @staticmethod
    def _mark_remote_check_pending(
        account: dict,
        event: str,
        now: str,
        *,
        remove_invalid: bool | None = None,
        scope: str = "account",
    ) -> None:
        existing_scope = (
            str(account.get("pending_auth_scope") or "").strip().lower()
            if account.get("last_remote_check_result") == "pending"
            else ""
        )
        account["last_remote_check_result"] = "pending"
        account["last_remote_check_event"] = event
        account["last_remote_check_attempt_at"] = now
        account["last_remote_check_error"] = None
        account["last_remote_check_error_at"] = None
        account["pending_auth_verification_id"] = uuid4().hex
        account["pending_auth_scope"] = (
            "account"
            if existing_scope == "account" or scope != "image"
            else "image"
        )
        requested_remove = (
            config.auto_remove_invalid_accounts
            if remove_invalid is None
            else bool(remove_invalid)
        )
        existing_remove = account.get("pending_auth_remove_invalid")
        account["pending_auth_remove_invalid"] = bool(existing_remove) or requested_remove

    def schedule_auth_verification(
        self,
        access_token: str,
        event: str,
        *,
        expected_access_token: str | None = None,
        expected_refresh_token: str | None = None,
        expected_last_token_refresh_at: str | None = None,
        remove_invalid: bool | None = None,
        scope: str = "account",
    ) -> bool:
        """Block a rejected account now and verify it in the background."""
        if not access_token:
            return False
        now = datetime.now(timezone.utc).isoformat()
        with self._image_slot_condition:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return False
            expected_generation = None
            if expected_access_token is not None and expected_refresh_token is not None:
                expected_generation = (
                    str(expected_access_token or "").strip(),
                    str(expected_refresh_token or "").strip(),
                    str(expected_last_token_refresh_at or "").strip(),
                )
                if self._credential_generation(access_token, current) != expected_generation:
                    return False
            next_item = dict(current)
            self._mark_remote_check_pending(
                next_item,
                event,
                now,
                remove_invalid=remove_invalid,
                scope=scope,
            )
            account = self._normalize_account(next_item)
            if account is None:
                return False
            pending_verification_id = account.get("pending_auth_verification_id")
            self._accounts[access_token] = account
            saved = self._save_accounts(
                expected_credential_generation=expected_generation,
            )
            if not saved:
                return False
            access_token = self._resolve_access_token_locked(access_token)
            persisted = self._accounts.get(access_token)
            if (
                persisted is None
                or persisted.get("pending_auth_verification_id")
                != pending_verification_id
            ):
                return False
            pending_marker = self._remote_check_marker(persisted)
            pending_generation = self._credential_generation(access_token, persisted)
            self._image_slot_condition.notify_all()
        scheduled = self._schedule_account_refresh_after_image_failure(access_token, force=True)
        if not scheduled:
            self._record_remote_check_error(
                access_token,
                event,
                "Account verification could not be scheduled.",
                expected_access_token=pending_generation[0],
                expected_refresh_token=pending_generation[1],
                expected_remote_check_marker=pending_marker,
            )
        return scheduled

    def _verify_pending_auth(self, access_token: str, event: str) -> None:
        active_token, refresh_token, account = self._credential_snapshot(access_token)
        if not account or account.get("last_remote_check_result") != "pending":
            return
        pending_remove = account.get("pending_auth_remove_invalid")
        remove_invalid = (
            config.auto_remove_invalid_accounts
            if pending_remove is None
            else self._bool_value(pending_remove)
        )
        initial_token = active_token
        initial_refresh_token = refresh_token
        initial_remote_check_marker = self._remote_check_marker(account)

        from services.openai_backend_api import InvalidAccessTokenError, OpenAIBackendAPI

        def request_user_info(token: str) -> dict[str, Any]:
            with account_processing_slot():
                with OpenAIBackendAPI(token) as backend:
                    return backend.get_user_info()

        def reschedule_pending(token: str) -> None:
            current_token, _, current = self._credential_snapshot(token)
            if current and current.get("last_remote_check_result") == "pending":
                self._schedule_account_refresh_after_image_failure(
                    current_token,
                    force=True,
                )

        def snapshot_changed(token: str, refresh: str) -> bool:
            current_token, current_refresh, current = self._credential_snapshot(token)
            if current and (current_token, current_refresh) == (token, refresh):
                return False
            return True

        def request_user_info_for_snapshot(token: str, refresh: str) -> dict[str, Any]:
            try:
                result = request_user_info(token)
            except Exception as exc:
                if snapshot_changed(token, refresh):
                    raise RefreshCredentialsChangedError() from exc
                raise
            if snapshot_changed(token, refresh):
                raise RefreshCredentialsChangedError()
            return result

        try:
            result = request_user_info_for_snapshot(initial_token, initial_refresh_token)
        except InvalidAccessTokenError as initial_error:
            if not initial_refresh_token:
                self.handle_invalid_token(
                    initial_token,
                    event,
                    error=str(initial_error),
                    remove=remove_invalid,
                    expected_access_token=initial_token,
                    expected_refresh_token=initial_refresh_token,
                    expected_remote_check_marker=initial_remote_check_marker,
                )
                reschedule_pending(initial_token)
                return
            try:
                active_token = self.force_refresh_access_token(
                    initial_token,
                    event=f"{event}:auth_recovery",
                    raise_on_error=True,
                    image_scope=True,
                    expected_credentials=(
                        initial_token,
                        initial_refresh_token,
                        initial_remote_check_marker[0],
                    ),
                )
            except TerminalRefreshTokenError as refresh_error:
                self.handle_invalid_token(
                    initial_token,
                    event,
                    error=str(initial_error),
                    remove=remove_invalid,
                    expected_access_token=initial_token,
                    expected_refresh_token=initial_refresh_token,
                    expected_remote_check_marker=initial_remote_check_marker,
                    token_refresh_error=str(refresh_error),
                    refresh_token_terminal=True,
                )
                reschedule_pending(initial_token)
                return
            except Exception as exc:
                self._record_remote_check_error(
                    initial_token,
                    event,
                    str(exc),
                    expected_access_token=initial_token,
                    expected_refresh_token=initial_refresh_token,
                    expected_remote_check_marker=initial_remote_check_marker,
                )
                reschedule_pending(initial_token)
                return

            verification_token, verification_refresh_token, verification_account = (
                self._credential_snapshot(active_token)
            )
            if not verification_account:
                return
            verification_remote_check_marker = self._remote_check_marker(verification_account)
            try:
                result = request_user_info_for_snapshot(
                    verification_token,
                    verification_refresh_token,
                )
            except InvalidAccessTokenError as exc:
                self.handle_invalid_token(
                    verification_token,
                    event,
                    error=str(exc),
                    remove=remove_invalid,
                    expected_access_token=verification_token,
                    expected_refresh_token=verification_refresh_token,
                    expected_remote_check_marker=verification_remote_check_marker,
                )
                reschedule_pending(verification_token)
                return
            except Exception as exc:
                self._record_remote_check_error(
                    verification_token,
                    event,
                    str(exc),
                    expected_access_token=verification_token,
                    expected_refresh_token=verification_refresh_token,
                    expected_remote_check_marker=verification_remote_check_marker,
                )
                reschedule_pending(verification_token)
                return

            self._record_refresh_success(
                verification_token,
                result,
                event,
                expected_access_token=verification_token,
                expected_refresh_token=verification_refresh_token,
                expected_remote_check_marker=verification_remote_check_marker,
            )
            reschedule_pending(verification_token)
            return
        except Exception as exc:
            self._record_remote_check_error(
                initial_token,
                event,
                str(exc),
                expected_access_token=initial_token,
                expected_refresh_token=initial_refresh_token,
                expected_remote_check_marker=initial_remote_check_marker,
            )
            reschedule_pending(initial_token)
            return

        self._record_refresh_success(
            initial_token,
            result,
            event,
            expected_access_token=initial_token,
            expected_refresh_token=initial_refresh_token,
            expected_remote_check_marker=initial_remote_check_marker,
        )
        reschedule_pending(initial_token)

    def _refresh_account_after_image_failure(self, access_token: str) -> None:
        try:
            account = self.get_account(access_token) or {}
            event = str(account.get("last_remote_check_event") or "account_failure")
            if account.get("last_remote_check_result") == "pending":
                if account.get("pending_auth_scope") == "image":
                    self._verify_pending_auth(access_token, event)
                else:
                    self.fetch_remote_info(access_token, event)
            else:
                self.fetch_remote_info(access_token, event)
        except Exception as exc:
            account = self.get_account(access_token)
            log_service.add(
                LOG_TYPE_ACCOUNT,
                "鉴权失败后核验账号失败",
                {
                    "token": anonymize_token(access_token),
                    "error": self._credential_error_text(
                        exc,
                        account,
                        access_token=access_token,
                    ),
                },
            )

    def _schedule_account_refresh_after_image_failure(self, access_token: str, *, force: bool = False) -> bool:
        if not access_token:
            return False
        now = time.monotonic()
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            account = self._accounts.get(access_token) or {}
            requested_scope = (
                "image" if account.get("pending_auth_scope") == "image" else "account"
            )
            with self._image_failure_refresh_lock:
                cutoff = now - self._IMAGE_FAILURE_REFRESH_DEDUP_SECONDS
                self._image_failure_refresh_started_at = {
                    token: started_at
                    for token, started_at in self._image_failure_refresh_started_at.items()
                    if token in self._image_failure_refresh_active or started_at >= cutoff
                }
                active_refresh_token = next(
                    (
                        token for token in self._image_failure_refresh_active
                        if self._resolve_access_token_locked(token) == access_token
                    ),
                    None,
                )
                pending_refresh_token = next(
                    (
                        token for token in self._image_failure_refresh_pending_set
                        if self._resolve_access_token_locked(token) == access_token
                    ),
                    None,
                )
                last_started_at = max(
                    (
                        started_at
                        for token, started_at in self._image_failure_refresh_started_at.items()
                        if self._resolve_access_token_locked(token) == access_token
                    ),
                    default=0.0,
                )
                if active_refresh_token is not None:
                    if force:
                        self._image_failure_refresh_rerun.add(active_refresh_token)
                    return True
                if pending_refresh_token is not None:
                    existing_scope = self._image_failure_refresh_pending_scopes.get(
                        pending_refresh_token,
                        "account",
                    )
                    self._image_failure_refresh_pending_scopes[pending_refresh_token] = (
                        "account"
                        if "account" in {existing_scope, requested_scope}
                        else "image"
                    )
                    return True
                if (
                    not force
                    and now - last_started_at < self._IMAGE_FAILURE_REFRESH_DEDUP_SECONDS
                ):
                    return False
                self._image_failure_refresh_pending.append(access_token)
                self._image_failure_refresh_pending_set.add(access_token)
                self._image_failure_refresh_pending_scopes[access_token] = requested_scope
        self._start_pending_image_failure_refreshes()
        return True

    def _start_pending_image_failure_refreshes(self) -> None:
        while True:
            with self._image_failure_refresh_lock:
                if not self._image_failure_refresh_pending:
                    return

                task_count = (
                    len(self._image_failure_refresh_active)
                    + len(self._image_failure_refresh_pending)
                )
                max_workers = account_processing_worker_count(task_count)
                if len(self._image_failure_refresh_active) >= max_workers:
                    return

                access_token = self._image_failure_refresh_pending.popleft()
                refresh_scope = self._image_failure_refresh_pending_scopes.get(
                    access_token,
                    "account",
                )
                self._image_failure_refresh_pending_set.discard(access_token)
                self._image_failure_refresh_pending_scopes.pop(access_token, None)
                self._image_failure_refresh_active.add(access_token)
                self._image_failure_refresh_active_scopes[access_token] = refresh_scope
                self._image_failure_refresh_started_at[access_token] = time.monotonic()

            def refresh(token: str = access_token) -> None:
                try:
                    self._refresh_account_after_image_failure(token)
                finally:
                    with self._lock:
                        resolved_token = self._resolve_access_token_locked(token)
                        account = self._accounts.get(resolved_token)
                        with self._image_failure_refresh_lock:
                            self._image_failure_refresh_active.discard(token)
                            self._image_failure_refresh_active_scopes.pop(token, None)
                            rerun_requested = token in self._image_failure_refresh_rerun
                            self._image_failure_refresh_rerun.discard(token)
                            if (
                                rerun_requested
                                and account is not None
                                and account.get("last_remote_check_result") == "pending"
                                and resolved_token not in self._image_failure_refresh_pending_set
                            ):
                                self._image_failure_refresh_pending.append(resolved_token)
                                self._image_failure_refresh_pending_set.add(resolved_token)
                                self._image_failure_refresh_pending_scopes[resolved_token] = (
                                    "image"
                                    if account.get("pending_auth_scope") == "image"
                                    else "account"
                                )
                    self._start_pending_image_failure_refreshes()

            (
                expected_access_token,
                expected_refresh_token,
                expected_account,
            ) = self._credential_snapshot(access_token)
            expected_remote_check_marker = self._remote_check_marker(expected_account)
            try:
                Thread(
                    target=refresh,
                    name="image-account-refresh",
                    daemon=True,
                ).start()
            except Exception as exc:
                with self._image_failure_refresh_lock:
                    self._image_failure_refresh_active.discard(access_token)
                    self._image_failure_refresh_active_scopes.pop(access_token, None)
                self._record_remote_check_error(
                    access_token,
                    "image_failure",
                    str(exc),
                    expected_access_token=expected_access_token,
                    expected_refresh_token=expected_refresh_token,
                    expected_remote_check_marker=expected_remote_check_marker,
                )
                log_service.add(
                    LOG_TYPE_ACCOUNT,
                    "image failure refresh scheduling failed",
                    {
                        "token": anonymize_token(access_token),
                        "error": self._credential_error_text(
                            exc,
                            expected_account,
                            access_token=access_token,
                        ),
                    },
                )

    def fetch_remote_info(
        self,
        access_token: str,
        event: str = "fetch_remote_info",
        remove_invalid: bool | None = None,
        *,
        image_scope: bool = False,
        allow_refresh_token_exchange: bool = True,
        preflight_refresh: bool = True,
    ) -> dict[str, Any] | None:
        if not access_token:
            raise ValueError("access_token is required")

        if allow_refresh_token_exchange and preflight_refresh:
            refresh_kwargs = {"event": f"{event}:preflight"}
            if image_scope:
                refresh_kwargs["image_scope"] = True
            active_token = self.ensure_access_token(access_token, **refresh_kwargs)
        else:
            active_token = self.resolve_access_token(access_token) or access_token
        from services.openai_backend_api import InvalidAccessTokenError, OpenAIBackendAPI

        def request_user_info(token: str) -> dict[str, Any]:
            with account_processing_slot():
                with OpenAIBackendAPI(token) as backend:
                    return backend.get_user_info()

        request_token, request_refresh_token, request_account = self._credential_snapshot(active_token)
        if not request_account:
            raise RefreshCredentialsChangedError()
        request_generation = self._credential_generation(request_token, request_account)
        successful_snapshot = (request_token, request_refresh_token)
        successful_remote_check_marker = self._remote_check_marker(request_account)
        try:
            result = request_user_info(request_token)
        except InvalidAccessTokenError as exc:
            rejected_error: InvalidAccessTokenError | None = exc
            rejected_snapshot = (request_token, request_refresh_token)
            rejected_remote_check_marker = successful_remote_check_marker
            current_token, current_refresh_token, current_account = self._credential_snapshot(request_token)
            current_snapshot = (current_token, current_refresh_token)
            current_remote_check_marker = self._remote_check_marker(current_account)
            current_generation = self._credential_generation(current_token, current_account)
            if current_account and current_generation != request_generation:
                try:
                    result = request_user_info(current_token)
                except InvalidAccessTokenError as current_exc:
                    rejected_error = current_exc
                    rejected_snapshot = current_snapshot
                    rejected_remote_check_marker = current_remote_check_marker
                except Exception as current_exc:
                    self._record_remote_check_error(
                        current_token,
                        event,
                        str(current_exc),
                        expected_access_token=current_token,
                        expected_refresh_token=current_refresh_token,
                        expected_remote_check_marker=current_remote_check_marker,
                    )
                    raise
                else:
                    successful_snapshot = current_snapshot
                    successful_remote_check_marker = current_remote_check_marker
                    active_token = current_token
                    rejected_error = None

            if rejected_error is not None:
                if image_scope:
                    self.schedule_auth_verification(
                        rejected_snapshot[0],
                        event,
                        expected_access_token=rejected_snapshot[0],
                        expected_refresh_token=rejected_snapshot[1],
                        expected_last_token_refresh_at=rejected_remote_check_marker[0],
                        remove_invalid=remove_invalid,
                        scope="image",
                    )
                    raise rejected_error

                if not allow_refresh_token_exchange:
                    refresh_token_can_recover = bool(rejected_snapshot[1]) and not bool(
                        (current_account or {}).get("refresh_token_invalid_at")
                    )
                    self.handle_invalid_token(
                        rejected_snapshot[0],
                        event,
                        error=str(rejected_error),
                        remove=False if refresh_token_can_recover else remove_invalid,
                        expected_access_token=rejected_snapshot[0],
                        expected_refresh_token=rejected_snapshot[1],
                        expected_remote_check_marker=rejected_remote_check_marker,
                    )
                    raise rejected_error

                before_refresh = self.get_account(rejected_snapshot[0]) or {}
                before_refresh_at = str(before_refresh.get("last_token_refresh_at") or "")
                before_error_at = str(before_refresh.get("last_token_refresh_error_at") or "")
                try:
                    active_token = self.force_refresh_access_token(
                        rejected_snapshot[0],
                        event=f"{event}:invalid_access_token",
                        expected_credentials=(
                            rejected_snapshot[0],
                            rejected_snapshot[1],
                            rejected_remote_check_marker[0],
                        ),
                    )
                except TerminalRefreshTokenError as refresh_error:
                    self.handle_invalid_token(
                        rejected_snapshot[0],
                        event,
                        error=str(rejected_error),
                        remove=remove_invalid,
                        expected_access_token=rejected_snapshot[0],
                        expected_refresh_token=rejected_snapshot[1],
                        expected_remote_check_marker=rejected_remote_check_marker,
                        token_refresh_error=str(refresh_error),
                        refresh_token_terminal=True,
                    )
                    raise
                except RefreshCredentialsChangedError:
                    raise
                except Exception as refresh_exc:
                    self._record_remote_check_error(
                        rejected_snapshot[0],
                        event,
                        str(refresh_exc),
                        expected_access_token=rejected_snapshot[0],
                        expected_refresh_token=rejected_snapshot[1],
                        expected_remote_check_marker=rejected_remote_check_marker,
                    )
                    raise

                after_refresh = self.get_account(active_token or rejected_snapshot[0]) or {}
                after_refresh_at = str(after_refresh.get("last_token_refresh_at") or "")
                after_error_at = str(after_refresh.get("last_token_refresh_error_at") or "")
                refresh_failed = bool(after_error_at and after_error_at != before_error_at)
                refresh_succeeded = bool(after_refresh_at and after_refresh_at != before_refresh_at)
                if refresh_failed and not refresh_succeeded:
                    self._record_remote_check_error(
                        rejected_snapshot[0],
                        event,
                        str(
                            after_refresh.get("last_token_refresh_error")
                            or "refresh token failed"
                        ),
                        expected_access_token=rejected_snapshot[0],
                        expected_refresh_token=rejected_snapshot[1],
                        expected_remote_check_marker=rejected_remote_check_marker,
                    )
                    raise rejected_error

                verification_token, verification_refresh_token, verification_account = (
                    self._credential_snapshot(active_token)
                )
                if not verification_account:
                    raise RefreshCredentialsChangedError()
                verification_remote_check_marker = self._remote_check_marker(verification_account)
                try:
                    result = request_user_info(verification_token)
                except InvalidAccessTokenError as retry_exc:
                    self.handle_invalid_token(
                        verification_token,
                        event,
                        error=str(retry_exc),
                        remove=remove_invalid,
                        expected_access_token=verification_token,
                        expected_refresh_token=verification_refresh_token,
                        expected_remote_check_marker=verification_remote_check_marker,
                    )
                    raise
                except Exception as retry_exc:
                    self._record_remote_check_error(
                        verification_token,
                        event,
                        str(retry_exc),
                        expected_access_token=verification_token,
                        expected_refresh_token=verification_refresh_token,
                        expected_remote_check_marker=verification_remote_check_marker,
                    )
                    raise
                active_token = verification_token
                successful_snapshot = (verification_token, verification_refresh_token)
                successful_remote_check_marker = verification_remote_check_marker
        except Exception as exc:
            self._record_remote_check_error(
                request_token,
                event,
                str(exc),
                expected_access_token=request_token,
                expected_refresh_token=request_refresh_token,
                expected_remote_check_marker=successful_remote_check_marker,
            )
            raise

        updated = self._record_refresh_success(
            active_token,
            result,
            event,
            expected_access_token=successful_snapshot[0],
            expected_refresh_token=successful_snapshot[1],
            expected_remote_check_marker=successful_remote_check_marker,
        )
        if updated is not None:
            return updated
        # update_account 可能因为“自动移除额度耗尽账号”删除了远程确认限流的账号。
        # 调用方仍需要知道本次预检的真实结果，不能把它混成普通预检失败。
        if str(result.get("status") or "") == "\u9650\u6d41" and config.auto_remove_rate_limited_accounts:
            return {**result, "access_token": active_token, "_removed_after_refresh": True}
        current_token, current_refresh_token, current_account = self._credential_snapshot(active_token)
        if (
            current_account is not None
            and current_token == successful_snapshot[0]
            and current_refresh_token == successful_snapshot[1]
        ):
            # A newer verification marker won the compare-and-swap. Preserve and
            # return that newer account state instead of reporting a credential race.
            return current_account
        # A deleted account must not leak its stale token back into the selection path.
        raise RefreshCredentialsChangedError()
