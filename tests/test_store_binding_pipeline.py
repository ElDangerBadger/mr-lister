"""Adversarial destination pinning and unchanged legacy fingerprint vectors."""

from datetime import timedelta
from hashlib import sha256

import pytest
from botocore.exceptions import ClientError
from pydantic import SecretStr, ValidationError

from mr_lister.cloud.phase7_provider_credentials import (
    ProductionPublicationProviderCredentialAuthority,
)
from mr_lister.connections.binding import StoreBindingAuthority
from mr_lister.control.dynamodb import DynamoDBSellerControlStore
from mr_lister.control.errors import IdempotencyConflictError, InvalidControlStateError
from mr_lister.control.models import (
    ControlJobRecord,
    ControlJobState,
    ProviderCallPermit,
    ProviderCallPermitStatus,
    WorkRequest,
    WorkRequestStatus,
    WorkType,
)
from mr_lister.control.source_artwork import (
    source_artifact_authority_fingerprint,
    validate_source_artifact_authority,
)
from mr_lister.control.store import InMemorySellerControlStore
from mr_lister.control.upload_models import (
    UploadCommandType,
    UploadIntentCommit,
    UploadIntentStatus,
)
from mr_lister.control.upload_service import UploadDependencyUnavailableError
from mr_lister.production.printify import PrintifyAuthenticationError
from mr_lister.production.provider_resources import (
    OwnerBoundProviderDraftResources,
    OwnerPrintifyConnection,
)
from mr_lister.publication.execution_fingerprints import execution_record_fingerprint
from mr_lister.publication.fingerprints import publication_snapshot_fingerprint
from mr_lister.publication.models import PublicationSnapshot
from mr_lister.publication.provider_credentials import (
    PublicationProviderCredentialError,
    issue_bound_publication_provider_credential,
)
from tests.test_phase6_dynamodb_store import SOURCE_FP
from tests.test_phase6_upload_models import _intent, _receipt
from tests.test_phase6_upload_service import NOW, OWNER, _harness, _png
from tests.test_phase71_publication_store import make_authority, make_transaction
from tests.test_phase72_publication_provider_boundary import _authority


def binding(*, owner=OWNER, connection="1", shop=42, epoch=1):
    return StoreBindingAuthority.create(
        owner_id=owner,
        connection_id="conn_" + connection * 32,
        shop_binding_id="binding_" + connection * 32,
        shop_id=shop,
        authorization_epoch=epoch,
    )


class Guard:
    def __init__(self, exact):
        self.exact = exact
        self.calls = []

    def assert_current(self, value):
        self.calls.append(value)
        if value != self.exact:
            raise ValueError("stale")

    def current_epoch_condition(self, value):
        self.assert_current(value)
        return {
            "ConditionCheck": {
                "TableName": "AccountConnections",
                "Key": {"PK": {"S": value.connection_id}},
                "ConditionExpression": "authorization_epoch = :epoch AND #status = :active",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": {
                    ":epoch": {"N": str(value.authorization_epoch)},
                    ":active": {"S": "active"},
                },
            }
        }


def create(harness, pinned, *, key="binding-upload"):
    content = _png()
    result = harness.service.create_upload(
        owner_id=OWNER,
        idempotency_key=key,
        filename="seller-art.png",
        content_type="image/png",
        content_sha256=sha256(content).hexdigest(),
        size_bytes=len(content),
        store_binding=pinned,
    )
    return content, result


def test_upload_pins_original_binding_into_intent_source_job_and_source_fingerprint():
    original = binding()
    guard = Guard(original)
    harness = _harness(store=InMemorySellerControlStore(binding_guard=guard))
    content, started = create(harness, original)
    harness.artifacts.stage(content)
    harness.service.complete_upload(
        owner_id=OWNER, upload_id=started.receipt.upload_id, idempotency_key="complete-binding"
    )
    intent = harness.store.get_upload_intent_for_owner(OWNER, started.receipt.upload_id)
    job = harness.store.get_job(started.receipt.job_id)
    source = harness.store.get_source_artifact(job.job_id)
    assert intent.store_binding == source.store_binding == job.store_binding == original
    assert source.fingerprint == source_artifact_authority_fingerprint(source)
    rewritten = source.model_copy(update={"store_binding": binding(connection="2")})
    with pytest.raises(ValueError):
        validate_source_artifact_authority(rewritten)
    assert guard.calls and all(value == original for value in guard.calls)


@pytest.mark.parametrize("replacement", [binding(connection="2"), binding(epoch=2), None])
def test_create_idempotency_rejects_another_connection_epoch_or_missing_binding(replacement):
    original = binding()
    harness = _harness(store=InMemorySellerControlStore(binding_guard=Guard(original)))
    create(harness, original)
    with pytest.raises(IdempotencyConflictError):
        create(harness, replacement)


