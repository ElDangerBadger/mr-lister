"""DynamoDB transaction adapter for the Phase 6 seller-control boundary."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Any

from botocore.exceptions import ClientError

from mr_lister.control.errors import (
    ConcurrentControlModificationError,
    IdempotencyConflictError,
    NotFoundError,
)
from mr_lister.control.models import (
    AgentPreparationEvidence,
    ArtworkAnalysisRecord,
    CommandReceipt,
    ControlJobRecord,
    DomainEvent,
    FailureRecord,
    PricingEvidenceRecord,
    PricingSnapshot,
    ProductSyncRecord,
    ProviderCallPermit,
    ProviderCallPermitStatus,
    ProviderUploadAttempt,
    ProviderWriteAttempt,
    ReviewContent,
    SourceArtifactRecord,
    UploadedArtworkRecord,
    WorkRequest,
    WorkRequestStatus,
)
from mr_lister.control.source_artwork import validate_source_artifact_authority
from mr_lister.control.store import (
    CommandCommit,
    OwnerJobPage,
    decode_owner_job_cursor,
    encode_owner_job_cursor,
    owner_job_sort_key,
    revalidate_work_request,
    validate_command_commit,
    validate_initial_job,
)
from mr_lister.control.upload_models import (
    COMPLETED_UPLOAD_INTENT_TTL,
    UPLOAD_RECEIPT_TTL,
    UploadCompletionCommit,
    UploadIntent,
    UploadIntentCommit,
    UploadIntentStatus,
    UploadReceipt,
)


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: int) -> dict[str, str]:
    return {"N": str(value)}


def _bool(value: bool) -> dict[str, bool]:
    return {"BOOL": value}


def _job_pk(job_id: str) -> str:
    return f"JOB#{job_id}"


def _owner_pk(owner_id: str) -> str:
    return f"OWNER#{owner_id}"


def _upload_pk(upload_id: str) -> str:
    return f"UPLOAD#{upload_id}"


def _receipt_sk(command_type: str, job_id: str, key_digest: str) -> str:
    material = f"{command_type}\0{job_id}\0{key_digest}".encode()
    return f"RECEIPT#{sha256(material).hexdigest()}"


def _upload_receipt_sk(command_type: str, upload_id: str, key_digest: str) -> str:
    material = f"UPLOAD\0{command_type}\0{upload_id}\0{key_digest}".encode()
    return f"UPLOAD_RECEIPT#{sha256(material).hexdigest()}"


def _payload(model: Any) -> str:
    return model.model_dump_json()


def _job_item(job: ControlJobRecord) -> dict[str, dict[str, Any]]:
    return {
        "PK": _s(_job_pk(job.job_id)),
        "SK": _s("META"),
        "entity_type": _s("CONTROL_JOB"),
        "contract_version": _s(job.contract_version),
        "owner_id": _s(job.owner_id),
        "owner_jobs_pk": _s(_owner_pk(job.owner_id)),
        "owner_jobs_sk": _s(owner_job_sort_key(job)),
        "state": _s(job.state.value),
        "record_version": _n(job.record_version),
        "event_sequence": _n(job.event_sequence),
        "review_version": _n(job.review_version),
        "cancellation_requested": _bool(job.cancellation_requested_at is not None),
        "payload": _s(_payload(job)),
    }


def _record_item(
    *, job_id: str, sort_key: str, entity_type: str, record: Any
) -> dict[str, dict[str, Any]]:
    return {
        "PK": _s(_job_pk(job_id)),
        "SK": _s(sort_key),
        "entity_type": _s(entity_type),
        "contract_version": _s(record.contract_version),
        "payload": _s(_payload(record)),
    }


def _work_item(work: WorkRequest) -> dict[str, dict[str, Any]]:
    item = _record_item(
        job_id=work.job_id,
        sort_key=f"WORK#{work.work_request_id}",
        entity_type="WORK_REQUEST",
        record=work,
    )
    item["work_status"] = _s(work.status.value)
    item["work_request_id"] = _s(work.work_request_id)
    if work.status in {WorkRequestStatus.PENDING, WorkRequestStatus.CLAIMED}:
        due_at = (
            work.lease_expires_at
            if work.status is WorkRequestStatus.CLAIMED
            else work.next_dispatch_at
        )
        assert due_at is not None
        item["dispatch_pk"] = _s("WORK_DUE#0")
        item["dispatch_sk"] = _s(f"{int(due_at.timestamp()):020d}#{work.work_request_id}")
    elif work.status is WorkRequestStatus.DISPATCHED:
        item["recovery_pk"] = _s("WORK_RECOVERY#0")
        item["recovery_sk"] = _s(f"{int(work.updated_at.timestamp()):020d}#{work.work_request_id}")
    return item


def _receipt_item(receipt: CommandReceipt) -> dict[str, dict[str, Any]]:
    return {
        "PK": _s(_owner_pk(receipt.owner_id)),
        "SK": _s(
            _receipt_sk(
                receipt.command_type,
                receipt.job_id,
                receipt.idempotency_key_digest,
            )
        ),
        "entity_type": _s("COMMAND_RECEIPT"),
        "contract_version": _s(receipt.contract_version),
        "job_id": _s(receipt.job_id),
        "command_type": _s(receipt.command_type),
        "key_digest": _s(receipt.idempotency_key_digest),
        "request_fingerprint": _s(receipt.request_fingerprint),
        "payload": _s(_payload(receipt)),
    }


def _upload_intent_item(intent: UploadIntent) -> dict[str, dict[str, Any]]:
    item = {
        "PK": _s(_upload_pk(intent.upload_id)),
        "SK": _s("META"),
        "entity_type": _s("UPLOAD_INTENT"),
        "contract_version": _s(intent.contract_version),
        "owner_id": _s(intent.owner_id),
        "upload_status": _s(intent.status.value),
        "record_version": _n(intent.record_version),
        "payload": _s(_payload(intent)),
    }
    if intent.status in {
        UploadIntentStatus.OPEN,
        UploadIntentStatus.CANCELLED,
        UploadIntentStatus.EXPIRED,
    }:
        item["expires_at"] = _n(int(intent.intent_expires_at.timestamp()))
    elif intent.status is UploadIntentStatus.COMPLETED:
        assert intent.completed_at is not None
        item["expires_at"] = _n(
            int((intent.completed_at + COMPLETED_UPLOAD_INTENT_TTL).timestamp())
        )
    return item


def _upload_receipt_item(receipt: UploadReceipt) -> dict[str, dict[str, Any]]:
    return {
        "PK": _s(_owner_pk(receipt.owner_id)),
        "SK": _s(
            _upload_receipt_sk(
                receipt.command_type.value,
                receipt.upload_id,
                receipt.idempotency_key_digest,
            )
        ),
        "entity_type": _s("UPLOAD_RECEIPT"),
        "contract_version": _s(receipt.contract_version),
        "upload_id": _s(receipt.upload_id),
        "job_id": _s(receipt.job_id),
        "command_type": _s(receipt.command_type.value),
        "key_digest": _s(receipt.idempotency_key_digest),
        "request_fingerprint": _s(receipt.request_fingerprint),
        "expires_at": _n(int((receipt.created_at + UPLOAD_RECEIPT_TTL).timestamp())),
        "payload": _s(_payload(receipt)),
    }


class DynamoDBSellerControlStore:
    """Single-table adapter whose mutations mirror ``CommandCommit`` exactly."""

    def __init__(self, *, client: Any, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

    def get_job(self, job_id: str) -> ControlJobRecord:
        item = self._get(_job_pk(job_id), "META")
        payload = None if item is None else item.get("payload", {}).get("S")
        if item is None or item.get("entity_type", {}).get("S") != "CONTROL_JOB" or not payload:
            raise NotFoundError("The requested job was not found")
        job = ControlJobRecord.model_validate_json(payload)
        stored_owner = item.get("owner_id", {}).get("S")
        if job.job_id != job_id or stored_owner != job.owner_id:
            raise NotFoundError("The requested job was not found")
        return job

    def get_job_for_owner(self, owner_id: str, job_id: str) -> ControlJobRecord:
        item = self._get(_job_pk(job_id), "META")
        if (
            item is None
            or item.get("entity_type", {}).get("S") != "CONTROL_JOB"
            or item.get("owner_id", {}).get("S") != owner_id
        ):
            raise NotFoundError("The requested job was not found")
        payload = item.get("payload", {}).get("S")
        if not payload:
            raise NotFoundError("The requested job was not found")
        job = ControlJobRecord.model_validate_json(payload)
        if job.job_id != job_id or job.owner_id != owner_id:
            raise NotFoundError("The requested job was not found")
        return job

    def list_jobs_for_owner(
        self,
        owner_id: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> OwnerJobPage:
        if not 1 <= limit <= 100:
            raise ValueError("Owner job page limit must be between 1 and 100")
        request: dict[str, Any] = {
            "TableName": self._table_name,
            "IndexName": "OwnerJobsIndex",
            "KeyConditionExpression": "owner_jobs_pk = :owner_jobs_pk",
            "ExpressionAttributeValues": {":owner_jobs_pk": _s(_owner_pk(owner_id))},
            "ScanIndexForward": False,
            "Limit": limit,
        }
        if cursor is not None:
            sort_key, job_id = decode_owner_job_cursor(cursor)
            request["ExclusiveStartKey"] = {
                "PK": _s(_job_pk(job_id)),
                "SK": _s("META"),
                "owner_jobs_pk": _s(_owner_pk(owner_id)),
                "owner_jobs_sk": _s(sort_key),
            }
        response = self._client.query(**request)
        jobs: list[ControlJobRecord] = []
        for item in response.get("Items", []):
            payload = item.get("payload", {}).get("S")
            if (
                item.get("entity_type", {}).get("S") != "CONTROL_JOB"
                or item.get("owner_id", {}).get("S") != owner_id
                or item.get("owner_jobs_pk", {}).get("S") != _owner_pk(owner_id)
                or not payload
            ):
                raise NotFoundError("The requested job page was not found")
            job = ControlJobRecord.model_validate_json(payload)
            if (
                job.owner_id != owner_id
                or item.get("PK", {}).get("S") != _job_pk(job.job_id)
                or item.get("SK", {}).get("S") != "META"
                or item.get("owner_jobs_sk", {}).get("S") != owner_job_sort_key(job)
            ):
                raise NotFoundError("The requested job page was not found")
            jobs.append(job)
        next_cursor = None
        if response.get("LastEvaluatedKey") and jobs:
            next_cursor = encode_owner_job_cursor(jobs[-1])
        return OwnerJobPage(jobs=tuple(jobs), next_cursor=next_cursor)

    def get_upload_intent_for_owner(self, owner_id: str, upload_id: str) -> UploadIntent:
        item = self._get(_upload_pk(upload_id), "META")
        if (
            item is None
            or item.get("entity_type", {}).get("S") != "UPLOAD_INTENT"
            or item.get("owner_id", {}).get("S") != owner_id
        ):
            raise NotFoundError("The requested upload was not found")
        payload = item.get("payload", {}).get("S")
        if not payload:
            raise NotFoundError("The requested upload was not found")
        intent = UploadIntent.model_validate_json(payload)
        if intent.owner_id != owner_id or intent.upload_id != upload_id:
            raise NotFoundError("The requested upload was not found")
        return intent

    def resolve_upload_receipt(
        self,
        owner_id: str,
        command_type: str,
        upload_id: str,
        key_digest: str,
    ) -> UploadReceipt | None:
        item = self._get(
            _owner_pk(owner_id),
            _upload_receipt_sk(command_type, upload_id, key_digest),
        )
        if item is None:
            return None
        payload = item.get("payload", {}).get("S")
        if (
            item.get("entity_type", {}).get("S") != "UPLOAD_RECEIPT"
            or item.get("upload_id", {}).get("S") != upload_id
            or item.get("command_type", {}).get("S") != command_type
            or item.get("key_digest", {}).get("S") != key_digest
            or not payload
        ):
            return None
        receipt = UploadReceipt.model_validate_json(payload)
        if receipt.owner_id != owner_id:
            return None
        return receipt

    def commit_upload_intent(self, commit: UploadIntentCommit) -> UploadReceipt:
        intent_put: dict[str, Any]
        if commit.current is None:
            intent_put = self._put_new(_upload_intent_item(commit.updated))
        else:
            current = commit.current
            intent_put = {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _upload_intent_item(commit.updated),
                    "ConditionExpression": (
                        "owner_id = :owner_id AND record_version = :record_version AND "
                        "upload_status = :upload_status AND payload = :expected_payload"
                    ),
                    "ExpressionAttributeValues": {
                        ":owner_id": _s(current.owner_id),
                        ":record_version": _n(current.record_version),
                        ":upload_status": _s(current.status.value),
                        ":expected_payload": _s(_payload(current)),
                    },
                }
            }
        try:
            self._transact(
                [intent_put, self._put_new(_upload_receipt_item(commit.receipt))],
                commit.receipt.receipt_id,
            )
        except ClientError as error:
            if self._is_transaction_replay_error(error):
                return self._resolve_upload_after_cancel(commit.receipt)
            raise
        return commit.receipt

    def complete_upload(self, commit: UploadCompletionCommit) -> UploadReceipt:
        current = commit.intent.current
        assert current is not None
        updated_intent_put = {
            "Put": {
                "TableName": self._table_name,
                "Item": _upload_intent_item(commit.intent.updated),
                "ConditionExpression": (
                    "owner_id = :owner_id AND record_version = :record_version AND "
                    "upload_status = :upload_status AND payload = :expected_payload"
                ),
                "ExpressionAttributeValues": {
                    ":owner_id": _s(current.owner_id),
                    ":record_version": _n(current.record_version),
                    ":upload_status": _s(current.status.value),
                    ":expected_payload": _s(_payload(current)),
                },
            }
        }
        items = [
            updated_intent_put,
            self._put_new(_job_item(commit.job)),
            self._put_new(
                _record_item(
                    job_id=commit.job.job_id,
                    sort_key="SOURCE",
                    entity_type="SOURCE_ARTIFACT",
                    record=commit.source_artifact,
                )
            ),
            self._put_new(
                _record_item(
                    job_id=commit.job.job_id,
                    sort_key=f"EVENT#{commit.event.sequence:020d}",
                    entity_type="DOMAIN_EVENT",
                    record=commit.event,
                )
            ),
            self._put_new(_upload_receipt_item(commit.intent.receipt)),
            self._put_new(_work_item(commit.work_request)),
        ]
        try:
            self._transact(items, commit.intent.receipt.receipt_id)
        except ClientError as error:
            if self._is_transaction_replay_error(error):
                return self._resolve_upload_after_cancel(commit.intent.receipt)
            raise
        return commit.intent.receipt

    def get_review(self, job_id: str, review_version: int) -> ReviewContent:
        return self._get_record(
            job_id,
            f"REVIEW#{review_version:020d}",
            ReviewContent,
            "review",
        )

    def get_source_artifact(self, job_id: str) -> SourceArtifactRecord:
        source = self._get_record(job_id, "SOURCE", SourceArtifactRecord, "source artifact")
        return validate_source_artifact_authority(source)

    def get_artwork_analysis(self, job_id: str, analysis_id: str) -> ArtworkAnalysisRecord:
        return self._get_record(
            job_id,
            f"ARTWORK_ANALYSIS#{analysis_id}",
            ArtworkAnalysisRecord,
            "artwork analysis",
        )

    def get_agent_evidence(self, job_id: str, evidence_id: str) -> AgentPreparationEvidence:
        return self._get_record(
            job_id,
            f"AGENT_EVIDENCE#{evidence_id}",
            AgentPreparationEvidence,
            "agent preparation evidence",
        )

    def get_product_sync(self, job_id: str, sync_id: str) -> ProductSyncRecord:
        return self._get_record(
            job_id, f"PRODUCT_SYNC#{sync_id}", ProductSyncRecord, "product synchronization"
        )

    def get_provider_upload_attempt(self, job_id: str, attempt_id: str) -> ProviderUploadAttempt:
        return self._get_record(
            job_id,
            f"PROVIDER_UPLOAD_ATTEMPT#{attempt_id}",
            ProviderUploadAttempt,
            "provider upload attempt",
        )

    def get_uploaded_artwork(self, job_id: str, upload_id: str) -> UploadedArtworkRecord:
        return self._get_record(
            job_id,
            f"UPLOADED_ARTWORK#{upload_id}",
            UploadedArtworkRecord,
            "uploaded artwork",
        )

    def get_pricing(self, job_id: str, snapshot_id: str) -> PricingSnapshot:
        return self._get_record(
            job_id, f"PRICING#{snapshot_id}", PricingSnapshot, "pricing snapshot"
        )

    def get_pricing_evidence(self, job_id: str, snapshot_id: str) -> PricingEvidenceRecord:
        return self._get_record(
            job_id,
            f"PRICING_EVIDENCE#{snapshot_id}",
            PricingEvidenceRecord,
            "pricing evidence",
        )

    def get_failure(self, job_id: str, failure_id: str) -> FailureRecord:
        return self._get_record(job_id, f"FAILURE#{failure_id}", FailureRecord, "failure")

    def get_provider_write_attempt(self, job_id: str, attempt_id: str) -> ProviderWriteAttempt:
        return self._get_record(
            job_id,
            f"PROVIDER_ATTEMPT#{attempt_id}",
            ProviderWriteAttempt,
            "provider write attempt",
        )

    def get_provider_call_permit(self, job_id: str, attempt_id: str) -> ProviderCallPermit:
        return self._get_record(
            job_id,
            f"PROVIDER_PERMIT#{attempt_id}",
            ProviderCallPermit,
            "provider call permit",
        )

    def consume_provider_call_permit(
        self,
        job: ControlJobRecord,
        work: WorkRequest,
        attempt_id: str,
        *,
        now: datetime,
    ) -> ProviderCallPermit | None:
        if (
            job.active_work_request_id != work.work_request_id
            or work.job_id != job.job_id
            or work.status not in {WorkRequestStatus.CLAIMED, WorkRequestStatus.DISPATCHED}
        ):
            return None
        current = self.get_provider_call_permit(job.job_id, attempt_id)
        if current.status is not ProviderCallPermitStatus.AVAILABLE:
            return None
        consumed = ProviderCallPermit.model_validate(
            {
                **current.model_dump(mode="python"),
                "status": ProviderCallPermitStatus.CONSUMED,
                "consumed_at": now,
                "consumed_work_request_id": work.work_request_id,
            }
        )
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "ConditionCheck": {
                            "TableName": self._table_name,
                            "Key": {"PK": _s(_job_pk(job.job_id)), "SK": _s("META")},
                            "ConditionExpression": "payload = :expected_job",
                            "ExpressionAttributeValues": {":expected_job": _s(_payload(job))},
                        }
                    },
                    {
                        "ConditionCheck": {
                            "TableName": self._table_name,
                            "Key": {
                                "PK": _s(_job_pk(job.job_id)),
                                "SK": _s(f"WORK#{work.work_request_id}"),
                            },
                            "ConditionExpression": "payload = :expected_work",
                            "ExpressionAttributeValues": {":expected_work": _s(_payload(work))},
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": _record_item(
                                job_id=job.job_id,
                                sort_key=f"PROVIDER_PERMIT#{attempt_id}",
                                entity_type="PROVIDER_CALL_PERMIT",
                                record=consumed,
                            ),
                            "ConditionExpression": "payload = :expected_payload",
                            "ExpressionAttributeValues": {
                                ":expected_payload": _s(_payload(current))
                            },
                        }
                    },
                ],
                ClientRequestToken=sha256(
                    (
                        f"consume:{job.job_id}:{attempt_id}:{job.record_version}:{_payload(work)}"
                    ).encode()
                ).hexdigest()[:32],
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {
                "TransactionCanceledException",
                "IdempotentParameterMismatchException",
            }:
                return None
            raise
        return consumed

    def get_work_request(self, job_id: str, work_request_id: str) -> WorkRequest:
        return self._get_record(job_id, f"WORK#{work_request_id}", WorkRequest, "work request")

    def resolve_receipt(
        self, owner_id: str, command_type: str, job_id: str, key_digest: str
    ) -> CommandReceipt | None:
        item = self._get(
            _owner_pk(owner_id),
            _receipt_sk(command_type, job_id, key_digest),
        )
        return None if item is None else CommandReceipt.model_validate_json(item["payload"]["S"])

    def create_job(
        self,
        *,
        job: ControlJobRecord,
        event: DomainEvent,
        receipt: CommandReceipt,
        work_request: WorkRequest | None = None,
        source_artifact: SourceArtifactRecord | None = None,
    ) -> CommandReceipt:
        validate_initial_job(job, event, receipt, work_request, source_artifact)
        assert source_artifact is not None
        items = [
            self._put_new(_job_item(job)),
            self._put_new(
                _record_item(
                    job_id=job.job_id,
                    sort_key=f"EVENT#{event.sequence:020d}",
                    entity_type="DOMAIN_EVENT",
                    record=event,
                )
            ),
            self._put_new(_receipt_item(receipt)),
            self._put_new(
                _record_item(
                    job_id=job.job_id,
                    sort_key="SOURCE",
                    entity_type="SOURCE_ARTIFACT",
                    record=source_artifact,
                )
            ),
        ]
        if work_request is not None:
            items.append(self._put_new(_work_item(work_request)))
        try:
            self._transact(items, receipt.receipt_id)
        except ClientError as error:
            if self._is_transaction_replay_error(error):
                return self._resolve_after_cancel(receipt)
            raise
        return receipt

    def commit_command(self, commit: CommandCommit) -> CommandReceipt:
        validate_command_commit(commit)
        current = commit.current
        job_put = {
            "Put": {
                "TableName": self._table_name,
                "Item": _job_item(commit.updated),
                "ConditionExpression": (
                    "contract_version = :contract_version AND owner_id = :owner_id AND "
                    "record_version = :record_version AND event_sequence = :event_sequence AND "
                    "#state = :state AND review_version = :review_version AND "
                    "cancellation_requested = :cancellation_requested AND "
                    "payload = :expected_payload"
                ),
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": {
                    ":contract_version": _s(current.contract_version),
                    ":owner_id": _s(current.owner_id),
                    ":record_version": _n(current.record_version),
                    ":event_sequence": _n(current.event_sequence),
                    ":state": _s(current.state.value),
                    ":review_version": _n(current.review_version),
                    ":cancellation_requested": _bool(current.cancellation_requested_at is not None),
                    ":expected_payload": _s(_payload(current)),
                },
            }
        }
        items: list[dict[str, Any]] = [
            job_put,
            self._put_new(
                _record_item(
                    job_id=commit.updated.job_id,
                    sort_key=f"EVENT#{commit.event.sequence:020d}",
                    entity_type="DOMAIN_EVENT",
                    record=commit.event,
                )
            ),
            self._put_new(_receipt_item(commit.receipt)),
        ]
        if commit.provider_write_retry_basis is not None:
            retry_basis = commit.provider_write_retry_basis
            items.append(
                {
                    "ConditionCheck": {
                        "TableName": self._table_name,
                        "Key": {
                            "PK": _s(_job_pk(commit.updated.job_id)),
                            "SK": _s(f"PROVIDER_ATTEMPT#{retry_basis.attempt_id}"),
                        },
                        "ConditionExpression": "payload = :expected_retry_basis",
                        "ExpressionAttributeValues": {
                            ":expected_retry_basis": _s(_payload(retry_basis))
                        },
                    }
                }
            )
        records: tuple[tuple[Any, str, str], ...] = (
            (
                commit.review,
                f"REVIEW#{commit.review.review_version:020d}" if commit.review else "",
                "REVIEW",
            ),
            (
                commit.artwork_analysis,
                (
                    f"ARTWORK_ANALYSIS#{commit.artwork_analysis.analysis_id}"
                    if commit.artwork_analysis
                    else ""
                ),
                "ARTWORK_ANALYSIS",
            ),
            (
                commit.agent_evidence,
                (
                    f"AGENT_EVIDENCE#{commit.agent_evidence.evidence_id}"
                    if commit.agent_evidence
                    else ""
                ),
                "AGENT_PREPARATION_EVIDENCE",
            ),
            (
                commit.review_decision,
                f"DECISION#{commit.review_decision.decision_id}" if commit.review_decision else "",
                "REVIEW_DECISION",
            ),
            (
                commit.cancellation_decision,
                f"CANCELLATION#{commit.cancellation_decision.decision_id}"
                if commit.cancellation_decision
                else "",
                "CANCELLATION_DECISION",
            ),
            (
                commit.product_sync,
                f"PRODUCT_SYNC#{commit.product_sync.sync_id}" if commit.product_sync else "",
                "PRODUCT_SYNC",
            ),
            (
                commit.provider_upload_attempt,
                (
                    f"PROVIDER_UPLOAD_ATTEMPT#{commit.provider_upload_attempt.attempt_id}"
                    if commit.provider_upload_attempt
                    else ""
                ),
                "PROVIDER_UPLOAD_ATTEMPT",
            ),
            (
                commit.uploaded_artwork,
                (
                    f"UPLOADED_ARTWORK#{commit.uploaded_artwork.upload_id}"
                    if commit.uploaded_artwork
                    else ""
                ),
                "UPLOADED_ARTWORK",
            ),
            (
                commit.provider_write_attempt,
                (
                    f"PROVIDER_ATTEMPT#{commit.provider_write_attempt.attempt_id}"
                    if commit.provider_write_attempt
                    else ""
                ),
                "PROVIDER_WRITE_ATTEMPT",
            ),
            (
                commit.provider_call_permit,
                (
                    f"PROVIDER_PERMIT#{commit.provider_call_permit.attempt_id}"
                    if commit.provider_call_permit
                    else ""
                ),
                "PROVIDER_CALL_PERMIT",
            ),
            (
                commit.reconciliation_observation,
                (
                    f"RECONCILIATION#{commit.reconciliation_observation.observation_id}"
                    if commit.reconciliation_observation
                    else ""
                ),
                "RECONCILIATION_OBSERVATION",
            ),
            (
                commit.upload_reconciliation_observation,
                (
                    "UPLOAD_RECONCILIATION#"
                    f"{commit.upload_reconciliation_observation.observation_id}"
                    if commit.upload_reconciliation_observation
                    else ""
                ),
                "UPLOAD_RECONCILIATION_OBSERVATION",
            ),
            (
                commit.pricing_snapshot,
                f"PRICING#{commit.pricing_snapshot.snapshot_id}" if commit.pricing_snapshot else "",
                "PRICING_SNAPSHOT",
            ),
            (
                commit.pricing_evidence,
                (
                    f"PRICING_EVIDENCE#{commit.pricing_evidence.snapshot_id}"
                    if commit.pricing_evidence
                    else ""
                ),
                "PRICING_EVIDENCE",
            ),
            (
                commit.failure,
                f"FAILURE#{commit.failure.failure_id}" if commit.failure else "",
                "FAILURE",
            ),
        )
        for record, sort_key, entity_type in records:
            if record is not None:
                items.append(
                    self._put_new(
                        _record_item(
                            job_id=commit.updated.job_id,
                            sort_key=sort_key,
                            entity_type=entity_type,
                            record=record,
                        )
                    )
                )
        if commit.work_request is not None:
            items.append(self._put_new(_work_item(commit.work_request)))
        if commit.work_update is not None:
            expected, changed = commit.work_update
            items.append(
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": _work_item(changed),
                        "ConditionExpression": "payload = :expected_payload",
                        "ExpressionAttributeValues": {":expected_payload": _s(_payload(expected))},
                    }
                }
            )
        if commit.provider_call_permit_update is not None:
            expected_permit, retired_permit = commit.provider_call_permit_update
            items.append(
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": _record_item(
                            job_id=retired_permit.job_id,
                            sort_key=f"PROVIDER_PERMIT#{retired_permit.attempt_id}",
                            entity_type="PROVIDER_CALL_PERMIT",
                            record=retired_permit,
                        ),
                        "ConditionExpression": "payload = :expected_payload",
                        "ExpressionAttributeValues": {
                            ":expected_payload": _s(_payload(expected_permit))
                        },
                    }
                }
            )
        try:
            self._transact(items, commit.receipt.receipt_id)
        except ClientError as error:
            if self._is_transaction_replay_error(error):
                existing = self.resolve_receipt(
                    commit.receipt.owner_id,
                    commit.receipt.command_type,
                    commit.receipt.job_id,
                    commit.receipt.idempotency_key_digest,
                )
                if existing is not None:
                    if existing.request_fingerprint == commit.receipt.request_fingerprint:
                        return existing
                    raise IdempotencyConflictError(
                        "The idempotency key was used for another request"
                    ) from error
                raise ConcurrentControlModificationError(
                    "The job changed before the command could commit"
                ) from error
            raise
        return commit.receipt

    def nudge_pending_work(
        self, job_id: str, work_request_id: str, *, now: datetime
    ) -> WorkRequest:
        current = self.get_work_request(job_id, work_request_id)
        if current.status is not WorkRequestStatus.PENDING or current.next_dispatch_at <= now:
            return current
        updated = revalidate_work_request(
            current,
            next_dispatch_at=now,
            updated_at=now,
        )
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=_work_item(updated),
                ConditionExpression="work_status = :pending AND payload = :expected_payload",
                ExpressionAttributeValues={
                    ":pending": _s(WorkRequestStatus.PENDING.value),
                    ":expected_payload": _s(_payload(current)),
                },
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return self.get_work_request(job_id, work_request_id)
            raise
        return updated

    def list_due_work(self, *, now: datetime, limit: int = 100) -> tuple[WorkRequest, ...]:
        response = self._client.query(
            TableName=self._table_name,
            IndexName="DueWorkIndex",
            KeyConditionExpression="dispatch_pk = :dispatch_pk AND dispatch_sk <= :dispatch_sk",
            ExpressionAttributeValues={
                ":dispatch_pk": _s("WORK_DUE#0"),
                ":dispatch_sk": _s(f"{int(now.timestamp()):020d}#~"),
            },
            ScanIndexForward=True,
            Limit=limit,
        )
        return tuple(
            WorkRequest.model_validate_json(item["payload"]["S"])
            for item in response.get("Items", [])
        )

    def claim_work(
        self,
        job_id: str,
        work_request_id: str,
        *,
        now: datetime,
        claim_id: str,
        lease_expires_at: datetime,
    ) -> WorkRequest | None:
        current = self.get_work_request(job_id, work_request_id)
        claimable = (
            current.status is WorkRequestStatus.PENDING and current.next_dispatch_at <= now
        ) or (
            current.status is WorkRequestStatus.CLAIMED
            and current.lease_expires_at is not None
            and current.lease_expires_at <= now
        )
        if not claimable:
            return None
        claimed = revalidate_work_request(
            current,
            status=WorkRequestStatus.CLAIMED,
            attempt_count=current.attempt_count + 1,
            claim_id=claim_id,
            lease_expires_at=lease_expires_at,
            last_error_code=None,
            updated_at=now,
        )
        return self._replace_work_conditionally(current, claimed, return_none_on_conflict=True)

    def mark_work_dispatched(
        self,
        job_id: str,
        work_request_id: str,
        *,
        claim_id: str,
        execution_arn: str,
        now: datetime,
    ) -> WorkRequest:
        current = self.get_work_request(job_id, work_request_id)
        if current.status is WorkRequestStatus.COMPLETED:
            return current
        if current.status is not WorkRequestStatus.CLAIMED or current.claim_id != claim_id:
            raise ConcurrentControlModificationError("The work claim is no longer current")
        dispatched = revalidate_work_request(
            current,
            status=WorkRequestStatus.DISPATCHED,
            claim_id=None,
            lease_expires_at=None,
            execution_arn=execution_arn,
            updated_at=now,
        )
        try:
            result = self._replace_work_conditionally(current, dispatched)
        except ConcurrentControlModificationError:
            latest = self.get_work_request(job_id, work_request_id)
            if latest.status is WorkRequestStatus.COMPLETED:
                return latest
            raise
        assert result is not None
        return result

    def defer_claimed_work(
        self,
        job_id: str,
        work_request_id: str,
        *,
        claim_id: str,
        retry_at: datetime,
        error_code: str,
        now: datetime,
    ) -> WorkRequest:
        current = self.get_work_request(job_id, work_request_id)
        if current.status is WorkRequestStatus.COMPLETED:
            return current
        if current.status is not WorkRequestStatus.CLAIMED or current.claim_id != claim_id:
            raise ConcurrentControlModificationError("The work claim is no longer current")
        deferred = revalidate_work_request(
            current,
            lease_expires_at=retry_at,
            last_error_code=error_code,
            updated_at=now,
        )
        try:
            result = self._replace_work_conditionally(current, deferred)
        except ConcurrentControlModificationError:
            latest = self.get_work_request(job_id, work_request_id)
            if latest.status is WorkRequestStatus.COMPLETED:
                return latest
            raise
        assert result is not None
        return result

    def release_work(
        self,
        job_id: str,
        work_request_id: str,
        *,
        claim_id: str,
        next_dispatch_at: datetime,
        error_code: str,
        now: datetime,
    ) -> WorkRequest:
        current = self.get_work_request(job_id, work_request_id)
        if current.status is not WorkRequestStatus.CLAIMED or current.claim_id != claim_id:
            raise ConcurrentControlModificationError("The work claim is no longer current")
        released = revalidate_work_request(
            current,
            status=WorkRequestStatus.PENDING,
            claim_id=None,
            lease_expires_at=None,
            next_dispatch_at=next_dispatch_at,
            last_error_code=error_code,
            updated_at=now,
        )
        result = self._replace_work_conditionally(current, released)
        assert result is not None
        return result

    def _replace_work_conditionally(
        self,
        current: WorkRequest,
        updated: WorkRequest,
        *,
        return_none_on_conflict: bool = False,
    ) -> WorkRequest | None:
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=_work_item(updated),
                ConditionExpression="payload = :expected_payload",
                ExpressionAttributeValues={":expected_payload": _s(_payload(current))},
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                if return_none_on_conflict:
                    return None
                raise ConcurrentControlModificationError(
                    "The work request changed before it could be updated"
                ) from error
            raise
        return updated

    def _get_record(self, job_id: str, sort_key: str, model: Any, label: str) -> Any:
        self.get_job(job_id)
        item = self._get(_job_pk(job_id), sort_key)
        if item is None:
            raise NotFoundError(f"The requested {label} was not found")
        return model.model_validate_json(item["payload"]["S"])

    def _get(self, partition_key: str, sort_key: str) -> dict[str, Any] | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key={"PK": _s(partition_key), "SK": _s(sort_key)},
            ConsistentRead=True,
        )
        return response.get("Item")

    def _put_new(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "Put": {
                "TableName": self._table_name,
                "Item": item,
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        }

    def _transact(self, items: list[dict[str, Any]], identity: str) -> None:
        self._client.transact_write_items(
            TransactItems=items,
            ClientRequestToken=sha256(identity.encode()).hexdigest()[:32],
        )

    def _resolve_after_cancel(self, receipt: CommandReceipt) -> CommandReceipt:
        existing = self.resolve_receipt(
            receipt.owner_id,
            receipt.command_type,
            receipt.job_id,
            receipt.idempotency_key_digest,
        )
        if existing is not None and existing.request_fingerprint == receipt.request_fingerprint:
            return existing
        if existing is not None:
            raise IdempotencyConflictError("The idempotency key was used for another request")
        raise ConcurrentControlModificationError("The job could not be created atomically")

    def _resolve_upload_after_cancel(self, receipt: UploadReceipt) -> UploadReceipt:
        existing = self.resolve_upload_receipt(
            receipt.owner_id,
            receipt.command_type.value,
            receipt.upload_id,
            receipt.idempotency_key_digest,
        )
        if existing is not None and existing.request_fingerprint == receipt.request_fingerprint:
            return existing
        if existing is not None:
            raise IdempotencyConflictError(
                "The idempotency key was used for another upload request"
            )
        raise ConcurrentControlModificationError("The upload changed before it could commit")

    @staticmethod
    def _is_transaction_replay_error(error: ClientError) -> bool:
        return error.response.get("Error", {}).get("Code") in {
            "TransactionCanceledException",
            "IdempotentParameterMismatchException",
        }
