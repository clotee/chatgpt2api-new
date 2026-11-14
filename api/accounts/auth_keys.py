from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from fastapi.concurrency import run_in_threadpool

from contracts.auth import (
    UserKeyCreateRequest,
    UserKeyCreateResult,
    UserKeyDeleteResult,
    UserKeyListView,
    UserKeyUpdateRequest,
    UserKeyUpdateResult,
)
from services.auth_service import auth_service

from api.support import require_admin


def register_user_key_routes(router: APIRouter) -> None:
    @router.get("/api/auth/users", response_model=UserKeyListView)
    async def list_user_keys(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        items = await run_in_threadpool(auth_service.list_keys, role="user")
        return {"items": items}

    @router.post("/api/auth/users", response_model=UserKeyCreateResult)
    async def create_user_key(body: UserKeyCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            item, raw_key = await run_in_threadpool(auth_service.create_key, role="user", name=body.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {"item": item, "raw_key": raw_key}

    @router.post("/api/auth/users/{key_id}", response_model=UserKeyUpdateResult)
    async def update_user_key(
            key_id: str,
            body: UserKeyUpdateRequest,
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        updates = {
            key: value
            for key, value in {
                "name": body.name,
                "enabled": body.enabled,
                "key": body.key,
            }.items()
            if value is not None
        }
        if not updates:
            raise HTTPException(status_code=400, detail={"error": "还没有检测到改动，请修改后再保存"})
        try:
            item = await run_in_threadpool(auth_service.update_key, key_id, updates, role="user")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "这条用户密钥不存在，可能已经被删除"})
        return {"item": item}

    @router.delete("/api/auth/users/{key_id}", response_model=UserKeyDeleteResult)
    async def delete_user_key(key_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        deleted = await run_in_threadpool(auth_service.delete_key, key_id, role="user")
        if not deleted:
            raise HTTPException(status_code=404, detail={"error": "这条用户密钥不存在，可能已经被删除"})
        return {"deleted_id": key_id}
