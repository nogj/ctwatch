import hashlib
import json
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def conditional_failure(error: ClientError) -> bool:
    return error.response["Error"]["Code"] == "ConditionalCheckFailedException"


def make_event(entry, log, match: dict, version: str) -> dict:
    identity = {
        key: match[key]
        for key in ("watched_domain", "observed_domain_ascii", "match_type", "wildcard")
    }
    identity.update(certificate_sha256=entry.fingerprint, ruleset_version=version)
    event_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": 1,
        "event_id": event_id,
        **match,
        "certificate_sha256": entry.fingerprint,
        "artifact_type": entry.entry_type,
        "ruleset_version": version,
        "log_id": log.log_id,
        "log_url": log.url,
        "entry_index": entry.index,
        "submission_timestamp_ms": entry.timestamp_ms,
        "detected_at_ms": int(time.time() * 1000),
    }


class Store:
    def __init__(self, table_name: str, owner: str, lease_seconds: int = 120):
        self.table = boto3.resource(
            "dynamodb",
            config=Config(
                retries={"mode": "adaptive", "max_attempts": 8},
                connect_timeout=5,
                read_timeout=20,
            ),
        ).Table(table_name)
        self.owner = owner
        self.lease_seconds = lease_seconds

    def acquire(self, log_id: str) -> dict | None:
        now = int(time.time())
        try:
            return self.table.update_item(
                Key={"pk": "LOG#" + log_id},
                UpdateExpression="SET lease_owner=:owner, lease_until=:until, kind=:kind",
                ConditionExpression=(
                    "attribute_not_exists(lease_until) OR lease_until < :now OR lease_owner=:owner"
                ),
                ExpressionAttributeValues={
                    ":owner": self.owner,
                    ":until": now + self.lease_seconds,
                    ":now": now,
                    ":kind": "LOG",
                },
                ReturnValues="ALL_NEW",
            )["Attributes"]
        except ClientError as error:
            if conditional_failure(error):
                return None
            raise

    def initialize(self, log_id: str, position: int, root: str):
        self.table.update_item(
            Key={"pk": "LOG#" + log_id},
            UpdateExpression=(
                "SET next_index=:position, observed_tree_size=:position, tree_root=:root"
            ),
            ConditionExpression="lease_owner=:owner AND attribute_not_exists(next_index)",
            ExpressionAttributeValues={":owner": self.owner, ":position": position, ":root": root},
        )

    def save_events(self, events: list[dict]):
        # All events must be durable before the caller commits its cursor.
        for event in {event["event_id"]: event for event in events}.values():
            try:
                self.table.put_item(
                    Item={
                        "pk": "EVENT#" + event["event_id"],
                        "kind": "EVENT",
                        "pending": "PENDING",
                        "status": "PENDING",
                        "event": event,
                    },
                    ConditionExpression="attribute_not_exists(pk)",
                )
            except ClientError as error:
                if not conditional_failure(error):
                    raise

    def commit(
        self, log_id: str, expected: int, position: int, size: int, root: str, drained: bool = False
    ):
        now = int(time.time())
        self.table.update_item(
            Key={"pk": "LOG#" + log_id},
            UpdateExpression=(
                "SET next_index=:position, observed_tree_size=:size, "
                "tree_root=:root, last_success=:now, lease_until=:until, "
                "drained=:drained"
            ),
            ConditionExpression=(
                "lease_owner=:owner AND lease_until >= :now AND next_index=:expected"
            ),
            ExpressionAttributeValues={
                ":owner": self.owner,
                ":position": position,
                ":expected": expected,
                ":size": size,
                ":root": root,
                ":now": now,
                ":until": now + self.lease_seconds,
                ":drained": drained,
            },
        )
