from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from fastapi.concurrency import run_in_threadpool

from services.account_service import account_service
from services.config import config

from api.accounts._shared import (
    AccountGroupBindRequest,
    AccountGroupRequest,
    _account_group_id,
    _account_group_payload,
    _account_mutation_events,
    _account_mutation_payload,
    _account_mutation_response,
    _account_not_found_errors,
    _account_operation_labels,
    _account_selection_targets,
    _clean_text,
    _config_dict_list,
    _upsert_account_group,
)
from api.support import require_admin


def register_account_group_routes(router: APIRouter) -> None:
    @router.get("/api/account-groups")
    async def list_account_groups(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return _account_group_payload()

    @router.post("/api/account-groups")
    async def save_account_group(body: AccountGroupRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return _upsert_account_group(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.delete("/api/account-groups/{group_id}")
    async def delete_account_group(group_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        normalized = _account_group_id(group_id)
        groups = _config_dict_list("account_groups")
        next_groups = [group for group in groups if _account_group_id(group.get("id")) != normalized]
        if len(next_groups) == len(groups):
            raise HTTPException(status_code=404, detail={"error": "account group not found"})
        updated = config.update({"account_groups": next_groups})
        targets = [
            (
                _clean_text(account.get("access_token")),
                _clean_text(account.get("management_id")),
            )
            for account in account_service.list_accounts()
            if _clean_text(account.get("group_id")) == normalized
            and _clean_text(account.get("access_token"))
            and _clean_text(account.get("management_id"))
        ]
        if targets:
            result = await run_in_threadpool(
                account_service.update_accounts,
                [token for token, _account_id in targets],
                {"group_id": ""},
                quiet=True,
            )
        else:
            result = {
                "updated_ids": [],
                "removed_ids": [],
                "missing_tokens": [],
            }
        updated_ids = list(result.get("updated_ids") or [])
        removed_ids = list(result.get("removed_ids") or [])
        id_by_token = {token: account_id for token, account_id in targets}
        removed_ids.extend(
            id_by_token[token]
            for token in result.get("missing_tokens") or []
            if token in id_by_token
        )
        return {
            "deleted": normalized,
            **_account_group_payload(updated.get("account_groups", [])),
            **_account_mutation_payload(
                updated_ids=updated_ids,
                removed_ids=removed_ids,
            ),
        }


def register_account_group_binding(router: APIRouter) -> None:
    @router.post("/api/accounts/group")
    async def bind_accounts_group(body: AccountGroupBindRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not body.selection and not body.account_ids and not body.access_tokens:
            raise HTTPException(status_code=400, detail={"error": "account_ids is required"})
        group_id = "" if body.group_id.strip() == "__ungrouped__" else _account_group_id(body.group_id)
        if group_id and not any(group.get("id") == group_id for group in _account_group_payload()["groups"]):
            raise HTTPException(status_code=404, detail={"error": "account group not found"})
        targets, missing_ids = _account_selection_targets(
            body.selection,
            body.account_ids,
            body.access_tokens,
        )
        if not targets:
            raise HTTPException(status_code=400, detail={"error": "no valid accounts selected"})
        labels = _account_operation_labels(targets)
        result = await run_in_threadpool(
            account_service.update_accounts,
            [token for token, _account_id in targets],
            {"group_id": group_id},
            quiet=True,
        )
        updated_ids = list(result.get("updated_ids") or [])
        removed_ids = list(result.get("removed_ids") or [])
        id_by_token = {token: account_id for token, account_id in targets}
        removed_ids.extend(
            id_by_token[token]
            for token in result.get("missing_tokens") or []
            if token in id_by_token
        )
        errors = _account_not_found_errors([
            *missing_ids,
            *removed_ids,
        ])
        return {
            "group_id": group_id,
            **_account_group_payload(),
            **_account_mutation_response(
                updated=len(updated_ids),
                removed=len(removed_ids),
                updated_ids=updated_ids,
                removed_ids=removed_ids,
                errors=errors,
                events=_account_mutation_events(
                    action="bind_account_group",
                    success_ids=updated_ids,
                    removed_ids=removed_ids,
                    errors=errors,
                    labels=labels,
                    success_message=(
                        "账号组已更新" if group_id else "账号已移出账号组"
                    ),
                    removed_message="账号组更新失败",
                ),
                include_items=False,
            ),
        }
