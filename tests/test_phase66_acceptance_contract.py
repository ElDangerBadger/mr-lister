from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from mr_lister.acceptance.phase6 import (
    PHASE66_CONTRACT_VERSION,
    AcceptanceEvidenceClass,
    Phase66AcceptanceManifest,
    evidence_record_json_schema,
    phase66_acceptance_manifest,
    phase66_manifest_digest,
    validate_phase66_evidence,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "contracts" / "acceptance" / "phase6.6.manifest.json"
SCHEMA_PATH = ROOT / "contracts" / "acceptance" / "phase6.6.evidence.schema.json"
EXPECTED_MANIFEST_DIGEST = "84851fe2ed78072d077cc5e642d0e222619b9a7226367219b536b7e2aaac7d73"


def _digest(character: str) -> str:
    return character * 64


def _render(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n"


def _assertions(gate_id: str) -> list[dict[str, object]]:
    gate = next(gate for gate in phase66_acceptance_manifest().gates if gate.gate_id == gate_id)
    return [
        {"assertion_id": assertion_id, "passed": True} for assertion_id in gate.required_assertions
    ]


def _privacy() -> dict[str, object]:
    return {
        "sanitizer_contract": "phase6.6-sanitized-evidence-v1",
        "forbidden_field_match_count": 0,
        "sensitive_value_match_count": 0,
        "free_text_value_count": 0,
    }


def _artifact(kind: str, character: str, *, artifact_format: str = "json") -> dict[str, object]:
    return {
        "kind": kind,
        "artifact_format": artifact_format,
        "artifact_digest": _digest(character),
        "byte_count": 1234,
        "redaction_verified": True,
    }


def _offline_record() -> dict[str, object]:
    gate_id = "offline.replay_matrix"
    return {
        "schema_version": PHASE66_CONTRACT_VERSION,
        "manifest_digest": phase66_manifest_digest(),
        "run_digest": _digest("1"),
        "source_commit_digest": _digest("2"),
        "gate_id": gate_id,
        "evidence_class": "offline",
        "outcome": "passed",
        "recorded_at": "2026-08-22T20:30:00Z",
        "assertions": _assertions(gate_id),
        "artifacts": [_artifact("test_report", "3", artifact_format="junit_xml")],
        "privacy": _privacy(),
    }


def _provider_record() -> dict[str, object]:
    gate_id = "provider.primary_same_job_canary"
    return {
        "schema_version": PHASE66_CONTRACT_VERSION,
        "manifest_digest": phase66_manifest_digest(),
        "run_digest": _digest("4"),
        "source_commit_digest": _digest("5"),
        "deployment_digest": _digest("6"),
        "actor_digests": [_digest("7")],
        "job_digest": _digest("8"),
        "work_digest": _digest("9"),
        "correlation_digest": _digest("a"),
        "gate_id": gate_id,
        "evidence_class": "provider_destructive",
        "outcome": "passed",
        "recorded_at": "2026-08-22T21:00:00Z",
        "assertions": _assertions(gate_id),
        "artifacts": [
            _artifact("provider_call_ledger", "1"),
            _artifact("canary_summary", "2"),
            _artifact("log_audit", "3"),
        ],
        "privacy": _privacy(),
        "provider_gate_attestation": {
            "run_gate_digest": _digest("b"),
            "provider_write_gate_digest": _digest("c"),
            "approved_scope": "unpublished_draft_create_update_only",
            "root_credentials_rejected": True,
            "publication_capability_absent": True,
            "approved_max_product_posts": 1,
            "approved_max_product_puts": 2,
        },
        "provider_call_summary": {
            "artwork_upload_count": 1,
            "product_post_count": 1,
            "product_put_count": 2,
            "product_get_count": 7,
            "forbidden_attempt_count": 0,
            "publish_attempt_count": 0,
            "order_attempt_count": 0,
            "fulfillment_attempt_count": 0,
            "final_state": "unpublished_unlocked",
        },
    }


def _moderated_record() -> dict[str, object]:
    gate_id = "moderated.first_time_seller_exit"
    return {
        "schema_version": PHASE66_CONTRACT_VERSION,
        "manifest_digest": phase66_manifest_digest(),
        "run_digest": _digest("d"),
        "source_commit_digest": _digest("e"),
        "deployment_digest": _digest("f"),
        "actor_digests": [_digest("0")],
        "job_digest": _digest("1"),
        "gate_id": gate_id,
        "evidence_class": "moderated_user",
        "outcome": "passed",
        "recorded_at": "2026-08-22T22:00:00Z",
        "assertions": _assertions(gate_id),
        "artifacts": [_artifact("moderated_session_record", "6")],
        "privacy": _privacy(),
        "moderated_session": {
            "participant_digest": _digest("2"),
            "consent_record_digest": _digest("3"),
            "task_script_digest": _digest("4"),
            "session_record_digest": _digest("5"),
            "first_time_seller": True,
            "external_documentation_used": False,
            "operator_intervention_count": 0,
            "completed_supported_flow": True,
            "duration_seconds": 900,
        },
    }


def _recursive_property_names(value: object) -> set[str]:
    names: set[str] = set()
    if isinstance(value, Mapping):
        properties = value.get("properties")
        if isinstance(properties, Mapping):
            names.update(str(name) for name in properties)
        for nested in value.values():
            names.update(_recursive_property_names(nested))
    elif isinstance(value, list):
        for nested in value:
            names.update(_recursive_property_names(nested))
    return names


def _object_schemas(value: object) -> list[Mapping[str, Any]]:
    schemas: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        if value.get("type") == "object":
            schemas.append(value)
        for nested in value.values():
            schemas.extend(_object_schemas(nested))
    elif isinstance(value, list):
        for nested in value:
            schemas.extend(_object_schemas(nested))
    return schemas


def test_checked_in_manifest_is_the_exact_frozen_deterministic_contract() -> None:
    checked_in_text = MANIFEST_PATH.read_text(encoding="utf-8")
    checked_in = json.loads(checked_in_text)
    parsed = Phase66AcceptanceManifest.model_validate(checked_in)

    assert parsed == phase66_acceptance_manifest()
    assert checked_in_text == _render(parsed.model_dump(mode="json"))
    assert phase66_manifest_digest() == EXPECTED_MANIFEST_DIGEST
    assert len(parsed.gates) == 12
    assert len(parsed.phase6_exit_gate_ids) == 11


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("frozen_at",), 0),
        (("gates", 0, "minimum_evidence_records"), "1"),
        (("gates", 0, "blocking_phase6_exit"), 1),
    ),
)
def test_manifest_boundary_rejects_json_type_coercion(
    path: tuple[object, ...],
    value: object,
) -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    target: object = manifest
    for component in path[:-1]:
        if isinstance(component, int):
            assert isinstance(target, list)
            target = target[component]
        else:
            assert isinstance(target, dict)
            target = target[component]
    assert isinstance(target, dict)
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        Phase66AcceptanceManifest.model_validate(target)


