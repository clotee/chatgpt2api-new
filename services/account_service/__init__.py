"""account_service 包门面。

公共 Interface 保持 `from services.account_service import account_service`
不变；实现按子域拆分进包内模块（Mixin 组合，见 T-21）。
"""

from ._all import AccountService, account_service
from ._shared import (
    ImageAccountSelectionError,
    OAuthRefreshError,
    RefreshCredentialsChangedError,
    TerminalRefreshTokenError,
)

__all__ = [
    "AccountService",
    "ImageAccountSelectionError",
    "OAuthRefreshError",
    "RefreshCredentialsChangedError",
    "TerminalRefreshTokenError",
    "account_service",
]
