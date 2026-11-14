from __future__ import annotations

from ._shared import OAuthRefreshError, RefreshCredentialsChangedError, TerminalRefreshTokenError, _CredentialGeneration
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from services.account_processing import account_processing_slot, account_processing_worker_count
from services.image_failure import classify_image_exception
from services.log_service import LOG_TYPE_ACCOUNT, log_service
from typing import Any
from utils.helper import anonymize_token

class TokenRefreshMixin:
    """OAuth RT->AT token refresh（T-21 Mixin 拆分，方法块自 _all.py 原样搬迁）。"""

    def _get_account_for_token(self, access_token: str) -> tuple[str, dict | None]:
        with self._lock:
            resolved = self._resolve_access_token_locked(access_token)
            account = self._accounts.get(resolved)
            return resolved, dict(account) if account else None

    def _credential_snapshot(self, access_token: str) -> tuple[str, str, dict | None]:
        resolved, account = self._get_account_for_token(access_token)
        if not account:
            return resolved, "", None
        active_token = str(account.get("access_token") or resolved or access_token).strip()
        refresh_token = str(account.get("refresh_token") or "").strip()
        return active_token, refresh_token, account

    def _record_token_refresh_error(
        self,
        access_token: str,
        event: str,
        error: str,
        *,
        expected_access_token: str | None = None,
        expected_refresh_token: str | None = None,
        expected_last_token_refresh_at: str | None = None,
        terminal: bool = False,
    ) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            resolved = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(resolved)
            if current is None:
                return False
            expected_generation = None
            if expected_access_token is not None and expected_refresh_token is not None:
                expected_generation = (
                    str(expected_access_token or "").strip(),
                    str(expected_refresh_token or "").strip(),
                    str(expected_last_token_refresh_at or "").strip(),
                )
                if self._credential_generation(resolved, current) != expected_generation:
                    return False
            next_item = dict(current)
            next_item["last_token_refresh_error"] = str(error or "refresh token failed")
            next_item["last_token_refresh_error_at"] = now
            if terminal:
                next_item["refresh_token_invalid_at"] = now
            account = self._normalize_account(next_item)
            if account is None:
                return False
            self._accounts[resolved] = account
            saved = self._save_accounts(
                expected_credential_generation=expected_generation,
            )
            if not saved:
                return False
            resolved = self._resolve_access_token_locked(resolved)
            persisted = self._accounts.get(resolved)
            if (
                persisted is None
                or persisted.get("last_token_refresh_error_at") != now
            ):
                return False
        log_service.add(
            LOG_TYPE_ACCOUNT,
            "refresh_token 刷新 access_token 失败",
            {
                "source": event,
                "token": anonymize_token(resolved),
                "error": str(persisted.get("last_token_refresh_error") or ""),
            },
        )
        return True

    def _recent_token_refresh_error(self, account: dict) -> bool:
        last_error_at = self._parse_time(account.get("last_token_refresh_error_at"))
        if last_error_at is None:
            return False
        return (datetime.now(timezone.utc) - last_error_at).total_seconds() < self._TOKEN_REFRESH_ERROR_BACKOFF_SECONDS

    def _request_access_token_refresh(
        self,
        refresh_token: str,
        account: dict | None = None,
        *,
        image_scope: bool = False,
    ) -> dict[str, str]:
        from curl_cffi import requests
        from services.proxy_service import proxy_settings

        session = requests.Session(**proxy_settings.build_session_kwargs(account=account, impersonate="chrome110", verify=True))
        try:
            with account_processing_slot():
                response = session.post(
                    self._OAUTH_TOKEN_URL,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": self._OAUTH_USER_AGENT,
                    },
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": self._OAUTH_CLIENT_ID,
                    },
                    timeout=60,
                )
            raw_text = self._safe_response_text(response)
            try:
                data = response.json() if raw_text else {}
            except Exception:
                data = {}
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code != 200 or not isinstance(data, dict) or not data.get("access_token"):
                error_code, description = self._oauth_refresh_error_fields(data)
                description = description or raw_text
                error_type = (
                    TerminalRefreshTokenError
                    if self._is_terminal_refresh_error(status_code, error_code, description)
                    else OAuthRefreshError
                )
                raise error_type(status_code, error_code, description)
            return {
                "access_token": str(data.get("access_token") or "").strip(),
                "refresh_token": str(data.get("refresh_token") or refresh_token).strip(),
                "id_token": str(data.get("id_token") or "").strip(),
            }
        finally:
            session.close()

    def _apply_refreshed_tokens(
        self,
        old_access_token: str,
        token_data: dict,
        event: str,
        *,
        expected_access_token: str | None = None,
        expected_refresh_token: str,
        expected_last_token_refresh_at: str | None = None,
    ) -> str:
        now = datetime.now(timezone.utc).isoformat()
        expected_access_token = expected_access_token or old_access_token
        expected_generation = (
            str(expected_access_token or "").strip(),
            str(expected_refresh_token or "").strip(),
            str(expected_last_token_refresh_at or "").strip(),
        )
        with self._image_slot_condition:
            old_token = self._resolve_access_token_locked(old_access_token)
            current = self._accounts.get(old_token)
            if current is None:
                raise RefreshCredentialsChangedError()
            if self._credential_generation(old_token, current) != expected_generation:
                raise RefreshCredentialsChangedError()
            new_token = str(token_data.get("access_token") or old_token).strip()
            if not new_token:
                return old_token
            if new_token != old_token and new_token in self._accounts:
                raise RefreshCredentialsChangedError()

            next_item = dict(current)
            next_item["access_token"] = new_token
            if token_data.get("refresh_token"):
                next_item["refresh_token"] = str(token_data.get("refresh_token") or "").strip()
            if token_data.get("id_token"):
                next_item["id_token"] = str(token_data.get("id_token") or "").strip()
            self._scrub_diagnostic_secrets(
                next_item,
                [
                    current.get("access_token"),
                    current.get("refresh_token"),
                    current.get("id_token"),
                ],
            )
            next_item["last_token_refresh_at"] = now
            next_item["last_token_refresh_error"] = None
            next_item["last_token_refresh_error_at"] = None
            next_item["refresh_token_invalid_at"] = None
            next_item["invalid_count"] = 0
            next_item["last_invalid_at"] = None
            next_item["last_refresh_error"] = None
            next_item["last_refresh_error_at"] = None
            if current.get("status") == "\u5f02\u5e38":
                next_item["status"] = "\u6b63\u5e38"
            if current.get("last_remote_check_result") == "invalid":
                next_item["last_remote_check_result"] = None
                next_item["last_remote_check_error"] = None
                next_item["last_remote_check_error_at"] = None
                next_item["last_remote_check_event"] = None
                next_item["pending_auth_remove_invalid"] = None
                next_item["pending_auth_scope"] = None

            account = self._normalize_account(next_item)
            if account is None:
                return old_token

            rotated = new_token != old_token
            alias_sources = {
                source
                for source in {old_token, *self._token_aliases}
                if self._resolve_access_token_locked(source) == old_token
            }
            if rotated:
                self._accounts.pop(old_token, None)
            self._accounts[new_token] = account
            saved = self._save_accounts(
                expected_credential_generation=expected_generation,
            )
            final_token = (
                new_token
                if new_token in self._accounts
                else next(
                    (
                        token
                        for token, item in self._accounts.items()
                        if item.get("management_id") == current.get("management_id")
                    ),
                    None,
                )
            )
            if not saved or final_token != new_token:
                if final_token:
                    self._move_account_runtime_token_locked(
                        final_token,
                        alias_sources,
                    )
                else:
                    self._remove_account_runtime_state_locked(alias_sources)
                self._image_slot_condition.notify_all()
                raise RefreshCredentialsChangedError()
            if rotated:
                self._move_account_runtime_token_locked(
                    new_token,
                    alias_sources,
                )
            self._image_slot_condition.notify_all()

        log_service.add(
            LOG_TYPE_ACCOUNT,
            "refresh_token 已刷新 access_token",
            {"source": event, "token": anonymize_token(new_token), "rotated": rotated},
        )
        return new_token

    def _refresh_access_token_owner(
        self,
        active_token: str,
        refresh_token: str,
        account: dict,
        *,
        event: str,
        image_scope: bool,
    ) -> str:
        try:
            if image_scope:
                token_data = self._request_access_token_refresh(
                    refresh_token,
                    account,
                    image_scope=True,
                )
            else:
                token_data = self._request_access_token_refresh(refresh_token, account)
        except TerminalRefreshTokenError as exc:
            exc.expected_access_token = active_token
            exc.expected_refresh_token = refresh_token
            exc.expected_last_token_refresh_at = str(
                account.get("last_token_refresh_at") or ""
            )
            current_token, current_refresh_token, current = self._credential_snapshot(active_token)
            if not current or (current_token, current_refresh_token) != (active_token, refresh_token):
                raise RefreshCredentialsChangedError() from exc
            raise
        except Exception as exc:
            recorded = self._record_token_refresh_error(
                active_token,
                event,
                str(exc or ""),
                expected_access_token=active_token,
                expected_refresh_token=refresh_token,
                expected_last_token_refresh_at=str(
                    account.get("last_token_refresh_at") or ""
                ),
            )
            if recorded:
                raise
            raise RefreshCredentialsChangedError() from exc

        return self._apply_refreshed_tokens(
            active_token,
            token_data,
            event,
            expected_access_token=active_token,
            expected_refresh_token=refresh_token,
            expected_last_token_refresh_at=str(
                account.get("last_token_refresh_at") or ""
            ),
        )

    def _maintain_access_token(
        self,
        access_token: str,
        *,
        force: bool = False,
        event: str = "refresh_access_token",
        raise_on_error: bool = False,
        image_scope: bool = False,
        expected_credentials: _CredentialGeneration | None = None,
        skip_if_image_busy: bool = False,
    ) -> str:
        if not access_token:
            return ""
        for credential_attempt in range(2):
            resolved_token, account = self._get_account_for_token(access_token)
            if not account:
                raise RefreshCredentialsChangedError()
            active_token = str(account.get("access_token") or resolved_token or access_token)
            needs_refresh = self._token_needs_refresh(active_token, force=force)
            refresh_backoff = not force and self._recent_token_refresh_error(account)
            refresh_token = str(account.get("refresh_token") or "").strip()
            current_generation = self._credential_generation(active_token, account)
            if (
                expected_credentials is not None
                and current_generation != expected_credentials
            ):
                raise RefreshCredentialsChangedError()
            if not refresh_token:
                return active_token
            if account.get("refresh_token_invalid_at") and not force:
                remaining = self._token_expires_in(active_token)
                if remaining is None or remaining > 0:
                    return active_token
                raise TerminalRefreshTokenError(
                    400,
                    "invalid_refresh_token",
                    "refresh token is marked invalid",
                )

            key = current_generation
            if skip_if_image_busy:
                with self._image_slot_condition:
                    current_token = self._resolve_access_token_locked(active_token)
                    current = self._accounts.get(current_token)
                    current_generation = self._credential_generation(current_token, current)
                    if not current or current_generation != key or (
                        expected_credentials is not None
                        and current_generation != expected_credentials
                    ):
                        if expected_credentials is not None or credential_attempt > 0:
                            raise RefreshCredentialsChangedError()
                        continue
                    image_busy = int(self._image_inflight.get(current_token, 0)) > 1
                    with self._oauth_refresh_flights_lock:
                        future = self._oauth_refresh_flights.get(key)
                        owner = future is None
                        if future is None:
                            if image_busy or not needs_refresh or refresh_backoff:
                                return active_token
                            future = Future()
                            self._oauth_refresh_flights[key] = future
            else:
                with self._oauth_refresh_flights_lock:
                    future = self._oauth_refresh_flights.get(key)
                    owner = future is None
                    if future is None:
                        if not needs_refresh or refresh_backoff:
                            return active_token
                        future = Future()
                        self._oauth_refresh_flights[key] = future
            if owner:
                try:
                    result = self._refresh_access_token_owner(
                        active_token,
                        refresh_token,
                        account,
                        event=event,
                        image_scope=image_scope,
                    )
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            try:
                return future.result()
            except TerminalRefreshTokenError as exc:
                expected_access_token = str(
                    getattr(exc, "expected_access_token", active_token) or active_token
                )
                expected_refresh_token = str(
                    getattr(exc, "expected_refresh_token", refresh_token) or refresh_token
                )
                expected_last_token_refresh_at = str(
                    getattr(exc, "expected_last_token_refresh_at", "") or ""
                )
                current_token, current_refresh_token, current = self._credential_snapshot(active_token)
                if current and (current_token, current_refresh_token) != (
                    expected_access_token,
                    expected_refresh_token,
                ):
                    if expected_credentials is not None:
                        raise RefreshCredentialsChangedError() from exc
                    if credential_attempt == 0:
                        access_token = current_token
                        continue
                    raise RefreshCredentialsChangedError() from exc
                error_str = str(exc)
                recorded = self._record_token_refresh_error(
                    active_token,
                    event,
                    error_str,
                    expected_access_token=expected_access_token,
                    expected_refresh_token=expected_refresh_token,
                    expected_last_token_refresh_at=expected_last_token_refresh_at,
                    terminal=True,
                )
                if not recorded:
                    current_token, _current_refresh, current = self._credential_snapshot(
                        active_token
                    )
                    if current is not None:
                        if (
                            expected_credentials is not None
                            and self._credential_generation(current_token, current)
                            != expected_credentials
                        ):
                            raise RefreshCredentialsChangedError() from exc
                        return current_token
                    raise RefreshCredentialsChangedError() from exc
                raise
            except RefreshCredentialsChangedError:
                current_token, current = self._get_account_for_token(active_token)
                if current is None:
                    raise
                latest_generation = self._credential_generation(current_token, current)
                if expected_credentials is not None or latest_generation == current_generation:
                    raise
                # A concurrent successful exchange already produced a usable AT.
                if latest_generation[2] != current_generation[2]:
                    return str(current.get("access_token") or current_token or active_token)
                if credential_attempt == 0:
                    access_token = current_token
                    continue
                raise
            except Exception:
                if raise_on_error:
                    raise
                current_token, current = self._get_account_for_token(active_token)
                if current:
                    return str(current.get("access_token") or current_token or active_token)
                raise RefreshCredentialsChangedError()
            finally:
                if owner:
                    with self._oauth_refresh_flights_lock:
                        if self._oauth_refresh_flights.get(key) is future:
                            self._oauth_refresh_flights.pop(key, None)
        raise RefreshCredentialsChangedError()

    def ensure_access_token(
        self,
        access_token: str,
        *,
        event: str = "ensure_access_token",
        raise_on_error: bool = False,
        image_scope: bool = False,
        expected_credentials: _CredentialGeneration | None = None,
        skip_if_image_busy: bool = False,
    ) -> str:
        """Return a usable AT, renewing it through RT only when needed."""
        return self._maintain_access_token(
            access_token,
            force=False,
            event=event,
            raise_on_error=raise_on_error,
            image_scope=image_scope,
            expected_credentials=expected_credentials,
            skip_if_image_busy=skip_if_image_busy,
        )

    def force_refresh_access_token(
        self,
        access_token: str,
        *,
        event: str = "force_refresh_access_token",
        raise_on_error: bool = False,
        image_scope: bool = False,
        expected_credentials: _CredentialGeneration | None = None,
    ) -> str:
        """Force one RT-to-AT exchange for an explicit recovery or admin action."""
        return self._maintain_access_token(
            access_token,
            force=True,
            event=event,
            raise_on_error=raise_on_error,
            image_scope=image_scope,
            expected_credentials=expected_credentials,
        )

    def refresh_access_token(
        self,
        access_token: str,
        *,
        force: bool = False,
        event: str = "refresh_access_token",
        remove_invalid: bool | None = None,
        raise_on_error: bool = False,
        image_scope: bool = False,
        expected_credentials: _CredentialGeneration | None = None,
        skip_if_image_busy: bool = False,
    ) -> str:
        """Compatibility wrapper; new callers use ensure/force methods."""
        _ = remove_invalid
        operation = self.force_refresh_access_token if force else self.ensure_access_token
        kwargs = {
            "event": event,
            "raise_on_error": raise_on_error,
            "image_scope": image_scope,
            "expected_credentials": expected_credentials,
        }
        if not force:
            kwargs["skip_if_image_busy"] = skip_if_image_busy
        return operation(access_token, **kwargs)

    def list_expiring_access_tokens(self) -> list[str]:
        self._refresh_accounts_snapshot_if_stale()
        with self._lock:
            return [
                token
                for account in self._accounts.values()
                if account.get("status") in {"正常", "限流"}
                and account.get("last_remote_check_result") != "pending"
                and str(account.get("refresh_token") or "").strip()
                and not account.get("refresh_token_invalid_at")
                and (token := str(account.get("access_token") or "").strip())
                and self._token_needs_refresh(token)
            ]

    def _exchange_access_tokens(
        self,
        access_tokens: list[str],
        progress_id: str | None = None,
        *,
        force: bool,
        event: str,
        finalize_progress: bool = True,
    ) -> dict[str, Any]:
        """Exchange RT-to-AT credentials without querying account metadata."""
        requested_tokens = list(dict.fromkeys(token for token in access_tokens if token))
        if not requested_tokens:
            result = {
                "refreshed": 0,
                "skipped": 0,
                "updated_ids": [],
                "removed_ids": [],
                "errors": [],
            }
            if progress_id and finalize_progress:
                self.finish_refresh_progress(progress_id, result)
            return result

        if progress_id and self.get_refresh_progress(progress_id) is None:
            self.init_refresh_progress(progress_id, len(requested_tokens))

        targets: list[tuple[str, str]] = []
        initial_errors: list[dict[str, Any]] = []
        for requested_token in requested_tokens:
            active_token, _refresh_token, account = self._credential_snapshot(requested_token)
            account_id = str((account or {}).get("management_id") or "").strip()
            if not account or not account_id:
                error = {
                    "id": account_id,
                    "token": anonymize_token(requested_token),
                    "code": "account_not_found",
                    "error": "account not found",
                }
                initial_errors.append(error)
                if progress_id:
                    self.update_refresh_progress(
                        progress_id,
                        requested_token,
                        account_id=account_id,
                        action="refresh_access_token",
                        event_status="failed",
                        event_message=error["error"],
                    )
                continue
            targets.append((active_token, account_id))

        refreshed = 0
        skipped = 0
        refreshed_ids: list[str] = []
        errors = list(initial_errors)

        def exchange(
            token: str,
            account_id: str,
        ) -> tuple[str, dict | None, bool, dict[str, Any] | None]:
            active_token, refresh_token, account = self._credential_snapshot(token)
            if not account:
                return token, None, False, {
                    "id": account_id,
                    "token": anonymize_token(token),
                    "code": "account_not_found",
                    "error": "account not found",
                }
            if not refresh_token:
                return active_token, account, False, {
                    "id": account_id,
                    "token": anonymize_token(active_token),
                    "code": "refresh_token_missing",
                    "error": "refresh token is missing",
                }
            if account.get("refresh_token_invalid_at"):
                return active_token, account, False, {
                    "id": account_id,
                    "token": anonymize_token(active_token),
                    "code": "refresh_token_invalid",
                    "error": "refresh token is invalid",
                }
            before_refresh_at = str(account.get("last_token_refresh_at") or "")
            try:
                operation = (
                    self.force_refresh_access_token
                    if force
                    else self.ensure_access_token
                )
                refreshed_token = operation(
                    active_token,
                    raise_on_error=True,
                    event=event,
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                failure = classify_image_exception(exc)
                current_token = self.resolve_access_token(active_token) or active_token
                current_account = self.get_account(current_token) or account
                return current_token, self.get_account(current_token), False, {
                    "id": account_id,
                    "token": anonymize_token(active_token),
                    "error": self._credential_error_text(
                        exc,
                        current_account,
                        access_token=active_token,
                    ),
                    **failure.diagnostic_fields(),
                }
            result_account = self.get_account(refreshed_token)
            after_refresh_at = str((result_account or {}).get("last_token_refresh_at") or "")
            exchanged = bool(
                refreshed_token != active_token
                or (after_refresh_at and after_refresh_at != before_refresh_at)
            )
            return refreshed_token, result_account, exchanged, None

        max_workers = account_processing_worker_count(len(targets))
        executor = ThreadPoolExecutor(max_workers=max_workers) if max_workers else None
        try:
            futures = {
                executor.submit(exchange, token, account_id): (token, account_id)
                for token, account_id in targets
            } if executor else {}
            for future in as_completed(futures):
                token, account_id = futures[future]
                active_token, result_account, exchanged, error = future.result()
                if error:
                    errors.append(error)
                    event_status = "failed"
                    event_message = str(
                        error.get("error")
                        or "\u8bbf\u95ee\u4ee4\u724c\u5237\u65b0\u5931\u8d25"
                    )
                elif exchanged:
                    refreshed += 1
                    refreshed_ids.append(account_id)
                    event_status = "success"
                    event_message = "\u8bbf\u95ee\u4ee4\u724c\u5df2\u5237\u65b0"
                else:
                    skipped += 1
                    event_status = "skipped"
                    event_message = "\u8bbf\u95ee\u4ee4\u724c\u65e0\u9700\u66f4\u65b0"
                if progress_id:
                    self.update_refresh_progress(
                        progress_id,
                        active_token or token,
                        result_account,
                        account_id=account_id,
                        action="refresh_access_token",
                        event_status=event_status,
                        event_message=event_message,
                    )
        except (KeyboardInterrupt, SystemExit):
            if progress_id:
                self.finish_refresh_progress(progress_id, error="cancelled")
            if executor:
                executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            if executor:
                executor.shutdown(wait=True, cancel_futures=True)

        existing_ids = {
            str(account.get("management_id") or "").strip()
            for account in self.list_accounts()
            if str(account.get("management_id") or "").strip()
        }
        target_ids = [account_id for _token, account_id in targets]
        result = {
            "refreshed": refreshed,
            "skipped": skipped,
            "updated_ids": [
                account_id for account_id in refreshed_ids if account_id in existing_ids
            ],
            "removed_ids": [account_id for account_id in target_ids if account_id not in existing_ids],
            "errors": errors,
        }
        if progress_id and finalize_progress:
            self.finish_refresh_progress(progress_id, result)
        return result

    def refresh_access_tokens(
        self,
        access_tokens: list[str],
        progress_id: str | None = None,
        *,
        finalize_progress: bool = True,
    ) -> dict[str, Any]:
        """Force RT-to-AT exchanges for an explicit administrator action."""
        return self._exchange_access_tokens(
            access_tokens,
            progress_id,
            force=True,
            event="refresh_access_tokens",
            finalize_progress=finalize_progress,
        )

    def renew_expiring_access_tokens(self, access_tokens: list[str]) -> dict[str, Any]:
        """Conditionally renew ATs selected by a periodic expiry scan."""
        return self._exchange_access_tokens(
            access_tokens,
            force=False,
            event="renew_expiring_access_tokens",
        )