def test_foreign_owner_is_refused_before_intent_or_s3_authorization():
    harness = _harness()
    with pytest.raises(ValueError):
        create(harness, binding(owner="b" * 64))
    assert not harness.artifacts.authorization_calls
    assert not harness.store.jobs


def test_missing_guard_blocks_modern_intake_without_owner_store_fallback():
    harness = _harness()
    with pytest.raises(InvalidControlStateError):
        create(harness, binding())
    assert not harness.artifacts.authorization_calls


def test_epoch_revoked_before_completion_creates_no_job_and_releases_unreferenced_source():
    original = binding()
    guard = Guard(original)
    harness = _harness(store=InMemorySellerControlStore(binding_guard=guard))
    content, started = create(harness, original)
    harness.artifacts.stage(content)
    guard.exact = binding(epoch=2)
    with pytest.raises(UploadDependencyUnavailableError):
        harness.service.complete_upload(
            owner_id=OWNER, upload_id=started.receipt.upload_id, idempotency_key="complete-revoked"
        )
    assert not harness.store.jobs
    assert harness.artifacts.release_calls
    # Revocation must never prevent canceling the local reserved upload.
    canceled = harness.service.cancel_upload(
        owner_id=OWNER, upload_id=started.receipt.upload_id, idempotency_key="cancel-revoked"
    )
    assert canceled.receipt.status is UploadIntentStatus.CANCELLED


