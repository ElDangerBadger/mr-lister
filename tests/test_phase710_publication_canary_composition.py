"""Offline composition checks for the exact-bound Phase 7 canary."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import mr_lister.cloud.phase7_canary_composition as composition
from mr_lister.publication.canary_runtime import (
    PublicationCanaryMode,
    PublicationCanaryRuntime,
    build_publication_canary_binding,
)
from mr_lister.publication.execution_fingerprints import execution_record_fingerprint
from mr_lister.publication.execution_models import (
    PublicationProviderAuditCategory,
    PublicationProviderAuditDecision,
    PublicationProviderAuditRecord,
)
from mr_lister.publication.provider_boundary import RedirectSafePublicationTransport
from mr_lister.publication.provider_credentials import BoundPublicationProviderCredential
from tests.test_phase71_publication_store import OWNER_ID
from tests.test_phase72_publication_execution import Harness

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = (ROOT / "config/product_profiles/gildan_64000_swiftpod.json").resolve()
PROFILE_FINGERPRINT = "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"
REGION = "us-west-2"
ACCOUNT_ID = "123456789012"
SECRET_ARN = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT_ID}:secret:mr-lister/dev/printify-Ab12Cd"
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


class InertDynamoClient:
    def __init__(self) -> None:
        self.operations: list[str] = []

    def get_item(self, **_values: object) -> object:
        self.operations.append("get_item")
        raise AssertionError("composition must not read state")

    def query(self, **_values: object) -> object:
        self.operations.append("query")
        raise AssertionError("composition must not query state")

    def transact_write_items(self, **_values: object) -> object:
        self.operations.append("transact_write_items")
        raise AssertionError("composition must not write state")


class InertSecretsClient:
    def __init__(self, response: object | None = None) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def get_secret_value(self, **values: object) -> object:
        self.calls.append(values)
        if self.response is None:
            raise AssertionError("composition must not resolve a secret")
        return self.response


class InertCredentials:
    def __init__(self) -> None:
        self.calls = 0

    def resolve_exact(self, **_values: object) -> object:
        self.calls += 1
        raise AssertionError("composition must not resolve credentials")


class InertTransport:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, **_values: object) -> object:
        self.calls += 1
        raise AssertionError("composition must not call the provider")


class RecordingClientFactory:
    def __init__(self) -> None:
        self.dynamodb = InertDynamoClient()
        self.secrets = InertSecretsClient()
        self.calls: list[tuple[str, str]] = []

    def __call__(self, service_name: str, *, region_name: str) -> object:
        self.calls.append((service_name, region_name))
        return self.dynamodb if service_name == "dynamodb" else self.secrets


def _binding(harness: Harness):  # type: ignore[no-untyped-def]
    return build_publication_canary_binding(
        harness.authority,
        mode=PublicationCanaryMode.READ_ONLY_PREFLIGHT,
    )


def _environment(binding: object) -> dict[str, object]:
    return {
        "AWS_REGION": REGION,
        "MR_LISTER_ENVIRONMENT": "dev",
        "MR_LISTER_AWS_ACCOUNT_ID": ACCOUNT_ID,
        "MR_LISTER_STATE_TABLE": "mr-lister-phase6-dev",
        "MR_LISTER_RELEASE_FINGERPRINT": "b" * 64,
        "MR_LISTER_PHASE7_CANARY_RELEASE_FINGERPRINT": "c" * 64,
        "MR_LISTER_PHASE7_CANARY_BINDING_FINGERPRINT": binding.fingerprint,
        "MR_LISTER_PHASE7_CANARY_MODE": binding.mode.value,
        "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": PROFILE_FINGERPRINT,
        "MR_LISTER_PRODUCT_PROFILE_PATH": str(PROFILE_PATH),
        "MR_LISTER_PRINTIFY_SECRET_ARN": SECRET_ARN,
        "MR_LISTER_PHASE7_SCAFFOLD_ONLY": "false",
        "MR_LISTER_PHASE7_QUERY_ENABLED": "false",
        "MR_LISTER_PHASE7_REQUEST_ENABLED": "false",
        "MR_LISTER_PHASE7_PUBLICATION_ENABLED": "false",
        "MR_LISTER_PHASE7_CANARY_ENABLED": "true",
    }


def test_private_configuration_needs_no_cognito_and_pins_the_packaged_binding() -> None:
    harness = Harness()
    binding = _binding(harness)
    environment = _environment(binding)

    assert not any("COGNITO" in name for name in environment)
    configured = composition.load_phase7_canary_configuration(
        environment,
        binding=binding,
    )

    assert configured.binding == binding
    assert configured.application_release_fingerprint == binding.release_manifest_fingerprint
    assert configured.secret_arn == SECRET_ARN
    assert configured.profile.exact.fingerprint == PROFILE_FINGERPRINT


def test_canary_joins_shared_worker_graph_without_state_secret_or_wire_io() -> None:
    harness = Harness()
    binding = _binding(harness)
    configured = composition.load_phase7_canary_configuration(
        _environment(binding),
        binding=binding,
    )
    dynamodb = InertDynamoClient()
    credentials = InertCredentials()
    transport = InertTransport()

    runtime = composition.compose_publication_canary_runtime(
        configured,
        dynamodb=dynamodb,
        credentials=credentials,  # type: ignore[arg-type]
        transport=transport,  # type: ignore[arg-type]
        rejected_audit_writer=lambda _record: None,
        clock=lambda: NOW,
    )

    assert isinstance(runtime, PublicationCanaryRuntime)
    assert runtime._binding == binding
    assert runtime._coordinator._store._client is dynamodb
    assert (
        runtime._coordinator._boundary_factory._user_agent == composition.PHASE7_CANARY_USER_AGENT
    )
    assert dynamodb.operations == []
    assert credentials.calls == transport.calls == 0


def test_handler_build_creates_only_regional_state_and_secret_clients_without_io() -> None:
    harness = Harness()
    binding = _binding(harness)
    factory = RecordingClientFactory()

    handler = composition.build_publication_canary_handler(
        _environment(binding),
        binding=binding,
        client_factory=factory,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    assert factory.calls == [("dynamodb", REGION), ("secretsmanager", REGION)]
    assert factory.dynamodb.operations == []
    assert factory.secrets.calls == []
    assert isinstance(
        handler._runtime._coordinator._boundary_factory._transport,
        RedirectSafePublicationTransport,
    )


def test_narrow_credential_authority_reads_the_exact_secret_afresh_per_resolution() -> None:
    harness = Harness()
    harness.dispatch_and_reconstruct()
    authority = harness.authority.provider_authority
    assert authority is not None
    secret = InertSecretsClient(
        {
            "ARN": SECRET_ARN,
            "VersionStages": ["AWSCURRENT"],
            "SecretString": json.dumps(
                {
                    "schema_version": "phase6-printify-owner-v1",
                    "owner_id": OWNER_ID,
                    "shop_id": authority.printify_shop_id,
                    "api_token": "token-one",
                }
            ),
        }
    )
    credentials = composition.FreshCanaryPublicationProviderCredentialAuthority(
        client=secret,
        secret_arn=SECRET_ARN,
    )

    first = credentials.resolve_exact(authority=authority)
    second = credentials.resolve_exact(authority=authority)

    assert isinstance(first, BoundPublicationProviderCredential)
    assert isinstance(second, BoundPublicationProviderCredential)
    assert secret.calls == [{"SecretId": SECRET_ARN}, {"SecretId": SECRET_ARN}]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MR_LISTER_PHASE7_PUBLICATION_ENABLED", "true"),
        ("MR_LISTER_PHASE7_CANARY_ENABLED", "false"),
        ("MR_LISTER_PHASE7_CANARY_MODE", "publish_once"),
        ("MR_LISTER_AWS_ACCOUNT_ID", "000000000000"),
        (
            "MR_LISTER_PRINTIFY_SECRET_ARN",
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:mr-lister/x-Ab12Cd",
        ),
    ],
)
def test_drifting_or_broad_configuration_fails_before_client_construction(
    name: str,
    value: object,
) -> None:
    harness = Harness()
    binding = _binding(harness)
    environment = _environment(binding)
    environment[name] = value
    factory = RecordingClientFactory()

    with pytest.raises(composition.Phase7CanaryConfigurationError) as captured:
        composition.build_publication_canary_handler(
            environment,
            binding=binding,
            client_factory=factory,  # type: ignore[arg-type]
        )

    assert str(captured.value) == "Phase 7 canary configuration is invalid"
    assert factory.calls == []


def _audit_record(
    decision: PublicationProviderAuditDecision,
) -> PublicationProviderAuditRecord:
    if decision is PublicationProviderAuditDecision.REJECTED:
        values = {
            "decision": decision,
            "method_category": "FORBIDDEN",
            "route_template": "forbidden_operation",
            "category": PublicationProviderAuditCategory.FORBIDDEN_ROUTE,
        }
    else:
        values = {
            "decision": decision,
            "method_category": "GET",
            "route_template": "/v1/shops.json",
            "category": PublicationProviderAuditCategory.SHOP_GET_ALLOWED,
        }
    return PublicationProviderAuditRecord(
        **values,
        fingerprint=execution_record_fingerprint("provider_audit_record", values),
    )


def test_default_rejected_audit_writer_logs_only_the_closed_sanitized_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("ERROR", logger="mr_lister.phase7.canary.rejected_audit")

    composition.write_sanitized_rejected_audit(
        _audit_record(PublicationProviderAuditDecision.REJECTED)
    )

    payload = json.loads(caplog.records[-1].getMessage())
    assert payload["event"] == "phase7_canary_provider_rejected"
    assert payload["decision"] == "rejected"
    assert set(payload) == {
        "category",
        "decision",
        "event",
        "fingerprint",
        "method_category",
        "route_template",
    }
    assert OWNER_ID not in caplog.text
    with pytest.raises(RuntimeError, match="rejected-audit logger is unavailable"):
        composition.write_sanitized_rejected_audit(
            _audit_record(PublicationProviderAuditDecision.ALLOWED)
        )
