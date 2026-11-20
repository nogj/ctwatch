"""Lambda entry point; also scheduled to recover pending rows after stream retention."""

import json
import logging
import os
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer

LOGGER = logging.getLogger(__name__)


def json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(type(value).__name__)


def publish(item, table, sqs):
    if item.get("kind") != "EVENT" or item.get("status") != "PENDING":
        return
    # Sending first can duplicate delivery after a crash; marking first could lose the event.
    sqs.send_message(
        QueueUrl=os.environ["QUEUE_URL"],
        MessageBody=json.dumps(
            item["event"], default=json_default, ensure_ascii=True, separators=(",", ":")
        ),
    )
    table.update_item(
        Key={"pk": item["pk"]},
        UpdateExpression="SET #status=:sent, expires_at=:expiry REMOVE pending",
        ConditionExpression="#status=:pending",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":sent": "SENT",
            ":pending": "PENDING",
            ":expiry": int(time.time()) + 90 * 86400,
        },
    )


def handler(event, context):
    table = boto3.resource("dynamodb").Table(os.environ["STATE_TABLE"])
    sqs = boto3.client("sqs")
    if "Records" in event:
        failures = []
        deserializer = TypeDeserializer()
        for record in event["Records"]:
            try:
                item = {
                    key: deserializer.deserialize(value)
                    for key, value in record.get("dynamodb", {}).get("NewImage", {}).items()
                }
                publish(item, table, sqs)
            except Exception:
                LOGGER.exception("outbox publication failed")
                failures.append({"itemIdentifier": record["dynamodb"]["SequenceNumber"]})
        return {"batchItemFailures": failures}
    # Pending rows have no TTL; an extended outage cannot expire unpublished matches.
    key = None
    while context.get_remaining_time_in_millis() > 10_000:
        kwargs = {
            "IndexName": "PendingIndex",
            "KeyConditionExpression": Key("pending").eq("PENDING"),
            "Limit": 100,
        }
        if key:
            kwargs["ExclusiveStartKey"] = key
        response = table.query(**kwargs)
        for item in response["Items"]:
            if context.get_remaining_time_in_millis() < 5000:
                return
            try:
                publish(item, table, sqs)
            except Exception:
                LOGGER.exception("pending outbox publication failed")
        key = response.get("LastEvaluatedKey")
        if not key:
            return