def test_checked_in_evidence_schema_is_the_exact_generated_structural_contract() -> None:
    checked_in_text = SCHEMA_PATH.read_text(encoding="utf-8")
    live = evidence_record_json_schema()
    assert json.loads(checked_in_text) == live
    assert checked_in_text == _render(live)


def test_manifest_separates_evidence_classes_and_provider_authority() -> None:
    manifest = phase66_acceptance_manifest()
    by_class = {
        evidence_class: tuple(
            gate for gate in manifest.gates if gate.evidence_class is evidence_class
        )
        for evidence_class in AcceptanceEvidenceClass
    }

    assert {evidence_class: len(gates) for evidence_class, gates in by_class.items()} == {
        AcceptanceEvidenceClass.OFFLINE: 4,
        AcceptanceEvidenceClass.DEPLOYED_NON_DESTRUCTIVE: 3,
        AcceptanceEvidenceClass.PROVIDER_DESTRUCTIVE: 3,
        AcceptanceEvidenceClass.MODERATED_USER: 2,
    }
    for gate in by_class[AcceptanceEvidenceClass.PROVIDER_DESTRUCTIVE]:
        assert gate.provider_mutation_policy == "double_gated"
        assert gate.double_gate_labels == ("run_gate", "provider_write_gate")
    for gate in by_class[AcceptanceEvidenceClass.MODERATED_USER]:
        assert gate.provider_mutation_policy == "separate_provider_evidence"
        assert gate.double_gate_labels == ()
    assert by_class[AcceptanceEvidenceClass.MODERATED_USER][-1].minimum_evidence_records == 5
    assert by_class[AcceptanceEvidenceClass.MODERATED_USER][-1].blocking_phase6_exit is False


