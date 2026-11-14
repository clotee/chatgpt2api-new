from __future__ import annotations

import json
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from contracts.account_test import AccountTestRequest, AccountTestResult
from services.account_service import account_service
from services.account_test_service import account_test_service
from services.log_service import LoggedCall

from api.accounts._shared import (
    AccountCreateRequest,
    AccountDeleteRequest,
    AccountExportRequest,
    AccountImportCleanupRequest,
    AccountSelectionPreviewRequest,
    AccountUpdateRequest,
    _account_detail_for_api,
    _account_export_headers,
    _account_export_summary,
    _account_for_api,
    _account_group_names,
    _account_mutation_events,
    _account_mutation_payload,
    _account_mutation_response,
    _account_not_found_errors,
    _account_operation_labels,
    _account_payload_token,
    _account_selection_preview,
    _account_selection_targets,
    _account_targets,
    _account_zip_bytes,
    _accounts_page,
    _clean_text,
    _download_timestamp,
    _get_account_by_id,
    _get_account_by_token_identity,
    _init_account_mutation_progress,
    _partition_account_ids,
    _publish_account_mutation_progress,
    _publish_account_mutation_stage,
    _refresh_error_id_map,
    _sanitize_refresh_errors,
    _schedule_account_operation,
    _status_matches_filter,
    _target_account_group_id,
    _unique_tokens,
)
from api.support import require_admin
from utils.helper import anonymize_token


