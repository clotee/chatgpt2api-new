from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from services.account_processing import account_processing_worker_count
from services.image_failure import classify_image_exception
from typing import Any
from utils.helper import anonymize_token

class AccountSyncMixin:
    """account metadata and quota synchronization（T-21 Mixin 拆分，方法块自 _all.py 原样搬迁）。"""

    def sync_accounts_and_quota(
        self,
        access_tokens: list[str],
        progress_id: str | None = None,
        remove_invalid: bool | None = None,
        *,
        finalize_progress: bool = True,
    ) -> dict[str, Any]:
        """Synchronize remote account metadata and image quota."""
        access_tokens = list(dict.fromkeys(token for token in access_tokens if token))
        if not access_tokens:
            items = self.list_accounts()
            result = {"synced": 0, "errors": [], "items": items}
            if progress_id and finalize_progress:
                self.finish_refresh_progress(progress_id, result)
            return result

        synced = 0
        errors = []
        max_workers = account_processing_worker_count(len(access_tokens))

        if progress_id and self.get_refresh_progress(progress_id) is None:
            self.init_refresh_progress(progress_id, len(access_tokens))

        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            futures = {}
            for token in access_tokens:
                account_id, account_label = self._account_operation_identity(
                    self.get_account(token)
                )
                future = executor.submit(
                    self.fetch_remote_info,
                    token,
                    "sync_accounts_and_quota",
                    remove_invalid,
                    preflight_refresh=False,
                )
                futures[future] = (token, account_id, account_label)
            for future in as_completed(futures):
                token, account_id, account_label = futures[future]
                result_account = None
                try:
                    account = future.result()
                except (KeyboardInterrupt, SystemExit):
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise
                except Exception as exc:
                    failure = classify_image_exception(exc)
                    account = self.get_account(token)
                    errors.append({
                        "token": anonymize_token(token),
                        "account_id": account_id,
                        "account_label": account_label,
                        "error": self._credential_error_text(
                            exc,
                            account,
                            access_token=token,
                        ),
                        **failure.diagnostic_fields(),
                    })
                    event_status = "failed"
                    event_message = self._credential_error_text(
                        exc,
                        account,
                        access_token=token,
                    )
                else:
                    if account is not None:
                        synced += 1
                        result_account = account
                        event_status = "success"
                        event_message = "\u8d26\u53f7\u4e0e\u989d\u5ea6\u5df2\u540c\u6b65"
                    else:
                        event_status = "failed"
                        event_message = "\u8d26\u53f7\u4e0e\u989d\u5ea6\u540c\u6b65\u5931\u8d25"

                if progress_id:
                    self.update_refresh_progress(
                        progress_id,
                        token,
                        result_account,
                        account_id=account_id,
                        account_label=account_label,
                        action="sync_account",
                        event_status=event_status,
                        event_message=event_message,
                    )
        except (KeyboardInterrupt, SystemExit):
            if progress_id:
                self.finish_refresh_progress(progress_id, error="cancelled")
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True, cancel_futures=True)

        result = {
            "synced": synced,
            "errors": errors,
            "items": self.list_accounts(),
        }

        if progress_id and finalize_progress:
            self.finish_refresh_progress(progress_id, result)

        return result

    def refresh_accounts(
        self,
        access_tokens: list[str],
        progress_id: str | None = None,
        remove_invalid: bool | None = None,
        *,
        finalize_progress: bool = True,
    ) -> dict[str, Any]:
        """Compatibility alias for synchronize-account-and-quota callers."""
        result = self.sync_accounts_and_quota(
            access_tokens,
            progress_id,
            remove_invalid,
            finalize_progress=finalize_progress,
        )
        return {**result, "refreshed": int(result.get("synced") or 0)}
