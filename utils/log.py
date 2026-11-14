import json
import logging
from typing import Any, Callable

from utils.diagnostics import sanitize_log_value

_level_provider: Callable[[], Any] | None = None


def set_log_level_provider(provider: Callable[[], Any] | None) -> None:
    """注入日志级别配置来源（由 services 层在启动时注册），
    使 utils 不反向依赖 services.config。"""
    global _level_provider
    _level_provider = provider


class Logger:
    def __init__(self, name: str = "chatgpt2api") -> None:
        self._logger = logging.getLogger(name)
        if not self._logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
            self._logger.addHandler(handler)
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False

    def _enabled(self, level: str) -> bool:
        levels: set[str] = set()
        if _level_provider is not None:
            try:
                levels = {str(item).strip().lower() for item in (_level_provider() or [])}
            except Exception:
                levels = set()
        return level in (levels or {"info", "warning", "error"})

    def _sanitize(self, value: Any) -> Any:
        return sanitize_log_value(value)

    def _message(self, value: Any) -> str:
        sanitized = self._sanitize(value)
        if isinstance(sanitized, str):
            return sanitized
        return json.dumps(sanitized, ensure_ascii=False, default=str)

    def debug(self, message: Any) -> None:
        if self._enabled("debug"):
            formatted = self._message(message)
            self._logger.debug(formatted)

    def info(self, message: Any) -> None:
        if self._enabled("info"):
            formatted = self._message(message)
            self._logger.info(formatted)

    def warning(self, message: Any) -> None:
        if self._enabled("warning"):
            formatted = self._message(message)
            self._logger.warning(formatted)

    def error(self, message: Any) -> None:
        if self._enabled("error"):
            formatted = self._message(message)
            self._logger.error(formatted)


logger = Logger()
