from __future__ import annotations

import asyncio
import io
import json
import re
import zipfile
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import HTTPException
from pydantic import BaseModel, Field

from services.account_operation_events import (
    normalize_account_operation_events,
    project_account_operation_presentation,
)
from services.account_service import account_service
from services.account_view import account_detail, account_memory_state, account_row, account_status_category
from services.config import config
from services.proxy_management_service import project_proxy_assignment, proxy_management_service
from utils.diagnostics import sanitize_diagnostic_text
from utils.helper import anonymize_token


_ACCOUNT_OPERATION_TASKS: set[asyncio.Task[Any]] = set()


def _validate_remote_base_url(base_url: str) -> None:
    parsed = urlparse(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail={"error": "base_url must be a valid http(s) URL"})


def _schedule_account_operation(coroutine: Any) -> Any:
    task = asyncio.create_task(coroutine)
    if isinstance(task, asyncio.Task):
        _ACCOUNT_OPERATION_TASKS.add(task)
        task.add_done_callback(_ACCOUNT_OPERATION_TASKS.discard)
    return task


class AccountSelectionScope(BaseModel):
    mode: Literal["explicit", "filter", "all"] = "explicit"
    account_ids: list[str] = Field(default_factory=list, max_length=5000)
    excluded_account_ids: list[str] = Field(default_factory=list, max_length=5000)
    keyword: str = ""
    status: str = "all"
    group_id: str = "all"
    memory_state: Literal["all", "enabled", "disabled", "unknown"] = "all"


class AccountSelectionPreviewRequest(BaseModel):
    selection: AccountSelectionScope


class AccountCreateRequest(BaseModel):
    tokens: list[str] = Field(default_factory=list, max_length=5000)
    accounts: list[dict[str, Any]] = Field(default_factory=list, max_length=5000)
    sync_after_import: bool | None = None
    refresh: bool | None = None
    restore: bool = False
    return_items: bool = True
    target_group_id: str | None = None


class AccountDeleteRequest(BaseModel):
    account_ids: list[str] = Field(default_factory=list, max_length=5000)
    tokens: list[str] = Field(default_factory=list, max_length=5000)
    selection: AccountSelectionScope | None = None


class AccountImportCleanupRequest(BaseModel):
    access_tokens: list[str] = Field(default_factory=list, max_length=5000)
    account_ids: list[str] = Field(default_factory=list, max_length=5000)
    remove: bool = False


class AccountOperationRequest(BaseModel):
    account_ids: list[str] = Field(default_factory=list, max_length=5000)
    access_tokens: list[str] = Field(default_factory=list, max_length=5000)
    selection: AccountSelectionScope | None = None


class AccountMemoryRequest(AccountOperationRequest):
    action: Literal["enable", "disable", "refresh", "clear"]


class AccountExportRequest(BaseModel):
    account_ids: list[str] = Field(default_factory=list, max_length=5000)
    access_tokens: list[str] = Field(default_factory=list, max_length=5000)
    format: Literal["json", "zip", "txt"] = "json"
    selection: AccountSelectionScope | None = None


class AccountUpdateRequest(BaseModel):
    id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    type: str | None = None
    source_type: str | None = None
    quota: int | None = None
    proxy: str | None = None
    group_id: str | None = None


class AccountBatchUpdateRequest(BaseModel):
    account_ids: list[str] = Field(default_factory=list, max_length=5000)
    access_tokens: list[str] = Field(default_factory=list, max_length=5000)
    selection: AccountSelectionScope | None = None
    status: Literal["正常", "限流", "异常", "禁用"] | None = None
    operation: Literal["update", "enable", "disable", "reset"] | None = None


class AccountGroupBindRequest(BaseModel):
    account_ids: list[str] = Field(default_factory=list, max_length=5000)
    access_tokens: list[str] = Field(default_factory=list, max_length=5000)
    selection: AccountSelectionScope | None = None
    group_id: str = ""


class AccountGroupRequest(BaseModel):
    id: str = ""
    name: str = ""
    proxy: str = ""
    proxy_group_id: str = ""
    enabled: bool = True
    notes: str = ""
    create_only: bool = False


class CPAPoolCreateRequest(BaseModel):
    name: str = ""
    base_url: str = ""
    secret_key: str = ""


class CPAPoolUpdateRequest(BaseModel):
    name: str | None = None
    base_url: str | None = None
    secret_key: str | None = None


