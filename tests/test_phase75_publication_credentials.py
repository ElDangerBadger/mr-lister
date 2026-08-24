"""Offline Phase 7.5 exact credential authority and pre-claim ordering gates."""

from __future__ import annotations

import pickle
from dataclasses import dataclass
from io import StringIO

import pytest
from pydantic import SecretStr, ValidationError

from mr_lister.cloud.phase7_provider_credentials import (
    ProductionPublicationProviderCredentialAuthority,
    build_phase7_publication_provider_credential_authority,
)
from mr_lister.production.provider_resources import OwnerPrintifyConnection
from mr_lister.publication.application import DurablePublicationPreCallGuard
from mr_lister.publication.contract import PublicationPermitState, PublicationState
from mr_lister.publication.execution_commands import RecordPublicationPostOutcomeCommand
from mr_lister.publication.execution_fingerprints import execution_record_fingerprint
from mr_lister.publication.execution_models import PublicationProviderAuthority
from mr_lister.publication.provider_boundary import (
    PrintifyPublicationBoundary,
    PublicationProviderInputError,
    StagedPrintifyPublicationBoundary,
)
from mr_lister.publication.provider_coordinator import (
    PublicationProviderCoordinator,
    PublicationProviderCoordinatorAction,
    PublicationProviderCoordinatorError,
)
from mr_lister.publication.provider_credentials import (
    BoundPublicationProviderCredential,
    OwnerBoundPrintifyCredential,
    PublicationProviderCredentialBinding,
    PublicationProviderCredentialError,
    build_publication_provider_credential_binding,
    issue_bound_publication_provider_credential,
)
from tests.test_phase6_provider_secrets import (
    SECRET_ARN,
    RecordingSecretsManager,
    _response,
    _secret_string,
)
from tests.test_phase71_publication_service import ProfileAuthority
from tests.test_phase71_publication_service import _authority as request_authority
from tests.test_phase71_publication_store import OWNER_ID
from tests.test_phase72_publication_execution import Harness
from tests.test_phase72_publication_provider_boundary import (
    TOKEN,
    MemoryAudit,
    ScriptedTransport,
    _authority,
)
from tests.test_phase73_publication_provider_boundary import (
    DurableAuditSink,
    _reader,
)

GENERIC_ERROR = "Publication provider credential is unavailable"


def _credential(
    authority: PublicationProviderAuthority | None = None,
    *,
    token: str = TOKEN,
) -> BoundPublicationProviderCredential:
    return issue_bound_publication_provider_credential(
        authority=authority or _authority(),
        bearer_token=SecretStr(token),
    )


def _assert_generic(error: pytest.ExceptionInfo[PublicationProviderCredentialError]) -> None:
    assert str(error.value) == GENERIC_ERROR
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert TOKEN not in str(error.value)


def test_binding_is_content_bound_to_owner_shop_aggregate_snapshot_and_authority() -> None:
    authority = _authority()
    binding = build_publication_provider_credential_binding(authority)

    assert binding.owner_id == authority.owner_id
    assert binding.printify_shop_id == authority.printify_shop_id
    assert binding.aggregate_id == authority.aggregate_id
    assert binding.snapshot_id == authority.snapshot_id
    assert binding.snapshot_fingerprint == authority.snapshot_fingerprint
    assert binding.provider_authority_id == authority.provider_authority_id
    assert binding.provider_authority_fingerprint == authority.fingerprint
    assert binding.fingerprint == execution_record_fingerprint(
        "provider_credential_binding",
        binding,
    )
    assert TOKEN not in binding.model_dump_json()

    with pytest.raises(ValidationError):
        PublicationProviderCredentialBinding.model_validate(
            {
                **binding.model_dump(mode="python"),
                "aggregate_id": "another_aggregate",
            }
        )


def test_opaque_credential_and_lower_level_credential_never_serialize_or_log_token() -> None:
    authority = _authority()
    credential = _credential(authority)
    lower = credential.for_authority(authority)
    stream = StringIO()
    print(credential, credential.binding.model_dump_json(), lower, file=stream)

    assert TOKEN not in repr(credential)
    assert TOKEN not in stream.getvalue()
    assert TOKEN not in lower.model_dump_json()
    assert "bearer_token" not in lower.model_dump()
    assert lower.owner_id == authority.owner_id
    assert lower.printify_shop_id == authority.printify_shop_id
    assert lower.bearer_token.get_secret_value() == TOKEN
    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(credential)
    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(lower)
    with pytest.raises(AttributeError, match="immutable"):
        credential._binding = credential.binding  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_id": "9" * 64},
        {"printify_shop_id": 43},
        {"aggregate_id": "another_aggregate"},
        {"snapshot_id": "another_snapshot"},
        {"snapshot_fingerprint": "d" * 64},
        {"provider_authority_id": "another_provider_authority"},
    ],
)
def test_credential_cannot_cross_any_owner_shop_aggregate_or_snapshot_binding(
    changes: dict[str, object],
) -> None:
    credential = _credential(_authority())

    with pytest.raises(PublicationProviderCredentialError) as captured:
        credential.for_authority(_authority(**changes))

    _assert_generic(captured)


