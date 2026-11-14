from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.concurrency import run_in_threadpool

from services.account_operation_events import normalize_account_operation_events
from services.account_service import account_service

from api.accounts._shared import (
    AccountBatchUpdateRequest,
    AccountMemoryRequest,
    AccountOperationRequest,
    _account_mutation_events,
    _account_mutation_payload,
    _account_not_found_errors,
    _account_operation_labels,
    _account_operation_progress_for_api,
    _account_selection_targets,
    _account_status_operation,
    _init_account_mutation_progress,
    _partition_account_ids,
    _publish_account_mutation_progress,
    _publish_account_mutation_stage,
    _sanitize_refresh_errors,
    _schedule_account_operation,
    _selected_refresh_context,
)
from api.support import require_admin


def register_account_operation_routes(router: APIRouter) -> None:
    @router.post("/api/accounts/refresh-access-token")
    async def refresh_access_tokens(
        body: AccountOperationRequest,
        authorization: str | None = Header(default=None),
    ):
        """Force RT-to-AT exchange only; no account metadata or quota request."""
        require_admin(authorization)
        targets, missing_ids = _account_selection_targets(
            body.selection,
            body.account_ids,
            body.access_tokens,
            default_all=False,
        )
        if not targets:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "no valid accounts selected",
                    "errors": _account_not_found_errors(missing_ids),
                },
            )

        progress_id = str(uuid.uuid4())
        (
            access_tokens,
            target_ids,
            id_by_token_hint,
            sensitive_values,
            proxy_values,
        ) = _selected_refresh_context(targets)
        account_service.init_refresh_progress(
            progress_id,
            len(access_tokens) + len(missing_ids),
        )
        for missing_id in missing_ids:
            account_service.update_refresh_progress(
                progress_id,
                missing_id,
                account_id=missing_id,
                action="refresh_access_token",
                event_status="failed",
                event_message="account_not_found · \u8d26\u53f7\u4e0d\u5b58\u5728",
            )

        async def _do_refresh_access_tokens():
            try:
                raw_result = await run_in_threadpool(
                    account_service.refresh_access_tokens,
                    access_tokens,
                    progress_id,
                    finalize_progress=False,
                )
                result = _account_mutation_payload(
                    refreshed=max(0, int(raw_result.get("refreshed") or 0)),
                    skipped=max(0, int(raw_result.get("skipped") or 0)),
                    updated_ids=list(raw_result.get("updated_ids") or []),
                    removed_ids=list(raw_result.get("removed_ids") or []),
                    errors=[
                        *_account_not_found_errors(missing_ids),
                        *_sanitize_refresh_errors(
                            raw_result.get("errors"),
                            id_by_token_hint=id_by_token_hint,
                            sensitive_values=sensitive_values,
                            proxy_values=proxy_values,
                        ),
                    ],
                    include_items=False,
                )
                account_service.finish_refresh_progress(
                    progress_id,
                    result,
                )
            except Exception:
                account_service.finish_refresh_progress(
                    progress_id,
                    error="access token refresh failed",
                )

        _schedule_account_operation(_do_refresh_access_tokens())
        return {
            "progress_id": progress_id,
            "target_ids": target_ids,
            "errors": _account_not_found_errors(missing_ids),
        }

    @router.post("/api/accounts/memory")
    async def manage_account_memory(
        body: AccountMemoryRequest,
        authorization: str | None = Header(default=None),
    ):
        """Manage memory for a fixed, explicitly selected account scope."""
        require_admin(authorization)
        targets, missing_ids = _account_selection_targets(
            body.selection,
            body.account_ids,
            body.access_tokens,
            default_all=False,
        )
        if not targets:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "no valid accounts selected",
                    "errors": _account_not_found_errors(missing_ids),
                },
            )

        progress_id = str(uuid.uuid4())
        (
            _access_tokens,
            target_ids,
            id_by_token_hint,
            sensitive_values,
            proxy_values,
        ) = _selected_refresh_context(targets)
        action = f"{body.action}_memory"
        _init_account_mutation_progress(
            progress_id,
            total=len(target_ids) + len(missing_ids),
            missing_ids=missing_ids,
            action=action,
        )
        action_label = {"enable": "开启记忆", "disable": "关闭记忆", "refresh": "刷新记忆状态", "clear": "清空记忆"}[body.action]
        account_service.update_refresh_progress_stage(
            progress_id,
            "queued",
            f"正在准备{action_label}",
        )
        enabled = {"enable": True, "disable": False, "refresh": None, "clear": None}[body.action]

        async def _do_manage_account_memory() -> None:
            try:
                raw_result = await run_in_threadpool(
                    account_service.manage_account_memory,
                    target_ids,
                    enabled=enabled,
                    clear=body.action == "clear",
                    progress_id=progress_id,
                    finalize_progress=False,
                )
                result = _account_mutation_payload(
                    confirmed=max(0, int(raw_result.get("confirmed") or 0)),
                    pending=max(0, int(raw_result.get("pending") or 0)),
                    skipped=max(0, int(raw_result.get("skipped") or 0)),
                    updated_ids=list(raw_result.get("updated_ids") or []),
                    removed_ids=[],
                    errors=[
                        *_account_not_found_errors(missing_ids),
                        *_sanitize_refresh_errors(
                            raw_result.get("errors"),
                            id_by_token_hint=id_by_token_hint,
                            sensitive_values=sensitive_values,
                            proxy_values=proxy_values,
                        ),
                    ],
                    events=normalize_account_operation_events(
                        raw_result.get("events"),
                        sensitive_values=sensitive_values,
                        proxy_values=proxy_values,
                    ),
                    include_items=False,
                )
                account_service.finish_refresh_progress(progress_id, result)
            except asyncio.CancelledError:
                account_service.finish_refresh_progress(progress_id, error="memory operation cancelled")
                raise
            except Exception:
                account_service.finish_refresh_progress(progress_id, error="memory operation failed")

        _schedule_account_operation(_do_manage_account_memory())
        return {
            "progress_id": progress_id,
            "target_ids": target_ids,
            "errors": _account_not_found_errors(missing_ids),
        }

    @router.post("/api/accounts/sync")
    @router.post("/api/accounts/refresh")
    async def sync_accounts_and_quota(
        body: AccountOperationRequest,
        authorization: str | None = Header(default=None),
    ):
        """Synchronize remote account metadata and image quota.

        The /refresh path remains as a compatibility alias for existing clients.
        """
        require_admin(authorization)
        targets, missing_ids = _account_selection_targets(
            body.selection,
            body.account_ids,
            body.access_tokens,
            default_all=True,
        )
        if not targets:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "no valid accounts selected",
                    "errors": _account_not_found_errors(missing_ids),
                },
            )

        progress_id = str(uuid.uuid4())
        (
            access_tokens,
            target_ids,
            id_by_token_hint,
            sensitive_values,
            proxy_values,
        ) = _selected_refresh_context(targets)
        account_service.init_refresh_progress(
            progress_id,
            len(access_tokens) + len(missing_ids),
        )
        for missing_id in missing_ids:
            account_service.update_refresh_progress(
                progress_id,
                missing_id,
                account_id=missing_id,
                action="sync_account",
                event_status="failed",
                event_message="account_not_found · \u8d26\u53f7\u4e0d\u5b58\u5728",
            )

        async def _do_sync_accounts_and_quota():
            try:
                raw_result = await run_in_threadpool(
                    account_service.sync_accounts_and_quota,
                    access_tokens,
                    progress_id,
                    finalize_progress=False,
                )
                updated_ids, removed_ids = _partition_account_ids(target_ids)
                result = _account_mutation_payload(
                    synced=max(0, int(raw_result.get("synced") or 0)),
                    updated_ids=updated_ids,
                    removed_ids=removed_ids,
                    errors=[
                        *_account_not_found_errors(missing_ids),
                        *_sanitize_refresh_errors(
                            raw_result.get("errors"),
                            id_by_token_hint=id_by_token_hint,
                            sensitive_values=sensitive_values,
                            proxy_values=proxy_values,
                        ),
                    ],
                    include_items=False,
                )
                account_service.finish_refresh_progress(
                    progress_id,
                    result,
                )
            except Exception:
                account_service.finish_refresh_progress(
                    progress_id,
                    error="account and quota synchronization failed",
                )

        _schedule_account_operation(_do_sync_accounts_and_quota())

        return {
            "progress_id": progress_id,
            "target_ids": target_ids,
            "errors": _account_not_found_errors(missing_ids),
        }

    async def get_account_operation_progress(
        progress_id: str,
        *,
        legacy_sync_alias: bool = False,
    ) -> dict[str, Any]:
        progress = account_service.get_refresh_progress(progress_id)
        if progress is None:
            raise HTTPException(status_code=404, detail={"error": "progress not found"})
        return _account_operation_progress_for_api(
            progress,
            legacy_sync_alias=legacy_sync_alias,
        )

    @router.get("/api/accounts/operations/{progress_id}")
    async def get_canonical_account_operation_progress(
        progress_id: str,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return await get_account_operation_progress(progress_id)

    @router.get("/api/accounts/refresh/progress/{progress_id}")
    async def get_legacy_account_refresh_progress(
        progress_id: str,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return await get_account_operation_progress(
            progress_id,
            legacy_sync_alias=True,
        )


def register_account_batch_update(router: APIRouter) -> None:
    @router.post("/api/accounts/batch-update")
    async def batch_update_accounts(body: AccountBatchUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not body.selection and not body.account_ids and not body.access_tokens:
            raise HTTPException(status_code=400, detail={"error": "account_ids is required"})
        updates = {key: value for key, value in {"status": body.status}.items() if value is not None}
        if not updates:
            raise HTTPException(status_code=400, detail={"error": "no updates provided"})
        action, success_message, failure_message = _account_status_operation(
            body.status,
            body.operation,
        )
        targets, missing_ids = _account_selection_targets(
            body.selection,
            body.account_ids,
            body.access_tokens,
        )
        if not targets:
            raise HTTPException(status_code=400, detail={"error": "no valid accounts selected"})
        labels = _account_operation_labels(targets)
        tokens = [token for token, _account_id in targets]
        target_ids = [account_id for _token, account_id in targets]
        id_by_token = {token: account_id for token, account_id in targets}
        progress_id = str(uuid.uuid4())
        initial_errors = _account_not_found_errors(missing_ids)
        _init_account_mutation_progress(
            progress_id,
            total=len(targets) + len(missing_ids),
            missing_ids=missing_ids,
            action=action,
        )
        _publish_account_mutation_stage(progress_id, "prepare_accounts", len(targets))

        async def _do_batch_update_accounts() -> None:
            try:
                raw_result = await run_in_threadpool(
                    account_service.update_accounts,
                    tokens,
                    updates,
                    quiet=True,
                    progress_callback=lambda stage, total: _publish_account_mutation_stage(
                        progress_id,
                        stage,
                        total,
                    ),
                )
                updated_ids = list(raw_result.get("updated_ids") or [])
                removed_ids = list(raw_result.get("removed_ids") or [])
                removed_ids.extend(
                    id_by_token[token]
                    for token in raw_result.get("missing_tokens") or []
                    if token in id_by_token
                )
                errors = _account_not_found_errors([
                    *missing_ids,
                    *removed_ids,
                ])
                events = _account_mutation_events(
                    action=action,
                    success_ids=updated_ids,
                    removed_ids=removed_ids,
                    errors=errors,
                    labels=labels,
                    success_message=success_message,
                    removed_message=failure_message,
                )
                result = _account_mutation_payload(
                    updated=len(updated_ids),
                    removed=len(removed_ids),
                    updated_ids=updated_ids,
                    removed_ids=removed_ids,
                    errors=errors,
                    events=events,
                    include_items=False,
                )
                _publish_account_mutation_progress(
                    progress_id,
                    targets=targets,
                    labels=labels,
                    events=events,
                )
                account_service.finish_refresh_progress(progress_id, result)
            except Exception:
                account_service.finish_refresh_progress(
                    progress_id,
                    error="account batch update failed",
                )

        _schedule_account_operation(_do_batch_update_accounts())
        return {
            "progress_id": progress_id,
            "target_ids": target_ids,
            "errors": initial_errors,
        }
