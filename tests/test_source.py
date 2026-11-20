import asyncio
import base64

import httpx
import pytest

from ctwatch.ct import Log
from ctwatch.source import Sources


def vector(data, width):
    return len(data).to_bytes(width, "big") + data


def leaf(der):
    return (1234).to_bytes(8, "big") + b"\x00\x00" + vector(der, 3) + b"\x00\x00"


def test_static_partial_fallback_never_reads_beyond_checkpoint(certificate):
    tile = (leaf(certificate) + b"\x00\x00") * 256
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if ".p/" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, content=tile)

    async def exercise():
        source = Sources()
        await source.client.aclose()
        source.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            values = await source.entries(
                Log("a", "a", "https://a/", b"", "static", "usable"), 250, 253
            )
            assert [value.index for value in values] == [250, 251, 252]
            assert calls == ["/tile/data/000.p/253", "/tile/data/000"]
        finally:
            await source.close()

    asyncio.run(exercise())


def test_short_rfc_responses_advance_by_actual_count(certificate):
    item = {
        "leaf_input": base64.b64encode(b"\x00\x00" + leaf(certificate)).decode(),
        "extra_data": "AAAA",
    }
    starts = []

    def respond(request):
        start = int(request.url.params["start"])
        end = int(request.url.params["end"])
        starts.append(start)
        return httpx.Response(200, json={"entries": [item] * min(3, end - start + 1)})

    async def exercise():
        source = Sources()
        await source.client.aclose()
        source.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            values = await source.read_range(
                Log("a", "a", "https://a/", b"", "rfc6962", "usable"), 10, 20
            )
            assert [value.index for value in values] == list(range(10, 20))
            assert starts == [10, 13, 16, 19]
        finally:
            await source.close()

    asyncio.run(exercise())


def test_empty_rfc_response_cannot_commit():
    async def exercise():
        source = Sources()
        await source.client.aclose()
        source.client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"entries": []}))
        )
        try:
            with pytest.raises(ValueError, match="length"):
                await source.entries(Log("a", "a", "https://a/", b"", "rfc6962", "usable"), 0, 1)
        finally:
            await source.close()

    asyncio.run(exercise())