def register_account_query_routes(router: APIRouter) -> None:
    @router.get("/api/accounts")
    async def get_accounts(
            page: int = Query(default=1, ge=1),
            page_size: int = Query(default=500, ge=1, le=500),
            keyword: str = "",
            status: str = "all",
            group_id: str = "all",
            memory_state: Literal["all", "enabled", "disabled", "unknown"] = "all",
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return _accounts_page(
            page=page,
            page_size=page_size,
            keyword=keyword,
            status=status,
            group_id=group_id,
            memory_state=memory_state,
        )

    @router.post("/api/accounts/selection-preview")
    async def preview_account_selection(
        body: AccountSelectionPreviewRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return await run_in_threadpool(_account_selection_preview, body.selection)

    @router.get("/api/accounts/{account_id}")
    async def get_account_detail(account_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        account = _get_account_by_id(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail={"error": "account not found"})
        return {"item": _account_detail_for_api(account)}

    @router.post(
        "/api/accounts/{account_id}/test",
        response_model=AccountTestResult,
    )
    async def test_account(
        account_id: str,
        body: AccountTestRequest,
        authorization: str | None = Header(default=None),
    ):
        identity = require_admin(authorization)
        if _get_account_by_id(account_id) is None:
            raise HTTPException(status_code=404, detail={"error": "account not found"})
        payload = {**body.model_dump(), "account_id": account_id}
        call = LoggedCall(
            identity,
            "/api/accounts/{account_id}/test",
            body.model,
            "账号画图测试" if body.mode == "image" else "账号对话测试",
            request_text=body.prompt,
            image_request=body.mode == "image",
        )
        return await call.run(account_test_service.execute, payload)

    @router.get("/api/accounts/{account_id}/access-token")
    async def get_account_access_token(
        account_id: str,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        account = _get_account_by_id(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail={"error": "account not found"})
        access_token = _clean_text(account.get("access_token"))
        if not access_token:
            raise HTTPException(status_code=404, detail={"error": "access token not found"})
        return JSONResponse(
            {"access_token": access_token},
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/api/accounts/{account_id}/refresh-token")
    async def get_account_refresh_token(
        account_id: str,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        account = _get_account_by_id(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail={"error": "account not found"})
        refresh_token = _clean_text(account.get("refresh_token"))
        if not refresh_token:
            raise HTTPException(status_code=404, detail={"error": "refresh token not found"})
        return JSONResponse(
            {"refresh_token": refresh_token},
            headers={"Cache-Control": "no-store"},
        )


def register_account_write_routes(router: APIRouter) -> None:
    @router.post("/api/accounts")
    async def create_accounts(body: AccountCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        target_group_id = _target_account_group_id(body.target_group_id)
        account_payloads = [
            {**item, **({"group_id": target_group_id} if target_group_id is not None else {})}
            for item in body.accounts
            if isinstance(item, dict)
        ]
        payload_tokens = [_account_payload_token(item) for item in account_payloads]
        tokens = _unique_tokens([*body.tokens, *payload_tokens])
        if not tokens:
            raise HTTPException(status_code=400, detail={"error": "tokens is required"})
        try:
            if account_payloads:
                result = await run_in_threadpool(
                    account_service.add_account_items,
                    account_payloads,
                    return_items=False,
                    restore=body.restore,
                )
                payload_token_set = set(_unique_tokens(payload_tokens))
                extra_tokens = [token for token in tokens if token not in payload_token_set]
                if extra_tokens:
                    if target_group_id is None:
                        extra_result = await run_in_threadpool(
                            account_service.add_accounts,
                            extra_tokens,
                            return_items=False,
                        )
                    else:
                        extra_result = await run_in_threadpool(
                            account_service.add_account_items,
                            [{"access_token": token, "group_id": target_group_id} for token in extra_tokens],
                            return_items=False,
                        )
                    result["added"] = int(result.get("added") or 0) + int(extra_result.get("added") or 0)
                    result["skipped"] = int(result.get("skipped") or 0) + int(extra_result.get("skipped") or 0)
            else:
                if target_group_id is None:
                    result = await run_in_threadpool(
                        account_service.add_accounts,
                        tokens,
                        return_items=False,
                    )
                else:
                    result = await run_in_threadpool(
                        account_service.add_account_items,
                        [{"access_token": token, "group_id": target_group_id} for token in tokens],
                        return_items=False,
                    )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"error": str(exc)},
            ) from exc

        targets: list[tuple[str, str]] = []
        sensitive_values = list(tokens)
        proxy_values: list[str] = []
        for token in tokens:
            account = _get_account_by_token_identity(token)
            account_id = _clean_text((account or {}).get("management_id"))
            active_token = _clean_text((account or {}).get("access_token")) or token
            if account_id:
                targets.append((active_token, account_id))
            for key in ("refresh_token", "id_token"):
                value = _clean_text((account or {}).get(key))
                if value:
                    sensitive_values.append(value)
            proxy = _clean_text((account or {}).get("proxy"))
            if proxy:
                proxy_values.append(proxy)

        sync_after_import = (
            bool(body.sync_after_import)
            if body.sync_after_import is not None
            else bool(body.refresh)
            if body.refresh is not None
            else True
        )
        if not sync_after_import:
            updated_ids = [account_id for _token, account_id in targets]
            labels = _account_operation_labels(targets)
            payload = _account_mutation_response(
                added=max(0, int(result.get("added") or 0)),
                skipped=max(0, int(result.get("skipped") or 0)),
                synced=0,
                updated_ids=updated_ids,
                events=_account_mutation_events(
                    action="import_account",
                    success_ids=updated_ids,
                    labels=labels,
                    success_message="\u8d26\u53f7\u5df2\u4fdd\u5b58",
                ),
                include_items=body.return_items,
            )
            if body.sync_after_import is None and body.refresh is not None:
                payload["refreshed"] = payload["synced"]
            return payload

        refresh_result = await run_in_threadpool(
            account_service.sync_accounts_and_quota,
            _unique_tokens([active_token for active_token, _account_id in targets]),
        )
        updated_ids, removed_ids = _partition_account_ids([
            account_id for _token, account_id in targets
        ])
        sanitized_errors = _sanitize_refresh_errors(
            refresh_result.get("errors"),
            id_by_token_hint=_refresh_error_id_map(targets),
            sensitive_values=sensitive_values,
            proxy_values=proxy_values,
        )
        labels = _account_operation_labels(targets)
        payload = _account_mutation_response(
            added=max(0, int(result.get("added") or 0)),
            skipped=max(0, int(result.get("skipped") or 0)),
            synced=max(0, int(refresh_result.get("synced") or 0)),
            updated_ids=updated_ids,
            removed_ids=removed_ids,
            errors=sanitized_errors,
            events=_account_mutation_events(
                action="import_account",
                success_ids=updated_ids,
                removed_ids=removed_ids,
                errors=sanitized_errors,
                labels=labels,
                success_message="\u8d26\u53f7\u5df2\u4fdd\u5b58",
                removed_message="\u8d26\u53f7\u4fdd\u5b58\u540e\u5df2\u88ab\u79fb\u9664",
            ),
            include_items=body.return_items,
        )
        if body.sync_after_import is None and body.refresh is not None:
            payload["refreshed"] = payload["synced"]
        return payload

    @router.delete("/api/accounts")
    async def delete_accounts(body: AccountDeleteRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not body.selection and not body.account_ids and not body.tokens:
            raise HTTPException(status_code=400, detail={"error": "account_ids is required"})
        targets, missing_ids = _account_selection_targets(
            body.selection,
            body.account_ids,
            body.tokens,
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
            action="delete_account",
        )
        _publish_account_mutation_stage(progress_id, "prepare_accounts", len(targets))

        async def _do_delete_accounts() -> None:
            try:
                raw_result = await run_in_threadpool(
                    account_service.delete_accounts,
                    tokens,
                    return_items=False,
                    progress_callback=lambda stage, total: _publish_account_mutation_stage(
                        progress_id,
                        stage,
                        total,
                    ),
                )
                removed_ids = list(raw_result.get("removed_ids") or [])
                missing_target_ids = [
                    id_by_token[token]
                    for token in raw_result.get("missing_tokens") or []
                    if token in id_by_token
                ]
                errors = _account_not_found_errors([
                    *missing_ids,
                    *missing_target_ids,
                ])
                events = _account_mutation_events(
                    action="delete_account",
                    removed_ids=removed_ids,
                    errors=errors,
                    labels=labels,
                    success_message="\u8d26\u53f7\u5df2\u5220\u9664",
                )
                result = _account_mutation_payload(
                    removed=max(0, int(raw_result.get("removed") or 0)),
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
                    error="account deletion failed",
                )

        _schedule_account_operation(_do_delete_accounts())
        return {
            "progress_id": progress_id,
            "target_ids": target_ids,
            "errors": initial_errors,
        }

    @router.post("/api/accounts/import-cleanup")
    async def cleanup_imported_abnormal_accounts(
            body: AccountImportCleanupRequest,
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        requested_ids = list(dict.fromkeys(
            _clean_text(item).lower() for item in body.account_ids if _clean_text(item)
        ))
        legacy_tokens = _unique_tokens(body.access_tokens)
        if not requested_ids and not legacy_tokens:
            raise HTTPException(status_code=400, detail={"error": "account_ids is required"})
        targets, missing_ids = _account_targets(requested_ids, legacy_tokens)
        abnormal_targets = [
            (token, account_id)
            for token, account_id in targets
            if _status_matches_filter(account_service.get_account(token) or {}, "abnormal")
        ]
        labels = _account_operation_labels(abnormal_targets)
        removed_ids: list[str] = []
        deletion_missing_ids: list[str] = []
        if body.remove and abnormal_targets:
            delete_result = account_service.delete_accounts(
                [token for token, _account_id in abnormal_targets],
                return_items=False,
            )
            removed_ids = list(delete_result.get("removed_ids") or [])
            id_by_token = {token: account_id for token, account_id in abnormal_targets}
            deletion_missing_ids = [
                id_by_token[token]
                for token in delete_result.get("missing_tokens") or []
                if token in id_by_token
            ]
        return _account_mutation_payload(
            checked=len(targets),
            abnormal=len(abnormal_targets),
            removed=len(removed_ids),
            removed_ids=removed_ids,
            errors=_account_not_found_errors([*missing_ids, *deletion_missing_ids]),
            events=(
                _account_mutation_events(
                    action="delete_account",
                    removed_ids=removed_ids,
                    errors=_account_not_found_errors([*missing_ids, *deletion_missing_ids]),
                    labels=labels,
                    success_message="账号已删除",
                    removed_message="导入后异常账号已删除",
                )
                if body.remove
                else []
            ),
            include_items=False,
        )


def register_account_export_update_routes(router: APIRouter) -> None:
    @router.post("/api/accounts/export")
    async def export_accounts(body: AccountExportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        targets, missing_ids = _account_selection_targets(
            body.selection,
            body.account_ids,
            body.access_tokens,
            default_all=True,
        )
        missing_legacy_tokens = [
            token
            for token in _unique_tokens(body.access_tokens)
            if _get_account_by_token_identity(token) is None
        ]
        if missing_ids or missing_legacy_tokens:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "one or more accounts were not found",
                    "errors": [
                        *_account_not_found_errors(missing_ids),
                        *[
                            {
                                "id": anonymize_token(token),
                                "code": "account_not_found",
                                "message": "legacy access token was not found",
                            }
                            for token in missing_legacy_tokens
                        ],
                    ],
                },
            )
        if not targets:
            raise HTTPException(status_code=400, detail={"error": "no valid accounts selected"})
        access_tokens = [token for token, _account_id in targets]
        items = account_service.build_export_items(
            access_tokens,
            full=body.format != "zip",
        )
        if not items:
            raise HTTPException(status_code=400, detail={"error": "没有可导出的账号"})
        summary = _account_export_summary(len(targets), len(items))
        export_headers = _account_export_headers(summary)

        timestamp = _download_timestamp()
        if body.format == "zip":
            content = _account_zip_bytes(items)
            return Response(
                content,
                media_type="application/zip",
                headers={
                    **export_headers,
                    "Content-Disposition": f'attachment; filename="codex-accounts-{timestamp}.zip"',
                },
            )

        if body.format == "txt":
            content = "\n".join(str(item.get("access_token") or "") for item in items) + "\n"
            return Response(
                content,
                media_type="text/plain; charset=utf-8",
                headers={
                    **export_headers,
                    "Content-Disposition": f'attachment; filename="codex-access-tokens-{timestamp}.txt"',
                },
            )

        payload: dict[str, Any] | list[dict[str, Any]] = items[0] if len(items) == 1 else items
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            media_type="application/json",
            headers={
                **export_headers,
                "Content-Disposition": f'attachment; filename="codex-accounts-{timestamp}.json"',
            },
        )

    @router.post("/api/accounts/update")
    async def update_account(body: AccountUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        account_id = _clean_text(body.id).lower()
        uses_management_id = bool(account_id)
        legacy_token = _clean_text(body.access_token)
        if account_id:
            current = _get_account_by_id(account_id)
        elif legacy_token:
            current = account_service.get_account(legacy_token)
            account_id = _clean_text((current or {}).get("management_id"))
        else:
            raise HTTPException(status_code=400, detail={"error": "account id is required"})
        if current is None or not account_id:
            raise HTTPException(status_code=404, detail={"error": "account not found"})
        updates = {
            key: value
            for key, value in {
                "type": body.type,
                "source_type": body.source_type,
                "quota": body.quota,
                "proxy": body.proxy,
                "group_id": body.group_id,
            }.items()
            if value is not None
        }
        if uses_management_id:
            replacement_access_token = _clean_text(body.access_token)
            replacement_refresh_token = _clean_text(body.refresh_token)
            if replacement_access_token:
                updates["access_token"] = replacement_access_token
            if replacement_refresh_token:
                updates["refresh_token"] = replacement_refresh_token
        if not updates:
            raise HTTPException(status_code=400, detail={"error": "no updates provided"})
        access_token = _clean_text(current.get("access_token"))
        labels = _account_operation_labels([(access_token, account_id)])
        try:
            account = account_service.update_account(access_token, updates)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"error": str(exc)},
            ) from exc
        if account is None:
            if _get_account_by_id(account_id) is None:
                payload = _account_mutation_response(
                    updated=0,
                    removed=1,
                    removed_ids=[account_id],
                    events=_account_mutation_events(
                        action="update_account",
                        removed_ids=[account_id],
                        labels=labels,
                        success_message="账号已更新",
                        removed_message="账号更新失败",
                    ),
                    include_items=False,
                )
                payload["item"] = None
                return payload
            raise HTTPException(status_code=404, detail={"error": "account not found"})
        payload = _account_mutation_response(
            updated=1,
            removed=0,
            updated_ids=[account_id],
            events=_account_mutation_events(
                action="update_account",
                success_ids=[account_id],
                labels=labels,
                success_message="账号已更新",
            ),
        )
        payload["item"] = _account_for_api(account, _account_group_names())
        return payload
