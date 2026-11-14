from __future__ import annotations

from fastapi import APIRouter

from api.accounts.accounts import (
    register_account_export_update_routes,
    register_account_query_routes,
    register_account_write_routes,
)
from api.accounts.auth_keys import register_user_key_routes
from api.accounts.groups import register_account_group_binding, register_account_group_routes
from api.accounts.operations import register_account_batch_update, register_account_operation_routes
from api.accounts.remote_import import register_remote_import_routes


def create_router() -> APIRouter:
    """Aggregate account-management routes in the original registration order.

    The call order below mirrors the route decorator order of the former
    single-file ``api/accounts.py`` ``create_router`` and must not be
    reordered: overlapping paths depend on it.
    """
    router = APIRouter()
    register_user_key_routes(router)
    register_account_query_routes(router)
    register_account_group_routes(router)
    register_account_write_routes(router)
    register_account_operation_routes(router)
    register_account_export_update_routes(router)
    register_account_batch_update(router)
    register_account_group_binding(router)
    register_remote_import_routes(router)
    return router


__all__ = ["create_router"]
