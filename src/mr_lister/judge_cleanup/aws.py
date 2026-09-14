"""Read-only source adapter and separate-table CAS/audit persistence."""

from __future__ import annotations

import json
from base64 import urlsafe_b64encode
from datetime import datetime
from typing import Any

from mr_lister.control.dynamodb import DynamoDBSellerControlStore
from mr_lister.control.models import ControlJobRecord
from mr_lister.control.store import decode_owner_job_cursor, owner_job_sort_key
from mr_lister.judge_cleanup.models import (
    TERMINAL,
    CleanupConfig,
    CleanupRecord,
    PublicationEvidence,
)
from mr_lister.judge_cleanup.service import CleanupRunError, DiscoveryPage
from mr_lister.publication.execution_dynamodb import DynamoDBPublicationExecutionStore


def s(value: str) -> dict[str, str]:
    return {"S": value}


def due_key(at: datetime, job_id: str) -> str:
    seconds = int(at.timestamp())
    return f"{seconds * 1_000_000 + at.microsecond:020d}#{job_id}"


class DynamoCleanupSource:
    def __init__(self, *, client: Any, config: CleanupConfig) -> None:
        self.config = config
        self.client = client
        self.control = DynamoDBSellerControlStore(
            client=client, table_name=config.source_table_name
        )
        self.publication = DynamoDBPublicationExecutionStore(
            client=client, table_name=config.source_table_name
        )

    def discover(self, cursor: str | None, limit: int) -> DiscoveryPage:
        if type(limit) is not int or not 1 <= limit <= 25:
            raise ValueError("Cleanup discovery page is outside its bound")
        owner = self.config.judge_owner_id
        request = {
            "TableName": self.config.source_table_name,
            "IndexName": "OwnerJobsIndex",
            "KeyConditionExpression": "owner_jobs_pk = :owner",
            "ExpressionAttributeValues": {":owner": s(f"OWNER#{owner}")},
            "ScanIndexForward": False,
            "Limit": limit,
        }
        if cursor is not None:
            sort_key, job_id = decode_owner_job_cursor(cursor)
            request["ExclusiveStartKey"] = {
                "PK": s(f"JOB#{job_id}"),
                "SK": s("META"),
                "owner_jobs_pk": s(f"OWNER#{owner}"),
                "owner_jobs_sk": s(sort_key),
            }
        response = self.client.query(**request)
        items = response.get("Items", [])
        if not isinstance(items, list) or len(items) > limit:
            raise CleanupRunError("Cleanup discovery response exceeded its bound")
        jobs, invalid = [], 0
        for item in items:
            try:
                job = ControlJobRecord.model_validate_json(item["payload"]["S"])
                if (
                    job.owner_id != owner
                    or item.get("owner_id") != s(owner)
                    or item.get("entity_type") != s("CONTROL_JOB")
                    or item.get("PK") != s(f"JOB#{job.job_id}")
                    or item.get("SK") != s("META")
                    or item.get("owner_jobs_pk") != s(f"OWNER#{owner}")
                    or item.get("owner_jobs_sk") != s(owner_job_sort_key(job))
                ):
                    raise ValueError
                if job.publication_terminal_state == "published":
                    jobs.append(job.job_id)
            except Exception:
                invalid += 1
        next_cursor = None
        last = response.get("LastEvaluatedKey")
        if last:
            sort_key = last["owner_jobs_sk"]["S"]
            next_cursor = urlsafe_b64encode(sort_key.encode()).decode().rstrip("=")
            _, job_id = decode_owner_job_cursor(next_cursor)
            if (
                last
                != {
                    "PK": s(f"JOB#{job_id}"),
                    "SK": s("META"),
                    "owner_jobs_pk": s(f"OWNER#{owner}"),
                    "owner_jobs_sk": s(sort_key),
                }
                or next_cursor == cursor
            ):
                raise CleanupRunError("Cleanup discovery cursor changed owner or did not advance")
        return DiscoveryPage(tuple(jobs), next_cursor, invalid)

    def load(self, job_id: str) -> PublicationEvidence:
        job = self.control.get_job_for_owner(self.config.judge_owner_id, job_id)
        if job.publication_terminal_state != "published" or job.publication_aggregate_id is None:
            raise ValueError("Cleanup source is not confirmed published")
        authority = self.publication.load_execution_authority(
            self.config.judge_owner_id, job.publication_aggregate_id
        )
        # The existing strict execution graph validates all claim, evidence and settlement links.
        return PublicationEvidence(
            job=job,
            aggregate=authority.aggregate,
            snapshot=authority.snapshot,
            provider=authority.provider_authority,
            observation=authority.last_product_observation,
            result=authority.result,
        )


