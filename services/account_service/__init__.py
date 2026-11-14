"""account_service 包门面。

公共 Interface 保持 `from services.account_service import account_service`
不变；实现按子域拆分进包内模块（Mixin 组合，见 T-21）。
"""

from ._core import AccountServiceCore
from ._memory import AccountMemoryMixin
from ._mutations import AccountMutationMixin
from ._pool import PoolHealthMixin
from ._primitives import AccountPrimitivesMixin
from ._progress import RefreshProgressMixin
from ._refresh import TokenRefreshMixin
from ._remote_check import RemoteCheckMixin
from ._selection import ImageSelectionMixin
from ._shared import (
    ImageAccountSelectionError,
    OAuthRefreshError,
    RefreshCredentialsChangedError,
    TerminalRefreshTokenError,
)
from ._sync import AccountSyncMixin
from services.config import config
from services.proxy_management_service import proxy_management_service


class AccountService(
    AccountServiceCore,
    AccountPrimitivesMixin,
    RefreshProgressMixin,
    TokenRefreshMixin,
    RemoteCheckMixin,
    AccountMutationMixin,
    ImageSelectionMixin,
    AccountSyncMixin,
    AccountMemoryMixin,
    PoolHealthMixin,
):
    pass


account_service = AccountService(
    config.get_storage_backend(),
    proxy_reference_mutation=proxy_management_service.mutate_assignment_references,
)
proxy_management_service.bind_account_provider(account_service.list_accounts)

__all__ = [
    "AccountService",
    "ImageAccountSelectionError",
    "OAuthRefreshError",
    "RefreshCredentialsChangedError",
    "TerminalRefreshTokenError",
    "account_service",
]