class RecordingClient:
    def __init__(self, *, reject_epoch=False):
        self.transactions = []
        self.reject_epoch = reject_epoch

    def transact_write_items(self, **request):
        self.transactions.append(request)
        if self.reject_epoch:
            raise ClientError(
                {"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems"
            )


def test_upload_creation_connection_epoch_check_is_inside_the_same_dynamo_transaction():
    pinned = binding()
    intent = _intent(store_binding=pinned)
    receipt = _receipt(
        UploadCommandType.CREATE_UPLOAD,
        status=UploadIntentStatus.OPEN,
        version=0,
        created_at=intent.created_at,
    )
    client = RecordingClient()
    guard = Guard(pinned)
    store = DynamoDBSellerControlStore(client=client, table_name="Control", binding_guard=guard)
    store.commit_upload_intent(UploadIntentCommit(updated=intent, receipt=receipt))
    actions = client.transactions[0]["TransactItems"]
    assert len(actions) == 3
    assert actions[-1] == guard.current_epoch_condition(pinned)
    assert all("Put" in action for action in actions[:-1])


def permit_graph(pinned):
    job = ControlJobRecord(
        owner_id=OWNER,
        job_id="job_binding",
        store_binding=pinned,
        state=ControlJobState.INTAKE_VALIDATED,
        active_work_request_id="work_binding",
        created_at=NOW,
        updated_at=NOW,
    )
    work = WorkRequest(
        owner_id=OWNER,
        job_id=job.job_id,
        work_request_id="work_binding",
        receipt_id="receipt_binding",
        work_type=WorkType.PREPARE,
        input_fingerprint="a" * 64,
        execution_name="execution_binding",
        status=WorkRequestStatus.CLAIMED,
        claim_id="claim_binding",
        attempt_count=1,
        lease_expires_at=NOW + timedelta(minutes=1),
        next_dispatch_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    permit = ProviderCallPermit(
        attempt_id="attempt_binding",
        job_id=job.job_id,
        work_request_id=work.work_request_id,
        created_at=NOW,
    )
    return job, work, permit


class PermitStore(DynamoDBSellerControlStore):
    def __init__(self, client, guard, permit):
        super().__init__(client=client, table_name="Control", binding_guard=guard)
        self.permit = permit

    def get_provider_call_permit(self, job_id, attempt_id):
        return self.permit


@pytest.mark.parametrize("revoked_in_transaction", [False, True])
def test_provider_permit_and_original_connection_epoch_are_claimed_atomically(
    revoked_in_transaction,
):
    pinned = binding()
    job, work, permit = permit_graph(pinned)
    guard = Guard(pinned)
    client = RecordingClient(reject_epoch=revoked_in_transaction)
    store = PermitStore(client, guard, permit)
    consumed = store.consume_provider_call_permit(job, work, permit.attempt_id, now=NOW)
    actions = client.transactions[0]["TransactItems"]
    assert len(actions) == 4
    assert actions[-1] == guard.current_epoch_condition(pinned)
    assert actions[2]["Put"]["Item"]["entity_type"] == {"S": "PROVIDER_CALL_PERMIT"}
    assert (consumed is None) == revoked_in_transaction
    assert permit.status is ProviderCallPermitStatus.AVAILABLE


class Resolver:
    def __init__(self, pinned):
        self.pinned = pinned
        self.calls = []
        self.legacy_calls = []
        self.preferred = binding(connection="2", shop=43)

    def resolve_exact(self, *, binding):
        self.calls.append(binding)
        return OwnerPrintifyConnection(
            owner_id=self.pinned.owner_id,
            shop_id=self.pinned.shop_id,
            store_binding=self.pinned,
            api_token=SecretStr("test-only-token"),
        )

    def resolve(self, *, owner_id):
        self.legacy_calls.append(owner_id)
        raise AssertionError("Must not consult the owner fallback")


def resources(resolver):
    return OwnerBoundProviderDraftResources(
        connection_resolver=resolver,
        s3_client=object(),
        artifact_bucket="private-art",
        bucket_owner_account_id="123456789012",
    )


def test_provider_resources_resolve_original_connection_after_current_shop_changes():
    original = binding()
    resolver = Resolver(original)
    base = resources(resolver)
    bound = base.for_binding(owner_id=OWNER, binding=original)
    bound.synchronizer(owner_id=OWNER, shop_id=original.shop_id)
    resolver.preferred = binding(connection="3", shop=44)
    bound.synchronizer(owner_id=OWNER, shop_id=original.shop_id)
    assert resolver.calls == [original, original]
    assert not resolver.legacy_calls
    with pytest.raises(PrintifyAuthenticationError):
        base.synchronizer(owner_id=OWNER, shop_id=original.shop_id)
    assert not resolver.legacy_calls


@pytest.mark.parametrize("replacement", [binding(connection="2"), binding(epoch=2)])
def test_same_shop_wrong_connection_or_epoch_cannot_supply_provider_credentials(replacement):
    original = binding()
    resolver = Resolver(replacement)
    bound = resources(resolver).for_binding(owner_id=OWNER, binding=original)
    with pytest.raises(PrintifyAuthenticationError):
        bound.synchronizer(owner_id=OWNER, shop_id=original.shop_id)
    assert not resolver.legacy_calls


def test_publication_capability_cannot_cross_connections_even_for_the_same_owner_and_shop():
    legacy = _authority()
    original = binding(owner=legacy.owner_id, shop=legacy.printify_shop_id)
    exact = _authority(store_binding=original)
    other = _authority(
        store_binding=binding(owner=legacy.owner_id, shop=legacy.printify_shop_id, connection="2")
    )
    capability = issue_bound_publication_provider_credential(
        authority=exact, bearer_token=SecretStr("test-only-token")
    )
    assert capability.for_authority(exact).store_binding == original
    with pytest.raises(PublicationProviderCredentialError):
        capability.for_authority(other)
    resolver = Resolver(other.store_binding)
    credentials = ProductionPublicationProviderCredentialAuthority(connections=resolver)
    with pytest.raises(PublicationProviderCredentialError):
        credentials.resolve_exact(authority=exact)
    assert not resolver.legacy_calls


def test_legacy_golden_source_sync_snapshot_and_provider_authority_hashes_are_unchanged():
    authority = make_authority()
    snapshot = make_transaction(authority).commit.snapshot
    provider = _authority()
    assert SOURCE_FP == "688018617c694d426a42cdeb24d4094f4fb9594fd2088e547d74a4849d5530a5"
    assert (
        authority.product_sync.fingerprint
        == "b9ad7977ff852bf7283164da8815dce4a0fe321662882ffc1a24939f78a73f23"
    )
    assert (
        snapshot.fingerprint == "ffede868ed2cd7ed2b2395f380163bab06c84877f176d0ea8a06d915913cd055"
    )
    assert (
        provider.fingerprint == "663a3e74f5915d426bdcfbdca724521fe01bc5ead28efc481a7ce47bef3fc3f2"
    )
    for value in (
        authority.current_job,
        authority.source,
        authority.product_sync,
        snapshot,
        provider,
    ):
        assert "store_binding" not in value.model_dump(mode="python")
        assert "store_binding" not in value.model_dump_json()


def test_publication_snapshot_hash_binds_original_connection_and_rejects_shop_substitution():
    legacy = make_transaction(make_authority()).commit.snapshot
    pinned = binding(owner=legacy.owner_id, shop=legacy.printify_shop_id)
    material = legacy.model_dump(mode="python", exclude={"fingerprint"})
    material["store_binding"] = pinned
    material["fingerprint"] = publication_snapshot_fingerprint(material)
    modern = PublicationSnapshot.model_validate(material)
    assert modern.fingerprint != legacy.fingerprint
    assert modern.store_binding == pinned
    with pytest.raises(ValidationError):
        PublicationSnapshot.model_validate(
            {
                **modern.model_dump(mode="python"),
                "store_binding": binding(owner=legacy.owner_id, shop=legacy.printify_shop_id + 1),
            }
        )
    assert (
        execution_record_fingerprint(
            "provider_authority", _authority(store_binding=binding(owner=_authority().owner_id))
        )
        != _authority().fingerprint
    )
