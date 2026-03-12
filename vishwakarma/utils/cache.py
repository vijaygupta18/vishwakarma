import threading
import time
from typing import Any


class TTLCache:
    """
    Simple thread-safe TTL cache.
    Used for caching toolset connectivity status so we don't
    re-ping Prometheus/Elasticsearch on every request.
    """

    def __init__(self, ttl_seconds: int = 300):
        self._cache: dict[str, tuple[Any, float]] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.time() > expires_at:
                del self._cache[key]
                return None
            return value

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        ttl = ttl or self._ttl
        with self._lock:
            self._cache[key] = (value, time.time() + ttl)

    def delete(self, key: str) -> None:
        with self._lock:
            self._cache.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None


# Global toolset status cache (5 min TTL by default)
toolset_status_cache = TTLCache(ttl_seconds=300)
