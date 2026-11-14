from __future__ import annotations

import time
from ._shared import RefreshCredentialsChangedError, _CredentialGeneration
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from services.account_processing import account_processing_slot, account_processing_worker_count
from typing import Any
from uuid import uuid4

class AccountMemoryMixin:
    """account memory maintenance（T-21 Mixin 拆分，方法块自 _all.py 原样搬迁）。"""

    def manage_account_memory(
        self,
        account_ids: list[str],
        enabled: bool | None = None,
        progress_id: str | None = None,
        *,
        finalize_progress: bool = True,
        clear: bool = False,
    ) -> dict[str, Any]:
        """Maintain memory using one guarded lifecycle for single and batch operations."""
        if enabled is not None and not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean or None")
        if not isinstance(clear, bool) or (clear and enabled is not None):
            raise ValueError("clear cannot be combined with enabled")
        target_ids = list(dict.fromkeys(
            str(value or "").strip().lower() for value in account_ids if str(value or "").strip()
        ))
        action = "clear_memory" if clear else (
            "refresh_memory" if enabled is None else ("enable_memory" if enabled else "disable_memory")
        )
        result: dict[str, Any] = {
            "confirmed": 0, "pending": 0, "skipped": 0,
            "updated_ids": [], "removed_ids": [], "errors": [],
        }
        if progress_id and self.get_refresh_progress(progress_id) is None:
            self.init_refresh_progress(progress_id, len(target_ids))
        if progress_id:
            self.update_refresh_progress_stage(progress_id, "running", "正在清空记忆" if clear else "正在处理记忆设置")

        def operate(account_id: str) -> dict[str, Any]:
            from services.openai_backend_api import MemoryStateInvalidError, OpenAIBackendAPI

            account = self.get_account_by_id(account_id)
            token = str((account or {}).get("access_token") or "")
            outcome: dict[str, Any] = {
                "status": "failed", "account": account, "token": token,
                "message": "账号不存在", "updated": False,
            }
            if account is None:
                return outcome
            with self._lock:
                if account_id in self._memory_active_account_ids:
                    outcome.update(status="skipped", message="该账号已有记忆操作正在执行")
                    return outcome
                self._memory_active_account_ids.add(account_id)
            operation_id = uuid4().hex
            generation: _CredentialGeneration | None = None
            try:
                with account_processing_slot():
                    deadline = time.monotonic() + 60.0
                    with self._lock:
                        current = self._accounts.get(token)
                        if current is None or current.get("management_id") != account_id:
                            outcome.update(status="skipped", message="账号或凭据已变更，请重新操作")
                            return outcome
                        generation = self._credential_generation(token, current)
                        previous_operation = str(current.get("memory_operation_id") or "")
                        self._accounts[token] = {**current, "memory_operation_id": operation_id}
                        claimed = self._save_accounts(
                            expected_credential_generation=generation,
                            expected_memory_operation=(token, previous_operation),
                        )
                    if not claimed:
                        outcome.update(status="skipped", message="账号或记忆操作已变更，请重新操作")
                        return outcome

                    observed: bool | None = None
                    cleared: bool | None = None
                    checked_at = ""
                    accepted = False
                    verify_pending_clear = False
                    clear_check_error = ""
                    status, message = "verified", "记忆状态已刷新"

                    def timeout() -> float:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("记忆操作超时")
                        return min(20.0, remaining)

                    try:
                        token = self.ensure_access_token(
                            token, event=action, raise_on_error=True,
                            expected_credentials=generation,
                        )
                        token, _, account = self._credential_snapshot(token)
                        if (
                            account is None
                            or account.get("management_id") != account_id
                            or account.get("memory_operation_id") != operation_id
                        ):
                            raise RefreshCredentialsChangedError()
                        generation = self._credential_generation(token, account)
                        auth = self._decode_jwt_payload(token).get("https://api.openai.com/auth")
                        auth = auth if isinstance(auth, dict) else {}
                        upstream_id = str(account.get("account_id") or auth.get("chatgpt_account_id") or "").strip()
                        verify_pending_clear = (
                            not clear and enabled is None and account.get("memory_clear_status") == "pending"
                        )
                        with OpenAIBackendAPI(token) as backend:
                            if clear:
                                backend.clear_saved_memories(upstream_id, timeout_secs=timeout())
                                accepted = True
                                cleared = backend.saved_memories_empty(upstream_id, timeout_secs=timeout())
                            else:
                                if enabled is not None:
                                    backend.set_memory_enabled(upstream_id, enabled, timeout_secs=timeout())
                                    accepted = True
                                try:
                                    observed = backend.get_memory_enabled(upstream_id, timeout_secs=timeout())
                                    if observed is not None:
                                        checked_at = datetime.now(timezone.utc).isoformat()
                                finally:
                                    if verify_pending_clear:
                                        try:
                                            cleared = backend.saved_memories_empty(upstream_id, timeout_secs=timeout())
                                        except Exception:
                                            # Independent verification preserves the switch snapshot.
                                            clear_check_error = "清空结果验证失败，待验证"
                        if clear:
                            if cleared is True:
                                message = "已确认清空已保存的记忆"
                            else:
                                status = "pending"
                                message = (
                                    "清空请求已接受，上游仍返回已保存的记忆，待验证"
                                    if cleared is False else "清空请求已接受，无法确认记忆是否为空，待验证"
                                )
                        elif observed is None:
                            status = "pending" if accepted else "failed"
                            message = "修改请求已接受，上游未返回记忆状态，待验证" if accepted else "上游未返回记忆状态"
                        else:
                            if enabled is not None and observed != enabled:
                                status, message = "failed", "上游确认的记忆状态与目标不一致"
                            elif enabled is not None:
                                message = "记忆已确认开启" if observed else "记忆已确认关闭"
                    except RefreshCredentialsChangedError:
                        outcome.update(status="skipped", message="账号或凭据已变更，请重新操作")
                        return outcome
                    except MemoryStateInvalidError:
                        status, message = ("pending" if clear and accepted else "failed"), "上游记忆状态格式无效"
                    except Exception as exc:
                        status = "pending" if accepted else "failed"
                        detail = self._credential_error_text(exc, account, access_token=token)
                        prefix = "清空请求已接受，结果验证失败，待验证：" if clear else "修改请求已接受，状态验证失败，待验证："
                        message = (prefix if accepted else "记忆操作失败：") + detail

                    with self._lock:
                        current = self._accounts.get(token)
                        if (
                            current is None
                            or self._credential_generation(token, current) != generation
                            or current.get("management_id") != account_id
                            or current.get("memory_operation_id") != operation_id
                        ):
                            outcome.update(status="skipped", message="账号或记忆操作已变更，请重新操作")
                            return outcome
                        operation_at = datetime.now(timezone.utc).isoformat()
                        field_prefix = "memory_clear" if clear else "memory_operation"
                        next_item = {
                            **current,
                            f"{field_prefix}_status": status,
                            f"{field_prefix}_error": "" if status == "verified" else message[:500],
                            f"{field_prefix}_at": operation_at,
                        }
                        if clear and status == "verified":
                            next_item["memory_cleared_at"] = operation_at
                        if verify_pending_clear:
                            if cleared is True:
                                next_item.update(
                                    memory_clear_status="verified", memory_clear_error="",
                                    memory_cleared_at=operation_at,
                                )
                                message += "；已确认记忆为空"
                            else:
                                clear_message = clear_check_error or "尚未确认记忆为空，清空记录仍待验证"
                                next_item["memory_clear_error"] = clear_message
                                message += "；" + clear_message
                        if observed is not None:
                            next_item["memory_enabled"] = observed
                            next_item["memory_checked_at"] = checked_at
                        self._accounts[token] = next_item
                        saved = self._save_accounts(
                            expected_credential_generation=generation,
                            expected_memory_operation=(token, operation_id),
                        )
                        persisted = self._accounts.get(token)
                        if not saved or persisted is None or persisted.get("memory_operation_id") != operation_id:
                            outcome.update(status="skipped", message="账号或记忆操作已变更，请重新操作")
                            return outcome
                        outcome_status = "confirmed" if status == "verified" else status
                        if verify_pending_clear and cleared is not True and outcome_status == "confirmed":
                            outcome_status = "pending"
                        outcome.update(
                            status=outcome_status,
                            account=dict(persisted), token=token, message=message, updated=True,
                        )
                        return outcome
            except Exception:
                # Storage errors can contain SQL parameters from a whole batch.
                outcome.update(message="记忆操作未能保存，请刷新账号后重试")
                return outcome
            finally:
                with self._lock:
                    self._memory_active_account_ids.discard(account_id)

        executor = ThreadPoolExecutor(max_workers=account_processing_worker_count(len(target_ids))) if target_ids else None
        try:
            futures = {executor.submit(operate, account_id): account_id for account_id in target_ids} if executor else {}
            for future in as_completed(futures):
                account_id = futures[future]
                outcome = future.result()
                status = outcome["status"]
                account = outcome["account"]
                if status in {"confirmed", "pending", "skipped"}:
                    result[status] += 1
                else:
                    result["errors"].append({
                        "id": account_id, "account_id": account_id,
                        "error": outcome["message"],
                    })
                if outcome["updated"]:
                    result["updated_ids"].append(account_id)
                if progress_id:
                    self.update_refresh_progress(
                        progress_id, outcome["token"], account,
                        account_id=account_id,
                        action=action,
                        event_status={"confirmed": "success", "pending": "info"}.get(status, status),
                        event_message=outcome["message"],
                    )
        except BaseException:
            if progress_id and finalize_progress:
                self.finish_refresh_progress(progress_id, error="记忆操作已中断")
            if executor:
                executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            if executor:
                executor.shutdown(wait=True, cancel_futures=True)
        if progress_id and finalize_progress:
            self.finish_refresh_progress(progress_id, result)
        return result
