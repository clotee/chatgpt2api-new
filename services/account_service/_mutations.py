from __future__ import annotations

from ._shared import _RemoteCheckMarker
from services.account_processing import account_processing_batch
from services.config import config
from services.log_service import LOG_TYPE_ACCOUNT, log_service
from typing import Any, Callable
from utils.helper import anonymize_token

class AccountMutationMixin:
    """account add/update/delete/export mutations（T-21 Mixin 拆分，方法块自 _all.py 原样搬迁）。"""

    @staticmethod
    def _account_payload_token(item: dict) -> str:
        return str(item.get("access_token") or item.get("accessToken") or "").strip()

    @staticmethod
    def _refresh_token_aware_updates(
        current: dict[str, Any],
        updates: dict[str, Any],
        *,
        preserve_lifecycle: bool = False,
    ) -> dict[str, Any]:
        normalized = dict(updates)
        if "refresh_token" not in normalized:
            return normalized
        current_refresh_token = str(current.get("refresh_token") or "").strip()
        updated_refresh_token = str(normalized.get("refresh_token") or "").strip()
        if preserve_lifecycle and "refresh_token_invalid_at" in normalized:
            normalized["refresh_token_invalid_at"] = (
                normalized.get("refresh_token_invalid_at") or None
            )
        else:
            normalized["refresh_token_invalid_at"] = (
                None
                if updated_refresh_token != current_refresh_token
                else current.get("refresh_token_invalid_at") or None
            )
        return normalized

    @staticmethod
    def _prepare_account_payload(item: dict, *, restore: bool = False) -> dict | None:
        if not isinstance(item, dict):
            return None
        access_token = AccountMutationMixin._account_payload_token(item)
        if not access_token:
            return None
        payload = dict(item)
        payload.pop("accessToken", None)
        if not restore:
            payload.pop("management_id", None)
            payload.pop("access_token_fingerprints", None)
        payload["access_token"] = access_token
        # CPA/Codex 导出文件里的 `type=codex` 是导出格式，不是号池套餐类型。
        if str(payload.get("type") or "").strip().lower() == "codex":
            payload["export_type"] = "codex"
            payload["source_type"] = "codex"
            payload.pop("type", None)
        if str(payload.get("export_type") or "").strip().lower() == "codex":
            payload["source_type"] = "codex"
        if payload.get("plan_type") and not payload.get("type"):
            payload["type"] = str(payload.get("plan_type") or "").strip()
        return payload

    def _run_proxy_assignment_mutation(
        self,
        references: list[tuple[object, object]],
        mutation: Callable[[list[str]], Any],
    ) -> Any:
        if self._proxy_reference_mutation is not None:
            return self._proxy_reference_mutation(references, mutation)

        normalized: list[str] = []
        for value, legacy_group_id in references:
            raw = str(value or "").strip()
            if not raw and str(legacy_group_id or "").strip():
                raw = f"group:{str(legacy_group_id).strip()}"
            if raw.lower() == "global":
                raw = ""
            normalized.append(raw)
        return mutation(normalized)

    def _with_normalized_proxy_updates(
        self,
        updates: dict[str, Any],
        mutation: Callable[[dict[str, Any]], Any],
    ) -> Any:
        normalized_updates = dict(updates)
        if "proxy" not in normalized_updates and "proxy_group_id" not in normalized_updates:
            return mutation(normalized_updates)

        return self._run_proxy_assignment_mutation(
            [
                (
                    normalized_updates.get("proxy"),
                    normalized_updates.get("proxy_group_id"),
                )
            ],
            lambda normalized: mutation({
                **normalized_updates,
                "proxy": normalized[0],
            }),
        )

    @account_processing_batch
    def add_account_items(
        self,
        items: list[dict],
        return_items: bool = True,
        *,
        restore: bool = False,
        return_item_results: bool = False,
    ) -> dict:
        source_items = list(items)
        payloads: list[dict] = []
        payload_indices: list[int] = []
        for index, item in enumerate(source_items):
            payload = self._prepare_account_payload(item, restore=restore)
            if payload is None:
                continue
            payloads.append(payload)
            payload_indices.append(index)

        result = self._add_account_payloads(
            payloads,
            return_items=return_items,
            preserve_lifecycle=restore,
            return_item_results=return_item_results,
        )
        if return_item_results:
            aligned_results = ["invalid"] * len(source_items)
            for index, status in zip(
                payload_indices,
                result.get("item_results") or [],
            ):
                aligned_results[index] = status
            result["item_results"] = aligned_results
        return result

    @account_processing_batch
    def add_accounts(self, tokens: list[str], source_type: str = "web", return_items: bool = True) -> dict:
        tokens = list(dict.fromkeys(token for token in tokens if token))
        if not tokens:
            return {"added": 0, "skipped": 0, "items": self.list_accounts() if return_items else []}
        return self._add_account_payloads([
            {"access_token": token, "source_type": self._normalize_source_type(source_type)}
            for token in tokens
        ], return_items=return_items)

    def _add_account_payloads(
        self,
        payloads: list[dict],
        return_items: bool = True,
        *,
        preserve_lifecycle: bool = False,
        return_item_results: bool = False,
    ) -> dict:
        proxy_payload_indices = [
            index
            for index, payload in enumerate(payloads)
            if (
                isinstance(payload, dict)
                and self._account_payload_token(payload)
                and ("proxy" in payload or "proxy_group_id" in payload)
            )
        ]
        if not proxy_payload_indices:
            return self._add_account_payloads_impl(
                payloads,
                return_items=return_items,
                preserve_lifecycle=preserve_lifecycle,
                return_item_results=return_item_results,
            )

        references = [
            (
                payloads[index].get("proxy"),
                payloads[index].get("proxy_group_id"),
            )
            for index in proxy_payload_indices
        ]

        def persist(normalized_references: list[str]) -> dict:
            normalized_payloads = list(payloads)
            for index, proxy in zip(proxy_payload_indices, normalized_references):
                normalized_payloads[index] = {
                    **payloads[index],
                    "proxy": proxy,
                }
            return self._add_account_payloads_impl(
                normalized_payloads,
                return_items=return_items,
                preserve_lifecycle=preserve_lifecycle,
                return_item_results=return_item_results,
            )

        return self._run_proxy_assignment_mutation(references, persist)

    def _add_account_payloads_impl(
        self,
        payloads: list[dict],
        return_items: bool = True,
        *,
        preserve_lifecycle: bool = False,
        return_item_results: bool = False,
    ) -> dict:
        deduped: dict[str, dict] = {}
        input_tokens: list[str] = []
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            access_token = self._account_payload_token(payload)
            if not access_token:
                continue
            input_tokens.append(access_token)
            current = deduped.get(access_token, {})
            deduped[access_token] = {**current, **payload, "access_token": access_token}

        if not deduped:
            result = {
                "added": 0,
                "skipped": 0,
                "items": self.list_accounts() if return_items else [],
            }
            if return_item_results:
                result["item_results"] = []
            return result

        with self._lock:
            added = 0
            skipped = len(input_tokens) - len(deduped)
            outcomes_by_token: dict[str, str] = {}
            management_id_owners = {
                str(account.get("management_id") or "").strip().lower(): token
                for token, account in self._accounts.items()
                if str(account.get("management_id") or "").strip()
            }
            token_fingerprint_owners = {
                fingerprint: token
                for token, account in self._accounts.items()
                for fingerprint in self._normalize_access_token_fingerprints(
                    account.get("access_token_fingerprints"),
                    token,
                )
            }
            for access_token, payload in deduped.items():
                resolved_token = self._resolve_access_token_locked(access_token)
                if resolved_token != access_token and resolved_token in self._accounts:
                    # Re-importing a rotated token must not recreate its former account.
                    skipped += 1
                    outcomes_by_token[access_token] = "skipped"
                    continue
                current = self._accounts.get(access_token)
                if current is None:
                    fingerprint = self._access_token_fingerprint(access_token)
                    if fingerprint in token_fingerprint_owners:
                        # Persisted fingerprints preserve rotation lineage across restarts
                        # without storing former access tokens.
                        skipped += 1
                        outcomes_by_token[access_token] = "skipped"
                        continue
                    added += 1
                    outcomes_by_token[access_token] = "added"
                    current = {"created_at": self._now()}
                else:
                    skipped += 1
                    outcomes_by_token[access_token] = "skipped"
                incoming = self._refresh_token_aware_updates(
                    current,
                    payload,
                    preserve_lifecycle=preserve_lifecycle,
                )
                if not incoming.get("created_at"):
                    incoming.pop("created_at", None)
                merged = {**current, **incoming, "access_token": access_token}
                incoming_type = self._normalize_account_type(incoming.get("type"))
                current_type = self._normalize_account_type(current.get("type"))
                merged["type"] = incoming_type or current_type
                account = self._normalize_account(merged)
                if account is not None:
                    previous_management_id = str(
                        current.get("management_id") or ""
                    ).strip().lower()
                    used_management_ids = {
                        management_id
                        for management_id, owner in management_id_owners.items()
                        if owner != access_token
                    }
                    account["management_id"] = self._unique_management_id(
                        access_token,
                        account.get("management_id"),
                        used_management_ids,
                    )
                    self._accounts[access_token] = account
                    if (
                        previous_management_id
                        and previous_management_id != account["management_id"]
                        and management_id_owners.get(previous_management_id) == access_token
                    ):
                        management_id_owners.pop(previous_management_id, None)
                    management_id_owners[account["management_id"]] = access_token
                    for fingerprint in account["access_token_fingerprints"]:
                        token_fingerprint_owners[fingerprint] = access_token
            conflict_existing_tokens: set[str] = set()
            self._save_accounts(
                conflict_existing_tokens=conflict_existing_tokens,
            )
            for access_token in conflict_existing_tokens:
                if outcomes_by_token.get(access_token) != "added":
                    continue
                outcomes_by_token[access_token] = "skipped"
                added = max(0, added - 1)
                skipped += 1
            if added:
                self._cumulative_total += added
                self._save_cumulative_total()
            items = [dict(item) for item in self._accounts.values()] if return_items else []
            log_service.add(LOG_TYPE_ACCOUNT, f"新增 {added} 个账号，跳过 {skipped} 个",
                            {"added": added, "skipped": skipped})
        result = {"added": added, "skipped": skipped, "items": items}
        if return_item_results:
            seen_tokens: set[str] = set()
            item_results: list[str] = []
            for access_token in input_tokens:
                if access_token in seen_tokens:
                    item_results.append("skipped")
                    continue
                seen_tokens.add(access_token)
                item_results.append(outcomes_by_token.get(access_token, "skipped"))
            result["item_results"] = item_results
        return result

    @staticmethod
    def _report_account_mutation_progress(
        callback: Callable[[str, int], None] | None,
        stage: str,
        total: int,
    ) -> None:
        if callback is None:
            return
        try:
            callback(stage, max(0, int(total)))
        except Exception:
            # Progress reporting is auxiliary and must not change a committed mutation.
            pass

    @staticmethod
    def _add_account_log_best_effort(message: str, data: dict[str, Any]) -> None:
        try:
            log_service.add(LOG_TYPE_ACCOUNT, message, data)
        except Exception:
            # The account store is authoritative; diagnostics must not reverse its outcome.
            pass

    @account_processing_batch
    def delete_accounts(
        self,
        tokens: list[str],
        return_items: bool = True,
        *,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> dict:
        requested_tokens = list(dict.fromkeys(token for token in tokens if token))
        if not requested_tokens:
            return {
                "removed": 0,
                "removed_ids": [],
                "missing_tokens": [],
                "items": self.list_accounts() if return_items else [],
            }
        self._report_account_mutation_progress(
            progress_callback,
            "prepare_accounts",
            len(requested_tokens),
        )
        with self._lock:
            target_tokens: list[str] = []
            candidate_ids: list[str] = []
            missing_tokens: list[str] = []
            seen_tokens: set[str] = set()
            for requested_token in requested_tokens:
                access_token = self._resolve_access_token_locked(requested_token)
                if access_token in seen_tokens:
                    continue
                seen_tokens.add(access_token)
                account = self._accounts.get(access_token)
                if account is None:
                    missing_tokens.append(requested_token)
                    continue
                target_tokens.append(access_token)
                management_id = str(account.get("management_id") or "").strip().lower()
                if management_id:
                    candidate_ids.append(management_id)

            target_set = set(target_tokens)
            removed = sum(self._accounts.pop(token, None) is not None for token in target_tokens)
            if removed:
                self._report_account_mutation_progress(
                    progress_callback,
                    "save_accounts",
                    removed,
                )
                self._save_accounts()
                self._remove_account_runtime_state_locked(target_set)
                self._add_account_log_best_effort(
                    f"删除 {removed} 个账号",
                    {"removed": removed},
                )
            existing_ids = {
                str(account.get("management_id") or "").strip().lower()
                for account in self._accounts.values()
                if str(account.get("management_id") or "").strip()
            }
            removed_ids = [
                management_id
                for management_id in dict.fromkeys(candidate_ids)
                if management_id not in existing_ids
            ]
            items = [dict(item) for item in self._accounts.values()] if return_items else []
        self._report_account_mutation_progress(
            progress_callback,
            "publish_results",
            len(removed_ids),
        )
        return {
            "removed": len(removed_ids),
            "removed_ids": removed_ids,
            "missing_tokens": missing_tokens,
            "items": items,
        }

    def _normalized_account_update_locked(
            self,
            access_token: str,
            current: dict[str, Any],
            updates: dict[str, Any],
            *,
            preserve_disabled: bool = False,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Build one normalized account update while the account lock is held."""
        normalized_updates = self._refresh_token_aware_updates(current, updates)
        merged = {**current, **normalized_updates, "access_token": access_token}
        if (
            "proxy" in updates
            and str(current.get("proxy") or "").strip()
            != str(merged.get("proxy") or "").strip()
        ):
            self._scrub_diagnostic_secrets(merged, [current.get("proxy")])
        if preserve_disabled and current.get("status") == "禁用":
            merged["status"] = "禁用"
        if (
            current.get("status") == "限流"
            and "status" in updates
            and self._normalize_account_status(updates.get("status"), merged) == "正常"
            and "restore_at" not in updates
        ):
            merged["restore_at"] = None

        account = self._normalize_account(merged)
        if account is None:
            return None, None
        if account.get("status") == "异常" and config.auto_remove_invalid_accounts:
            return account, "自动移除异常账号"
        if account.get("status") == "限流" and config.auto_remove_rate_limited_accounts:
            return account, "自动移除额度耗尽账号"
        return account, None

    @account_processing_batch
    def update_accounts(
        self,
        access_tokens: list[str],
        updates: dict,
        *,
        quiet: bool = True,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> dict[str, Any]:
        if not updates or not any(str(token or "").strip() for token in access_tokens):
            return self._update_accounts_impl(
                access_tokens,
                updates,
                quiet=quiet,
                progress_callback=progress_callback,
            )
        return self._with_normalized_proxy_updates(
            updates,
            lambda normalized: self._update_accounts_impl(
                access_tokens,
                normalized,
                quiet=quiet,
                progress_callback=progress_callback,
            ),
        )

    def _update_accounts_impl(
        self,
        access_tokens: list[str],
        updates: dict,
        *,
        quiet: bool = True,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> dict[str, Any]:
        """Apply one update to many accounts with a single storage mutation."""
        requested_tokens = list(dict.fromkeys(
            str(token or "").strip() for token in access_tokens if str(token or "").strip()
        ))
        if not requested_tokens or not updates:
            return {
                "updated_ids": [],
                "removed_ids": [],
                "missing_tokens": requested_tokens,
            }

        self._report_account_mutation_progress(
            progress_callback,
            "prepare_accounts",
            len(requested_tokens),
        )

        with self._lock:
            requested: list[tuple[str, str]] = []
            missing_tokens: list[str] = []
            auto_remove_events: list[tuple[str, str]] = []
            seen_tokens: set[str] = set()
            changed = False

            for requested_token in requested_tokens:
                access_token = self._resolve_access_token_locked(requested_token)
                if access_token in seen_tokens:
                    continue
                seen_tokens.add(access_token)
                current = self._accounts.get(access_token)
                if current is None:
                    missing_tokens.append(requested_token)
                    continue

                management_id = str(current.get("management_id") or "").strip().lower()
                if not management_id:
                    missing_tokens.append(requested_token)
                    continue
                requested.append((access_token, management_id))

                account, auto_remove_message = self._normalized_account_update_locked(
                    access_token,
                    current,
                    updates,
                )
                if account is None:
                    missing_tokens.append(requested_token)
                    continue
                if auto_remove_message:
                    self._accounts.pop(access_token, None)
                    changed = True
                    auto_remove_events.append((access_token, auto_remove_message))
                    continue

                if account != current:
                    self._accounts[access_token] = account
                    changed = True

            if changed:
                self._report_account_mutation_progress(
                    progress_callback,
                    "save_accounts",
                    len(requested),
                )
                self._save_accounts()

            existing_ids = {
                str(account.get("management_id") or "").strip().lower()
                for account in self._accounts.values()
                if str(account.get("management_id") or "").strip()
            }
            updated_ids = [
                management_id
                for _token, management_id in requested
                if management_id in existing_ids
            ]
            removed_ids = [
                management_id
                for _token, management_id in requested
                if management_id not in existing_ids
            ]
            removed_tokens = {
                token
                for token, management_id in requested
                if management_id not in existing_ids
            }
            if removed_tokens:
                self._remove_account_runtime_state_locked(removed_tokens)

            for access_token, message in auto_remove_events:
                self._add_account_log_best_effort(
                    message,
                    {"token": anonymize_token(access_token)},
                )
            if not quiet and updated_ids:
                self._add_account_log_best_effort(
                    f"\u6279\u91cf\u66f4\u65b0 {len(updated_ids)} \u4e2a\u8d26\u53f7",
                    {"updated": len(updated_ids)},
                )

            result = {
                "updated_ids": updated_ids,
                "removed_ids": removed_ids,
                "missing_tokens": missing_tokens,
            }
        self._report_account_mutation_progress(
            progress_callback,
            "publish_results",
            len(result["updated_ids"]) + len(result["removed_ids"]),
        )
        return result

    @staticmethod
    def _remote_check_marker(
        account: dict | None,
    ) -> _RemoteCheckMarker:
        item = account or {}
        return (
            str(item.get("last_token_refresh_at") or ""),
            str(item.get("last_remote_check_result") or ""),
            str(item.get("last_remote_check_attempt_at") or ""),
            str(item.get("last_remote_check_event") or ""),
            item.get("pending_auth_remove_invalid"),
            str(item.get("pending_auth_scope") or ""),
            str(item.get("pending_auth_verification_id") or ""),
        )

    def update_account(
        self,
        access_token: str,
        updates: dict,
        quiet: bool = False,
        *,
        expected_access_token: str | None = None,
        expected_refresh_token: str | None = None,
        expected_remote_check_marker: _RemoteCheckMarker | None = None,
        preserve_disabled: bool = False,
    ) -> dict | None:
        if not access_token:
            return None
        return self._with_normalized_proxy_updates(
            updates,
            lambda normalized: self._update_account_impl(
                access_token,
                normalized,
                quiet,
                expected_access_token=expected_access_token,
                expected_refresh_token=expected_refresh_token,
                expected_remote_check_marker=expected_remote_check_marker,
                preserve_disabled=preserve_disabled,
            ),
        )

    def _update_account_impl(
        self,
        access_token: str,
        updates: dict,
        quiet: bool = False,
        *,
        expected_access_token: str | None = None,
        expected_refresh_token: str | None = None,
        expected_remote_check_marker: _RemoteCheckMarker | None = None,
        preserve_disabled: bool = False,
    ) -> dict | None:
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return None
            if expected_access_token is not None and (
                access_token != str(expected_access_token or "").strip()
            ):
                return None
            if expected_refresh_token is not None and (
                str(current.get("refresh_token") or "").strip()
                != str(expected_refresh_token or "").strip()
            ):
                return None
            if (
                expected_remote_check_marker is not None
                and self._remote_check_marker(current) != expected_remote_check_marker
            ):
                return None
            normalized_updates = dict(updates)
            replacement_access_token = str(
                normalized_updates.pop("access_token", "") or ""
            ).strip()
            next_access_token = replacement_access_token or access_token
            resolved_replacement = self._resolve_access_token_locked(next_access_token)
            if (
                next_access_token != access_token
                and resolved_replacement in self._accounts
                and resolved_replacement != access_token
            ):
                raise ValueError("access token already belongs to another account")

            refresh_token_changed = (
                "refresh_token" in normalized_updates
                and str(normalized_updates.get("refresh_token") or "").strip()
                != str(current.get("refresh_token") or "").strip()
            )
            credentials_changed = next_access_token != access_token or refresh_token_changed
            if credentials_changed:
                self._scrub_diagnostic_secrets(
                    normalized_updates,
                    [
                        current.get("access_token"),
                        current.get("refresh_token"),
                        current.get("id_token"),
                    ],
                )
                normalized_updates.update({
                    "last_token_refresh_error": None,
                    "last_token_refresh_error_at": None,
                    "last_refresh_error": None,
                    "last_refresh_error_at": None,
                    "last_invalid_at": None,
                    "invalid_count": 0,
                })
                if refresh_token_changed:
                    normalized_updates["refresh_token_invalid_at"] = None
                if current.get("last_remote_check_result") == "invalid":
                    normalized_updates.update({
                        "last_remote_check_result": None,
                        "last_remote_check_error": None,
                        "last_remote_check_error_at": None,
                        "last_remote_check_event": None,
                        "pending_auth_remove_invalid": None,
                        "pending_auth_scope": None,
                    })
                if current.get("status") == "异常":
                    normalized_updates["status"] = "正常"
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
            account, auto_remove_message = self._normalized_account_update_locked(
                next_access_token,
                current,
                normalized_updates,
                preserve_disabled=preserve_disabled,
            )
            if account is None:
                return None
            if auto_remove_message:
                self._accounts.pop(access_token, None)
                saved = self._save_accounts(
                    expected_credential_generation=expected_generation,
                )
                if not saved:
                    return None
                self._remove_account_runtime_state_locked({access_token})
                log_service.add(
                    LOG_TYPE_ACCOUNT,
                    auto_remove_message,
                    {"token": anonymize_token(access_token)},
                )
                return None
            rotated = next_access_token != access_token
            alias_sources = {
                source
                for source in {access_token, *self._token_aliases}
                if self._resolve_access_token_locked(source) == access_token
            }
            if rotated:
                self._accounts.pop(access_token, None)
            self._accounts[next_access_token] = account
            saved = self._save_accounts(
                expected_credential_generation=expected_generation,
            )
            if not saved:
                return None
            if rotated:
                self._token_aliases.pop(next_access_token, None)
                self._move_account_runtime_token_locked(next_access_token, alias_sources)
                self._image_slot_condition.notify_all()
            access_token = self._resolve_access_token_locked(next_access_token)
            persisted = self._accounts.get(access_token)
            if persisted is None:
                return None
            if not quiet:
                log_service.add(LOG_TYPE_ACCOUNT, "更新账号",
                                {"token": anonymize_token(access_token), "status": persisted.get("status")})
            return dict(persisted)
        return None

    def build_export_items(
            self,
            access_tokens: list[str] | None = None,
            *,
            full: bool = False,
    ) -> list[dict[str, Any]]:
        self._refresh_accounts_snapshot_if_stale()
        target_tokens = set(token for token in (access_tokens or []) if token)
        with self._lock:
            accounts = [
                dict(item)
                for item in self._accounts.values()
                if not target_tokens or str(item.get("access_token") or "") in target_tokens
            ]

        items: list[dict[str, Any]] = []
        for account in accounts:
            access_token = str(account.get("access_token") or "").strip()
            refresh_token = str(account.get("refresh_token") or "").strip()
            id_token = str(account.get("id_token") or "").strip()
            if not access_token:
                continue

            if full:
                item: dict[str, Any] = {"access_token": access_token}
                for key in (
                    "management_id",
                    "access_token_fingerprints",
                    "refresh_token",
                    "id_token",
                    "password",
                    "type",
                    "plan_type",
                    "export_type",
                    "source_type",
                    "status",
                    "quota",
                    "image_quota_unknown",
                    "limits_progress",
                    "default_model_slug",
                    "email",
                    "user_id",
                    "account_id",
                    "proxy",
                    "group_id",
                    "created_at",
                    "last_token_refresh_at",
                    "last_token_refresh_error",
                    "last_token_refresh_error_at",
                    "refresh_token_invalid_at",
                    "last_remote_checked_at",
                    "last_remote_check_attempt_at",
                    "last_remote_check_error",
                    "last_remote_check_error_at",
                    "last_remote_check_event",
                    "last_remote_check_result",
                ):
                    if key in account and account.get(key) not in (None, ""):
                        item[key] = account.get(key)
                items.append(item)
                continue

            access_payload = self._decode_jwt_payload(access_token)
            id_payload = self._decode_jwt_payload(id_token)
            auth_claim = access_payload.get("https://api.openai.com/auth")
            auth_claim = auth_claim if isinstance(auth_claim, dict) else {}
            profile_claim = access_payload.get("https://api.openai.com/profile")
            profile_claim = profile_claim if isinstance(profile_claim, dict) else {}

            email = (
                str(account.get("email") or "").strip()
                or str(profile_claim.get("email") or "").strip()
                or str(id_payload.get("email") or "").strip()
            )
            account_id = (
                str(account.get("account_id") or "").strip()
                or str(auth_claim.get("chatgpt_account_id") or "").strip()
                or str(account.get("user_id") or "").strip()
            )
            item: dict[str, Any] = {
                "type": str(account.get("export_type") or "codex"),
                "email": email,
                "account_id": account_id,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "id_token": id_token,
                "expired": self._timestamp_to_iso(access_payload.get("exp")),
                "last_refresh": self._timestamp_to_iso(access_payload.get("iat")),
            }
            password = str(account.get("password") or "").strip()
            if password:
                item["password"] = password
            items.append(item)
        return items
