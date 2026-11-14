from __future__ import annotations

from datetime import datetime, timezone
from services.config import config
from services.log_service import LOG_TYPE_ACCOUNT, log_service
from typing import Any

class PoolHealthMixin:
    """account pool health, auto-remove and stats（T-21 Mixin 拆分，方法块自 _all.py 原样搬迁）。"""

    def list_limited_tokens(self) -> list[str]:
        self._refresh_accounts_snapshot_if_stale()
        with self._lock:
            return [
                token
                for item in self._accounts.values()
                if item.get("status") == "限流"
                   and item.get("last_remote_check_result") != "pending"
                   and (token := item.get("access_token") or "")
            ]

    def _auto_remove_tokens_locked(
        self,
        *,
        remove_invalid: bool,
        remove_rate_limited: bool,
    ) -> tuple[list[str], list[str]]:
        invalid_tokens = [
            token
            for item in self._accounts.values()
            if remove_invalid
               and item.get("status") == "异常"
               and (token := item.get("access_token") or "")
        ]
        rate_limited_tokens = [
            token
            for item in self._accounts.values()
            if remove_rate_limited
               and item.get("status") == "限流"
               and (token := item.get("access_token") or "")
        ]
        return invalid_tokens, rate_limited_tokens

    def preview_auto_remove_accounts(
        self,
        *,
        remove_invalid: bool | None = None,
        remove_rate_limited: bool | None = None,
    ) -> dict[str, Any]:
        self._refresh_accounts_snapshot_if_stale()
        remove_invalid = config.auto_remove_invalid_accounts if remove_invalid is None else bool(remove_invalid)
        remove_rate_limited = (
            config.auto_remove_rate_limited_accounts
            if remove_rate_limited is None
            else bool(remove_rate_limited)
        )
        with self._lock:
            invalid_tokens, rate_limited_tokens = self._auto_remove_tokens_locked(
                remove_invalid=remove_invalid,
                remove_rate_limited=remove_rate_limited,
            )
        invalid = len(invalid_tokens)
        rate_limited = len(rate_limited_tokens)
        return {
            "dry_run": True,
            "invalid": invalid,
            "rate_limited": rate_limited,
            "total_removed": invalid + rate_limited,
            "auto_remove_invalid_accounts": remove_invalid,
            "auto_remove_rate_limited_accounts": remove_rate_limited,
        }

    def cleanup_auto_remove_accounts(
        self,
        *,
        remove_invalid: bool | None = None,
        remove_rate_limited: bool | None = None,
    ) -> dict[str, Any]:
        self._refresh_accounts_snapshot_if_stale()
        remove_invalid = config.auto_remove_invalid_accounts if remove_invalid is None else bool(remove_invalid)
        remove_rate_limited = (
            config.auto_remove_rate_limited_accounts
            if remove_rate_limited is None
            else bool(remove_rate_limited)
        )
        with self._lock:
            invalid_tokens, rate_limited_tokens = self._auto_remove_tokens_locked(
                remove_invalid=remove_invalid,
                remove_rate_limited=remove_rate_limited,
            )

        target_tokens = list(dict.fromkeys([*invalid_tokens, *rate_limited_tokens]))
        result = self.delete_accounts(target_tokens, return_items=False) if target_tokens else {"removed": 0}
        removed = int(result.get("removed") or 0)
        if removed:
            log_service.add(
                LOG_TYPE_ACCOUNT,
                "按账号自动移除策略清理账号",
                {
                    "removed": removed,
                    "invalid": len(invalid_tokens),
                    "rate_limited": len(rate_limited_tokens),
                },
            )
        return {
            "dry_run": False,
            "invalid": len(invalid_tokens),
            "rate_limited": len(rate_limited_tokens),
            "total_removed": removed,
            "auto_remove_invalid_accounts": remove_invalid,
            "auto_remove_rate_limited_accounts": remove_rate_limited,
        }

    def list_normal_tokens(self) -> list[str]:
        self._refresh_accounts_snapshot_if_stale()
        with self._lock:
            return [
                token
                for item in self._accounts.values()
                if item.get("status") == "正常"
                   and item.get("last_remote_check_result") != "pending"
                   and (token := item.get("access_token") or "")
            ]

    @classmethod
    def _pool_health_freshness_seconds(cls, freshness_seconds: int | float | None = None) -> int:
        if freshness_seconds is not None:
            try:
                return max(60, int(float(freshness_seconds)))
            except (TypeError, ValueError):
                pass
        return max(60, int(config.refresh_account_interval_minute) * 60)

    @classmethod
    def _remote_check_is_fresh(cls, account: dict, now: datetime, freshness_seconds: int) -> bool:
        checked_at = cls._parse_time(account.get("last_remote_checked_at"))
        return checked_at is not None and (now - checked_at).total_seconds() <= freshness_seconds

    @classmethod
    def _remote_check_attempt_is_recent(cls, account: dict, now: datetime, freshness_seconds: int) -> bool:
        attempted_at = cls._parse_time(account.get("last_remote_check_attempt_at"))
        return attempted_at is not None and (now - attempted_at).total_seconds() <= freshness_seconds

    @classmethod
    def _pool_health_metrics_from_accounts(
        cls,
        accounts: list[dict],
        *,
        now: datetime,
        freshness_seconds: int,
    ) -> dict[str, Any]:
        local_normal = [
            item
            for item in accounts
            if cls._is_account_selectable(item, allow_limited=False)
        ]
        confirmed_normal = [
            item
            for item in local_normal
            if cls._remote_check_is_fresh(item, now, freshness_seconds)
        ]
        latest_checked_at = ""
        for item in accounts:
            checked_at = cls._parse_time(item.get("last_remote_checked_at"))
            if checked_at is None:
                continue
            value = checked_at.isoformat()
            if not latest_checked_at or value > latest_checked_at:
                latest_checked_at = value
        return {
            "current_quota": sum(
                int(item.get("quota") or 0)
                for item in confirmed_normal
                if not item.get("image_quota_unknown")
            ),
            "current_available": len(confirmed_normal),
            "estimated_quota": sum(
                int(item.get("quota") or 0)
                for item in local_normal
                if not item.get("image_quota_unknown")
            ),
            "estimated_available": len(local_normal),
            "unconfirmed_available": max(0, len(local_normal) - len(confirmed_normal)),
            "unknown_quota_count": sum(1 for item in confirmed_normal if item.get("image_quota_unknown")),
            "pool_freshness_seconds": freshness_seconds,
            "pool_last_checked_at": latest_checked_at,
        }

    @classmethod
    def _pool_health_stale_tokens(
        cls,
        accounts: list[dict],
        *,
        now: datetime,
        freshness_seconds: int,
    ) -> list[str]:
        stale: list[tuple[float, str]] = []
        for item in accounts:
            token = str(item.get("access_token") or "").strip()
            if not token or item.get("status") != "正常":
                continue
            if cls._remote_check_is_fresh(item, now, freshness_seconds):
                continue
            if cls._remote_check_attempt_is_recent(item, now, freshness_seconds):
                continue
            checked_at = cls._parse_time(item.get("last_remote_checked_at"))
            sort_key = checked_at.timestamp() if checked_at else 0.0
            stale.append((sort_key, token))
        stale.sort(key=lambda item: item[0])
        return [token for _, token in stale]

    @staticmethod
    def _pool_health_target_reached(
        metrics: dict[str, Any],
        *,
        target_quota: int | None = None,
        target_available: int | None = None,
    ) -> bool:
        if target_quota is not None and int(metrics.get("current_quota") or 0) >= max(1, int(target_quota)):
            return True
        if target_available is not None and int(metrics.get("current_available") or 0) >= max(1, int(target_available)):
            return True
        return False

    def evaluate_account_pool(
        self,
        *,
        refresh_stale: bool = False,
        target_quota: int | None = None,
        target_available: int | None = None,
        freshness_seconds: int | float | None = None,
    ) -> dict[str, Any]:
        """Return registration-facing account pool metrics from remotely confirmed data.

        Local quota is useful for display, but registration stop decisions should not
        trust stale local quota.  This method refreshes only stale normal accounts,
        in small batches, until the requested target is confirmed or no eligible
        stale accounts remain.
        """
        freshness = self._pool_health_freshness_seconds(freshness_seconds)
        refreshed = 0
        refresh_errors: list[dict[str, Any]] = []

        while True:
            now = datetime.now(timezone.utc)
            accounts = self.list_accounts()
            metrics = self._pool_health_metrics_from_accounts(
                accounts,
                now=now,
                freshness_seconds=freshness,
            )
            if not refresh_stale:
                return {
                    **metrics,
                    "pool_refreshed": refreshed,
                    "pool_refresh_errors": refresh_errors,
                }
            if self._pool_health_target_reached(
                metrics,
                target_quota=target_quota,
                target_available=target_available,
            ):
                return {
                    **metrics,
                    "pool_refreshed": refreshed,
                    "pool_refresh_errors": refresh_errors,
                }

            stale_tokens = self._pool_health_stale_tokens(
                accounts,
                now=now,
                freshness_seconds=freshness,
            )
            if not stale_tokens:
                return {
                    **metrics,
                    "pool_refreshed": refreshed,
                    "pool_refresh_errors": refresh_errors,
                }

            batch = stale_tokens[: self._POOL_HEALTH_REFRESH_BATCH_SIZE]
            result = self.sync_accounts_and_quota(batch)
            refreshed += int(result.get("synced") or 0)
            refresh_errors.extend(result.get("errors") or [])

    def get_stats(self) -> dict:
        self._refresh_accounts_snapshot_if_stale()
        with self._lock:
            items = list(self._accounts.values())
        total = len(items)
        active = sum(1 for a in items if a.get("status") == "正常")
        limited = sum(1 for a in items if a.get("status") == "限流")
        abnormal = sum(1 for a in items if a.get("status") == "异常")
        disabled = sum(1 for a in items if a.get("status") == "禁用")
        normal_items = [a for a in items if a.get("status") == "正常"]
        total_quota = sum(max(0, int(a.get("quota") or 0)) for a in normal_items)
        unlimited = sum(1 for a in normal_items if self._is_unlimited_image_quota_account(a))
        unknown_quota = sum(
            1
            for a in normal_items
            if (
                bool(a.get("image_quota_unknown"))
                or (not bool(a.get("image_quota_unknown")) and max(0, int(a.get("quota") or 0)) <= 0)
            )
            and not self._is_unlimited_image_quota_account(a)
        )
        total_success = sum(int(a.get("success") or 0) for a in items)
        total_fail = sum(int(a.get("fail") or 0) for a in items)
        by_type = {}
        for a in items:
            t = a.get("type") or "unknown"
            by_type[t] = by_type.get(t, 0) + 1
        return {
            "total": total,
            "cumulative_total": self._cumulative_total,
            "active": active,
            "limited": limited,
            "abnormal": abnormal,
            "disabled": disabled,
            "total_quota": total_quota,
            "unlimited_quota_count": unlimited,
            "unknown_quota_count": unknown_quota,
            "total_success": total_success,
            "total_fail": total_fail,
            "by_type": by_type,
        }

    def account_health(self) -> dict:
        stats = self.get_stats()
        return {
            "healthy": stats["active"] > 0 or stats["unlimited_quota_count"] > 0,
            "status": "ok" if stats["active"] > 0 else "degraded",
            **stats,
        }