class CPAImportRequest(BaseModel):
    names: list[str] = Field(default_factory=list, max_length=5000)
    target_group_id: str | None = None


class Sub2APIServerCreateRequest(BaseModel):
    name: str = ""
    base_url: str = ""
    email: str = ""
    password: str = ""
    api_key: str = ""
    group_id: str = ""


class Sub2APIServerUpdateRequest(BaseModel):
    name: str | None = None
    base_url: str | None = None
    email: str | None = None
    password: str | None = None
    api_key: str | None = None
    group_id: str | None = None


class Sub2APIImportGroupBinding(BaseModel):
    remote_group_id: str = ""
    name: str = ""
    account_ids: list[str] = Field(default_factory=list, max_length=5000)


class Sub2APIImportRequest(BaseModel):
    account_ids: list[str] = Field(default_factory=list, max_length=5000)
    group_bindings: list[Sub2APIImportGroupBinding] = Field(default_factory=list, max_length=5000)
    create_account_groups: bool = True
    target_group_id: str | None = None


class OAuthLoginStartRequest(BaseModel):
    """起始 OAuth 桥。email_hint 可选，仅用于让 OpenAI 登录页预填邮箱。"""
    email_hint: str = ""


class OAuthLoginFinishRequest(BaseModel):
    """提交 callback。callback 既可以是完整 URL 也可以只填 code。"""
    session_id: str = ""
    callback: str = ""
    target_group_id: str | None = None


def _account_payload_token(item: dict[str, Any]) -> str:
    return str(item.get("access_token") or item.get("accessToken") or "").strip()


def _unique_tokens(tokens: list[str]) -> list[str]:
    return list(dict.fromkeys(str(token or "").strip() for token in tokens if str(token or "").strip()))