def test_model_copy_forged_binding_is_deep_reparsed_before_token_capability_exists() -> None:
    binding = build_publication_provider_credential_binding(_authority())
    forged = binding.model_copy(update={"printify_shop_id": 43})

    with pytest.raises(PublicationProviderCredentialError) as captured:
        BoundPublicationProviderCredential(
            binding=forged,
            bearer_token=SecretStr(TOKEN),
        )

    _assert_generic(captured)


@pytest.mark.parametrize(
    "token",
    ["", " token", "token ", "token\nvalue", "töken", "x" * 4_097],
)
def test_malformed_token_never_enters_a_bound_capability(token: str) -> None:
    with pytest.raises(PublicationProviderCredentialError) as captured:
        _credential(token=token)

    _assert_generic(captured)
    if token:
        assert token not in str(captured.value)


def test_lower_level_invalid_token_error_retains_no_secret_material_or_chain() -> None:
    token = f"invalid token {TOKEN}"

    with pytest.raises(PublicationProviderCredentialError) as captured:
        OwnerBoundPrintifyCredential(
            owner_id=_authority().owner_id,
            printify_shop_id=42,
            bearer_token=token,
        )

    _assert_generic(captured)
    assert token not in str(captured.value)


def test_lower_boundary_rejects_wrong_shop_before_any_wire_call() -> None:
    authority = _authority()
    transport = ScriptedTransport([])

    with pytest.raises(PublicationProviderInputError, match="owner/shop authority"):
        PrintifyPublicationBoundary(
            authority=authority,
            credential=OwnerBoundPrintifyCredential(
                owner_id=authority.owner_id,
                printify_shop_id=authority.printify_shop_id + 1,
                bearer_token=TOKEN,
            ),
            transport=transport,
            audit_sink=MemoryAudit(),
        )

    assert transport.calls == []


def test_staged_boundary_rejects_legacy_owner_shop_credential_without_graph_binding() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    authority = harness.authority.provider_authority
    assert authority is not None
    transport = ScriptedTransport([])

    with pytest.raises(PublicationProviderInputError, match="credential authority"):
        StagedPrintifyPublicationBoundary(
            execution_authority=harness.authority,
            credential=OwnerBoundPrintifyCredential(  # type: ignore[arg-type]
                owner_id=authority.owner_id,
                printify_shop_id=authority.printify_shop_id,
                bearer_token=TOKEN,
            ),
            transport=transport,
            audit_sink=DurableAuditSink(harness, []),
            evidence_store=harness.store,
            authority_reader=_reader(harness),
            clock=harness.clock,
        )

    assert transport.calls == []


@dataclass
class RotatingConnections:
    connections: list[OwnerPrintifyConnection | Exception]

    def __post_init__(self) -> None:
        self.calls: list[str] = []

    def resolve(self, *, owner_id: str) -> OwnerPrintifyConnection:
        self.calls.append(owner_id)
        value = self.connections.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _connection(
    authority: PublicationProviderAuthority,
    *,
    owner_id: str | None = None,
    shop_id: int | None = None,
    token: str = TOKEN,
) -> OwnerPrintifyConnection:
    return OwnerPrintifyConnection(
        owner_id=owner_id or authority.owner_id,
        shop_id=shop_id or authority.printify_shop_id,
        api_token=token,
    )


def test_production_adapter_resolves_fresh_and_observes_secret_rotation() -> None:
    authority = _authority()
    resolver = RotatingConnections(
        [
            _connection(authority, token="credential-before-rotation"),
            _connection(authority, token="credential-after-rotation"),
        ]
    )
    credentials = ProductionPublicationProviderCredentialAuthority(connections=resolver)

    first = credentials.resolve_exact(authority=authority).for_authority(authority)
    second = credentials.resolve_exact(authority=authority).for_authority(authority)

    assert first.bearer_token.get_secret_value() == "credential-before-rotation"
    assert second.bearer_token.get_secret_value() == "credential-after-rotation"
    assert resolver.calls == [authority.owner_id, authority.owner_id]