def test_evidence_json_schema_is_closed_discriminated_and_digest_only() -> None:
    schema = evidence_record_json_schema()
    forbidden = set(phase66_acceptance_manifest().forbidden_evidence_field_names)

    assert schema["$id"].endswith("/phase6.6/evidence.schema.json")
    assert schema["x-runtime-semantic-validator"] == (
        "mr_lister.acceptance.phase6.validate_phase66_evidence"
    )
    assert "Structural validation only" in schema["$comment"]
    assert schema["discriminator"]["propertyName"] == "evidence_class"
    assert len(schema["oneOf"]) == 4
    assert all(item.get("additionalProperties") is False for item in _object_schemas(schema))
    assert _recursive_property_names(schema).isdisjoint(forbidden)
    assert {
        "source_commit_digest",
        "deployment_digest",
        "job_digest",
        "work_digest",
        "correlation_digest",
    }.issubset(_recursive_property_names(schema))
    assert "source_commit" not in _recursive_property_names(schema)
    assert "job_id" not in _recursive_property_names(schema)


def test_each_evidence_class_accepts_only_its_closed_sanitized_shape() -> None:
    offline = validate_phase66_evidence(_offline_record())

    deployed = _offline_record()
    deployed_gate = "deployed.edge_auth_owner_smoke"
    deployed.update(
        {
            "gate_id": deployed_gate,
            "evidence_class": "deployed_non_destructive",
            "deployment_digest": _digest("4"),
            "actor_digests": [_digest("5"), _digest("6")],
            "assertions": _assertions(deployed_gate),
            "artifacts": [
                _artifact("deployment_snapshot", "7"),
                _artifact("canary_summary", "8"),
                _artifact("log_audit", "9"),
            ],
        }
    )
    deployed_evidence = validate_phase66_evidence(deployed)
    provider = validate_phase66_evidence(_provider_record())
    moderated = validate_phase66_evidence(_moderated_record())

    assert offline.evidence_class == "offline"
    assert deployed_evidence.evidence_class == "deployed_non_destructive"
    assert provider.evidence_class == "provider_destructive"
    assert moderated.evidence_class == "moderated_user"


@pytest.mark.parametrize(
    "record_factory",
    (_offline_record, _provider_record, _moderated_record),
)
def test_passed_evidence_requires_every_gate_specific_artifact(record_factory: object) -> None:
    record = record_factory()  # type: ignore[operator]
    record["artifacts"] = []

    with pytest.raises(ValidationError, match="required sanitized artifact"):
        validate_phase66_evidence(record)


def test_artifact_digests_cannot_be_reused_inside_one_evidence_record() -> None:
    record = _provider_record()
    artifacts = record["artifacts"]
    assert isinstance(artifacts, list)
    artifacts[1]["artifact_digest"] = artifacts[0]["artifact_digest"]

    with pytest.raises(ValidationError, match="unique digests"):
        validate_phase66_evidence(record)


def test_artifact_kind_requires_a_nonempty_matching_format() -> None:
    record = _provider_record()
    artifacts = record["artifacts"]
    assert isinstance(artifacts, list)
    artifacts[0]["artifact_format"] = "png"

    with pytest.raises(ValidationError, match="kind and format"):
        validate_phase66_evidence(record)

    artifacts[0]["artifact_format"] = "json"
    artifacts[0]["byte_count"] = 0
    with pytest.raises(ValidationError, match="greater than 0"):
        validate_phase66_evidence(record)


@pytest.mark.parametrize(
    "forbidden_field",
    (
        "owner_id",
        "access_token",
        "secret",
        "presigned_url",
        "provider_payload",
        "provider_response",
        "raw_payload",
    ),
)
def test_raw_authority_fields_are_rejected_even_when_nested(forbidden_field: str) -> None:
    record = _offline_record()
    assertions = record["assertions"]
    assert isinstance(assertions, list)
    assertions[0][forbidden_field] = "must-never-survive"

    with pytest.raises(ValidationError, match="forbidden raw-authority field"):
        validate_phase66_evidence(record)


