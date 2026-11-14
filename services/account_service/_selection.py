from __future__ import annotations

import time
from ._shared import ImageAccountSelectionError, RefreshCredentialsChangedError, TerminalRefreshTokenError
from datetime import datetime, timezone
from services.config import config
from services.image_failure import ImageFailure

class ImageSelectionMixin:
    """image/text account selection and in-flight slots（T-21 Mixin 拆分，方法块自 _all.py 原样搬迁）。"""

    def _list_ready_candidate_tokens(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
    ) -> list[str]:
        excluded = set(excluded_tokens or set())
        return [
            token
            for item in self._accounts.values()
            if self._is_image_account_available(item)
               and self._account_matches_plan_type(item, plan_type)
               and self._account_matches_any_plan_type(item, plan_types)
               and self._account_matches_source_type(item, source_type)
               and (token := item.get("access_token") or "")
               and token not in excluded
        ]

    def _list_available_candidate_tokens(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
    ) -> list[str]:
        max_concurrency = max(1, int(config.image_account_concurrency or 1))
        return [
            token
            for token in self._list_ready_candidate_tokens(
                excluded_tokens,
                plan_type,
                source_type,
                plan_types,
            )
            if int(self._image_inflight.get(token, 0)) < max_concurrency
        ]

    def _acquire_next_candidate_token(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
            deadline_monotonic: float | None = None,
    ) -> str:
        while True:
            with self._image_slot_condition:
                remaining = (
                    float(deadline_monotonic) - time.monotonic()
                    if deadline_monotonic is not None
                    else None
                )
                if remaining is not None and remaining <= 0:
                    raise ImageAccountSelectionError(
                        "deadline_exceeded",
                        "image request deadline exceeded while waiting for an account slot",
                    )
                # Token refresh can rotate an attempted account's access token while
                # this request waits for a slot. Resolve aliases on every pass so the
                # same account cannot be selected again under its refreshed token.
                resolved_excluded_tokens = {
                    self._resolve_access_token_locked(token)
                    for token in (excluded_tokens or set())
                    if token
                }
                if not self._list_ready_candidate_tokens(
                    resolved_excluded_tokens,
                    plan_type,
                    source_type,
                    plan_types,
                ):
                    raise self._no_ready_candidate_error(
                        plan_type,
                        source_type,
                        plan_types,
                        resolved_excluded_tokens,
                    )
                tokens = self._list_available_candidate_tokens(
                    resolved_excluded_tokens,
                    plan_type,
                    source_type,
                    plan_types,
                )
                if tokens:
                    access_token = tokens[self._index % len(tokens)]
                    self._index += 1
                    self._image_inflight[access_token] = int(self._image_inflight.get(access_token, 0)) + 1
                    return access_token
                self._image_slot_condition.wait(
                    timeout=min(1.0, remaining) if remaining is not None else 1.0
                )
            # The wait can outlive the account snapshot TTL. Refresh outside the
            # account lock before considering another candidate so a remote delete
            # or disable cannot be leased from the stale in-memory view.
            self._refresh_accounts_snapshot_if_stale(wait_for_refresh=True)

    def _no_ready_candidate_error(
            self,
            plan_type: str | None,
            source_type: str | None,
            plan_types: set[str] | tuple[str, ...] | None,
            excluded_tokens: set[str] | None,
    ) -> "ImageAccountSelectionError":
        """没有任何 ready 候选时区分两种成因。

        这是“初筛阶段”的判据：只看本地缓存状态，此时还没走远程预检，
        不存在“预检失败”这一维度。匹配账号全部为“限流”（限流只由远程确认写入）
        -> 额度耗尽（429）；否则一律归为可重试的 unavailable（503）。

        注意：get_available_access_token 里的 429 判据更严
        （额外要求 not saw_unavailable_failure），因为那是“预检阶段”，
        会有上游波动等可重试失败混入，不能仅凭限流就下终结性结论。
        """
        excluded = set(excluded_tokens or set())
        matched = 0
        limited = 0
        for item in self._accounts.values():
            token = item.get("access_token") or ""
            if not token or token in excluded:
                continue
            if not (
                self._account_matches_plan_type(item, plan_type)
                and self._account_matches_any_plan_type(item, plan_types)
                and self._account_matches_source_type(item, source_type)
            ):
                continue
            matched += 1
            if str(item.get("status") or "") == "限流":
                limited += 1
        if matched > 0 and limited == matched:
            return ImageAccountSelectionError(
                "quota_exhausted",
                "all matched image accounts are remote-confirmed quota exhausted",
            )
        return ImageAccountSelectionError(
            "unavailable",
            "no image account is ready for current model/status filters",
        )

    def release_image_slot(self, access_token: str) -> None:
        if not access_token:
            return
        with self._image_slot_condition:
            access_token = self._resolve_access_token_locked(access_token)
            self._release_image_slot_locked(access_token)
            self._image_slot_condition.notify_all()

    def _release_image_slot_locked(self, access_token: str) -> None:
        current_inflight = int(self._image_inflight.get(access_token, 0))
        if current_inflight <= 1:
            self._image_inflight.pop(access_token, None)
        else:
            self._image_inflight[access_token] = current_inflight - 1

    def get_available_access_token(
            self,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
            excluded_tokens: set[str] | None = None,
            deadline_monotonic: float | None = None,
    ) -> str:
        """从候选池中获取一个可用的图片生图 token。

        基于本地缓存做初筛，然后通过 fetch_remote_info 做远程验证（token 有效性、配额等）。
        限制最大尝试次数防止 token rotation 导致无限循环。
        """
        self._refresh_accounts_snapshot_if_stale()
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise ImageAccountSelectionError(
                "deadline_exceeded",
                "image request deadline exceeded before account selection",
            )
        max_attempts = 20  # 防止无限循环
        externally_excluded = set(excluded_tokens or set())
        attempted_tokens: set[str] = set()
        # 控制流只保留两个出口，但最终是否能说“额度耗尽”必须谨慎：
        # 只要出现过非额度类失败，就说明不能断言全部账号都耗尽，应返回可重试的 unavailable。
        saw_remote_quota_exhausted = False
        saw_unavailable_failure = False
        for _attempt in range(max_attempts):
            try:
                access_token = self._acquire_next_candidate_token(
                    excluded_tokens=externally_excluded | attempted_tokens,
                    plan_type=plan_type,
                    source_type=source_type,
                    plan_types=plan_types,
                    deadline_monotonic=deadline_monotonic,
                )
            except ImageAccountSelectionError as exc:
                if exc.kind == "deadline_exceeded":
                    raise
                if attempted_tokens:
                    break
                raise
            attempted_tokens.add(access_token)
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                self.release_image_slot(access_token)
                raise ImageAccountSelectionError(
                    "deadline_exceeded",
                    "image request deadline exceeded before remote account validation",
                )
            try:
                account = self.fetch_remote_info(
                    access_token,
                    "get_available_access_token",
                    image_scope=True,
                )
            except Exception:
                # 预检失败（上游波动/网络/401 等）：这个号这次不可用，换下一个。
                # 401 已在 fetch_remote_info 内部走异常处理，这里不再二次分类。
                saw_unavailable_failure = True
                self.release_image_slot(access_token)
                if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                    raise ImageAccountSelectionError(
                        "deadline_exceeded",
                        "image request deadline exceeded during remote account validation",
                    )
                continue
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                self.release_image_slot(access_token)
                raise ImageAccountSelectionError(
                    "deadline_exceeded",
                    "image request deadline exceeded during remote account validation",
                )
            # fetch_remote_info 内部可能因 token rotation 导致 access_token 变化，
            # 把新 token 也加入排除列表，防止重复尝试
            resolved = str((account or {}).get("access_token") or "")
            if resolved and resolved != access_token:
                attempted_tokens.add(resolved)
            if (
                    self._is_image_account_available(account or {})
                    and self._account_matches_plan_type(account or {}, plan_type)
                    and self._account_matches_any_plan_type(account or {}, plan_types)
                    and self._account_matches_source_type(account or {}, source_type)
            ):
                return str((account or {}).get("access_token") or access_token)
            if str((account or {}).get("status") or "") == "限流":
                saw_remote_quota_exhausted = True
            else:
                saw_unavailable_failure = True
            self.release_image_slot(access_token)
        if saw_remote_quota_exhausted and not saw_unavailable_failure:
            raise ImageAccountSelectionError(
                "quota_exhausted",
                f"all usable image accounts remote-confirmed quota exhausted after {len(attempted_tokens)} attempts",
            )
        raise ImageAccountSelectionError(
            "unavailable",
            f"no image account available after {len(attempted_tokens)} attempts",
        )

    def get_text_access_token(self, excluded_tokens: set[str] | None = None) -> str:
        self._refresh_accounts_snapshot_if_stale()
        attempted = set(excluded_tokens or set())
        while True:
            with self._lock:
                candidates = [
                    token
                    for account in self._accounts.values()
                    if self._is_account_selectable(
                        account,
                        allow_limited=True,
                        allow_image_pending=True,
                    )
                       and (token := account.get("access_token") or "")
                       and token not in attempted
                ]
                if not candidates:
                    return ""
                access_token = candidates[self._index % len(candidates)]
                self._index += 1
            attempted.add(access_token)
            try:
                return self.ensure_access_token(access_token, event="get_text_access_token")
            except (TerminalRefreshTokenError, RefreshCredentialsChangedError):
                continue

    def mark_text_used(self, access_token: str) -> None:
        if not access_token:
            return
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return
            next_item = dict(current)
            next_item["last_used_at"] = self._now()
            account = self._normalize_account(next_item)
            if account is None:
                return
            self._accounts[access_token] = account
            self._save_accounts()

    @classmethod
    def is_image_account_available(cls, account: dict) -> bool:
        return cls._is_image_account_available(account)

    @classmethod
    def is_unlimited_image_quota_account(cls, account: dict) -> bool:
        return cls._is_unlimited_image_quota_account(account)

    def mark_image_result(
        self,
        access_token: str,
        success: bool,
        *,
        failure: ImageFailure | None = None,
        quota_consumed: bool | None = None,
        capabilities: set[str] | tuple[str, ...] | None = None,
        expected_access_token: str | None = None,
        expected_refresh_token: str | None = None,
        expected_last_token_refresh_at: str | None = None,
    ) -> dict | None:
        # Retained as call metadata only; capability-specific account state is gone.
        _ = capabilities
        if not access_token:
            return None
        now = datetime.now(timezone.utc)
        should_verify_after_failure = False
        consumed_quota = success if quota_consumed is None else bool(quota_consumed)
        with self._image_slot_condition:
            access_token = self._resolve_access_token_locked(access_token)
            self._release_image_slot_locked(access_token)
            try:
                current = self._accounts.get(access_token)
                if current is None:
                    return None
                expected_generation = None
                if expected_access_token is not None and expected_refresh_token is not None:
                    expected_generation = (
                        str(expected_access_token or "").strip(),
                        str(expected_refresh_token or "").strip(),
                        str(expected_last_token_refresh_at or "").strip(),
                    )
                    if (
                        self._credential_generation(access_token, current)
                        != expected_generation
                    ):
                        return None
                next_item = dict(current)
                next_item["last_used_at"] = now.isoformat()
                image_quota_unknown = bool(next_item.get("image_quota_unknown"))
                if success:
                    next_item["success"] = int(next_item.get("success") or 0) + 1
                if consumed_quota:
                    if not image_quota_unknown:
                        current_quota = max(0, int(next_item.get("quota") or 0))
                        next_item["quota"] = max(0, current_quota - 1)
                        if current_quota <= 1:
                            # 本地扣减到 0 只能说明“展示值需要远程刷新”，不能直接证明账号已限流。
                            # 下一次调度会进入远程预检，由 get_user_info 的结果决定是否写入“限流”。
                            next_item["image_quota_unknown"] = True
                            next_item["last_quota_estimated_empty_at"] = now.isoformat()
                    if next_item.get("status") == "限流":
                        # 上游已经消耗图片额度，说明远程额度已恢复。
                        next_item["status"] = "正常"
                        next_item["image_quota_unknown"] = True
                        next_item["restore_at"] = None
                if not success and failure is not None and failure.verify_account:
                    next_item["fail"] = int(next_item.get("fail") or 0) + 1
                    self._mark_remote_check_pending(
                        next_item,
                        "image_failure",
                        now.isoformat(),
                        scope="image",
                    )
                    should_verify_after_failure = True
                account = self._normalize_account(next_item)
                if account is None:
                    return None
                self._accounts[access_token] = account
                saved = self._save_accounts(
                    expected_credential_generation=expected_generation,
                )
                if not saved:
                    should_verify_after_failure = False
                    return None
                access_token = self._resolve_access_token_locked(access_token)
                persisted = self._accounts.get(access_token)
                if persisted is None:
                    should_verify_after_failure = False
                    return None
                result = dict(persisted)
            finally:
                self._image_slot_condition.notify_all()
        if should_verify_after_failure:
            scheduled = self._schedule_account_refresh_after_image_failure(
                access_token,
                force=True,
            )
            if not scheduled:
                self._record_remote_check_error(
                    access_token,
                    "image_failure",
                    "Account verification could not be scheduled.",
                )
        return result
