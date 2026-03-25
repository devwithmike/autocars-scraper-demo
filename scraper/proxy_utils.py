"""
proxy_utils.py
--------------
Simulates a proxy pool helper.  Drop-in compatible: swap `ProxyManager` for a
real implementation without touching the scraper.

In production this would rotate through a list of authenticated proxies
(e.g. Brightdata, Oxylabs, Smartproxy).
"""

import random
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


_PROXY_POOL: list[str] = [
    "http://proxy-sim-1:8080",  # placeholder — not contacted
    "http://proxy-sim-2:8080",
    "http://proxy-sim-3:8080",
    "http://proxy-sim-4:8080",
    "http://proxy-sim-5:8080",
]

_FAILURE_THRESHOLD = 3   # mark proxy bad after N consecutive failures
_COOLDOWN_SECONDS  = 60  # seconds before retrying a bad proxy


class _ProxyState:
    """Track health of a single proxy endpoint."""
    def __init__(self, url: str):
        self.url            = url
        self.failures       = 0
        self.last_used      = 0.0
        self.marked_bad_at  = 0.0
        self.is_bad         = False

    def record_failure(self):
        self.failures += 1
        if self.failures >= _FAILURE_THRESHOLD:
            self.is_bad         = True
            self.marked_bad_at  = time.time()
            logger.warning("[proxy_utils] Proxy marked bad: %s", self.url)

    def record_success(self):
        self.failures = 0
        self.is_bad   = False

    def is_available(self) -> bool:
        if not self.is_bad:
            return True
        # Allow retry after cooldown
        if time.time() - self.marked_bad_at > _COOLDOWN_SECONDS:
            self.is_bad = False
            self.failures = 0
            logger.info("[proxy_utils] Proxy back in rotation: %s", self.url)
            return True
        return False


class ProxyManager:
    """
    Round-robin proxy manager with health tracking.

    Usage
    -----
        pm = ProxyManager()
        proxy_dict = pm.get()          # {"http": "...", "https": "..."}
        pm.report_success(proxy_dict)
        pm.report_failure(proxy_dict)
    """

    def __init__(self, pool: Optional[list[str]] = None):
        pool = pool or _PROXY_POOL
        self._states = [_ProxyState(url) for url in pool]
        self._index  = 0
        logger.info("[proxy_utils] ProxyManager initialised with %d proxies.", len(self._states))

    # ------------------------------------------------------------------
    def get(self) -> dict:
        """
        Return the next available proxy as a requests-compatible dict.
        Falls back to direct connection if all proxies are unhealthy.
        """
        available = [s for s in self._states if s.is_available()]
        if not available:
            logger.warning("[proxy_utils] All proxies unhealthy — using direct connection.")
            return {}

        state = random.choice(available)
        state.last_used = time.time()
        logger.debug("[proxy_utils] Using proxy: %s", state.url)
        return {"http": state.url, "https": state.url}

    def report_success(self, proxy_dict: dict):
        url = proxy_dict.get("http") or proxy_dict.get("https")
        if url:
            for s in self._states:
                if s.url == url:
                    s.record_success()
                    break

    def report_failure(self, proxy_dict: dict):
        url = proxy_dict.get("http") or proxy_dict.get("https")
        if url:
            for s in self._states:
                if s.url == url:
                    s.record_failure()
                    break

    # ------------------------------------------------------------------
    @property
    def healthy_count(self) -> int:
        return sum(1 for s in self._states if s.is_available())

    def status(self) -> list[dict]:
        return [
            {"url": s.url, "healthy": s.is_available(), "failures": s.failures}
            for s in self._states
        ]