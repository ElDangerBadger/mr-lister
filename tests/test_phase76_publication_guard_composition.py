"""Runtime composition and exact read-boundary tests for the Phase 7.6 guard."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from test_phase71_publication_service import _authority
from test_phase71_publication_store import OWNER_ID
from test_phase72_publication_execution import Harness
from test_phase73_publication_execution_dynamodb import (
    ExecutionMemoryDynamoClient,
    _dynamo_harness,
)

from mr_lister.cloud.phase7_guard_composition import (
    Phase7GuardConfigurationError,
    _ReadOnlyDynamoClient,
    _ReadOnlyPublicationGuardStore,
    build_publication_guard_handler,
    load_phase7_guard_configuration,
)
from mr_lister.publication.execution_commands import (
    RecordPublicationPostOutcomeCommand,
    RecordPublicationProductObservationCommand,
)
from mr_lister.publication.execution_models import PublicationCallPurpose

APPLICATION_RELEASE_FINGERPRINT = "b" * 64
GUARD_RELEASE_FINGERPRINT = "c" * 64
STATE_TABLE = "mr-lister-phase6-test"


class _Factory:
    def __init__(self, client: object) -> None:
        self.client = client
        self.calls: list[tuple[str, str]] = []

    def __call__(self, service_name: str, *, region_name: str) -> object:
        self.calls.append((service_name, region_name))
        assert service_name == "dynamodb"
        return self.client


def _environment(tmp_path: Path) -> dict[str, str]:
    _source, exact = _authority()
    profile_path = (tmp_path / f"{exact.profile.profile_id}.json").resolve()
    profile_path.write_text(exact.profile.model_dump_json(), encoding="utf-8")
    return {
        "AWS_REGION": "us-west-2",
        "MR_LISTER_STATE_TABLE": STATE_TABLE,
        "MR_LISTER_PHASE7_GUARD_RELEASE_FINGERPRINT": GUARD_RELEASE_FINGERPRINT,
        "MR_LISTER_RELEASE_FINGERPRINT": APPLICATION_RELEASE_FINGERPRINT,
        "MR_LISTER_PHASE7_SCAFFOLD_ONLY": "false",
        "MR_LISTER_PHASE7_GUARD_ENABLED": "true",
        "MR_LISTER_PHASE7_GUARD_MODE": "approval_version_read_only",
        "MR_LISTER_PHASE7_QUERY_ENABLED": "false",
        "MR_LISTER_PHASE7_REQUEST_ENABLED": "false",
        "MR_LISTER_PHASE7_PUBLICATION_ENABLED": "false",
        "MR_LISTER_PRODUCT_PROFILE_ID": exact.profile.profile_id,
        "MR_LISTER_PRODUCT_PROFILE_VERSION": str(exact.profile.profile_version),
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": exact.fingerprint,
        "MR_LISTER_PRODUCT_PROFILE_PATH": profile_path.as_posix(),
    }


def _verify(handler: Any, harness: Harness) -> dict[str, Any]:
    return handler(
        {
            "operation": "verify_authority",
            "owner_id": OWNER_ID,
            "aggregate_id": harness.aggregate_id,
        }
    )


def _evolved_dynamo_harness() -> tuple[Harness, ExecutionMemoryDynamoClient]:
    harness, _store, client = _dynamo_harness()
    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    _result, publish_claim = harness.claim_publish()
    publish_evidence = harness.publish_evidence(publish_claim, accepted=True)
    harness.clock.tick()
    harness.service.record_post_outcome(
        harness.command(
            RecordPublicationPostOutcomeCommand,
            "phase76_post_outcome",
            evidence=publish_evidence,
        )
    )
    harness.clock.tick()
    _result, product_claim = harness.claim_product(PublicationCallPurpose.VERIFICATION)
    product_evidence = harness.product_evidence(product_claim)
    harness.clock.tick()
    harness.service.record_product_observation(
        harness.command(
            RecordPublicationProductObservationCommand,
            "phase76_product_observation",
            evidence=product_evidence,
        )
    )
    return harness, client


def _move_first_row(
    client: ExecutionMemoryDynamoClient,
    *,
    aggregate_id: str,
    prefix: str,
) -> None:
    partition_key = f"PUBLICATION#{aggregate_id}"
    old_key = next(
        key for key in client.items if key[0] == partition_key and key[1].startswith(prefix)
    )
    row = copy.deepcopy(client.items.pop(old_key))
    moved_sort_key = f"{prefix}physically_moved"
    row["SK"] = {"S": moved_sort_key}
    client.items[(partition_key, moved_sort_key)] = row


def test_phase71_committed_graph_attests_current_and_status_constructs_no_client(
    tmp_path: Path,
) -> None:
    harness, _store, client = _dynamo_harness()
    factory = _Factory(client)
    handler = build_publication_guard_handler(_environment(tmp_path), client_factory=factory)
    configuration = load_phase7_guard_configuration(_environment(tmp_path))

    assert configuration.guard_release_fingerprint == GUARD_RELEASE_FINGERPRINT
    assert configuration.application_release_fingerprint == APPLICATION_RELEASE_FINGERPRINT
    assert configuration.eligibility.release_manifest_fingerprint == APPLICATION_RELEASE_FINGERPRINT

    status = handler({"operation": "status"})
    assert status["outcome"] == "sealed_configuration"
    assert status["publication_enabled"] is False
    assert status["provider_calls_authorized"] == 0
    assert factory.calls == []

    result = _verify(handler, harness)
    assert result["outcome"] == "authority_current"
    assert result["approval_authority_current"] is True
    assert factory.calls == [("dynamodb", "us-west-2")]
    assert OWNER_ID not in str(result)
    assert harness.aggregate_id not in str(result)


@pytest.mark.parametrize(
    "prefix",
    ["SNAPSHOT#", "CALL_CLAIM#", "PROVIDER_AUDIT#", "PRODUCT_OBSERVATION#"],
)
def test_payload_moved_to_another_physical_sort_key_is_rejected(
    tmp_path: Path,
    prefix: str,
) -> None:
    harness, client = _evolved_dynamo_harness()
    _move_first_row(client, aggregate_id=harness.aggregate_id, prefix=prefix)
    handler = build_publication_guard_handler(
        _environment(tmp_path),
        client_factory=_Factory(client),
    )

    result = _verify(handler, harness)

    assert result["outcome"] == "authority_rejected"
    assert result["approval_authority_current"] is False
    assert OWNER_ID not in str(result)
    assert harness.aggregate_id not in str(result)


def test_unknown_or_extra_publication_row_is_rejected(tmp_path: Path) -> None:
    harness, _store, client = _dynamo_harness()
    partition_key = f"PUBLICATION#{harness.aggregate_id}"
    client.items[(partition_key, "UNKNOWN#closed_inventory")] = {
        "PK": {"S": partition_key},
        "SK": {"S": "UNKNOWN#closed_inventory"},
        "entity_type": {"S": "PUBLICATION_UNKNOWN"},
        "contract_version": {"S": "7.0.1"},
        "payload": {"S": "{}"},
    }
    handler = build_publication_guard_handler(
        _environment(tmp_path),
        client_factory=_Factory(client),
    )

    assert _verify(handler, harness)["outcome"] == "authority_rejected"


def test_full_top_level_envelope_drift_is_rejected(tmp_path: Path) -> None:
    harness, _store, client = _dynamo_harness()
    partition_key = f"PUBLICATION#{harness.aggregate_id}"
    snapshot_key = next(
        key for key in client.items if key[0] == partition_key and key[1].startswith("SNAPSHOT#")
    )
    client.items[snapshot_key] = {
        **client.items[snapshot_key],
        "contract_version": {"S": "7.0.0"},
        "unexpected": {"S": "authority expansion"},
    }
    handler = build_publication_guard_handler(
        _environment(tmp_path),
        client_factory=_Factory(client),
    )

    assert _verify(handler, harness)["outcome"] == "authority_rejected"


def test_owner_aggregate_and_phase6_source_drift_collapse_to_identifier_free_rejection(
    tmp_path: Path,
) -> None:
    harness, _store, client = _dynamo_harness()
    handler = build_publication_guard_handler(
        _environment(tmp_path),
        client_factory=_Factory(client),
    )
    foreign_owner = "f" * 64
    results = [
        handler(
            {
                "operation": "verify_authority",
                "owner_id": foreign_owner,
                "aggregate_id": harness.aggregate_id,
            }
        ),
        handler(
            {
                "operation": "verify_authority",
                "owner_id": OWNER_ID,
                "aggregate_id": "aggregate_missing_phase76",
            }
        ),
    ]

    source_key = (f"JOB#{harness.transaction.updated_job.job_id}", "SOURCE")
    client.items[source_key] = {
        **client.items[source_key],
        "payload": {"S": client.items[source_key]["payload"]["S"] + " "},
    }
    results.append(_verify(handler, harness))

    assert all(result["outcome"] == "authority_rejected" for result in results)
    assert all(result["approval_authority_current"] is False for result in results)
    assert all(OWNER_ID not in str(result) for result in results)
    assert all(harness.aggregate_id not in str(result) for result in results)
    assert all(foreign_owner not in str(result) for result in results)


def test_944th_partition_row_exceeds_the_frozen_943_item_bound(tmp_path: Path) -> None:
    harness, _store, client = _dynamo_harness()
    partition_key = f"PUBLICATION#{harness.aggregate_id}"
    current = sum(1 for key in client.items if key[0] == partition_key)
    for index in range(944 - current):
        sort_key = f"UNKNOWN#{index:020d}"
        client.items[(partition_key, sort_key)] = {
            "PK": {"S": partition_key},
            "SK": {"S": sort_key},
            "entity_type": {"S": "PUBLICATION_UNKNOWN"},
            "contract_version": {"S": "7.0.1"},
            "payload": {"S": "{}"},
        }
    reader = _ReadOnlyPublicationGuardStore(
        client=_ReadOnlyDynamoClient(client),
        table_name=STATE_TABLE,
    )

    with pytest.raises(ValueError, match="authority is invalid"):
        reader._query_partition(harness.aggregate_id)
    assert client.query_requests[-1]["Limit"] == 944


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MR_LISTER_PHASE7_SCAFFOLD_ONLY", "true"),
        ("MR_LISTER_PHASE7_GUARD_ENABLED", "false"),
        ("MR_LISTER_PHASE7_QUERY_ENABLED", "true"),
        ("MR_LISTER_PHASE7_REQUEST_ENABLED", "true"),
        ("MR_LISTER_PHASE7_PUBLICATION_ENABLED", "true"),
        ("MR_LISTER_RELEASE_FINGERPRINT", "0" * 64),
    ],
)
def test_configuration_accepts_only_the_exact_active_read_tuple(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    environment = _environment(tmp_path)
    environment[name] = value

    with pytest.raises(Phase7GuardConfigurationError) as captured:
        load_phase7_guard_configuration(environment)

    assert str(captured.value) == "Phase 7 approval guard configuration is invalid"
    assert captured.value.__cause__ is None


def test_injected_dynamo_surface_retains_only_get_and_query() -> None:
    client = ExecutionMemoryDynamoClient()
    reader = _ReadOnlyDynamoClient(client)

    assert callable(reader.get_item)
    assert callable(reader.query)
    assert not hasattr(reader, "transact_write_items")
    assert not hasattr(reader, "put_item")
    assert not hasattr(reader, "delete_item")
