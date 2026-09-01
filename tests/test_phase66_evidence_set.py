from __future__ import annotations

import copy
import hashlib
import io
import json
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from mr_lister.acceptance.evidence_set import (
    EvidenceSetVerificationError,
    verify_phase66_evidence_set,
)
from mr_lister.acceptance.phase6 import (
    PHASE66_FIRST_TIME_SELLER_TASK_SHA256,
    AcceptanceEvidenceClass,
    ArtifactFormat,
    ArtifactKind,
    phase66_acceptance_manifest,
    phase66_manifest_digest,
    validate_phase66_evidence,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _json_bytes(label: str) -> bytes:
    return json.dumps(
        {"contract": "phase6.6-sanitized-artifact-v1", "result": "passed", "label": label},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _trace_bytes(label: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"trace-{label}.trace", b'{"type":"sanitized-browser-trace"}\n')
        archive.writestr("resources/summary.json", _json_bytes(label))
    return output.getvalue()


def _png_bytes(*, empty_image_stream: bool = False) -> bytes:
    def chunk(chunk_type: bytes, contents: bytes) -> bytes:
        checksum = zlib.crc32(chunk_type)
        checksum = zlib.crc32(contents, checksum)
        return (
            struct.pack(">I", len(contents)) + chunk_type + contents + struct.pack(">I", checksum)
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    pixels = b"" if empty_image_stream else zlib.compress(b"\x00\x00\x00\x00\x00")
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    )


@dataclass
class EvidenceBundle:
    root: Path
    records: list[dict[str, Any]]
    artifact_files: list[dict[str, Any]]

    def used_artifact_digests(self) -> set[str]:
        return {
            artifact["artifact_digest"]
            for record in self.records
            for artifact in record["artifacts"]
        }

    def prune_artifact_files(self) -> None:
        used = self.used_artifact_digests()
        self.artifact_files = [
            reference for reference in self.artifact_files if reference["artifact_digest"] in used
        ]


def _provider_summary(gate_id: str) -> dict[str, Any]:
    if gate_id == "provider.primary_same_job_canary":
        return {
            "artwork_upload_count": 1,
            "product_post_count": 1,
            "product_put_count": 2,
            "product_get_count": 1,
            "forbidden_attempt_count": 0,
            "publish_attempt_count": 0,
            "order_attempt_count": 0,
            "fulfillment_attempt_count": 0,
            "final_state": "unpublished_unlocked",
        }
    if gate_id == "provider.concurrency_canary":
        return {
            "artwork_upload_count": 0,
            "product_post_count": 0,
            "product_put_count": 1,
            "product_get_count": 1,
            "forbidden_attempt_count": 0,
            "publish_attempt_count": 0,
            "order_attempt_count": 0,
            "fulfillment_attempt_count": 0,
            "final_state": "unpublished_unlocked",
        }
    return {
        "artwork_upload_count": 0,
        "product_post_count": 0,
        "product_put_count": 0,
        "product_get_count": 0,
        "forbidden_attempt_count": 0,
        "publish_attempt_count": 0,
        "order_attempt_count": 0,
        "fulfillment_attempt_count": 0,
        "final_state": "not_created",
    }


def _first_time_seller_artifact(record: dict[str, Any], provider: dict[str, Any]) -> bytes:
    session = record["moderated_session"]
    validated_provider = validate_phase66_evidence(provider)
    provider_digest = hashlib.sha256(
        json.dumps(
            validated_provider.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return json.dumps(
        {
            "artifact_contract": "phase6.6-sanitized-moderated-session-record-v1",
            "gate_id": "moderated.first_time_seller_exit",
            "result": "passed",
            "recorded_at": record["recorded_at"],
            "source_commit_digest": record["source_commit_digest"],
            "deployment_digest": record["deployment_digest"],
            "run_digest": record["run_digest"],
            "job_digest": record["job_digest"],
            "actor_digest": record["actor_digests"][0],
            "work_digest": record["work_digest"],
            "correlation_digest": record["correlation_digest"],
            "provider_primary_record_digest": provider_digest,
            "task_contract_digest": PHASE66_FIRST_TIME_SELLER_TASK_SHA256,
            "participant_digest": session["participant_digest"],
            "consent_record_digest": session["consent_record_digest"],
            "session_record_digest": session["session_record_digest"],
            "first_time_seller": True,
            "explicit_consent": True,
            "completed_supported_flow": True,
            "duration_seconds": session["duration_seconds"],
            "authenticated_access": {
                "invited_seller": True,
                "mfa_complete": True,
                "authenticated_session_observed": True,
                "session_renewal_succeeded": True,
                "credential_material_retained": False,
            },
            "assistance": {
                "external_documentation_used": False,
                "moderator_help_used": False,
                "operator_intervention_count": 0,
            },
            "flow": {
                "supported_upload_completed": True,
                "artwork_normalization_completed": True,
                "browser_restarted": True,
                "same_job_recovered_after_restart": True,
                "same_job_strands_evidence_found": True,
                "unpublished_printify_draft_reviewed": True,
                "unpublished_boundary_understood": True,
                "human_approval_completed": True,
                "final_job_state": "APPROVED",
            },
            "accessibility": {
                "screen_reader_passed": True,
                "keyboard_only_passed": True,
                "visible_focus_passed": True,
                "contrast_passed": True,
                "reduced_motion_passed": True,
                "zoom_200_percent_passed": True,
            },
            "manual_journeys": {
                "upload_passed": True,
                "review_passed": True,
                "edit_passed": True,
                "refresh_passed": True,
                "cancel_passed": True,
                "retry_passed": True,
                "logout_passed": True,
            },
            "publication": {
                "publication_disabled": True,
                "publication_action_absent": True,
                "provider_draft_state": "unpublished_unlocked",
                "publication_attempt_count": 0,
                "order_attempt_count": 0,
                "fulfillment_attempt_count": 0,
                "provider_write_authority_is_separate": True,
            },
            "privacy": {
                "forbidden_field_match_count": 0,
                "sensitive_value_match_count": 0,
                "free_text_value_count": 0,
                "raw_authority_retained": False,
                "raw_identity_retained": False,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _make_bundle(tmp_path: Path, *, target_session_count: int = 0) -> EvidenceBundle:
    root = tmp_path / "artifacts"
    root.mkdir()
    records: list[dict[str, Any]] = []
    artifact_files: list[dict[str, Any]] = []
    manifest = phase66_acceptance_manifest()
    primary_job = _digest("provider-primary-job-0")
    artifact_number = 0

    for gate in manifest.gates:
        if not gate.blocking_phase6_exit and target_session_count == 0:
            continue
        record_count = target_session_count if not gate.blocking_phase6_exit else 1
        for record_number in range(record_count):
            run_label = (
                "moderated-provider-run"
                if gate.gate_id
                in {
                    "provider.primary_same_job_canary",
                    "moderated.first_time_seller_exit",
                }
                else f"run-{gate.gate_id}-{record_number}"
            )
            artifacts: list[dict[str, Any]] = []
            for kind in gate.required_artifact_kinds:
                artifact_number += 1
                artifact_format = (
                    ArtifactFormat.ZIP
                    if kind is ArtifactKind.BROWSER_TRACE
                    else ArtifactFormat.JSON
                )
                suffix = ".zip" if artifact_format is ArtifactFormat.ZIP else ".json"
                relative_path = f"gate-{artifact_number:02d}-{kind.value}{suffix}"
                contents = (
                    _trace_bytes(f"{gate.gate_id}-{record_number}")
                    if artifact_format is ArtifactFormat.ZIP
                    else _json_bytes(f"{gate.gate_id}-{kind.value}-{record_number}")
                )
                (root / relative_path).write_bytes(contents)
                digest = hashlib.sha256(contents).hexdigest()
                artifacts.append(
                    {
                        "kind": kind.value,
                        "artifact_format": artifact_format.value,
                        "artifact_digest": digest,
                        "byte_count": len(contents),
                        "redaction_verified": True,
                    }
                )
                artifact_files.append(
                    {
                        "artifact_digest": digest,
                        "kind": kind.value,
                        "artifact_format": artifact_format.value,
                        "relative_path": relative_path,
                    }
                )

            record: dict[str, Any] = {
                "schema_version": "6.6.0",
                "manifest_digest": phase66_manifest_digest(),
                "run_digest": _digest(run_label),
                "source_commit_digest": _digest("source-commit"),
                "gate_id": gate.gate_id,
                "evidence_class": gate.evidence_class.value,
                "outcome": "passed",
                "recorded_at": f"2026-08-23T06:{len(records):02d}:00Z",
                "assertions": [
                    {"assertion_id": assertion_id, "passed": True}
                    for assertion_id in gate.required_assertions
                ],
                "artifacts": artifacts,
                "privacy": {
                    "sanitizer_contract": "phase6.6-sanitized-evidence-v1",
                    "forbidden_field_match_count": 0,
                    "sensitive_value_match_count": 0,
                    "free_text_value_count": 0,
                },
            }
            if gate.evidence_class is AcceptanceEvidenceClass.DEPLOYED_NON_DESTRUCTIVE:
                record["deployment_digest"] = _digest("deployment")
                actor_count = 2 if gate.gate_id == "deployed.edge_auth_owner_smoke" else 1
                record["actor_digests"] = [
                    _digest(f"actor-{index}") for index in range(actor_count)
                ]
            elif gate.evidence_class is AcceptanceEvidenceClass.PROVIDER_DESTRUCTIVE:
                job_digest = (
                    primary_job
                    if gate.gate_id == "provider.primary_same_job_canary" and record_number == 0
                    else _digest(f"{gate.gate_id}-job-{record_number}")
                )
                record.update(
                    {
                        "deployment_digest": _digest("deployment"),
                        "actor_digests": [_digest("provider-actor")],
                        "job_digest": job_digest,
                        "provider_gate_attestation": {
                            "run_gate_digest": _digest(f"run-gate-{gate.gate_id}-{record_number}"),
                            "provider_write_gate_digest": _digest(
                                f"write-gate-{gate.gate_id}-{record_number}"
                            ),
                            "approved_scope": "unpublished_draft_create_update_only",
                            "root_credentials_rejected": True,
                            "publication_capability_absent": True,
                            "approved_max_product_posts": 1,
                            "approved_max_product_puts": 2,
                        },
                        "provider_call_summary": _provider_summary(gate.gate_id),
                    }
                )
                if gate.gate_id == "provider.primary_same_job_canary":
                    record["work_digest"] = _digest(f"primary-work-{record_number}")
                    record["correlation_digest"] = _digest(f"primary-correlation-{record_number}")
            elif gate.evidence_class is AcceptanceEvidenceClass.MODERATED_USER:
                record.update(
                    {
                        "deployment_digest": _digest("deployment"),
                        "actor_digests": [_digest("provider-actor")],
                        "work_digest": _digest("primary-work-0"),
                        "correlation_digest": _digest("primary-correlation-0"),
                        "moderated_session": {
                            "participant_digest": _digest(
                                f"participant-{gate.gate_id}-{record_number}"
                            ),
                            "consent_record_digest": _digest(
                                f"consent-{gate.gate_id}-{record_number}"
                            ),
                            "task_script_digest": PHASE66_FIRST_TIME_SELLER_TASK_SHA256,
                            "session_record_digest": _digest(
                                f"session-{gate.gate_id}-{record_number}"
                            ),
                            "first_time_seller": True,
                            "external_documentation_used": False,
                            "operator_intervention_count": 0,
                            "completed_supported_flow": True,
                            "duration_seconds": 600,
                        },
                    }
                )
                if gate.gate_id == "moderated.first_time_seller_exit":
                    record["job_digest"] = primary_job
            records.append(record)

    primary = next(
        record for record in records if record["gate_id"] == "provider.primary_same_job_canary"
    )
    moderated = next(
        record for record in records if record["gate_id"] == "moderated.first_time_seller_exit"
    )
    moderated_artifact = next(
        artifact
        for artifact in moderated["artifacts"]
        if artifact["kind"] == ArtifactKind.MODERATED_SESSION_RECORD.value
    )
    moderated_reference = next(
        reference
        for reference in artifact_files
        if reference["artifact_digest"] == moderated_artifact["artifact_digest"]
    )
    contents = _first_time_seller_artifact(moderated, primary)
    (root / moderated_reference["relative_path"]).write_bytes(contents)
    moderated_digest = hashlib.sha256(contents).hexdigest()
    moderated_artifact["artifact_digest"] = moderated_digest
    moderated_artifact["byte_count"] = len(contents)
    moderated_reference["artifact_digest"] = moderated_digest
    return EvidenceBundle(root=root, records=records, artifact_files=artifact_files)


def _verify(bundle: EvidenceBundle):
    return verify_phase66_evidence_set(
        bundle.records,
        bundle.artifact_files,
        allowed_artifact_root=bundle.root,
    )


def _reference_for(bundle: EvidenceBundle, artifact_digest: str) -> dict[str, Any]:
    return next(
        reference
        for reference in bundle.artifact_files
        if reference["artifact_digest"] == artifact_digest
    )


def _replace_artifact_bytes(
    bundle: EvidenceBundle,
    record_index: int,
    artifact_index: int,
    contents: bytes,
) -> None:
    artifact = bundle.records[record_index]["artifacts"][artifact_index]
    old_digest = artifact["artifact_digest"]
    reference = _reference_for(bundle, old_digest)
    (bundle.root / reference["relative_path"]).write_bytes(contents)
    digest = hashlib.sha256(contents).hexdigest()
    artifact["artifact_digest"] = digest
    artifact["byte_count"] = len(contents)
    reference["artifact_digest"] = digest


def test_complete_blocking_evidence_set_returns_only_closed_counters_and_digests(
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path)

    result = _verify(bundle)

    assert result.record_count == 11
    assert result.gate_count == 11
    assert result.blocking_gate_count == 11
    assert result.artifact_count == 20
    assert result.artifact_byte_count == sum(path.stat().st_size for path in bundle.root.iterdir())
    assert result.job_binding_count == 3
    assert result.run_count == 10
    assert result.source_commit_digest == _digest("source-commit")
    assert result.deployment_digest == _digest("deployment")
    payload = result.model_dump(mode="json")
    assert set(payload) == {
        "contract_version",
        "manifest_digest",
        "evidence_set_digest",
        "gate_set_digest",
        "source_commit_digest",
        "run_set_digest",
        "deployment_digest",
        "record_count",
        "gate_count",
        "blocking_gate_count",
        "artifact_count",
        "artifact_byte_count",
        "job_binding_count",
        "run_count",
    }
    assert not any("path" in key or "recorded" in key for key in payload)


def test_nonblocking_target_is_optional_but_closes_at_five_distinct_sessions(
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path, target_session_count=5)

    result = _verify(bundle)

    assert result.record_count == 16
    assert result.gate_count == 12
    assert result.artifact_count == 25
    assert result.run_count == 15


@pytest.mark.parametrize("outcome", ["failed", "inconclusive"])
def test_failed_or_inconclusive_record_never_closes_the_set(
    tmp_path: Path,
    outcome: str,
) -> None:
    bundle = _make_bundle(tmp_path)
    bundle.records[0]["outcome"] = outcome
    if outcome == "failed":
        bundle.records[0]["assertions"][0]["passed"] = False

    with pytest.raises(EvidenceSetVerificationError, match="outcome must be passed"):
        _verify(bundle)


def test_every_record_reenters_the_strict_phase66_validator(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    bundle.records[0]["artifacts"][0]["byte_count"] = "1"

    with pytest.raises(EvidenceSetVerificationError, match="failed Phase 6.6 validation"):
        _verify(bundle)


def test_manifest_prerequisites_are_closed_before_phase_exit(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    bundle.records = [
        record for record in bundle.records if record["gate_id"] != "offline.replay_matrix"
    ]
    bundle.prune_artifact_files()

    with pytest.raises(EvidenceSetVerificationError, match="prerequisite"):
        _verify(bundle)


def test_evidence_cannot_predate_the_frozen_contract(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    bundle.records[0]["recorded_at"] = "2026-08-22T19:59:59Z"

    with pytest.raises(EvidenceSetVerificationError, match="predates"):
        _verify(bundle)


def test_evidence_cannot_precede_its_prerequisite(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    concurrency = next(
        record for record in bundle.records if record["gate_id"] == "offline.concurrency_matrix"
    )
    concurrency["recorded_at"] = "2026-08-23T05:59:59Z"

    with pytest.raises(EvidenceSetVerificationError, match="before its prerequisite"):
        _verify(bundle)


def test_every_blocking_gate_is_required(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    bundle.records = [
        record for record in bundle.records if record["gate_id"] != "provider.cancellation_canary"
    ]
    bundle.prune_artifact_files()

    with pytest.raises(EvidenceSetVerificationError, match="incomplete"):
        _verify(bundle)


def test_represented_gate_must_meet_its_frozen_minimum_count(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path, target_session_count=1)

    with pytest.raises(EvidenceSetVerificationError, match="insufficient evidence"):
        _verify(bundle)


def test_blocking_destructive_gate_cannot_add_an_extra_passing_record(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    primary = next(
        record
        for record in bundle.records
        if record["gate_id"] == "provider.primary_same_job_canary"
    )
    duplicate = copy.deepcopy(primary)
    duplicate["run_digest"] = _digest("extra-primary-run")
    duplicate["job_digest"] = _digest("extra-primary-job")
    duplicate["work_digest"] = _digest("extra-primary-work")
    duplicate["correlation_digest"] = _digest("extra-primary-correlation")
    duplicate["provider_gate_attestation"]["run_gate_digest"] = _digest("extra-primary-run-gate")
    duplicate["provider_gate_attestation"]["provider_write_gate_digest"] = _digest(
        "extra-primary-write-gate"
    )
    bundle.records.append(duplicate)

    with pytest.raises(EvidenceSetVerificationError, match="exact frozen record count"):
        _verify(bundle)


@pytest.mark.parametrize(
    ("field", "record_index", "message"),
    [
        ("source_commit_digest", 0, "source commit"),
        ("deployment_digest", 4, "one deployment"),
    ],
)
def test_cross_record_source_run_and_deployment_drift_is_rejected(
    tmp_path: Path,
    field: str,
    record_index: int,
    message: str,
) -> None:
    bundle = _make_bundle(tmp_path)
    bundle.records[record_index][field] = _digest(f"drifted-{field}")

    with pytest.raises(EvidenceSetVerificationError, match=message):
        _verify(bundle)


def test_moderated_job_must_join_the_same_run_provider_primary(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    moderated = next(
        record
        for record in bundle.records
        if record["gate_id"] == "moderated.first_time_seller_exit"
    )
    moderated["job_digest"] = _digest("unrelated-job")

    with pytest.raises(EvidenceSetVerificationError, match="same-run provider"):
        _verify(bundle)


def test_moderated_run_must_join_the_same_job_provider_primary(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    moderated = next(
        record
        for record in bundle.records
        if record["gate_id"] == "moderated.first_time_seller_exit"
    )
    moderated["run_digest"] = _digest("unrelated-run")

    with pytest.raises(EvidenceSetVerificationError, match="same-run provider"):
        _verify(bundle)


def test_provider_records_cannot_share_one_run_gate(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    providers = [
        record for record in bundle.records if record["evidence_class"] == "provider_destructive"
    ]
    providers[1]["provider_gate_attestation"]["run_gate_digest"] = providers[0][
        "provider_gate_attestation"
    ]["run_gate_digest"]

    with pytest.raises(EvidenceSetVerificationError, match="run-gate authority"):
        _verify(bundle)


def test_moderated_records_must_share_the_frozen_task_script(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path, target_session_count=5)
    target = next(
        record for record in bundle.records if record["gate_id"] == "moderated.five_session_target"
    )
    target["moderated_session"]["task_script_digest"] = _digest("drifted-task-script")

    with pytest.raises(EvidenceSetVerificationError, match="failed Phase 6.6 validation"):
        _verify(bundle)


def test_generic_passed_json_cannot_replace_the_moderated_session_artifact(
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path)
    record_index = next(
        index
        for index, record in enumerate(bundle.records)
        if record["gate_id"] == "moderated.first_time_seller_exit"
    )
    _replace_artifact_bytes(bundle, record_index, 0, _json_bytes("generic-moderated-claim"))

    with pytest.raises(EvidenceSetVerificationError, match="authoritative schema"):
        _verify(bundle)


@pytest.mark.parametrize(
    ("path", "replacement", "message"),
    [
        (("accessibility", "contrast_passed"), False, "authoritative schema"),
        (("flow", "same_job_strands_evidence_found"), False, "authoritative schema"),
        (("publication", "publication_disabled"), False, "authoritative schema"),
        (("job_digest",), _digest("other-job"), "exact provider and session authority"),
        (
            ("provider_primary_record_digest",),
            _digest("other-provider-record"),
            "exact provider and session authority",
        ),
    ],
)
def test_moderated_artifact_facts_and_authorities_are_semantically_verified(
    tmp_path: Path,
    path: tuple[str, ...],
    replacement: object,
    message: str,
) -> None:
    bundle = _make_bundle(tmp_path)
    record_index = next(
        index
        for index, record in enumerate(bundle.records)
        if record["gate_id"] == "moderated.first_time_seller_exit"
    )
    artifact = bundle.records[record_index]["artifacts"][0]
    reference = _reference_for(bundle, artifact["artifact_digest"])
    value = json.loads((bundle.root / reference["relative_path"]).read_bytes())
    target = value
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = replacement
    contents = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    _replace_artifact_bytes(bundle, record_index, 0, contents)

    with pytest.raises(EvidenceSetVerificationError, match=message):
        _verify(bundle)


def test_exact_duplicate_record_is_rejected_before_it_can_inflate_counts(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    bundle.records.append(copy.deepcopy(bundle.records[0]))

    with pytest.raises(EvidenceSetVerificationError, match="Duplicate evidence"):
        _verify(bundle)


def test_semantic_moderated_session_reuse_is_rejected(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path, target_session_count=5)
    target_records = [
        record for record in bundle.records if record["gate_id"] == "moderated.five_session_target"
    ]
    target_records[1]["moderated_session"]["session_record_digest"] = target_records[0][
        "moderated_session"
    ]["session_record_digest"]

    with pytest.raises(EvidenceSetVerificationError, match="reuses a session"):
        _verify(bundle)


def test_one_provider_job_cannot_authorize_two_moderated_sessions(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path, target_session_count=5)
    first_time = next(
        record
        for record in bundle.records
        if record["gate_id"] == "moderated.first_time_seller_exit"
    )
    target = next(
        record for record in bundle.records if record["gate_id"] == "moderated.five_session_target"
    )
    target["run_digest"] = first_time["run_digest"]
    target["job_digest"] = first_time["job_digest"]

    with pytest.raises(EvidenceSetVerificationError, match="reuses a session authority"):
        _verify(bundle)


def test_provider_write_gate_reuse_is_rejected(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    provider_records = [
        record for record in bundle.records if record["evidence_class"] == "provider_destructive"
    ]
    provider_records[1]["provider_gate_attestation"]["provider_write_gate_digest"] = (
        provider_records[0]["provider_gate_attestation"]["provider_write_gate_digest"]
    )

    with pytest.raises(EvidenceSetVerificationError, match="write-gate authority"):
        _verify(bundle)


def test_artifact_digest_cannot_be_reused_across_records(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    bundle.records[1]["artifacts"][0] = copy.deepcopy(bundle.records[0]["artifacts"][0])

    with pytest.raises(EvidenceSetVerificationError, match="artifact reuse"):
        _verify(bundle)


def test_artifact_file_index_must_match_evidence_exactly(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    bundle.artifact_files.pop()

    with pytest.raises(EvidenceSetVerificationError, match="exactly match"):
        _verify(bundle)


def test_artifact_size_and_sha256_are_recomputed_from_disk(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    first = bundle.artifact_files[0]
    (bundle.root / first["relative_path"]).write_bytes(b"changed after attestation")

    with pytest.raises(EvidenceSetVerificationError, match="byte count"):
        _verify(bundle)

    original_size = bundle.records[0]["artifacts"][0]["byte_count"]
    replacement = b"x" * original_size
    (bundle.root / first["relative_path"]).write_bytes(replacement)
    with pytest.raises(EvidenceSetVerificationError, match="SHA-256"):
        _verify(bundle)


def test_artifact_kind_and_format_binding_must_match_the_record(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    bundle.artifact_files[0]["kind"] = "log_audit"

    with pytest.raises(EvidenceSetVerificationError, match="kind or format"):
        _verify(bundle)


def test_optional_screenshot_requires_a_structurally_valid_png(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    contents = _png_bytes()
    relative_path = "optional-screenshot.png"
    (bundle.root / relative_path).write_bytes(contents)
    digest = hashlib.sha256(contents).hexdigest()
    bundle.records[0]["artifacts"].append(
        {
            "kind": "screenshot",
            "artifact_format": "png",
            "artifact_digest": digest,
            "byte_count": len(contents),
            "redaction_verified": True,
        }
    )
    bundle.artifact_files.append(
        {
            "artifact_digest": digest,
            "kind": "screenshot",
            "artifact_format": "png",
            "relative_path": relative_path,
        }
    )

    assert _verify(bundle).artifact_count == 21

    _replace_artifact_bytes(bundle, 0, 1, _png_bytes(empty_image_stream=True))
    with pytest.raises(EvidenceSetVerificationError, match="declared format"):
        _verify(bundle)


def test_junit_artifact_requires_a_complete_testsuite_document(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    artifact = bundle.records[0]["artifacts"][0]
    reference = _reference_for(bundle, artifact["artifact_digest"])
    old_path = bundle.root / reference["relative_path"]
    relative_path = "offline-replay.xml"
    contents = b'<?xml version="1.0"?><testsuite tests="1"><testcase name="closed"/></testsuite>'
    old_path.unlink()
    (bundle.root / relative_path).write_bytes(contents)
    digest = hashlib.sha256(contents).hexdigest()
    artifact.update(
        {
            "artifact_format": "junit_xml",
            "artifact_digest": digest,
            "byte_count": len(contents),
        }
    )
    reference.update(
        {
            "artifact_format": "junit_xml",
            "artifact_digest": digest,
            "relative_path": relative_path,
        }
    )

    assert _verify(bundle).artifact_count == 20

    _replace_artifact_bytes(bundle, 0, 0, b"<testsuite><testcase></testsuite>")
    with pytest.raises(EvidenceSetVerificationError, match="declared format"):
        _verify(bundle)


@pytest.mark.parametrize(
    "contents",
    [
        (
            b'<?xml version="1.0"?><testsuite tests="1" failures="1">'
            b"<testcase><failure/></testcase></testsuite>"
        ),
        b'<?xml version="1.0"?><testsuite tests="1"><testcase><skipped/></testcase></testsuite>',
        b'<?xml version="1.0"?><testsuite tests="0"/>',
    ],
)
def test_junit_artifact_must_prove_a_nonempty_passing_run(tmp_path: Path, contents: bytes) -> None:
    bundle = _make_bundle(tmp_path)
    _replace_artifact_bytes(bundle, 0, 0, contents)
    artifact = bundle.records[0]["artifacts"][0]
    reference = _reference_for(bundle, artifact["artifact_digest"])
    old_path = bundle.root / reference["relative_path"]
    new_path = bundle.root / "failed-report.xml"
    old_path.rename(new_path)
    artifact["artifact_format"] = "junit_xml"
    reference["artifact_format"] = "junit_xml"
    reference["relative_path"] = new_path.name

    with pytest.raises(EvidenceSetVerificationError, match="declared format"):
        _verify(bundle)


@pytest.mark.parametrize(
    "contents",
    [
        b"null",
        b"{}",
        b'{"contract":"phase6.6-sanitized-artifact-v1","result":"failed"}',
        b'{"contract":"phase6.6-sanitized-artifact-v1","result":"passed","access_token":"redacted"}',
    ],
)
def test_json_artifact_must_be_a_passed_sanitized_envelope(tmp_path: Path, contents: bytes) -> None:
    bundle = _make_bundle(tmp_path)
    _replace_artifact_bytes(bundle, 0, 0, contents)

    with pytest.raises(EvidenceSetVerificationError, match="declared format"):
        _verify(bundle)


def test_utf16_junit_doctype_cannot_bypass_authority_declaration_scan(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    artifact = bundle.records[0]["artifacts"][0]
    reference = _reference_for(bundle, artifact["artifact_digest"])
    old_path = bundle.root / reference["relative_path"]
    relative_path = "doctype-probe.xml"
    contents = (
        '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE testsuite><testsuite tests="0"/>'
    ).encode("utf-16")
    old_path.unlink()
    (bundle.root / relative_path).write_bytes(contents)
    digest = hashlib.sha256(contents).hexdigest()
    artifact.update(
        {
            "artifact_format": "junit_xml",
            "artifact_digest": digest,
            "byte_count": len(contents),
        }
    )
    reference.update(
        {
            "artifact_format": "junit_xml",
            "artifact_digest": digest,
            "relative_path": relative_path,
        }
    )

    with pytest.raises(EvidenceSetVerificationError, match="declared format"):
        _verify(bundle)


@pytest.mark.parametrize(
    ("record_index", "artifact_index", "contents"),
    [
        (0, 0, b"not-json"),
        (0, 0, b"[" * 2_000 + b"0" + b"]" * 2_000),
        (3, 1, b"not-a-zip"),
    ],
)
def test_declared_file_format_is_verified_from_bytes(
    tmp_path: Path,
    record_index: int,
    artifact_index: int,
    contents: bytes,
) -> None:
    bundle = _make_bundle(tmp_path)
    _replace_artifact_bytes(bundle, record_index, artifact_index, contents)

    with pytest.raises(EvidenceSetVerificationError, match="declared format"):
        _verify(bundle)


def test_zero_byte_trace_member_is_not_browser_trace_evidence(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("empty.trace", b"")
    _replace_artifact_bytes(bundle, 3, 1, output.getvalue())

    with pytest.raises(EvidenceSetVerificationError, match="declared format"):
        _verify(bundle)


def test_unsupported_zip_compression_is_normalized_to_closed_failure(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    contents = bytearray(_trace_bytes("unsupported-compression"))
    local_header = contents.index(b"PK\x03\x04")
    central_header = contents.index(b"PK\x01\x02")
    struct.pack_into("<H", contents, local_header + 8, 99)
    struct.pack_into("<H", contents, central_header + 10, 99)
    _replace_artifact_bytes(bundle, 3, 1, bytes(contents))

    with pytest.raises(EvidenceSetVerificationError, match="declared format"):
        _verify(bundle)


def test_pathological_record_depth_is_normalized_to_closed_failure(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    deeply_nested: list[Any] = []
    current = deeply_nested
    for _ in range(2_000):
        nested: list[Any] = []
        current.append(nested)
        current = nested

    with pytest.raises(EvidenceSetVerificationError, match="depth bound"):
        verify_phase66_evidence_set(
            [deeply_nested],
            bundle.artifact_files,
            allowed_artifact_root=bundle.root,
        )


def test_oversized_structured_artifact_is_rejected_before_hash_or_parse(
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path)
    artifact = bundle.records[0]["artifacts"][0]
    reference = _reference_for(bundle, artifact["artifact_digest"])
    oversized = 100_000_001
    with (bundle.root / reference["relative_path"]).open("r+b") as output:
        output.truncate(oversized)
    artifact["artifact_digest"] = "e" * 64
    artifact["byte_count"] = oversized
    reference["artifact_digest"] = artifact["artifact_digest"]

    with pytest.raises(EvidenceSetVerificationError, match="format bound"):
        _verify(bundle)


@pytest.mark.parametrize(
    "malicious_path",
    ["../outside.json", "/tmp/outside.json", "nested/../../outside.json", "nested\\file.json"],
)
def test_artifact_path_traversal_and_noncanonical_paths_are_rejected(
    tmp_path: Path,
    malicious_path: str,
) -> None:
    bundle = _make_bundle(tmp_path)
    bundle.artifact_files[0]["relative_path"] = malicious_path

    with pytest.raises(EvidenceSetVerificationError, match="binding is invalid"):
        _verify(bundle)


def test_artifact_file_symlink_cannot_escape_the_allowed_root(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    first = bundle.artifact_files[0]
    target = bundle.root / first["relative_path"]
    outside = tmp_path / "outside.json"
    outside.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(EvidenceSetVerificationError, match="confined regular file"):
        _verify(bundle)


def test_intermediate_directory_symlink_is_not_followed(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    first = bundle.artifact_files[0]
    original = bundle.root / first["relative_path"]
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    outside = outside_directory / "artifact.json"
    outside.write_bytes(original.read_bytes())
    alias = bundle.root / "alias"
    alias.symlink_to(outside_directory, target_is_directory=True)
    first["relative_path"] = "alias/artifact.json"

    with pytest.raises(EvidenceSetVerificationError, match="confined regular file"):
        _verify(bundle)


def test_hard_link_cannot_reuse_one_physical_artifact(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    first = bundle.artifact_files[0]
    second = bundle.artifact_files[1]
    first_path = bundle.root / first["relative_path"]
    second_path = bundle.root / second["relative_path"]
    second_path.unlink()
    second_path.hardlink_to(first_path)
    second_artifact = bundle.records[1]["artifacts"][0]
    second_artifact.update(
        {
            "artifact_digest": "f" * 64,
            "byte_count": first_path.stat().st_size,
        }
    )
    second["artifact_digest"] = second_artifact["artifact_digest"]

    with pytest.raises(EvidenceSetVerificationError, match="confined regular file"):
        _verify(bundle)


def test_symlinked_allowed_root_is_rejected(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    root_alias = tmp_path / "artifact-root-alias"
    root_alias.symlink_to(bundle.root, target_is_directory=True)

    with pytest.raises(EvidenceSetVerificationError, match="stable directory"):
        verify_phase66_evidence_set(
            bundle.records,
            bundle.artifact_files,
            allowed_artifact_root=root_alias,
        )


def test_browser_trace_zip_rejects_member_traversal(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("../trace.trace", b"sanitized")
    _replace_artifact_bytes(bundle, 3, 1, output.getvalue())

    with pytest.raises(EvidenceSetVerificationError, match="declared format"):
        _verify(bundle)


def test_result_digest_is_order_independent_but_evidence_sensitive(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    original = _verify(bundle)
    bundle.records.reverse()
    bundle.artifact_files.reverse()

    reordered = _verify(bundle)

    assert reordered.evidence_set_digest == original.evidence_set_digest
    assert reordered.gate_set_digest == original.gate_set_digest
