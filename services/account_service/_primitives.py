from __future__ import annotations

import hashlib
from ._shared import _CredentialGeneration
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from services.account_credentials import access_token_expires_in_seconds, access_token_issued_at, decode_access_token_payload, project_upstream_credential_availability
from services.storage.base import StorageMutation
from utils.diagnostics import sanitize_diagnostic_text

class AccountPrimitivesMixin:
    """account normalize/predicate/JWT/CAS primitives（T-21 Mixin 拆分，方法块自 _all.py 原样搬迁）。"""

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _decode_jwt_payload(token: str) -> dict:
        return decode_access_token_payload(token)

    @staticmethod
    def _management_id_for_token(access_token: str) -> str:
        digest = hashlib.sha256(str(access_token or "").encode("utf-8")).hexdigest()[:24]
        return f"acct_{digest}"

    @staticmethod
    def _access_token_fingerprint(access_token: str) -> str:
        return hashlib.sha256(str(access_token or "").encode("utf-8")).hexdigest()

    @staticmethod
    def _scrub_diagnostic_secrets(
        account: dict,
        values: list[object],
    ) -> None:
        secrets = [str(value or "").strip() for value in values]
        secrets = [secret for secret in secrets if secret]
        if not secrets:
            return
        for key in (
            "last_remote_check_error",
            "last_refresh_error",
            "last_token_refresh_error",
        ):
            value = account.get(key)
            if not isinstance(value, str):
                continue
            account[key] = sanitize_diagnostic_text(
                value,
                sensitive_values=secrets,
            )

    @classmethod
    def _normalize_access_token_fingerprints(
        cls,
        value: object,
        access_token: str,
    ) -> list[str]:
        candidates = value if isinstance(value, list) else []
        fingerprints = [
            str(item or "").strip().lower()
            for item in candidates
            if len(str(item or "").strip()) == 64
            and all(char in "0123456789abcdef" for char in str(item or "").strip().lower())
        ]
        current = cls._access_token_fingerprint(access_token)
        fingerprints = list(dict.fromkeys(fingerprints))
        first = fingerprints[0] if fingerprints else current
        recent = [fingerprint for fingerprint in fingerprints[1:] if fingerprint != current]
        if current != first:
            recent.append(current)
        return [first, *recent[-(cls._ACCESS_TOKEN_FINGERPRINT_LIMIT - 1):]]

    @classmethod
    def _normalize_management_id(cls, value: object, access_token: str) -> str:
        candidate = str(value or "").strip().lower()
        suffix = candidate[5:] if candidate.startswith("acct_") else ""
        if len(suffix) == 24 and all(char in "0123456789abcdef" for char in suffix):
            return candidate
        return cls._management_id_for_token(access_token)

    @classmethod
    def _unique_management_id(
        cls,
        access_token: str,
        preferred: object,
        used_ids: set[str],
    ) -> str:
        candidate = cls._normalize_management_id(preferred, access_token)
        if candidate not in used_ids:
            return candidate
        counter = 1
        while True:
            digest = hashlib.sha256(
                f"{access_token}\0{counter}".encode("utf-8")
            ).hexdigest()[:24]
            candidate = f"acct_{digest}"
            if candidate not in used_ids:
                return candidate
            counter += 1

    @staticmethod
    def _parse_time(value: object) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            try:
                parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            except Exception:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _timestamp_to_iso(value: object) -> str:
        try:
            ts = int(value)
        except (TypeError, ValueError):
            return ""
        tz = timezone(timedelta(hours=8))
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz).isoformat()

    @staticmethod
    def _account_mutation(
        baseline: dict[str, dict],
        desired: dict[str, dict],
        revision: str,
    ) -> StorageMutation:
        return StorageMutation(
            upserts=tuple(
                deepcopy(account)
                for token, account in desired.items()
                if token not in baseline or baseline[token] != account
            ),
            delete_keys=tuple(token for token in baseline if token not in desired),
            expected_revision=revision,
        )

    @staticmethod
    def _integer_field(value: object) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _merge_additive_account_value(
        cls,
        field: str,
        baseline: dict,
        local: dict,
        remote: dict,
    ) -> int | None:
        if field not in baseline or field not in local or field not in remote:
            return None
        baseline_value = cls._integer_field(baseline[field])
        local_value = cls._integer_field(local[field])
        remote_value = cls._integer_field(remote[field])
        if baseline_value is None or local_value is None or remote_value is None:
            return None
        return max(0, remote_value + (local_value - baseline_value))

    @classmethod
    def _is_local_quota_consumption(cls, baseline: dict, local: dict) -> bool:
        # quota is also refreshed as an absolute upstream snapshot. A changed
        # last_used_at distinguishes an image-result decrement from that refresh.
        baseline_quota = cls._integer_field(baseline.get("quota"))
        local_quota = cls._integer_field(local.get("quota"))
        return bool(
            baseline_quota is not None
            and local_quota is not None
            and local_quota < baseline_quota
            and local.get("last_used_at") != baseline.get("last_used_at")
        )

    @classmethod
    def _merge_account_fields(
        cls,
        baseline: dict,
        local: dict,
        remote: dict,
    ) -> dict:
        merged = deepcopy(remote)
        for field in baseline.keys() | local.keys():
            baseline_has = field in baseline
            local_has = field in local
            if baseline_has == local_has and (
                not local_has or baseline[field] == local[field]
            ):
                continue
            if local_has:
                if field in cls._ADDITIVE_ACCOUNT_COUNTER_FIELDS:
                    additive_value = cls._merge_additive_account_value(
                        field,
                        baseline,
                        local,
                        remote,
                    )
                    if additive_value is not None:
                        merged[field] = additive_value
                        continue
                if field == "quota" and cls._is_local_quota_consumption(
                    baseline,
                    local,
                ):
                    additive_value = cls._merge_additive_account_value(
                        field,
                        baseline,
                        local,
                        remote,
                    )
                    if additive_value is not None:
                        merged[field] = additive_value
                        continue
                merged[field] = deepcopy(local[field])
            else:
                merged.pop(field, None)
        return merged

    @staticmethod
    def _rotated_account_token(
        accounts: dict[str, dict],
        baseline: dict[str, dict],
        old_token: str,
        old_account: dict,
    ) -> str | None:
        management_id = str(old_account.get("management_id") or "").strip()
        if not management_id:
            return None
        return next(
            (
                token
                for token, account in accounts.items()
                if token != old_token
                and token not in baseline
                and str(account.get("management_id") or "").strip() == management_id
            ),
            None,
        )

    @classmethod
    def _merge_accounts_after_conflict(
        cls,
        baseline: dict[str, dict],
        local: dict[str, dict],
        remote: dict[str, dict],
    ) -> dict[str, dict]:
        merged = deepcopy(remote)
        handled_local_tokens: set[str] = set()

        for token, baseline_account in baseline.items():
            if token in local:
                continue
            local_rotation = cls._rotated_account_token(
                local,
                baseline,
                token,
                baseline_account,
            )
            remote_rotation = cls._rotated_account_token(
                remote,
                baseline,
                token,
                baseline_account,
            )
            if local_rotation is None:
                # A local deletion is an explicit command. It also deletes a
                # concurrently rotated form of the same logical account.
                merged.pop(token, None)
                if remote_rotation is not None:
                    merged.pop(remote_rotation, None)
                continue

            handled_local_tokens.add(local_rotation)
            if token not in remote:
                # A remote deletion beats a stale token refresh. When both
                # processes rotated the account, retain the remote winner.
                continue
            merged.pop(token, None)
            merged[local_rotation] = cls._merge_account_fields(
                baseline_account,
                local[local_rotation],
                remote[token],
            )

        for token, local_account in local.items():
            if token in handled_local_tokens:
                continue
            baseline_account = baseline.get(token)
            if baseline_account is None:
                remote_account = remote.get(token)
                merged[token] = (
                    {**deepcopy(remote_account), **deepcopy(local_account)}
                    if remote_account is not None
                    else deepcopy(local_account)
                )
                continue
            if local_account == baseline_account:
                continue
            remote_account = remote.get(token)
            if remote_account is None:
                remote_rotation = cls._rotated_account_token(
                    remote,
                    baseline,
                    token,
                    baseline_account,
                )
                if remote_rotation is not None:
                    merged[remote_rotation] = cls._merge_account_fields(
                        baseline_account,
                        local_account,
                        remote[remote_rotation],
                    )
                    merged.pop(token, None)
                    continue
                # A remote deletion beats a stale background update. Keeping it
                # deleted also removes it from this process's in-memory view.
                merged.pop(token, None)
                continue
            merged[token] = cls._merge_account_fields(
                baseline_account,
                local_account,
                remote_account,
            )
        return merged

    @staticmethod
    def _is_account_selectable(
        account: dict,
        *,
        allow_limited: bool,
        allow_image_pending: bool = False,
    ) -> bool:
        if not isinstance(account, dict):
            return False
        allowed_statuses = {"正常", "限流"} if allow_limited else {"正常"}
        if account.get("status") not in allowed_statuses:
            return False
        credential_availability = project_upstream_credential_availability(
            str(account.get("access_token") or ""),
            str(account.get("refresh_token") or ""),
            access_confirmed_invalid=(
                str(account.get("last_remote_check_result") or "").strip().lower() == "invalid"
            ),
            refresh_confirmed_invalid=bool(account.get("refresh_token_invalid_at")),
        )
        if credential_availability.status == "unavailable":
            return False
        if account.get("last_remote_check_result") != "pending":
            return True
        return bool(
            allow_image_pending
            and account.get("pending_auth_scope") == "image"
        )

    @classmethod
    def _is_image_account_available(cls, account: dict) -> bool:
        if not cls._is_account_selectable(account, allow_limited=False):
            return False
        if bool(account.get("image_quota_unknown")):
            return True
        # quota 是展示/预估值，不能作为持久调度开关。
        # 只有远程确认后写入的“限流”状态才代表图片额度耗尽；否则 quota=0 也要允许进入预检，
        # 避免本地扣减或额度重置不同步时把账号锁死在候选池外。
        return account.get("status") == "正常" or int(account.get("quota") or 0) > 0

    @classmethod
    def _is_unlimited_image_quota_account(cls, account: dict) -> bool:
        if not isinstance(account, dict) or not bool(account.get("image_quota_unknown")):
            return False
        account_type = (cls._normalize_account_type(account.get("type")) or "").lower()
        return account_type in {"pro", "prolite"}

    @classmethod
    def _account_matches_plan_type(cls, account: dict, plan_type: str | None = None) -> bool:
        if not plan_type:
            return True
        normalized_plan = cls._normalize_account_type(plan_type)
        normalized_account = cls._normalize_account_type(account.get("type"))
        if not normalized_plan or not normalized_account:
            return False
        return normalized_plan.lower() == normalized_account.lower()

    @classmethod
    def _account_matches_source_type(cls, account: dict, source_type: str | None = None) -> bool:
        if not source_type:
            return True
        return cls._normalize_source_type(account.get("source_type")) == cls._normalize_source_type(source_type)

    @classmethod
    def _account_matches_any_plan_type(cls, account: dict, plan_types: set[str] | tuple[str, ...] | None = None) -> bool:
        if not plan_types:
            return True
        normalized_account = cls._normalize_account_type(account.get("type"))
        normalized_plans = {
            normalized
            for plan_type in plan_types
            if (normalized := cls._normalize_account_type(plan_type))
        }
        return bool(normalized_account and normalized_account in normalized_plans)

    @staticmethod
    def _normalize_source_type(value: object) -> str:
        raw = str(value or "").strip().lower()
        if raw in {"codex", "cpa", "cpa_json", "remote_cpa", "sub2api"}:
            return "codex"
        return "web"

    @staticmethod
    def _normalize_account_type(value: object) -> str | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        key = raw.lower().replace("-", "_").replace(" ", "_")
        compact = key.replace("_", "")
        aliases = {
            "free": "free",
            "plus": "Plus",
            "pro": "Pro",
            "prolite": "ProLite",
            "team": "Team",
            "business": "Team",
            "enterprise": "Enterprise",
        }
        return aliases.get(compact) or aliases.get(key)

    @staticmethod
    def _has_value(value: object) -> bool:
        return value is not None and str(value).strip() != ""

    @staticmethod
    def _bool_value(value: object, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        raw = str(value or "").strip().lower()
        if raw in {"1", "true", "yes", "y", "on"}:
            return True
        if raw in {"0", "false", "no", "n", "off", "none", "null", ""}:
            return False
        return default

    @classmethod
    def _normalize_account_status(cls, value: object, account: dict) -> str:
        if cls._bool_value(account.get("auto_disabled"), False):
            return "禁用"
        if account.get("enabled") is not None and not cls._bool_value(account.get("enabled"), True):
            return "禁用"
        raw = str(value or "").strip()
        if not raw:
            return "正常"
        aliases = {
            "正常": "正常",
            "normal": "正常",
            "ready": "正常",
            "限流": "限流",
            "limited": "限流",
            "rate_limited": "限流",
            "cooling": "限流",
            "backoff": "限流",
            "异常": "异常",
            "abnormal": "异常",
            "invalid": "异常",
            "error": "异常",
            "incomplete": "异常",
            "禁用": "禁用",
            "disabled": "禁用",
            "auto_disabled": "禁用",
        }
        return aliases.get(raw.lower(), aliases.get(raw, "正常"))

    @classmethod
    def _quota_value(cls, value: object, default: int = 0) -> int:
        if not cls._has_value(value):
            return max(0, int(default or 0))
        try:
            return max(0, int(float(str(value).strip())))
        except (TypeError, ValueError):
            return max(0, int(default or 0))

    @classmethod
    def _extract_image_quota_from_limits(cls, limits_progress: object) -> tuple[int | None, str | None, bool | None]:
        if not isinstance(limits_progress, list):
            return None, None, None
        if not limits_progress:
            return None, None, None

        for item in limits_progress:
            if not isinstance(item, dict):
                continue
            feature = str(
                item.get("feature_name")
                or item.get("feature")
                or item.get("name")
                or item.get("type")
                or ""
            ).strip().lower()
            if feature in {"image_gen", "image_generation", "image", "images"}:
                restore_at = str(
                    item.get("reset_after")
                    or item.get("restore_at")
                    or item.get("reset_at")
                    or ""
                ).strip() or None
                return cls._quota_value(item.get("remaining"), 0), restore_at, False

        return None, None, True

    @staticmethod
    def _jwt_exp(access_token: str) -> int:
        try:
            return int(AccountPrimitivesMixin._decode_jwt_payload(access_token).get("exp") or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _token_expires_in(cls, access_token: str) -> int | None:
        return access_token_expires_in_seconds(access_token)

    @classmethod
    def _token_needs_refresh(cls, access_token: str, *, force: bool = False) -> bool:
        if force:
            return True
        remaining = cls._token_expires_in(access_token)
        return remaining is not None and remaining <= cls._ACCESS_TOKEN_REFRESH_SKEW_SECONDS

    @classmethod
    def _token_issued_at(cls, access_token: str) -> datetime | None:
        issued_at = access_token_issued_at(access_token)
        if issued_at is None:
            return None
        return datetime.fromtimestamp(issued_at, tz=timezone.utc)

    @staticmethod
    def _safe_response_text(response: object, limit: int = 300) -> str:
        try:
            return str(getattr(response, "text", "") or "")[:limit]
        except Exception:
            return ""

    @staticmethod
    def _oauth_refresh_error_fields(data: object) -> tuple[str, str]:
        if not isinstance(data, dict):
            return "", ""
        error = data.get("error")
        nested = error if isinstance(error, dict) else {}
        code = str(
            nested.get("code")
            or data.get("code")
            or (error if isinstance(error, str) else "")
            or ""
        ).strip()
        description = str(
            data.get("error_description")
            or nested.get("message")
            or data.get("message")
            or nested.get("description")
            or ""
        ).strip()
        return code, description

    @classmethod
    def _is_terminal_refresh_error(cls, status_code: int, error_code: str, description: str) -> bool:
        if status_code in {408, 429} or status_code >= 500:
            return False
        normalized_code = str(error_code or "").strip().casefold()
        normalized_description = str(description or "").strip().casefold()
        if normalized_code in cls._TERMINAL_REFRESH_ERROR_CODES:
            return True
        return 400 <= status_code < 500 and any(
            fragment in normalized_description
            for fragment in cls._TERMINAL_REFRESH_MESSAGE_FRAGMENTS
        )

    @staticmethod
    def _credential_generation(
        access_token: str,
        account: dict | None,
    ) -> _CredentialGeneration:
        item = account or {}
        return (
            str(item.get("access_token") or access_token or "").strip(),
            str(item.get("refresh_token") or "").strip(),
            str(item.get("last_token_refresh_at") or "").strip(),
        )

    @staticmethod
    def _credential_error_text(
        error: object,
        account: dict | None,
        *,
        access_token: str = "",
    ) -> str:
        account = account or {}
        return sanitize_diagnostic_text(
            error,
            sensitive_values=(
                access_token,
                account.get("access_token"),
                account.get("refresh_token"),
                account.get("id_token"),
            ),
            proxy_values=(account.get("proxy"),),
            limit=2000,
        )