def test_evidence_rejects_manifest_drift_missing_assertions_and_free_text() -> None:
    wrong_manifest = _offline_record()
    wrong_manifest["manifest_digest"] = _digest("f")
    with pytest.raises(ValidationError, match="does not bind the frozen"):
        validate_phase66_evidence(wrong_manifest)

    missing_assertion = _offline_record()
    assertions = missing_assertion["assertions"]
    assert isinstance(assertions, list)
    assertions.pop()
    with pytest.raises(ValidationError, match="every frozen assertion"):
        validate_phase66_evidence(missing_assertion)

    free_text = _offline_record()
    free_text["notes"] = "raw observation"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        validate_phase66_evidence(free_text)


def test_provider_evidence_requires_both_gates_exact_counts_and_same_job_digests() -> None:
    missing_gate = _provider_record()
    missing_gate.pop("provider_gate_attestation")
    with pytest.raises(ValidationError):
        validate_phase66_evidence(missing_gate)

    excess_write = _provider_record()
    attestation = excess_write["provider_gate_attestation"]
    assert isinstance(attestation, dict)
    attestation["approved_max_product_puts"] = 1
    with pytest.raises(ValidationError, match="exceeds the double-gated authority"):
        validate_phase66_evidence(excess_write)

    missing_correlation = _provider_record()
    missing_correlation.pop("correlation_digest")
    with pytest.raises(ValidationError, match="Strands correlation digests"):
        validate_phase66_evidence(missing_correlation)

    same_gate = _provider_record()
    gate = same_gate["provider_gate_attestation"]
    assert isinstance(gate, dict)
    gate["provider_write_gate_digest"] = gate["run_gate_digest"]
    with pytest.raises(ValidationError, match="independently attested"):
        validate_phase66_evidence(same_gate)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("artwork_upload_count", 0),
        ("product_get_count", 0),
    ),
)
def test_primary_provider_canary_requires_upload_and_final_get(
    field: str,
    value: int,
) -> None:
    record = _provider_record()
    summary = record["provider_call_summary"]
    assert isinstance(summary, dict)
    summary[field] = value

    with pytest.raises(ValidationError, match="final GET readback"):
        validate_phase66_evidence(record)


@pytest.mark.parametrize(
    ("path", "coerced_value"),
    (
        (("assertions", 0, "passed"), "yes"),
        (("artifacts", 0, "redaction_verified"), 1),
        (("artifacts", 0, "byte_count"), "1234"),
    ),
)
def test_python_evidence_boundary_matches_strict_json_schema_types(
    path: tuple[object, ...],
    coerced_value: object,
) -> None:
    record = _offline_record()
    target: object = record
    for component in path[:-1]:
        if isinstance(component, int):
            assert isinstance(target, list)
            target = target[component]
        else:
            assert isinstance(target, dict)
            target = target[component]
    assert isinstance(target, dict)
    target[path[-1]] = coerced_value

    with pytest.raises(ValidationError):
        validate_phase66_evidence(record)


@pytest.mark.parametrize("timestamp", (1787428800, "1787428800", "2026-08-22T20:30:00+00:00"))
def test_evidence_timestamp_rejects_noncanonical_schema_coercions(timestamp: object) -> None:
    record = _offline_record()
    record["recorded_at"] = timestamp

    with pytest.raises(ValidationError, match="canonical UTC RFC3339"):
        validate_phase66_evidence(record)


def test_moderated_evidence_cannot_carry_provider_or_personal_payloads() -> None:
    intervened = _moderated_record()
    session = intervened["moderated_session"]
    assert isinstance(session, dict)
    session["operator_intervention_count"] = 1
    with pytest.raises(ValidationError, match="complete without intervention"):
        validate_phase66_evidence(intervened)

    provider_mixed = _moderated_record()
    provider_mixed["provider_call_summary"] = _provider_record()["provider_call_summary"]
    with pytest.raises(ValidationError):
        validate_phase66_evidence(provider_mixed)

    personal = _moderated_record()
    session = personal["moderated_session"]
    assert isinstance(session, dict)
    session["email"] = "seller@example.invalid"
    with pytest.raises(ValidationError, match="forbidden raw-authority field"):
        validate_phase66_evidence(personal)
