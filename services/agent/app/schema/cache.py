"""有界 TTL 缓存：按 (数据源, 授权配置指纹) 隔离，单调时钟计算过期。

键空间由“单数据源 + 固定授权范围”决定，最多一条有效条目；
时钟可注入（测试用假时钟），生产默认 time.monotonic。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class _Entry:
    value: Any
    expires_at: float  # 单调时钟的过期时刻


class TTLCache:
    """简单 TTL 缓存；不跨进程，重启清空。"""

    def __init__(self, ttl_seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[Any, _Entry] = {}

    def get(self, key: Any) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() >= entry.expires_at:
            self._entries.pop(key, None)
            return None
        return entry.value

    def set(self, key: Any, value: Any) -> None:
        self._entries[key] = _Entry(value=value, expires_at=self._clock() + self._ttl)

    def invalidate(self, key: Any | None = None) -> None:
        if key is None:
            self._entries.clear()
        else:
            self._entries.pop(key, None)
