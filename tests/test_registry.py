import asyncio
import base64
import hashlib
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ctwatch.registry import Registry


def catalog(*items):
    logs = []
    for name, state in items:
        key = name.encode()
        logs.append(
            {
                "description": name,
                "key": base64.b64encode(key).decode(),
                "log_id": base64.b64encode(hashlib.sha256(key).digest()).decode(),
                "state": {state: {}},
                "url": f"https://{name}.example/",
            }
        )
    return {"operators": [{"logs": logs}]}


class Sources:
    def __init__(self, **documents):
        self.documents = documents

    async def get(self, url):
        value = self.documents[url]
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(json=lambda: value)


def test_union_uses_identity_and_active_state_from_either_program():
    sources = Sources(
        chrome=catalog(("shared", "usable"), ("other", "retired")),
        apple=catalog(("shared", "rejected"), ("other", "usable"), ("apple", "qualified")),
    )
    registry = Registry("chrome", "apple")
    logs = asyncio.run(registry.refresh(sources))
    assert {log.name: log.state for log in logs} == {
        "shared": "usable",
        "other": "usable",
        "apple": "qualified",
    }
    assert len({log.log_id for log in logs}) == 3


def test_equal_state_prefers_chrome_endpoint():
    chrome = catalog(("shared", "usable"))
    apple = deepcopy(chrome)
    apple["operators"][0]["logs"][0]["url"] = "https://alternate.example/"
    logs = asyncio.run(Registry("chrome", "apple").refresh(Sources(chrome=chrome, apple=apple)))
    assert logs[0].url == "https://shared.example/"


def test_failed_catalog_retains_snapshot_and_other_catalog_still_refreshes():
    sources = Sources(chrome=catalog(("chrome", "usable")), apple=catalog(("apple", "usable")))
    registry = Registry("chrome", "apple")
    asyncio.run(registry.refresh(sources))
    sources.documents["apple"] = ValueError("temporary outage")
    sources.documents["chrome"] = catalog(("new", "qualified"))
    logs = asyncio.run(registry.refresh(sources))
    assert {log.name for log in logs} == {"new", "apple"}
    assert registry.report["apple"]["stale"]
    assert registry.report["apple"]["retained_logs"] == 1
    assert not registry.report["chrome"]["stale"]


def test_cold_start_can_use_one_catalog_but_not_zero():
    sources = Sources(chrome=ValueError("offline"), apple=catalog(("apple", "usable")))
    registry = Registry("chrome", "apple")
    assert len(asyncio.run(registry.refresh(sources))) == 1
    with pytest.raises(ValueError, match="no CT catalogs available"):
        asyncio.run(Registry("chrome", "").refresh(sources))


def test_bad_identity_or_empty_catalog_does_not_replace_valid_snapshot():
    sources = Sources(apple=catalog(("apple", "usable")))
    registry = Registry("", "apple")
    original = asyncio.run(registry.refresh(sources))
    sources.documents["apple"]["operators"][0]["logs"][0]["key"] = "eA=="
    assert asyncio.run(registry.refresh(sources)) == original
    assert "identity" in registry.report["apple"]["error"]
    sources.documents["apple"] = catalog()
    assert asyncio.run(registry.refresh(sources)) == original
    assert "empty" in registry.report["apple"]["error"]
