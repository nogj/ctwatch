import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from ctwatch.ct import Entry, Log
from ctwatch.pipeline import process_range
from ctwatch.store import Store
from infra.outbox import handler


@mock_aws
def test_full_pipeline_recovery_cursor_lease_and_outbox(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    dynamo = boto3.client("dynamodb")
    dynamo.create_table(
        TableName="state",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "pending", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "PendingIndex",
                "KeySchema": [{"AttributeName": "pending", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    sqs = boto3.client("sqs")
    queue = sqs.create_queue(QueueName="matches")["QueueUrl"]
    monkeypatch.setenv("STATE_TABLE", "state")
    monkeypatch.setenv("QUEUE_URL", queue)
    store = Store("state", "first")
    assert "next_index" not in store.acquire("a")
    store.initialize("a", 10, "root")
    assert Store("state", "second").acquire("a") is None
    entry = Entry(10, 1234, b"certificate", "certificate")
    log = Log("a", "log", "https://a/", b"", "static", "usable")
    matches = [
        {
            "watched_domain": "example.com",
            "observed_domain_ascii": "example.com",
            "observed_domain_unicode": "example.com",
            "wildcard": False,
            "match_type": "exact",
        }
    ]
    engine = Mock(version="v1")
    engine.analyze = AsyncMock(return_value=[matches])
    original = store.commit
    store.commit = Mock(side_effect=RuntimeError("crash after events persisted"))
    with pytest.raises(RuntimeError):
        asyncio.run(process_range([entry], engine, store, log, 10, 11, "root"))
    assert store.table.get_item(Key={"pk": "LOG#a"})["Item"]["next_index"] == 10
    assert (
        store.table.query(
            IndexName="PendingIndex",
            KeyConditionExpression="pending=:p",
            ExpressionAttributeValues={":p": "PENDING"},
        )["Count"]
        == 1
    )
    # Replaying the same range reuses event identity, then safely advances the cursor.
    store.commit = original
    assert asyncio.run(process_range([entry], engine, store, log, 10, 11, "root")) == (11, 1)
    assert store.table.get_item(Key={"pk": "LOG#a"})["Item"]["next_index"] == 11
    assert (
        store.table.query(
            IndexName="PendingIndex",
            KeyConditionExpression="pending=:p",
            ExpressionAttributeValues={":p": "PENDING"},
        )["Count"]
        == 1
    )
    context = Mock()
    context.get_remaining_time_in_millis.return_value = 60000
    handler({}, context)  # Scheduled recovery path works without stream history.
    messages = sqs.receive_message(QueueUrl=queue)["Messages"]
    event = json.loads(messages[0]["Body"])
    assert event["entry_index"] == 10
    assert event["watched_domain"] == "example.com"
    assert (
        store.table.query(
            IndexName="PendingIndex",
            KeyConditionExpression="pending=:p",
            ExpressionAttributeValues={":p": "PENDING"},
        )["Count"]
        == 0
    )
    saved = store.table.get_item(Key={"pk": "EVENT#" + event["event_id"]})["Item"]
    assert saved["status"] == "SENT"
    assert isinstance(saved["expires_at"], Decimal)
    with pytest.raises(ClientError):
        original("a", 10, 12, 12, "root")  # stale cursor cannot commit over a newer range