def test_production_adapter_prepares_from_one_deep_execution_authority_before_claim() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    authority = harness.authority.provider_authority
    assert authority is not None
    resolver = RotatingConnections([_connection(authority)])
    credentials = ProductionPublicationProviderCredentialAuthority(connections=resolver)

    prepared = credentials.prepare_credential(execution_authority=harness.authority)

    assert prepared.binding == build_publication_provider_credential_binding(authority)
    assert harness.authority.attempt.shop_get_call_count == 0
    assert harness.authority.attempt.product_get_call_count == 0
    assert harness.authority.attempt.publish_post_call_count == 0
    assert resolver.calls == [authority.owner_id]


def test_production_adapter_rejects_model_copy_authority_before_secret_resolution() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    authority = harness.authority.provider_authority
    assert authority is not None
    resolver = RotatingConnections([_connection(authority)])
    credentials = ProductionPublicationProviderCredentialAuthority(connections=resolver)
    forged = harness.authority.model_copy(
        update={
            "provider_authority": authority.model_copy(
                update={"printify_shop_id": authority.printify_shop_id + 1}
            )
        }
    )

    with pytest.raises(PublicationProviderCredentialError) as captured:
        credentials.prepare_credential(execution_authority=forged)

    _assert_generic(captured)
    assert resolver.calls == []


@pytest.mark.parametrize("mismatch", ["owner", "shop", "malformed_shop", "failure"])
def test_resolver_owner_shop_or_dependency_failure_is_one_value_free_error(
    mismatch: str,
) -> None:
    authority = _authority()
    if mismatch == "owner":
        value: OwnerPrintifyConnection | Exception = _connection(
            authority,
            owner_id="9" * 64,
        )
    elif mismatch == "shop":
        value = _connection(authority, shop_id=43)
    elif mismatch == "malformed_shop":
        value = _connection(authority).model_copy(update={"shop_id": True})
    else:
        value = RuntimeError(f"dependency leaked {TOKEN}")
    credentials = ProductionPublicationProviderCredentialAuthority(
        connections=RotatingConnections([value])
    )

    with pytest.raises(PublicationProviderCredentialError) as captured:
        credentials.resolve_exact(authority=authority)

    _assert_generic(captured)


def test_factory_reuses_exact_production_secret_resolver_without_sdk_or_cache() -> None:
    authority = _authority()
    token = "credential-from-secret-manager"
    client = RecordingSecretsManager(
        [
            _response(
                _secret_string(
                    owner_id=authority.owner_id,
                    shop_id=authority.printify_shop_id,
                    token=token,
                )
            )
        ]
    )
    credentials = build_phase7_publication_provider_credential_authority(
        client=client,
        secret_arn=SECRET_ARN,
    )

    lower = credentials.resolve_exact(authority=authority).for_authority(authority)

    assert lower.bearer_token.get_secret_value() == token
    assert client.requests == [{"SecretId": SECRET_ARN}]


def test_factory_rejects_secret_configuration_without_read_or_value_disclosure() -> None:
    client = RecordingSecretsManager([])
    secret_arn = "arn:aws:secretsmanager:us-west-2:123456789012:secret:*"

    with pytest.raises(PublicationProviderCredentialError) as captured:
        build_phase7_publication_provider_credential_authority(
            client=client,
            secret_arn=secret_arn,
        )

    assert str(captured.value) == "Publication provider credential configuration is invalid"
    assert secret_arn not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert client.requests == []


class FailingPrepareFactory:
    def __init__(self) -> None:
        self.prepare_calls = 0
        self.boundary_calls = 0

    def prepare_credential(
        self,
        *,
        execution_authority: object,
    ) -> BoundPublicationProviderCredential:
        del execution_authority
        self.prepare_calls += 1
        raise RuntimeError(f"private resolver failure {TOKEN}")

    def __call__(self, **_values: object) -> object:
        self.boundary_calls += 1
        raise AssertionError("Boundary construction must follow a prepared credential")


def _coordinator(harness: Harness, factory: object) -> PublicationProviderCoordinator:
    _, exact = request_authority()
    return PublicationProviderCoordinator(
        store=harness.store,
        execution=harness.service,
        boundary_factory=factory,  # type: ignore[arg-type]
        pre_call_guard=DurablePublicationPreCallGuard(
            store=harness.store,
            profiles=ProfileAuthority(exact),
            eligibility=harness.profile_eligibility,  # type: ignore[arg-type]
            release_manifest_fingerprint="b" * 64,
        ),
        clock=harness.clock,
    )


def _accepted_post(harness: Harness) -> None:
    _result, post_claim = harness.claim_publish()
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            harness.next_operation("accepted"),
            evidence=harness.publish_evidence(post_claim, accepted=True),
        )
    )
    harness.clock.tick()


