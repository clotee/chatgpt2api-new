"""进程内认证失败限流器。

单实例假设（见 docs/deployment.md）：多实例部署时各副本独立计数。
按 (client_ip, token 前 8 字符) 分桶；连续失败达到阈值后按指数退避
封锁该桶的后续尝试，成功认证立即清零。限流只影响失败路径——合法
凭据所在的桶不会被污染，因此正确密钥始终立即通过。
"""

from __future__ import annotations

import hashlib
from threading import Lock
from time import monotonic

_FAILURE_THRESHOLD = 5
_BASE_DELAY_SECONDS = 1.0
_MAX_DELAY_SECONDS = 60.0
_DECAY_SECONDS = 600.0
_MAX_BUCKETS = 8192


class AuthFailureThrottle:
    def __init__(self) -> None:
        self._lock = Lock()
        self._buckets: dict[str, dict[str, float]] = {}

    @staticmethod
    def _bucket_key(client_ip: str, token: str) -> str:
        return hashlib.sha256(
            f"{client_ip}|{token[:8]}".encode("utf-8")
        ).hexdigest()

    def retry_after(self, client_ip: str, token: str) -> float:
        """返回该桶剩余封锁秒数；0 表示允许尝试。"""
        now = monotonic()
        with self._lock:
            bucket = self._buckets.get(self._bucket_key(client_ip, token))
            if bucket is None:
                return 0.0
            blocked_until = float(bucket.get("blocked_until") or 0.0)
            return blocked_until - now if blocked_until > now else 0.0

    def record_failure(self, client_ip: str, token: str) -> None:
        now = monotonic()
        key = self._bucket_key(client_ip, token)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or now - float(bucket.get("first_failure") or 0.0) > _DECAY_SECONDS:
                bucket = {"count": 0.0, "first_failure": now, "blocked_until": 0.0}
                self._buckets[key] = bucket
                self._sweep_locked(now)
            bucket["count"] = float(bucket.get("count") or 0.0) + 1.0
            count = int(bucket["count"])
            if count >= _FAILURE_THRESHOLD:
                delay = min(
                    _MAX_DELAY_SECONDS,
                    _BASE_DELAY_SECONDS * (2 ** (count - _FAILURE_THRESHOLD)),
                )
                bucket["blocked_until"] = now + delay

    def record_success(self, client_ip: str, token: str) -> None:
        with self._lock:
            self._buckets.pop(self._bucket_key(client_ip, token), None)

    def _sweep_locked(self, now: float) -> None:
        if len(self._buckets) <= _MAX_BUCKETS:
            return
        for key in [
            key
            for key, bucket in self._buckets.items()
            if float(bucket.get("blocked_until") or 0.0) <= now
            and float(bucket.get("count") or 0.0) < _FAILURE_THRESHOLD
        ]:
            self._buckets.pop(key, None)
        overflow = len(self._buckets) - _MAX_BUCKETS
        if overflow > 0:
            oldest = sorted(
                self._buckets.items(),
                key=lambda item: float(item[1].get("first_failure") or 0.0),
            )[:overflow]
            for key, _ in oldest:
                self._buckets.pop(key, None)


auth_throttle = AuthFailureThrottle()
