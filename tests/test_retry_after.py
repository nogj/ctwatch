import asyncio
import datetime
from email.utils import format_datetime

import httpx
import pytest

from ctwatch.source import Sources, retry_after_seconds


@pytest.mark.parametrize(
    "value,expected",
    [
        ("120", 120),
        (" 15 ", 15),
        ("0", 0),
        (None, None),
        ("", None),
        ("garbage", None),
        ("-1", None),
        ("1.5", None),
        ("NaN", None),
        ("9" * 10000, None),
        ("١٥", None),
    ],
)
def test_delay_seconds_and_invalid_headers(value, expected):
    assert retry_after_seconds(value, 0) == expected


def test_http_date_and_date_in_past():
    date = datetime.datetime(2026, 11, 19, 14, 0, tzinfo=datetime.UTC)
    assert retry_after_seconds(format_datetime(date, usegmt=True), date.timestamp() - 90) == 90
    assert retry_after_seconds(format_datetime(date, usegmt=True), date.timestamp() + 90) == 0


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("as_date", [False, True])
def test_header_delays_next_request_to_same_server(status, as_date):
    async def exercise():
        source = Sources()
        await source.client.aclose()
        now, calls, waits = [1000.0], [], []
        source._clock = lambda: now[0]
        source._wall_clock = lambda: 1_000_000 + now[0]
        date = datetime.datetime.fromtimestamp(source._wall_clock() + 900, datetime.UTC)
        header = format_datetime(date, usegmt=True) if as_date else "900"

        async def sleep(delay):
            waits.append(delay)
            now[0] += delay

        source._sleep = sleep

        def respond(request):
            calls.append((request.url.path, now[0]))
            if len(calls) == 1:
                return httpx.Response(status, headers={"Retry-After": header})
            return httpx.Response(200)

        source.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await source.get("https://a.example/checkpoint")
            # Caller backoff may consume part of the requested cooldown.
            now[0] += 10
            await source.get("https://a.example/tile/data/000")
            assert waits == [890]
            assert calls[1][1] - calls[0][1] == 900  # not truncated to fallback's 300s cap
        finally:
            await source.close()

    asyncio.run(exercise())


def test_concurrent_requests_share_cooldown_without_blocking_other_hosts():
    async def exercise():
        source = Sources(concurrency=1)
        await source.client.aclose()
        now, calls = [0.0], []
        source._clock = lambda: now[0]
        sleeping, release = asyncio.Event(), asyncio.Event()

        async def sleep(delay):
            sleeping.set()
            await release.wait()
            now[0] += delay

        source._sleep = sleep

        def respond(request):
            calls.append((request.url.host, request.url.path, now[0]))
            if len(calls) == 1:
                return httpx.Response(429, headers={"Retry-After": "60"})
            return httpx.Response(200)

        source.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await source.get("https://a.example/checkpoint")
            first = asyncio.create_task(source.get("https://a.example/tile/data/000"))
            await asyncio.wait_for(sleeping.wait(), timeout=1)
            second = asyncio.create_task(source.get("https://a.example/tile/data/001"))
            await asyncio.wait_for(source.get("https://b.example/checkpoint"), timeout=1)
            assert len(calls) == 2
            assert calls[1][0] == "b.example"
            release.set()
            await asyncio.gather(first, second)
            assert all(when >= 60 for host, path, when in calls[2:])
        finally:
            await source.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("header", [None, "broken"])
def test_exponential_fallback_and_reset_on_success(header, monkeypatch):
    monkeypatch.setattr("ctwatch.source.random.uniform", lambda *_: 0)

    async def exercise():
        source = Sources()
        await source.client.aclose()
        now, waits = [0], []
        source._clock = lambda: now[0]
        statuses = iter([429, 429, 200, 429, 200])

        async def sleep(delay):
            waits.append(delay)
            now[0] += delay

        source._sleep = sleep
        source.client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    next(statuses), headers=({"Retry-After": header} if header is not None else {})
                )
            )
        )
        try:
            for status in [429, 429, 200, 429, 200]:
                if status == 429:
                    with pytest.raises(httpx.HTTPStatusError):
                        await source.get("https://a.example/checkpoint")
                else:
                    await source.get("https://a.example/checkpoint")
            assert waits == [2, 4, 2]
        finally:
            await source.close()

    asyncio.run(exercise())


def test_cooldown_is_cancellable():
    async def exercise():
        source = Sources()
        await source.client.aclose()
        sleeping = asyncio.Event()

        async def sleep(delay):
            sleeping.set()
            await asyncio.Event().wait()

        source._sleep = sleep
        source.client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(503, headers={"Retry-After": "86400"})
            )
        )
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await source.get("https://a.example/checkpoint")
            task = asyncio.create_task(source.get("https://a.example/checkpoint"))
            await asyncio.wait_for(sleeping.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await source.close()

    asyncio.run(exercise())