@pytest.mark.parametrize("operation", ["shop", "product_preflight", "publish", "poll"])
def test_credential_prepare_failure_precedes_every_durable_call_claim(operation: str) -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    if operation == "product_preflight":
        _result, shop_claim = harness.claim_shop()
        harness.stage_evidence(harness.shop_evidence(shop_claim))
    elif operation in {"publish", "poll"}:
        harness.complete_preflight()
        if operation == "poll":
            _accepted_post(harness)
    before = harness.authority
    factory = FailingPrepareFactory()

    with pytest.raises(PublicationProviderCoordinatorError) as captured:
        _coordinator(harness, factory).advance(
            owner_id=OWNER_ID,
            aggregate_id=harness.aggregate_id,
        )

    after = harness.authority
    assert str(captured.value) == GENERIC_ERROR
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert TOKEN not in str(captured.value)
    assert after.attempt.shop_get_call_count == before.attempt.shop_get_call_count
    assert after.attempt.product_get_call_count == before.attempt.product_get_call_count
    assert after.attempt.publish_post_call_count == before.attempt.publish_post_call_count
    assert after.permit.status is before.permit.status
    assert after.aggregate.record_version == before.aggregate.record_version
    assert factory.prepare_calls == 1
    assert factory.boundary_calls == 0
    if operation == "publish":
        assert after.permit.status is PublicationPermitState.AVAILABLE
        assert after.attempt.publish_post_call_count == 0


class PostClaimDriftFactory:
    def __init__(self, harness: Harness) -> None:
        self.harness = harness
        self.transport = ScriptedTransport([])

    @staticmethod
    def prepare_credential(*, execution_authority):  # type: ignore[no-untyped-def]
        authority = execution_authority.provider_authority
        assert authority is not None
        return _credential(authority)

    def __call__(self, *, execution_authority, credential):  # type: ignore[no-untyped-def]
        authority = execution_authority.provider_authority
        assert authority is not None
        values = {
            **authority.model_dump(
                mode="python",
                exclude={"contract_version", "fingerprint"},
            ),
            "provider_authority_id": "drifted_provider_authority",
        }
        drifted_provider = PublicationProviderAuthority(
            **values,
            fingerprint=execution_record_fingerprint("provider_authority", values),
        )
        drifted_execution = execution_authority.model_copy(
            update={"provider_authority": drifted_provider}
        )
        return StagedPrintifyPublicationBoundary(
            execution_authority=drifted_execution,
            credential=credential,
            transport=self.transport,
            audit_sink=DurableAuditSink(self.harness, []),
            evidence_store=self.harness.store,
            authority_reader=_reader(self.harness),
            clock=self.harness.clock,
        )


def test_post_claim_authority_drift_is_rejected_before_wire_or_evidence_stage() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    factory = PostClaimDriftFactory(harness)

    with pytest.raises(PublicationProviderInputError, match="credential authority"):
        _coordinator(harness, factory).advance(
            owner_id=OWNER_ID,
            aggregate_id=harness.aggregate_id,
        )

    assert harness.authority.attempt.shop_get_call_count == 1
    assert factory.transport.calls == []
    assert (
        harness.store.list_unconsumed_provider_evidence(
            OWNER_ID,
            harness.aggregate_id,
        )
        == ()
    )


class CountingExplodingFactory:
    def __init__(self) -> None:
        self.prepare_calls = 0
        self.boundary_calls = 0

    def prepare_credential(self, *, execution_authority):  # type: ignore[no-untyped-def]
        self.prepare_calls += 1
        authority = execution_authority.provider_authority
        assert authority is not None
        return _credential(authority)

    def __call__(self, **_values: object) -> object:
        self.boundary_calls += 1
        raise RuntimeError("synthetic crash before provider wire")


def test_restart_after_consumed_publish_claim_does_not_resolve_or_reuse_credential() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    factory = CountingExplodingFactory()
    coordinator = _coordinator(harness, factory)

    with pytest.raises(RuntimeError, match="synthetic crash"):
        coordinator.advance(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert factory.prepare_calls == 1
    assert factory.boundary_calls == 1
    assert harness.authority.permit.status is PublicationPermitState.CONSUMED
    assert harness.authority.attempt.publish_post_call_count == 1

    recovered = coordinator.advance(owner_id=OWNER_ID, aggregate_id=harness.aggregate_id)

    assert recovered.action is (
        PublicationProviderCoordinatorAction.RECOVERED_CONSUMED_PUBLISH_CLAIM
    )
    assert recovered.aggregate_state is PublicationState.PUBLICATION_RECONCILING
    assert factory.prepare_calls == 1
    assert factory.boundary_calls == 1