class DynamoCleanupStore:
    def __init__(self, *, client: Any, config: CleanupConfig) -> None:
        self.client, self.config = client, config
        self.table = config.cleanup_table_name

    def _key(self, job_id: str) -> dict[str, Any]:
        return {"PK": s(f"JOB#{job_id}"), "SK": s("STATE")}

    def _item(self, record: CleanupRecord) -> dict[str, Any]:
        item = {
            **self._key(record.evidence.job.job_id),
            "payload": s(record.model_dump_json()),
            "entity_type": s("JUDGE_CLEANUP"),
            "campaign_fingerprint": s(record.campaign_fingerprint),
        }
        if record.status not in TERMINAL:
            item.update(
                due_pk=s(f"CAMPAIGN#{record.campaign_id}"),
                due_sk=s(due_key(record.next_attempt_at, record.evidence.job.job_id)),
            )
        return item

    def _parse(self, item: dict[str, Any]) -> CleanupRecord:
        record = CleanupRecord.model_validate_json(item["payload"]["S"])
        if record.campaign_fingerprint != self.config.campaign_fingerprint or item != self._item(
            record
        ):
            raise CleanupRunError("Cleanup record binding is invalid")
        return record

    def get(self, job_id: str) -> CleanupRecord | None:
        item = self.client.get_item(
            TableName=self.table, Key=self._key(job_id), ConsistentRead=True
        ).get("Item")
        if item is None:
            return None
        record = self._parse(item)
        if record.evidence.job.job_id != job_id:
            raise CleanupRunError("Cleanup record identity changed")
        return record

    def register(self, record: CleanupRecord) -> bool:
        if record.status != "pending" or record.version != 0 or record.attempts != 0:
            raise ValueError("Cleanup registration requires pristine state")
        record.evidence.require_campaign(self.config)
        return self._commit(None, record)

    def compare_and_swap(self, old: CleanupRecord, new: CleanupRecord) -> bool:
        if (
            old.status in TERMINAL
            or new.updated_at < old.updated_at
            or new.attempts not in {old.attempts, old.attempts + 1}
            or (new.status == "leased") != (new.attempts == old.attempts + 1)
            or (
                old.status == "leased"
                and new.status == "leased"
                and new.updated_at < old.lease_until
            )
            or new.status == "pending"
            or new.version != old.version + 1
            or new.evidence != old.evidence
            or new.evidence_fingerprint != old.evidence_fingerprint
            or new.campaign_fingerprint != old.campaign_fingerprint
            or new.campaign_id != old.campaign_id
            or new.deadline != old.deadline
        ):
            raise ValueError("Cleanup CAS cannot change immutable authority")
        return self._commit(old, new)

    def _commit(self, old: CleanupRecord | None, new: CleanupRecord) -> bool:
        new = CleanupRecord.model_validate_json(new.model_dump_json())
        if new.campaign_fingerprint != self.config.campaign_fingerprint:
            raise ValueError("Cleanup write changed campaign")
        item = self._item(new)
        put: dict[str, Any] = {
            "TableName": self.table,
            "Item": item,
            "ConditionExpression": "attribute_not_exists(PK)",
        }
        if old is not None:
            put.update(
                ConditionExpression="payload = :old",
                ExpressionAttributeValues={":old": s(old.model_dump_json())},
            )
        audit = {
            "PK": item["PK"],
            "SK": s(f"AUDIT#{new.version:020d}"),
            "entity_type": s("JUDGE_CLEANUP_AUDIT"),
            "payload": s(
                json.dumps(
                    {
                        "campaign_id": new.campaign_id,
                        "campaign_fingerprint": new.campaign_fingerprint,
                        "evidence_fingerprint": new.evidence_fingerprint,
                        "version": new.version,
                        "attempts": new.attempts,
                        "status": new.status,
                        "outcome": new.last_outcome,
                        "at": new.updated_at.isoformat(),
                        "lease_id": new.lease_id,
                        "etsy_removal_verified": False,
                    },
                    sort_keys=True,
                )
            ),
        }
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {"Put": put},
                    {
                        "Put": {
                            "TableName": self.table,
                            "Item": audit,
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                ]
            )
        except Exception as error:
            response = getattr(error, "response", {})
            if response.get("Error", {}).get("Code") == "TransactionCanceledException" and any(
                r.get("Code") == "ConditionalCheckFailed"
                for r in response.get("CancellationReasons", [])
            ):
                return False
            raise CleanupRunError("Cleanup audit transaction was unavailable") from None
        return True

    def due(self, now: datetime, limit: int) -> tuple[CleanupRecord, ...]:
        response = self.client.query(
            TableName=self.table,
            IndexName="DueIndex",
            Limit=limit,
            KeyConditionExpression="due_pk = :campaign AND due_sk <= :now",
            ScanIndexForward=True,
            ExpressionAttributeValues={
                ":campaign": s(f"CAMPAIGN#{self.config.campaign_id}"),
                ":now": s(due_key(now, "~")),
            },
        )
        records = []
        for item in response.get("Items", []):
            indexed = self._parse(item)
            current = self.get(indexed.evidence.job.job_id)
            if (
                current is not None
                and current.status not in TERMINAL
                and current.next_attempt_at <= now
            ):
                records.append(current)
        return tuple(records)

    def _cursor_key(self) -> dict[str, Any]:
        return {"PK": s(f"CAMPAIGN#{self.config.campaign_id}"), "SK": s("DISCOVERY")}

    def cursor(self) -> str | None:
        item = self.client.get_item(
            TableName=self.table, Key=self._cursor_key(), ConsistentRead=True
        ).get("Item")
        if item is None:
            return None
        if item.get("campaign_fingerprint") != s(self.config.campaign_fingerprint):
            raise CleanupRunError("Cleanup discovery campaign changed")
        value = json.loads(item["payload"]["S"])
        if value is not None and (not isinstance(value, str) or len(value) > 4096):
            raise CleanupRunError("Cleanup discovery cursor is invalid")
        return value

    def advance_cursor(self, old: str | None, new: str | None) -> None:
        try:
            self.client.put_item(
                TableName=self.table,
                Item={
                    **self._cursor_key(),
                    "campaign_fingerprint": s(self.config.campaign_fingerprint),
                    "payload": s(json.dumps(new)),
                },
                ConditionExpression=(
                    "attribute_not_exists(PK) OR "
                    "(campaign_fingerprint = :campaign AND payload = :old)"
                ),
                ExpressionAttributeValues={
                    ":campaign": s(self.config.campaign_fingerprint),
                    ":old": s(json.dumps(old)),
                },
            )
        except Exception as error:
            if (
                getattr(error, "response", {}).get("Error", {}).get("Code")
                != "ConditionalCheckFailedException"
            ):
                raise CleanupRunError("Cleanup discovery checkpoint was unavailable") from None

    def discovery_failure(self, job_id: str | None, now: datetime) -> None:
        # One immutable sanitized discovery-error receipt per candidate/campaign; retries
        # still raise a run error, but a poisoned candidate does not stall pagination.
        key = {
            "PK": s(
                f"JOB#{job_id}" if job_id is not None else f"CAMPAIGN#{self.config.campaign_id}"
            ),
            "SK": s(f"DISCOVERY_ERROR#{self.config.campaign_id}"),
        }
        try:
            self.client.put_item(
                TableName=self.table,
                Item={
                    **key,
                    "campaign_fingerprint": s(self.config.campaign_fingerprint),
                    "payload": s(
                        json.dumps({"outcome": "source_unavailable", "at": now.isoformat()})
                    ),
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
        except Exception as error:
            if (
                getattr(error, "response", {}).get("Error", {}).get("Code")
                != "ConditionalCheckFailedException"
            ):
                raise CleanupRunError("Cleanup discovery audit was unavailable") from None