def _download_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _safe_export_name(value: str, fallback: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return (clean or fallback)[:80]


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _slug_id(value: object) -> str:
    raw = _clean_text(value).lower()
    chars: list[str] = []
    for char in raw:
        if char.isalnum() or char in {"-", "_"}:
            chars.append(char)
        elif char.isspace():
            chars.append("-")
    return "".join(chars).strip("-_")


def _config_dict_list(key: str) -> list[dict[str, Any]]:
    raw = config.get().get(key)
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _account_group_id(value: object) -> str:
    return _slug_id(value)


def _target_account_group_id(value: str | None) -> str | None:
    if value is None:
        return None
    group_id = _account_group_id(value)
    if not group_id:
        return ""
    if not any(group.get("id") == group_id for group in _account_group_payload()["groups"]):
        raise HTTPException(status_code=400, detail={"error": "account group not found"})
    return group_id


def _account_group_payload(groups: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    accounts = account_service.list_accounts()
    counts: dict[str, int] = {}
    for account in accounts:
        group_id = _clean_text(account.get("group_id"))
        if group_id:
            counts[group_id] = counts.get(group_id, 0) + 1
    proxy_groups = [
        group.model_dump(mode="json")
        for group in proxy_management_service.list_groups().groups
    ]
    proxy_group_names = {
        _clean_text(group.get("id")): _clean_text(group.get("name"))
        for group in proxy_groups
        if _clean_text(group.get("id"))
    }
    normalized_groups = []
    for group in groups if groups is not None else _config_dict_list("account_groups"):
        group_id = _account_group_id(group.get("id"))
        if not group_id:
            continue
        projection = project_proxy_assignment(
            group.get("proxy"),
            legacy_group_id=group.get("proxy_group_id"),
            group_names=proxy_group_names,
        )
        normalized_groups.append(
            {
                "id": group_id,
                "name": _clean_text(group.get("name")) or group_id,
                "proxy": projection.reference,
                "proxy_group_id": projection.group_id,
                "proxy_mode": projection.mode,
                "proxy_label": projection.label,
                "enabled": bool(group.get("enabled", True)),
                "notes": _clean_text(group.get("notes")),
                "account_count": counts.get(group_id, 0),
            }
        )
    return {
        "groups": normalized_groups,
        "proxy_groups": proxy_groups,
    }


def _upsert_account_group(body: AccountGroupRequest) -> dict[str, Any]:
    group_id = _account_group_id(body.id or body.name)
    if not group_id:
        raise ValueError("account group id is required")

    def persist(normalized_references: list[str]) -> dict[str, Any]:
        groups = _config_dict_list("account_groups")
        exists = any(_account_group_id(group.get("id")) == group_id for group in groups)
        if body.create_only and exists:
            raise ValueError("account group already exists")
        projection = project_proxy_assignment(normalized_references[0])
        item = {
            "id": group_id,
            "name": body.name.strip() or group_id,
            "proxy": projection.reference,
            "proxy_group_id": projection.group_id,
            "enabled": body.enabled,
            "notes": body.notes.strip(),
        }
        next_groups = [
            group
            for group in groups
            if _account_group_id(group.get("id")) != group_id
        ]
        next_groups.append(item)
        updated = config.update({"account_groups": next_groups})
        payload = _account_group_payload(updated.get("account_groups", []))
        projected_group = next(
            group for group in payload["groups"]
            if group["id"] == group_id
        )
        return {
            "group": projected_group,
            **payload,
        }

    return proxy_management_service.mutate_assignment_references(
        [(body.proxy, body.proxy_group_id)],
        persist,
    )


def _account_status_category(account: dict[str, Any]) -> Literal["normal", "limited", "abnormal", "disabled"]:
    return account_status_category(account)


def _account_group_names() -> dict[str, str]:
    return {
        group_id: _clean_text(group.get("name")) or group_id
        for group in _config_dict_list("account_groups")
        if (group_id := _account_group_id(group.get("id")))
    }


def _account_available(account: dict[str, Any]) -> bool:
    checker = getattr(account_service, "is_image_account_available", None)
    if callable(checker):
        return bool(checker(account))
    return _account_status_category(account) == "normal" and account.get("last_remote_check_result") != "pending"


def _account_unlimited_quota(account: dict[str, Any]) -> bool:
    checker = getattr(account_service, "is_unlimited_image_quota_account", None)
    return bool(checker(account)) if callable(checker) else False


def _account_for_api(
        account: dict[str, Any],
        group_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    group_id = _clean_text(account.get("group_id"))
    return account_row(
        account,
        available=_account_available(account),
        unlimited_quota=_account_unlimited_quota(account),
        group_name=(group_names or {}).get(group_id, group_id),
    )


def _account_detail_for_api(account: dict[str, Any]) -> dict[str, Any]:
    group_id = _clean_text(account.get("group_id"))
    group_names = _account_group_names()
    return account_detail(
        account,
        available=_account_available(account),
        unlimited_quota=_account_unlimited_quota(account),
        group_name=group_names.get(group_id, group_id),
    )


def _get_account_by_id(account_id: str) -> dict[str, Any] | None:
    getter = getattr(account_service, "get_account_by_id", None)
    if callable(getter):
        return getter(account_id)
    normalized = _clean_text(account_id).lower()
    return next(
        (
            account for account in account_service.list_accounts()
            if _clean_text(account.get("management_id")).lower() == normalized
        ),
        None,
    )


def _get_account_by_token_identity(access_token: str) -> dict[str, Any] | None:
    getter = getattr(account_service, "get_account_by_token_identity", None)
    if callable(getter):
        return getter(access_token)
    return account_service.get_account(access_token)


def _accounts_by_id() -> dict[str, dict[str, Any]]:
    return {
        account_id: account
        for account in account_service.list_accounts()
        if (account_id := _clean_text(account.get("management_id")).lower())
    }


def _partition_account_ids(account_ids: list[str]) -> tuple[list[str], list[str]]:
    requested = list(dict.fromkeys(
        _clean_text(account_id).lower()
        for account_id in account_ids
        if _clean_text(account_id)
    ))
    existing_ids = set(_accounts_by_id())
    return (
        [account_id for account_id in requested if account_id in existing_ids],
        [account_id for account_id in requested if account_id not in existing_ids],
    )


def _resolve_account_targets(
        account_ids: list[str],
        legacy_tokens: list[str] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    ids = list(dict.fromkeys(_clean_text(item).lower() for item in account_ids if _clean_text(item)))
    tokens: list[str] = []
    missing_ids: list[str] = []
    resolved_ids: list[str] = []
    resolver = getattr(account_service, "resolve_account_ids", None)
    if ids and callable(resolver):
        tokens, missing_ids = resolver(ids)
        missing = set(missing_ids)
        resolved_ids = [item for item in ids if item not in missing]
    elif ids:
        for account_id in ids:
            account = _get_account_by_id(account_id)
            token = _clean_text((account or {}).get("access_token"))
            if token:
                tokens.append(token)
                resolved_ids.append(account_id)
            else:
                missing_ids.append(account_id)

    for token in _unique_tokens(legacy_tokens or []):
        account = account_service.get_account(token)
        if account is None:
            continue
        tokens.append(_clean_text(account.get("access_token")) or token)
        account_id = _clean_text(account.get("management_id"))
        if account_id:
            resolved_ids.append(account_id)
    return _unique_tokens(tokens), list(dict.fromkeys(resolved_ids)), missing_ids


def _account_not_found_errors(account_ids: list[str]) -> list[dict[str, str]]:
    return [
        {"id": account_id, "code": "account_not_found", "message": "account not found"}
        for account_id in dict.fromkeys(
            _clean_text(item) for item in account_ids if _clean_text(item)
        )
    ]


def _account_targets(
        account_ids: list[str],
        legacy_tokens: list[str] | None = None,
) -> tuple[list[tuple[str, str]], list[str]]:
    tokens, _resolved_ids, missing_ids = _resolve_account_targets(account_ids, legacy_tokens)
    targets: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    for token in tokens:
        account = account_service.get_account(token)
        account_id = _clean_text((account or {}).get("management_id"))
        current_token = _clean_text((account or {}).get("access_token")) or token
        if not account_id or account_id in seen_ids:
            continue
        seen_ids.add(account_id)
        targets.append((current_token, account_id))
    return targets, missing_ids


def _refresh_error_id_map(targets: list[tuple[str, str]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for token, account_id in targets:
        if not token or not account_id:
            continue
        mapping[token] = account_id
        mapping[anonymize_token(token)] = account_id
    return mapping


def _refresh_error_context() -> tuple[dict[str, str], list[str], list[str]]:
    targets: list[tuple[str, str]] = []
    sensitive_values: list[str] = []
    proxy_values: list[str] = []
    for account in account_service.list_accounts():
        token = _clean_text(account.get("access_token"))
        account_id = _clean_text(account.get("management_id"))
        if token and account_id:
            targets.append((token, account_id))
        for key in ("access_token", "refresh_token", "id_token"):
            value = _clean_text(account.get(key))
            if value:
                sensitive_values.append(value)
        proxy = _clean_text(account.get("proxy"))
        if proxy:
            proxy_values.append(proxy)
    return (
        _refresh_error_id_map(targets),
        _unique_tokens(sensitive_values),
        _unique_tokens(proxy_values),
    )


def _selected_refresh_context(
    targets: list[tuple[str, str]],
) -> tuple[list[str], list[str], dict[str, str], list[str], list[str]]:
    access_tokens = [token for token, _account_id in targets]
    target_ids = [account_id for _token, account_id in targets]
    id_by_token_hint = _refresh_error_id_map(targets)
    sensitive_values = list(access_tokens)
    proxy_values: list[str] = []
    for token, _account_id in targets:
        account = account_service.get_account(token) or {}
        for key in ("refresh_token", "id_token"):
            value = _clean_text(account.get(key))
            if value:
                sensitive_values.append(value)
        proxy = _clean_text(account.get("proxy"))
        if proxy:
            proxy_values.append(proxy)
    return access_tokens, target_ids, id_by_token_hint, sensitive_values, proxy_values


def _sanitize_refresh_errors(
        errors: object,
        *,
        id_by_token_hint: dict[str, str] | None = None,
        sensitive_values: list[str] | None = None,
        proxy_values: list[str] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(errors, list):
        return []
    result: list[dict[str, Any]] = []
    id_by_token_hint = id_by_token_hint or {}
    sensitive_values = [value for value in (sensitive_values or []) if value]
    proxy_values = [value for value in (proxy_values or []) if value]
    diagnostic_keys = (
        "failure_code",
        "failure_scope",
        "failure_capability",
        "failure_retryable",
        "failure_account_failure",
        "failure_retry_after",
        "status_code",
        "error_type",
    )
    for error in errors:
        if isinstance(error, dict):
            token_hint = _clean_text(error.get("token"))
            message = _clean_text(error.get("error") or error.get("message")) or "account refresh failed"
            code = _clean_text(error.get("code") or error.get("failure_code")) or "account_refresh_failed"
            item: dict[str, Any] = {
                "id": _clean_text(error.get("id")) or id_by_token_hint.get(token_hint, ""),
                "code": code,
                "message": message,
            }
            for key in diagnostic_keys:
                if key in error and error.get(key) is not None:
                    item[key] = error.get(key)
        else:
            item = {
                "id": "",
                "code": "account_refresh_failed",
                "message": _clean_text(error) or "account refresh failed",
            }
        item["message"] = sanitize_diagnostic_text(
            item["message"],
            sensitive_values=sensitive_values,
            proxy_values=proxy_values,
            limit=2000,
        )
        for key in diagnostic_keys:
            value = item.get(key)
            if isinstance(value, str):
                item[key] = sanitize_diagnostic_text(
                    value,
                    sensitive_values=sensitive_values,
                    proxy_values=proxy_values,
                    limit=500,
                )
        result.append(item)
    return result


def _project_accounts(account_ids: list[str]) -> list[dict[str, Any]]:
    requested_ids = list(dict.fromkeys(
        _clean_text(account_id).lower()
        for account_id in account_ids
        if _clean_text(account_id)
    ))
    if not requested_ids:
        return []
    accounts_by_id = _accounts_by_id()
    group_names = _account_group_names()
    return [
        _account_for_api(accounts_by_id[account_id], group_names)
        for account_id in requested_ids
        if account_id in accounts_by_id
    ]


def _account_operation_labels(
    targets: list[tuple[str, str]],
) -> dict[str, str]:
    labels: dict[str, str] = {}
    for token, account_id in targets:
        normalized_id = _clean_text(account_id)
        if not normalized_id:
            continue
        account = account_service.get_account(token) or {}
        labels[normalized_id] = _clean_text(account.get("email")) or normalized_id
    return labels


def _account_status_operation(
    status: str | None,
    operation: str | None,
) -> tuple[str, str, str]:
    requested = _clean_text(operation).lower()
    expected_status = {
        "enable": "正常",
        "disable": "禁用",
        "reset": "正常",
    }
    if requested in expected_status and status != expected_status[requested]:
        raise HTTPException(
            status_code=400,
            detail={"error": f"operation {requested} requires status {expected_status[requested]}"},
        )
    resolved = requested or "update"
    return {
        "enable": ("enable_account", "账号已启用", "账号启用失败"),
        "disable": ("disable_account", "账号已禁用", "账号禁用失败"),
        "reset": ("reset_account", "账号状态已重置", "账号状态重置失败"),
        "update": ("update_account", "账号已更新", "账号更新失败"),
    }[resolved]


def _account_mutation_events(
    *,
    action: str,
    success_ids: list[str] | None = None,
    removed_ids: list[str] | None = None,
    errors: list[dict[str, Any]] | None = None,
    labels: dict[str, str] | None = None,
    success_message: str,
    removed_message: str = "",
) -> list[dict[str, Any]]:
    labels = labels or {}
    normalized_errors = [
        item for item in (errors or []) if isinstance(item, dict)
    ]
    error_ids = {
        _clean_text(item.get("id"))
        for item in normalized_errors
        if _clean_text(item.get("id"))
    }
    raw_events: list[dict[str, Any]] = []
    sequence = 0
    for account_id in dict.fromkeys(success_ids or []):
        normalized_id = _clean_text(account_id)
        if not normalized_id or normalized_id in error_ids:
            continue
        sequence += 1
        raw_events.append({
            "sequence": sequence,
            "account_id": normalized_id,
            "account_label": labels.get(normalized_id, normalized_id),
            "action": action,
            "status": "success",
            "message": success_message,
        })
    for account_id in dict.fromkeys(removed_ids or []):
        normalized_id = _clean_text(account_id)
        if not normalized_id or normalized_id in error_ids:
            continue
        sequence += 1
        raw_events.append({
            "sequence": sequence,
            "account_id": normalized_id,
            "account_label": labels.get(normalized_id, normalized_id),
            "action": action,
            "status": "success" if action == "delete_account" else "failed",
            "message": removed_message or success_message,
        })
    for raw_error in normalized_errors:
        account_id = _clean_text(raw_error.get("id"))
        error_code = _clean_text(raw_error.get("code") or raw_error.get("failure_code"))
        error_message = _clean_text(raw_error.get("message") or raw_error.get("error"))
        if error_code and error_message:
            event_message = f"{error_code} · {error_message}"
        else:
            event_message = error_message or error_code or "account operation failed"
        sequence += 1
        raw_events.append({
            "sequence": sequence,
            "account_id": account_id,
            "account_label": labels.get(account_id, account_id),
            "action": action,
            "status": "failed",
            "message": event_message,
        })
    return normalize_account_operation_events(raw_events)


def _account_mutation_payload(
        *,
        updated_ids: list[str] | None = None,
        removed_ids: list[str] | None = None,
        errors: list[dict[str, Any]] | None = None,
        events: list[dict[str, Any]] | None = None,
        include_items: bool = True,
        **counts: Any,
) -> dict[str, Any]:
    updated = list(dict.fromkeys(_clean_text(item) for item in (updated_ids or []) if _clean_text(item)))
    removed = list(dict.fromkeys(_clean_text(item) for item in (removed_ids or []) if _clean_text(item)))
    removed_set = set(removed)
    updated = [account_id for account_id in updated if account_id not in removed_set]
    if "updated" in counts:
        counts["updated"] = len(updated)
    if "removed" in counts:
        counts["removed"] = len(removed)
    payload = {
        **counts,
        "updated_ids": updated,
        "removed_ids": removed,
        "errors": list(errors or []),
        "events": normalize_account_operation_events(events or []),
        "items": _project_accounts(updated) if include_items else [],
    }
    return payload


def _account_mutation_response(**mutation: Any) -> dict[str, Any]:
    payload = _account_mutation_payload(**mutation)
    total = len({
        *payload["updated_ids"],
        *payload["removed_ids"],
        *(
            _clean_text(item.get("id"))
            for item in payload["errors"]
            if isinstance(item, dict) and _clean_text(item.get("id"))
        ),
    })
    payload.update(project_account_operation_presentation({
        "total": total,
        "processed": total,
        "done": True,
        "events": payload["events"],
        "result": payload,
    }))
    return payload


def _init_account_mutation_progress(
    progress_id: str,
    *,
    total: int,
    missing_ids: list[str],
    action: str,
) -> None:
    account_service.init_refresh_progress(progress_id, total)
    for missing_id in missing_ids:
        account_service.update_refresh_progress(
            progress_id,
            missing_id,
            account_id=missing_id,
            account_label=missing_id,
            action=action,
            event_status="failed",
            event_message="account_not_found · 账号不存在",
        )


def _publish_account_mutation_stage(
    progress_id: str,
    stage: str,
    total: int,
) -> None:
    count = max(0, int(total or 0))
    labels = {
        "prepare_accounts": f"\u6b63\u5728\u51c6\u5907 {count} \u4e2a\u8d26\u53f7",
        "save_accounts": f"\u6b63\u5728\u4fdd\u5b58 {count} \u4e2a\u8d26\u53f7",
        "publish_results": "\u8d26\u53f7\u53d8\u66f4\u5df2\u4fdd\u5b58\uff0c\u6b63\u5728\u6574\u7406\u7ed3\u679c",
    }
    account_service.update_refresh_progress_stage(
        progress_id,
        stage,
        labels.get(stage, "\u6b63\u5728\u5904\u7406\u8d26\u53f7"),
    )


def _publish_account_mutation_progress(
    progress_id: str,
    *,
    targets: list[tuple[str, str]],
    labels: dict[str, str],
    events: list[dict[str, Any]],
) -> None:
    events_by_id = {
        _clean_text(event.get("account_id")): event
        for event in events
        if isinstance(event, dict) and _clean_text(event.get("account_id"))
    }
    for token, account_id in targets:
        event = events_by_id.get(account_id, {})
        event_status = _clean_text(event.get("status")) or "info"
        account = account_service.get_account(token) or {
            "management_id": account_id,
            "email": labels.get(account_id, account_id),
            "status": "异常" if event_status == "failed" else "正常",
            "quota": 0,
        }
        account_service.update_refresh_progress(
            progress_id,
            token,
            account,
            account_id=account_id,
            account_label=labels.get(account_id, account_id),
            action=_clean_text(event.get("action")),
            event_status=event_status,
            event_message=_clean_text(event.get("message")),
        )


def _account_operation_progress_for_api(
    progress: dict[str, Any],
    *,
    legacy_sync_alias: bool = False,
) -> dict[str, Any]:
    payload = {
        key: progress.get(key)
        for key in (
            "total",
            "processed",
            "done",
            "stage",
            "stage_label",
            "status_counts",
            "total_quota",
        )
        if key in progress
    }
    payload["events"] = normalize_account_operation_events(progress.get("events"))
    payload["error"] = "账号操作失败" if progress.get("error") else None
    result = progress.get("result")
    if not isinstance(result, dict):
        payload["result"] = None
        payload.update(project_account_operation_presentation(payload))
        return payload
    id_by_token_hint, sensitive_values, proxy_values = _refresh_error_context()
    updated_ids = [
        _clean_text(item) for item in result.get("updated_ids", []) if _clean_text(item)
    ]
    removed_ids = [
        _clean_text(item) for item in result.get("removed_ids", []) if _clean_text(item)
    ]
    result_counts: dict[str, int] = {
        "skipped": max(0, int(result.get("skipped") or 0)),
    }
    if "synced" in result:
        synced = max(0, int(result.get("synced") or 0))
        if legacy_sync_alias:
            result_counts["refreshed"] = synced
        else:
            result_counts["synced"] = synced
    elif "refreshed" in result:
        result_counts["refreshed"] = max(0, int(result.get("refreshed") or 0))
    for key in ("updated", "removed", "added", "confirmed", "pending"):
        if key in result:
            result_counts[key] = max(0, int(result.get(key) or 0))
    payload["result"] = _account_mutation_payload(
        **result_counts,
        updated_ids=updated_ids,
        removed_ids=removed_ids,
        errors=_sanitize_refresh_errors(
            result.get("errors"),
            id_by_token_hint=id_by_token_hint,
            sensitive_values=sensitive_values,
            proxy_values=proxy_values,
        ),
        events=normalize_account_operation_events(result.get("events")),
    )
    payload.update(project_account_operation_presentation(
        payload,
        legacy_sync_alias=legacy_sync_alias,
    ))
    return payload


def _status_matches_filter(account: dict[str, Any], status_filter: str) -> bool:
    status_filter = status_filter.strip().lower()
    if not status_filter or status_filter == "all":
        return True
    if status_filter in {"normal", "limited", "abnormal", "disabled"}:
        return _account_status_category(account) == status_filter
    status = _clean_text(account.get("status"))
    status_map = {
        "normal": "\u6b63\u5e38",
        "limited": "\u9650\u6d41",
        "abnormal": "\u5f02\u5e38",
        "disabled": "\u7981\u7528",
    }
    expected = status_map.get(status_filter)
    return status == expected if expected else status.lower() == status_filter


def _account_matches_keyword(account: dict[str, Any], keyword: str) -> bool:
    needle = keyword.strip().lower()
    if not needle:
        return True
    fields = (
        account.get("access_token"),
        account.get("email"),
        account.get("user_id"),
        account.get("type"),
        account.get("source_type"),
        account.get("status"),
        account.get("proxy"),
        account.get("group_id"),
    )
    return any(needle in _clean_text(value).lower() for value in fields)


def _account_matches_group(account: dict[str, Any], group_id: str) -> bool:
    group_id = group_id.strip()
    if not group_id or group_id == "all":
        return True
    current = _clean_text(account.get("group_id"))
    if group_id == "__ungrouped__":
        return not current
    return current == group_id


def _account_matches_filters(
        account: dict[str, Any],
        *,
        keyword: str,
        status: str,
        group_id: str,
        memory_state: str = "all",
) -> bool:
    return (
        _account_matches_keyword(account, keyword)
        and _status_matches_filter(account, status)
        and _account_matches_group(account, group_id)
        and (memory_state == "all" or account_memory_state(account) == memory_state)
    )


def _filtered_accounts(
        accounts: list[dict[str, Any]],
        *,
        keyword: str,
        status: str,
        group_id: str,
        memory_state: str = "all",
) -> list[dict[str, Any]]:
    return [
        account
        for account in accounts
        if _account_matches_filters(
            account,
            keyword=keyword,
            status=status,
            group_id=group_id,
            memory_state=memory_state,
        )
    ]


def _account_selection_members(
        selection: AccountSelectionScope,
) -> list[tuple[str, str]]:
    accounts = account_service.list_accounts()
    if selection.mode == "filter":
        accounts = _filtered_accounts(
            accounts,
            keyword=selection.keyword,
            status=selection.status,
            group_id=selection.group_id,
            memory_state=selection.memory_state,
        )

    members: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    for account in accounts:
        account_id = _clean_text(account.get("management_id")).lower()
        access_token = _clean_text(account.get("access_token"))
        if not account_id or not access_token or account_id in seen_ids:
            continue
        seen_ids.add(account_id)
        members.append((access_token, account_id))
    return members


def _account_selection_targets(
        selection: AccountSelectionScope | None,
        account_ids: list[str] | None = None,
        legacy_tokens: list[str] | None = None,
        *,
        default_all: bool = False,
) -> tuple[list[tuple[str, str]], list[str]]:
    legacy_ids = list(account_ids or [])
    legacy_tokens = list(legacy_tokens or [])
    mode = selection.mode if selection is not None else ("all" if default_all and not legacy_ids and not legacy_tokens else "explicit")

    if mode == "explicit":
        selected_ids = [*(selection.account_ids if selection is not None else []), *legacy_ids]
        return _account_targets(selected_ids, legacy_tokens)

    excluded_ids = {
        _clean_text(account_id).lower()
        for account_id in (selection.excluded_account_ids if selection is not None else [])
        if _clean_text(account_id)
    }
    scope = selection or AccountSelectionScope(mode="all")
    targets = [
        (access_token, account_id)
        for access_token, account_id in _account_selection_members(scope)
        if account_id not in excluded_ids
    ]
    return targets, []


def _account_selection_preview(selection: AccountSelectionScope) -> dict[str, Any]:
    if selection.mode == "explicit":
        targets, missing_ids = _account_selection_targets(selection)
        return {
            "matching_count": len(targets),
            "selected_count": len(targets),
            "excluded_account_ids": [],
            "errors": _account_not_found_errors(missing_ids),
        }

    excluded_ids = {
        _clean_text(account_id).lower()
        for account_id in selection.excluded_account_ids
        if _clean_text(account_id)
    }
    matching_ids = [account_id for _access_token, account_id in _account_selection_members(selection)]
    matching_set = set(matching_ids)
    valid_excluded_ids = [
        account_id for account_id in selection.excluded_account_ids
        if _clean_text(account_id).lower() in matching_set
    ]
    valid_excluded_ids = list(dict.fromkeys(
        _clean_text(account_id).lower()
        for account_id in valid_excluded_ids
        if _clean_text(account_id).lower() in excluded_ids
    ))
    return {
        "matching_count": len(matching_ids),
        "selected_count": max(0, len(matching_ids) - len(valid_excluded_ids)),
        "excluded_account_ids": valid_excluded_ids,
        "errors": [],
    }


def _accounts_page(
        *,
        page: int,
        page_size: int,
        keyword: str,
        status: str,
        group_id: str,
        memory_state: str = "all",
) -> dict[str, Any]:
    items = account_service.list_accounts()
    filtered = _filtered_accounts(
        items,
        keyword=keyword,
        status=status,
        group_id=group_id,
        memory_state=memory_state,
    )
    safe_page = max(1, page)
    safe_page_size = max(1, min(page_size, 500))
    start = (safe_page - 1) * safe_page_size
    end = start + safe_page_size
    group_names = _account_group_names()
    return {
        "items": [_account_for_api(item, group_names) for item in filtered[start:end]],
        "total": len(filtered),
        "all_total": len(items),
        "page": safe_page,
        "page_size": safe_page_size,
    }


def _account_zip_bytes(items: list[dict[str, Any]]) -> bytes:
    buf = io.BytesIO()
    used_names: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, item in enumerate(items, start=1):
            raw_name = item.get("email") or item.get("account_id") or f"account-{index:03d}"
            base_name = _safe_export_name(raw_name, f"account-{index:03d}")
            name = base_name
            suffix = 2
            while name in used_names:
                name = f"{base_name}-{suffix}"
                suffix += 1
            used_names.add(name)
            archive.writestr(
                f"{name}.json",
                json.dumps(item, ensure_ascii=False, indent=2) + "\n",
            )
    return buf.getvalue()


def _account_export_summary(requested: int, exported: int) -> dict[str, int]:
    requested = max(0, int(requested))
    exported = max(0, int(exported))
    return {
        "requested": requested,
        "exported": exported,
        "skipped": max(0, requested - exported),
    }


def _account_export_headers(summary: dict[str, int]) -> dict[str, str]:
    return {
        "X-Export-Requested": str(summary["requested"]),
        "X-Exported": str(summary["exported"]),
        "X-Skipped": str(summary["skipped"]),
    }
