"""Paced provider requests with bounded retries and server-directed cooldowns."""

import math
import random
import threading
import time
from datetime import timezone
from email.utils import parsedate_to_datetime

import requests


class ProviderRateLimit(requests.RequestException):
    def __init__(self, retry_after, response=None):
        super().__init__("Provider is cooling down after throttling.", response=response)
        self.retry_after = retry_after


_locks = {name: threading.Lock() for name in ("arXiv", "Semantic Scholar", "Europe PMC")}
_last_request = {}
_blocked_until = {}


def retry_after_seconds(value):
    """Accept both Retry-After delta seconds and HTTP dates."""
    if not isinstance(value, str):
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, seconds) if math.isfinite(seconds) else None


def provider_get(provider, url, interval, **kwargs):
    """One connection per provider, at most two attempts; never retry before Retry-After."""
    with _locks[provider]:
        remaining = _blocked_until.get(provider, 0) - time.monotonic()
        if remaining > 0:
            raise ProviderRateLimit(remaining)
        for attempt in range(2):
            wait = interval - (time.monotonic() - _last_request.get(provider, -float("inf")))
            if wait > 0:
                time.sleep(wait)
            try:
                response = requests.get(url, **kwargs)
            finally:
                _last_request[provider] = time.monotonic()
            if response.status_code not in (429, 502, 503, 504):
                response.raise_for_status()
                return response
            delay = retry_after_seconds(response.headers.get("Retry-After"))
            if attempt == 0 and (delay is None or delay <= 5):
                response.close()
                time.sleep(max(delay or 0, 2) + random.uniform(0, 0.25))
                continue
            if response.status_code == 429 or delay is not None:
                cooldown = max(delay if delay is not None else 60, interval)
                _blocked_until[provider] = time.monotonic() + cooldown
                response.close()
                raise ProviderRateLimit(cooldown, response=response)
            try:
                response.raise_for_status()
            finally:
                response.close()
