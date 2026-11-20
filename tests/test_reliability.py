import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from ctwatch.cache import TTLCache
from ctwatch.ct import Entry, Log
from ctwatch.engine import Engine
from ctwatch.matcher import init_worker
from ctwatch.store import Store, make_event
from infra.outbox import publish


def test_cache_expiration_eviction_and_negative_result():
    now = [0]
    cache = TTLCache(2, 10, clock=lambda: now[0])
    cache.put("a", [])
    assert cache.get("a") == []
    cache.put("b", [1])
    cache.put("c", [2])
    assert cache.get("a") is None
    now[0] = 11
    assert cache.get("c") is None


def test_duplicate_events_across_logs_use_same_id():
    entry = Entry(1, 1, b"cert", "certificate")
    match = {
        "watched_domain": "example.com",
        "observed_domain_ascii": "example.com",
        "match_type": "exact",
        "wildcard": False,
    }
    a = make_event(entry, Log("a", "a", "https://a/", b"", "static", "usable"), match, "v1")
    b = make_event(
        Entry(22, 2, b"cert", "certificate"),
        Log("b", "b", "https://b/", b"", "static", "usable"),
        match,
        "v1",
    )
    assert a["event_id"] == b["event_id"]
    assert a["log_id"] != b["log_id"]
    assert (
        make_event(entry, Log("a", "a", "https://a/", b"", "static", "usable"), match, "v2")[
            "event_id"
        ]
        != a["event_id"]
    )


def test_persistent_event_failure_propagates_before_cursor_commit():
    store = Store.__new__(Store)
    store.table = Mock()
    store.table.put_item.side_effect = RuntimeError("DynamoDB unavailable")
    with pytest.raises(RuntimeError):
        store.save_events([{"event_id": "1"}])
    store.table.update_item.assert_not_called()


def test_duplicate_conditional_put_is_safe_and_pending_has_no_ttl():
    store = Store.__new__(Store)
    store.table = Mock()
    store.table.put_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"
    )
    store.save_events([{"event_id": "1"}])
    assert "expires_at" not in store.table.put_item.call_args.kwargs["Item"]


def test_outbox_send_failure_does_not_mark_sent(monkeypatch):
    monkeypatch.setenv("QUEUE_URL", "https://sqs.example/queue")
    table, sqs = Mock(), Mock()
    sqs.send_message.side_effect = RuntimeError("SQS unavailable")
    with pytest.raises(RuntimeError):
        publish({"kind": "EVENT", "status": "PENDING", "pk": "EVENT#1", "event": {}}, table, sqs)
    table.update_item.assert_not_called()


def test_outbox_crash_after_send_keeps_stable_event_for_retry(monkeypatch):
    monkeypatch.setenv("QUEUE_URL", "https://sqs.example/queue")
    table, sqs = Mock(), Mock()
    table.update_item.side_effect = [RuntimeError("crash"), {}]
    item = {"kind": "EVENT", "status": "PENDING", "pk": "EVENT#1", "event": {"event_id": "1"}}
    with pytest.raises(RuntimeError):
        publish(item, table, sqs)
    publish(item, table, sqs)
    assert sqs.send_message.call_args_list[0] == sqs.send_message.call_args_list[1]


def test_cursor_commit_is_fenced_against_stale_owner():
    store = Store.__new__(Store)
    store.table, store.owner, store.lease_seconds = Mock(), "owner", 120
    store.commit("log", 10, 20, 30, "root")
    args = store.table.update_item.call_args.kwargs
    assert "lease_owner=:owner" in args["ConditionExpression"]
    assert "next_index=:expected" in args["ConditionExpression"]
    assert args["ExpressionAttributeValues"][":expected"] == 10


def test_inflight_dedup_shares_analysis_and_cache(document, certificate):
    # Use a real executor and parser, without spawning subprocesses in this unit test.
    init_worker(document, "data/confusables.txt")
    engine = Engine.__new__(Engine)
    engine.cache, engine.inflight = TTLCache(10, 60), {}
    engine.pool, engine.slots = ThreadPoolExecutor(max_workers=1), asyncio.Semaphore(2)
    engine.hits = engine.misses = engine.active = 0
    engine.retiring = False
    entry = Entry(1, 1, certificate, "certificate")

    async def exercise():
        a, b = await asyncio.gather(engine.analyze([entry]), engine.analyze([entry]))
        assert a == b
        assert engine.misses == 1
        assert engine.hits == 1
        assert await engine.analyze([entry]) == a
        assert engine.misses == 1

    try:
        asyncio.run(exercise())
    finally:
        engine.close()
