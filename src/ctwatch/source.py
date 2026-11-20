import asyncio
import base64
import datetime
import logging
import math
import random
import time
from email.utils import parsedate_to_datetime

import httpx

from .ct import parse_rfc, parse_tile, static_head, tile_path, verify_sth

LOGGER = logging.getLogger("ctwatch")


def retry_after_seconds(value: str | None, now: float) -> float | None:
    """RFC 9110 section 10.2.3: delay-seconds or HTTP-date. None means invalid/absent."""
    if value is None:
        return None
    value = value.strip()
    try:
        if value.isascii() and value.isdigit():
            delay = float(value)
        else:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=datetime.UTC)
            delay = when.timestamp() - now
        return max(0.0, delay) if math.isfinite(delay) else None
    except (ValueError, TypeError, OverflowError):
        return None


class Sources:
    def __init__(self, concurrency=16):
        self.semaphore = asyncio.Semaphore(concurrency)
        self._origin_locks = {}
        self._cooldowns = {}
        self._throttles = {}
        self._clock = time.monotonic
        self._wall_clock = time.time
        self._sleep = asyncio.sleep
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(20),
            follow_redirects=False,
            headers={
                "User-Agent": "ctwatch/0.1 (+Certificate Transparency monitor)",
                "Accept-Encoding": "gzip, identity",
            },
            limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
        )

    async def get(self, url, params=None):
        parsed = httpx.URL(url)
        origin = (parsed.scheme, parsed.host, parsed.port)
        lock = self._origin_locks.setdefault(origin, asyncio.Lock())
        # Serialize requests to one origin so a queued request cannot bypass a new cooldown.
        # Sleep before reserving a global HTTP slot: other servers continue making progress.
        async with lock:
            while (delay := self._cooldowns.get(origin, 0) - self._clock()) > 0:
                await self._sleep(delay)
            async with self.semaphore:
                response = await self.client.get(url, params=params)
                if response.status_code in {429, 503}:
                    failures = self._throttles.get(origin, 0) + 1
                    self._throttles[origin] = failures
                    delay = retry_after_seconds(
                        response.headers.get("Retry-After"), self._wall_clock()
                    )
                    if delay is None:
                        delay = min(300, 2 ** min(failures, 9)) + random.uniform(0, 1)
                    self._cooldowns[origin] = max(
                        self._cooldowns.get(origin, 0), self._clock() + delay
                    )
                    LOGGER.warning(
                        "HTTP %s from %s; cooldown %.3fs", response.status_code, parsed.host, delay
                    )
                elif response.is_success:
                    self._throttles.pop(origin, None)
                    self._cooldowns.pop(origin, None)
                # Caller retains its range/cursor and retries; never acknowledges failed data.
                response.raise_for_status()
                return response

    async def head(self, log):
        if log.kind == "static":
            return static_head((await self.get(log.url + "checkpoint")).text, log)
        body = (await self.get(log.url + "ct/v1/get-sth")).json()
        verify_sth(
            log.key,
            body["timestamp"],
            body["tree_size"],
            base64.b64decode(body["sha256_root_hash"], validate=True),
            base64.b64decode(body["tree_head_signature"], validate=True),
        )
        return body["tree_size"], body["sha256_root_hash"]

    async def entries(self, log, start, size):
        if log.kind == "rfc6962":
            end = min(start + 255, size - 1)
            body = (
                await self.get(log.url + "ct/v1/get-entries", {"start": start, "end": end})
            ).json()
            values = body["entries"]
            if not values or len(values) > end - start + 1:
                raise ValueError("invalid get-entries response length")
            return [parse_rfc(start + i, value) for i, value in enumerate(values)]
        number, offset = divmod(start, 256)
        width = min(256, size - number * 256)
        path = "tile/data/" + tile_path(number)
        if width < 256:
            try:
                response = await self.get(log.url + path + f".p/{width}")
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 404:
                    raise
                # Partial tiles may disappear once the full tile is published.
                response = await self.get(log.url + path)
                width = 256
        else:
            response = await self.get(log.url + path)
        entries = parse_tile(response.content, number * 256)
        if len(entries) != width:
            raise ValueError("invalid data tile width")
        return entries[offset : min(width, size - number * 256)]

    async def read_range(self, log, start, size, limit=4096):
        """Aggregate tiles into cursor ranges; avoid a DynamoDB commit per tiny tile."""
        result = []
        position = start
        while position < size and len(result) < limit:
            entries = await self.entries(log, position, size)
            if not entries or entries[0].index != position:
                raise ValueError("empty or noncontiguous CT response")
            remaining = limit - len(result)
            result.extend(entries[:remaining])
            position = start + len(result)
        return result

    async def close(self):
        await self.client.aclose()
