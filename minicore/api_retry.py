"""迷你版 API 重试 + 指数退避。

对应原版 minicode/api_retry.py 的核心概念:
- 可恢复错误(超时/限流/5xx) → 重试,间隔递增
- 不可恢复错误(认证失败/参数错误) → 直接抛,不重试
"""
from __future__ import annotations

import time
from typing import Any, Callable


# 可恢复的错误标记(错误信息里含这些词 → 值得重试)
_RETRYABLE_MARKERS = (
    "429", "timeout", "timed out", "temporarily", "overloaded",
    "rate limit", "capacity", "unavailable", "500", "502", "503", "504",
    "connection", "reset", "internal error",
)

# 不可恢复的错误标记(含这些词 → 直接失败,不重试)
_NON_RETRYABLE_MARKERS = (
    "401", "403", "invalid api key", "unauthorized", "forbidden",
    "bad request", "invalid_request", "400", "authentication",
)


def _is_retryable(error: Exception) -> bool:
    """判断这个错误是否值得重试。"""
    msg = str(error).lower()
    if any(m in msg for m in _NON_RETRYABLE_MARKERS):
        return False
    return any(m in msg for m in _RETRYABLE_MARKERS)


def with_retry(
    func: Callable[[], Any],
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> Any:
    """带指数退避地调用 func。失败时自动重试,间隔递增。

    返回 func 的返回值;重试耗尽后抛出最后一次错误。
    """
    delay = base_delay
    for attempt in range(max_retries + 1):  # 首次 + max_retries 次重试
        try:
            return func()
        except Exception as e:
            if attempt >= max_retries or not _is_retryable(e):
                raise  # 重试耗尽或不可恢复 → 抛
            print(f"[重试] {type(e).__name__}: {str(e)[:60]} → 等 {delay:.1f}s 后重试 ({attempt+1}/{max_retries})")
            time.sleep(delay)
            delay *= 2  # 指数退避
    raise RuntimeError("unreachable")