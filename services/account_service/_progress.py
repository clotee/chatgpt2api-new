from __future__ import annotations

import time
from services.account_operation_events import normalize_account_operation_event
from threading import Lock
from typing import Any

class RefreshProgressMixin:
    """refresh progress tracking（T-21 Mixin 拆分，方法块自 _all.py 原样搬迁）。"""

    # 刷新进度追踪
    _refresh_progress: dict[str, dict] = {}
    _refresh_progress_lock = Lock()
    _refresh_progress_last_pruned_at = 0.0

    # ---- 刷新进度追踪 ----

    @classmethod
    def _prune_refresh_progress_locked(
        cls,
        now: float,
        *,
        target_id: str | None = None,
    ) -> None:
        full_scan_due = (
            now < cls._refresh_progress_last_pruned_at
            or now - cls._refresh_progress_last_pruned_at
            >= cls._REFRESH_PROGRESS_PRUNE_INTERVAL_SECONDS
        )
        if full_scan_due:
            candidates = list(cls._refresh_progress)
            cls._refresh_progress_last_pruned_at = now
        elif target_id and target_id in cls._refresh_progress:
            candidates = [target_id]
        else:
            return

        for progress_id in candidates:
            progress = cls._refresh_progress.get(progress_id)
            if progress is None:
                continue
            updated_at_value = progress.get("_updated_at_monotonic")
            if isinstance(updated_at_value, (int, float)):
                updated_at = float(updated_at_value)
            else:
                updated_at = now
                progress["_updated_at_monotonic"] = now
            ttl = (
                cls._REFRESH_PROGRESS_COMPLETED_TTL_SECONDS
                if progress.get("done")
                else cls._REFRESH_PROGRESS_ACTIVE_TTL_SECONDS
            )
            if now - updated_at >= ttl:
                cls._refresh_progress.pop(progress_id, None)

    def init_refresh_progress(self, progress_id: str, total: int) -> None:
        """初始化刷新进度记录。"""
        now = time.monotonic()
        with self._refresh_progress_lock:
            self._prune_refresh_progress_locked(now, target_id=progress_id)
            self._refresh_progress[progress_id] = {
                "total": total,
                "processed": 0,
                "done": False,
                "error": None,
                "status_counts": {"正常": 0, "限流": 0, "异常": 0, "禁用": 0},
                "total_quota": 0,
                "events": [],
                "_event_sequence": 0,
                "_updated_at_monotonic": now,
            }

    def update_refresh_progress_stage(
        self,
        progress_id: str,
        stage: str,
        stage_label: str,
    ) -> None:
        """Publish task-level progress without counting an account as completed."""
        now = time.monotonic()
        with self._refresh_progress_lock:
            self._prune_refresh_progress_locked(now, target_id=progress_id)
            progress = self._refresh_progress.get(progress_id)
            if progress is None or progress.get("done"):
                return
            progress["stage"] = str(stage or "").strip()
            progress["stage_label"] = str(stage_label or "").strip()
            progress["_updated_at_monotonic"] = now

    @staticmethod
    def _account_operation_identity(
        account: dict | None,
        *,
        fallback_id: str = "",
    ) -> tuple[str, str]:
        item = account or {}
        account_id = str(item.get("management_id") or fallback_id or "").strip()
        account_label = str(item.get("email") or account_id).strip()
        return account_id, account_label

    @classmethod
    def _append_refresh_progress_event_locked(
        cls,
        progress: dict[str, Any],
        *,
        account_id: str = "",
        account_label: str = "",
        action: str,
        status: str,
        message: str,
        sensitive_values: tuple[object, ...] = (),
        proxy_values: tuple[object, ...] = (),
    ) -> None:
        sequence = int(progress.get("_event_sequence") or 0) + 1
        event = normalize_account_operation_event(
            {
                "sequence": sequence,
                "account_id": account_id,
                "account_label": account_label,
                "action": action,
                "status": status,
                "message": message,
            },
            fallback_sequence=sequence,
            sensitive_values=sensitive_values,
            proxy_values=proxy_values,
        )
        if event is None:
            return
        events = progress.setdefault("events", [])
        events.append(event)
        if len(events) > cls._REFRESH_PROGRESS_EVENT_LIMIT:
            del events[:-cls._REFRESH_PROGRESS_EVENT_LIMIT]
        progress["_event_sequence"] = sequence

    def update_refresh_progress(
        self,
        progress_id: str,
        token: str,
        result_account: dict | None = None,
        *,
        account_id: str = "",
        account_label: str = "",
        action: str = "",
        event_status: str = "info",
        event_message: str = "",
    ) -> None:
        """刷新单个账号后，更新进度计数。"""
        account = result_account or self.get_account(token)
        status = str(account.get("status") or "正常").strip() if account else "异常"
        quota = max(0, int(account.get("quota") or 0)) if account else 0

        now = time.monotonic()
        with self._refresh_progress_lock:
            self._prune_refresh_progress_locked(now, target_id=progress_id)
            progress = self._refresh_progress.get(progress_id)
            if progress is None:
                return
            progress["processed"] += 1
            progress["status_counts"][status] = progress["status_counts"].get(status, 0) + 1
            progress["total_quota"] += quota
            if action and event_message:
                event_account_id, event_account_label = self._account_operation_identity(
                    account,
                    fallback_id=account_id,
                )
                sensitive_values = tuple(
                    value
                    for value in (
                        token,
                        (account or {}).get("access_token"),
                        (account or {}).get("refresh_token"),
                        (account or {}).get("id_token"),
                        (account or {}).get("password"),
                    )
                    if value
                )
                proxy_values = tuple(
                    value for value in ((account or {}).get("proxy"),) if value
                )
                self._append_refresh_progress_event_locked(
                    progress,
                    account_id=event_account_id,
                    account_label=account_label or event_account_label,
                    action=action,
                    status=event_status,
                    message=event_message,
                    sensitive_values=sensitive_values,
                    proxy_values=proxy_values,
                )
            progress["_updated_at_monotonic"] = now

    def finish_refresh_progress(
        self,
        progress_id: str,
        result: dict | None = None,
        error: str | None = None,
    ) -> None:
        """标记刷新完成。"""
        now = time.monotonic()
        with self._refresh_progress_lock:
            self._prune_refresh_progress_locked(now, target_id=progress_id)
            progress = self._refresh_progress.get(progress_id)
            if progress is None:
                return
            progress["done"] = True
            progress["result"] = result
            progress["stage"] = "completed"
            progress["stage_label"] = ""
            if error:
                progress["error"] = error
            progress["_updated_at_monotonic"] = now

    def get_refresh_progress(self, progress_id: str) -> dict | None:
        """查询刷新进度。"""
        now = time.monotonic()
        with self._refresh_progress_lock:
            self._prune_refresh_progress_locked(now, target_id=progress_id)
            progress = self._refresh_progress.get(progress_id)
            return {
                key: value
                for key, value in progress.items()
                if not key.startswith("_")
            } if progress else None

    def clean_refresh_progress(self, progress_id: str) -> None:
        """清理过期进度记录。"""
        with self._refresh_progress_lock:
            self._refresh_progress.pop(progress_id, None)
