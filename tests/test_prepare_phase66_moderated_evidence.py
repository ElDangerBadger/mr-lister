from __future__ import annotations

import ast
import json
import os
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from mr_lister.acceptance.evidence_set import (
    _declared_artifacts,
    _validated_artifact_files,
    _validated_records,
    _verify_artifacts,
)
from mr_lister.acceptance.phase6 import (
    AcceptanceEvidenceClass,
    ArtifactFormat,
    ArtifactKind,
    phase66_acceptance_manifest,
    phase66_manifest_digest,
    validate_phase66_evidence,
)
from tools import prepare_phase66_moderated_evidence as producer

CONSENT_AT = "2026-08-31T20:00:00Z"
STARTED_AT = "2026-08-31T20:10:00Z"
PROVIDER_AT = "2026-08-31T20:30:00Z"
COMPLETED_AT = "2026-08-31T20:45:00Z"


def _digest(label: str) -> str:
    return sha256(label.encode("ascii")).hexdigest()


SOURCE_DIGEST = _digest("source")
DEPLOYMENT_DIGEST = _digest("deployment")
RUN_DIGEST = _digest("run")
JOB_DIGEST = _digest("job")
ACTOR_DIGEST = _digest("actor")
WORK_DIGEST = _digest("work")
CORRELATION_DIGEST = _digest("correlation")
PARTICIPANT_DIGEST = _digest("participant")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _write_private(path: Path, value: object) -> tuple[str, int]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = path.parent
    while current.name != ".mr_lister_private":
        current.chmod(0o700)
        current = current.parent
    payload = _canonical(value)
    path.write_bytes(payload)
    path.chmod(0o600)
    return sha256(payload).hexdigest(), len(payload)


def _artifact(kind: ArtifactKind, payload: bytes) -> dict[str, object]:
    return {
        "artifact_digest": sha256(payload).hexdigest(),
        "artifact_format": ArtifactFormat.JSON.value,
        "byte_count": len(payload),
        "kind": kind.value,
        "redaction_verified": True,
    }


def _provider_record(artifacts: list[dict[str, object]]) -> dict[str, object]:
    gate = next(
        gate
        for gate in phase66_acceptance_manifest().gates
        if gate.gate_id == producer.PROVIDER_GATE_ID
    )
    return {
        "actor_digests": [ACTOR_DIGEST],
        "artifacts": artifacts,
        "assertions": [
            {
                "assertion_id": assertion_id,
                "observation_digest": _digest(f"provider-{assertion_id}"),
                "observed_count": 1,
                "passed": True,
            }
            for assertion_id in gate.required_assertions
        ],
        "correlation_digest": CORRELATION_DIGEST,
        "deployment_digest": DEPLOYMENT_DIGEST,
        "evidence_class": AcceptanceEvidenceClass.PROVIDER_DESTRUCTIVE.value,
        "gate_id": producer.PROVIDER_GATE_ID,
        "job_digest": JOB_DIGEST,
        "manifest_digest": phase66_manifest_digest(),
        "moderated_session": None,
        "outcome": "passed",
        "privacy": {
            "forbidden_field_match_count": 0,
            "free_text_value_count": 0,
            "sanitizer_contract": "phase6.6-sanitized-evidence-v1",
            "sensitive_value_match_count": 0,
        },
        "provider_call_summary": {
            "artwork_upload_count": 1,
            "product_post_count": 1,
            "product_put_count": 2,
            "product_get_count": 3,
            "forbidden_attempt_count": 0,
            "publish_attempt_count": 0,
            "order_attempt_count": 0,
            "fulfillment_attempt_count": 0,
            "final_state": "unpublished_unlocked",
        },
        "provider_gate_attestation": {
            "run_gate_digest": _digest("provider-run-gate"),
            "provider_write_gate_digest": _digest("provider-write-gate"),
            "approved_scope": "unpublished_draft_create_update_only",
            "root_credentials_rejected": True,
            "publication_capability_absent": True,
            "approved_max_product_posts": 1,
            "approved_max_product_puts": 2,
        },
        "recorded_at": PROVIDER_AT,
        "run_digest": RUN_DIGEST,
        "schema_version": "6.6.0",
        "source_commit_digest": SOURCE_DIGEST,
        "work_digest": WORK_DIGEST,
    }


def _consent() -> dict[str, object]:
    return {
        "consent_contract": producer.CONSENT_CONTRACT,
        "recorded_at": CONSENT_AT,
        "participant_digest": PARTICIPANT_DIGEST,
        "task_contract_digest": producer.TASK_CONTRACT_SHA256,
        "explicit_consent": True,
        "first_time_seller": True,
        "observation_recording_accepted": True,
        "raw_identity_retained": False,
        "free_text_value_count": 0,
    }


def _observation(
    *,
    consent_digest: str,
    provider_record_digest: str,
) -> dict[str, object]:
    return {
        "observation_contract": producer.OBSERVATION_CONTRACT,
        "started_at": STARTED_AT,
        "completed_at": COMPLETED_AT,
        "participant_digest": PARTICIPANT_DIGEST,
        "consent_record_digest": consent_digest,
        "authority": {
            "source_commit_digest": SOURCE_DIGEST,
            "deployment_digest": DEPLOYMENT_DIGEST,
            "run_digest": RUN_DIGEST,
            "job_digest": JOB_DIGEST,
            "actor_digest": ACTOR_DIGEST,
            "work_digest": WORK_DIGEST,
            "correlation_digest": CORRELATION_DIGEST,
            "provider_primary_record_digest": provider_record_digest,
            "task_contract_digest": producer.TASK_CONTRACT_SHA256,
        },
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
    }


@pytest.fixture
def private_closure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    tracked_contract = (
        producer.REPOSITORY_ROOT / producer.TASK_CONTRACT_RELATIVE_PATH
    ).read_bytes()
    repository = tmp_path / "repository"
    contract_path = repository / producer.TASK_CONTRACT_RELATIVE_PATH
    contract_path.parent.mkdir(parents=True)
    contract_path.write_bytes(tracked_contract)
    private_parent = repository / ".mr_lister_private"
    private = private_parent / "phase66-acceptance"
    closure = private / "closure"
    closure.mkdir(mode=0o700, parents=True)
    private_parent.chmod(0o700)
    private.chmod(0o700)
    closure.chmod(0o700)
    monkeypatch.setattr(producer, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(producer, "PRIVATE_ROOT", private)
    monkeypatch.setattr(producer, "_effective_uid", lambda: 501)
    return closure


def _inputs(
    closure: Path,
    *,
    consent_mutator: Any = None,
    observation_mutator: Any = None,
    provider_mutator: Any = None,
) -> dict[str, object]:
    provider_root = closure / "provider-primary"
    artifact_files: list[dict[str, object]] = []
    artifact_records: list[dict[str, object]] = []
    for kind, name in (
        (ArtifactKind.PROVIDER_CALL_LEDGER, "provider_call_ledger.json"),
        (ArtifactKind.CANARY_SUMMARY, "canary_summary.json"),
        (ArtifactKind.LOG_AUDIT, "log_audit.json"),
    ):
        payload = _canonical(
            {
                "artifact_contract": f"sanitized-{kind.value}-v1",
                "redaction_verified": True,
                "result": "passed",
            }
        )
        path = provider_root / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.chmod(0o600)
        artifact_records.append(_artifact(kind, payload))
        artifact_files.append(
            {
                "artifact_digest": sha256(payload).hexdigest(),
                "artifact_format": ArtifactFormat.JSON.value,
                "kind": kind.value,
                "relative_path": name,
            }
        )
    provider = _provider_record(artifact_records)
    if provider_mutator is not None:
        provider_mutator(provider)
    validated_provider = validate_phase66_evidence(provider)
    provider_record_digest = producer._digest(validated_provider.model_dump(mode="json"))
    records_path = provider_root / "records.json"
    records_sha, records_size = _write_private(records_path, [provider])
    artifact_files_path = provider_root / "artifact-files.json"
    artifact_files_sha, artifact_files_size = _write_private(artifact_files_path, artifact_files)

    consent = _consent()
    if consent_mutator is not None:
        consent_mutator(consent)
    consent_path = closure / "human-inputs" / "consent.json"
    consent_sha, consent_size = _write_private(consent_path, consent)
    observation = _observation(
        consent_digest=consent_sha,
        provider_record_digest=provider_record_digest,
    )
    if observation_mutator is not None:
        observation_mutator(observation)
    observation_path = closure / "human-inputs" / "session.json"
    observation_sha, observation_size = _write_private(observation_path, observation)
    return {
        "provider_records_path": records_path,
        "provider_records_sha256": records_sha,
        "provider_records_size": records_size,
        "provider_artifact_files_path": artifact_files_path,
        "provider_artifact_files_sha256": artifact_files_sha,
        "provider_artifact_files_size": artifact_files_size,
        "consent_path": consent_path,
        "consent_sha256": consent_sha,
        "consent_size": consent_size,
        "session_observation_path": observation_path,
        "session_observation_sha256": observation_sha,
        "session_observation_size": observation_size,
    }


def _prepare(
    closure: Path,
    *,
    inputs: dict[str, object] | None = None,
    **overrides: object,
) -> dict[str, object]:
    arguments = {
        "closure_root": closure,
        "task_contract_sha256": producer.TASK_CONTRACT_SHA256,
        **(inputs or _inputs(closure)),
        "clock": lambda: datetime(2026, 8, 31, 21, 0, tzinfo=UTC),
        **overrides,
    }
    return producer.prepare_phase66_moderated_evidence(**arguments)  # type: ignore[arg-type]


def _nested(value: dict[str, object], path: str) -> tuple[dict[str, object], str]:
    components = path.split(".")
    target = value
    for component in components[:-1]:
        nested = target[component]
        assert isinstance(nested, dict)
        target = nested
    return target, components[-1]


def test_frozen_task_contract_is_exact_and_manifest_bound() -> None:
    path = producer.REPOSITORY_ROOT / producer.TASK_CONTRACT_RELATIVE_PATH
    payload = path.read_bytes()
    assert sha256(payload).hexdigest() == producer.TASK_CONTRACT_SHA256
    contract = producer._FrozenTaskContract.model_validate_json(payload)
    assert contract.status == "frozen"
    assert contract.gate_id == producer.GATE_ID
    assert contract.acceptance_manifest_digest == phase66_manifest_digest()
    assert contract.manual_accessibility_checks == producer.EXPECTED_ACCESSIBILITY_CHECKS
    assert contract.manual_journeys == producer.EXPECTED_MANUAL_JOURNEYS


def test_producer_emits_exact_verified_sanitized_fragment(private_closure: Path) -> None:
    inputs = _inputs(private_closure)
    original_inputs = {
        str(path): path.read_bytes() for path in inputs.values() if isinstance(path, Path)
    }
    result = _prepare(private_closure, inputs=inputs)
    output = private_closure / producer.OUTPUT_DIRECTORY_NAME

    assert {path.name for path in output.iterdir()} == set(producer.OUTPUT_FILENAMES)
    assert os.stat(output, follow_symlinks=False).st_mode & 0o777 == 0o700
    assert all(
        os.stat(output / name, follow_symlinks=False).st_mode & 0o777 == 0o600
        for name in producer.OUTPUT_FILENAMES
    )
    records_value = json.loads((output / producer.RECORDS_FILENAME).read_bytes())
    files_value = json.loads((output / producer.ARTIFACT_FILES_FILENAME).read_bytes())
    records = _validated_records(records_value)
    record = validate_phase66_evidence(records_value[0])
    assert record.gate_id == producer.GATE_ID
    assert record.run_digest == RUN_DIGEST
    assert record.job_digest == JOB_DIGEST
    assert record.deployment_digest == DEPLOYMENT_DIGEST
    assert record.actor_digests == (ACTOR_DIGEST,)
    assert record.work_digest == WORK_DIGEST
    assert record.correlation_digest == CORRELATION_DIGEST
    assert tuple(item.assertion_id for item in record.assertions) == producer.EXPECTED_ASSERTIONS
    assert all(item.passed and item.observed_count == 1 for item in record.assertions)
    assert record.moderated_session is not None
    assert record.moderated_session.first_time_seller is True
    assert record.moderated_session.external_documentation_used is False
    assert record.moderated_session.operator_intervention_count == 0
    assert record.moderated_session.task_script_digest == producer.TASK_CONTRACT_SHA256
    declared = _declared_artifacts(records)
    files = _validated_artifact_files(files_value)
    assert _verify_artifacts(declared, files, output) == record.artifacts[0].byte_count
    artifact = producer._SanitizedSessionArtifact.model_validate(
        json.loads((output / producer.SESSION_RECORD_FILENAME).read_bytes())
    )
    assert artifact.authenticated_access.mfa_complete is True
    assert artifact.flow.same_job_strands_evidence_found is True
    assert artifact.publication.publication_disabled is True
    assert artifact.publication.provider_draft_state == "unpublished_unlocked"
    assert artifact.accessibility.screen_reader_passed is True
    assert artifact.accessibility.keyboard_only_passed is True
    assert artifact.accessibility.contrast_passed is True
    assert all(
        value is True for value in artifact.manual_journeys.model_dump(mode="python").values()
    )
    assert result["run_digest"] == RUN_DIGEST
    assert result["job_digest"] == JOB_DIGEST
    assert result["task_contract_digest"] == producer.TASK_CONTRACT_SHA256
    assert original_inputs == {
        str(path): path.read_bytes() for path in inputs.values() if isinstance(path, Path)
    }


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        ("authenticated_access.invited_seller", False),
        ("authenticated_access.mfa_complete", False),
        ("authenticated_access.authenticated_session_observed", False),
        ("authenticated_access.session_renewal_succeeded", False),
        ("assistance.external_documentation_used", True),
        ("assistance.moderator_help_used", True),
        ("assistance.operator_intervention_count", 1),
        ("flow.supported_upload_completed", False),
        ("flow.browser_restarted", False),
        ("flow.same_job_recovered_after_restart", False),
        ("flow.same_job_strands_evidence_found", False),
        ("flow.unpublished_boundary_understood", False),
        ("flow.human_approval_completed", False),
        ("accessibility.screen_reader_passed", False),
        ("accessibility.keyboard_only_passed", False),
        ("accessibility.contrast_passed", False),
        ("manual_journeys.upload_passed", False),
        ("manual_journeys.edit_passed", False),
        ("manual_journeys.cancel_passed", False),
        ("manual_journeys.retry_passed", False),
        ("manual_journeys.logout_passed", False),
        ("publication.publication_disabled", False),
        ("publication.publication_action_absent", False),
        ("publication.provider_draft_state", "published"),
        ("publication.provider_write_authority_is_separate", False),
    ],
)
def test_required_human_accessibility_and_publication_facts_fail_closed(
    private_closure: Path,
    path: str,
    replacement: object,
) -> None:
    def mutate(value: dict[str, object]) -> None:
        target, key = _nested(value, path)
        target[key] = replacement

    with pytest.raises(producer.Phase66ModeratedEvidenceError, match="closed sanitized schema"):
        _prepare(
            private_closure,
            inputs=_inputs(private_closure, observation_mutator=mutate),
        )
    assert not (private_closure / producer.OUTPUT_DIRECTORY_NAME).exists()


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("source_commit_digest", _digest("other-source")),
        ("deployment_digest", _digest("other-deployment")),
        ("run_digest", _digest("other-run")),
        ("job_digest", _digest("other-job")),
        ("actor_digest", _digest("other-actor")),
        ("work_digest", _digest("other-work")),
        ("correlation_digest", _digest("other-correlation")),
        ("provider_primary_record_digest", _digest("other-provider-record")),
    ],
)
def test_every_provider_authority_binding_must_match_gate_8(
    private_closure: Path,
    field: str,
    replacement: str,
) -> None:
    def mutate(value: dict[str, object]) -> None:
        authority = value["authority"]
        assert isinstance(authority, dict)
        authority[field] = replacement

    with pytest.raises(producer.Phase66ModeratedEvidenceError, match="exact primary provider"):
        _prepare(
            private_closure,
            inputs=_inputs(private_closure, observation_mutator=mutate),
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("explicit_consent", False),
        ("first_time_seller", False),
        ("observation_recording_accepted", False),
        ("raw_identity_retained", True),
        ("free_text_value_count", 1),
    ],
)
def test_consent_is_explicit_first_time_and_identity_free(
    private_closure: Path,
    field: str,
    replacement: object,
) -> None:
    def mutate(value: dict[str, object]) -> None:
        value[field] = replacement

    with pytest.raises(producer.Phase66ModeratedEvidenceError, match="closed sanitized schema"):
        _prepare(private_closure, inputs=_inputs(private_closure, consent_mutator=mutate))


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        ("authenticated_access.mfa_complete", 1),
        ("assistance.external_documentation_used", 0),
        ("assistance.operator_intervention_count", False),
        ("publication.publication_attempt_count", 0.0),
        ("privacy.raw_identity_retained", 0),
        ("privacy.free_text_value_count", False),
    ],
)
def test_boolean_and_integer_schema_values_do_not_coerce(
    private_closure: Path,
    path: str,
    replacement: object,
) -> None:
    def mutate(value: dict[str, object]) -> None:
        target, key = _nested(value, path)
        target[key] = replacement

    with pytest.raises(producer.Phase66ModeratedEvidenceError, match="closed sanitized schema"):
        _prepare(
            private_closure,
            inputs=_inputs(private_closure, observation_mutator=mutate),
        )


@pytest.mark.parametrize("field", ("email", "authorization", "provider_payload", "notes"))
def test_raw_identity_authority_payload_and_free_text_have_no_schema(
    private_closure: Path,
    field: str,
) -> None:
    def mutate(value: dict[str, object]) -> None:
        value[field] = "forbidden-value"

    with pytest.raises(producer.Phase66ModeratedEvidenceError, match="closed sanitized schema"):
        _prepare(
            private_closure,
            inputs=_inputs(private_closure, observation_mutator=mutate),
        )


def test_consent_and_provider_timestamps_must_fall_in_session(private_closure: Path) -> None:
    def late_consent(value: dict[str, object]) -> None:
        value["recorded_at"] = "2026-08-31T20:11:00Z"

    with pytest.raises(producer.Phase66ModeratedEvidenceError, match="timestamps"):
        _prepare(private_closure, inputs=_inputs(private_closure, consent_mutator=late_consent))

    def ends_before_provider(value: dict[str, object]) -> None:
        value["completed_at"] = "2026-08-31T20:20:00Z"

    with pytest.raises(producer.Phase66ModeratedEvidenceError, match="timestamps"):
        _prepare(
            private_closure,
            inputs=_inputs(private_closure, observation_mutator=ends_before_provider),
        )


def test_provider_fragment_artifacts_are_verified_before_human_evidence(
    private_closure: Path,
) -> None:
    inputs = _inputs(private_closure)
    ledger = private_closure / "provider-primary" / "provider_call_ledger.json"
    ledger.write_bytes(b'{"result":"passed","tampered":true}\n')
    ledger.chmod(0o600)
    with pytest.raises(producer.Phase66ModeratedEvidenceError, match="authoritative verification"):
        _prepare(private_closure, inputs=inputs)
    assert not (private_closure / producer.OUTPUT_DIRECTORY_NAME).exists()


@pytest.mark.parametrize(
    "binding",
    (
        "provider_records_sha256",
        "provider_artifact_files_sha256",
        "consent_sha256",
        "session_observation_sha256",
    ),
)
def test_every_input_digest_binding_fails_closed(
    private_closure: Path,
    binding: str,
) -> None:
    inputs = _inputs(private_closure)
    inputs[binding] = "0" * 64
    with pytest.raises(producer.Phase66ModeratedEvidenceError, match="exact binding"):
        _prepare(private_closure, inputs=inputs)


def test_root_execution_is_rejected_before_inputs_or_outputs(
    private_closure: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(producer, "_effective_uid", lambda: 0)
    with pytest.raises(producer.Phase66ModeratedEvidenceError, match="never be prepared as root"):
        _prepare(private_closure)
    assert not (private_closure / producer.OUTPUT_DIRECTORY_NAME).exists()


def test_contract_drift_or_missing_caller_binding_fails_closed(
    private_closure: Path,
) -> None:
    with pytest.raises(producer.Phase66ModeratedEvidenceError, match="did not bind"):
        _prepare(private_closure, task_contract_sha256="0" * 64)
    assert not (private_closure / producer.OUTPUT_DIRECTORY_NAME).exists()

    contract = producer.REPOSITORY_ROOT / producer.TASK_CONTRACT_RELATIVE_PATH
    contract.write_bytes(contract.read_bytes() + b"\n")
    with pytest.raises(producer.Phase66ModeratedEvidenceError, match="contract has drifted"):
        _prepare(private_closure)


def test_symlink_hardlink_escape_and_output_reuse_are_rejected(private_closure: Path) -> None:
    inputs = _inputs(private_closure)
    session = inputs["session_observation_path"]
    assert isinstance(session, Path)
    linked = session.with_name("linked-session.json")
    linked.symlink_to(session)
    inputs["session_observation_path"] = linked
    with pytest.raises(producer.Phase66ModeratedEvidenceError, match="stable regular file"):
        _prepare(private_closure, inputs=inputs)

    inputs = _inputs(private_closure)
    consent = inputs["consent_path"]
    assert isinstance(consent, Path)
    hardlink = consent.with_name("hardlinked-consent.json")
    os.link(consent, hardlink)
    with pytest.raises(producer.Phase66ModeratedEvidenceError, match="stable regular file"):
        _prepare(private_closure, inputs=inputs)
    hardlink.unlink()

    outside = private_closure.parent / "outside.json"
    outside.write_bytes(b"{}\n")
    outside.chmod(0o600)
    inputs = _inputs(private_closure)
    inputs["consent_path"] = outside
    with pytest.raises(producer.Phase66ModeratedEvidenceError, match="selected closure"):
        _prepare(private_closure, inputs=inputs)

    inputs = _inputs(private_closure)
    result = _prepare(private_closure, inputs=inputs)
    output = private_closure / producer.OUTPUT_DIRECTORY_NAME
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    with pytest.raises(producer.Phase66ModeratedEvidenceError, match="fresh closure child"):
        _prepare(private_closure, inputs=inputs)
    assert result["result"] == "passed"
    assert before == {path.name: path.read_bytes() for path in output.iterdir()}


def test_closure_root_must_use_the_phase66_acceptance_workspace(
    private_closure: Path,
) -> None:
    unrelated = private_closure.parents[1] / "unrelated-closure"
    unrelated.mkdir(mode=0o700)
    with pytest.raises(producer.Phase66ModeratedEvidenceError, match="private workspace"):
        _prepare(private_closure, closure_root=unrelated)


def test_output_directory_swap_is_detected_before_success(
    private_closure: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_verify = producer._verify_artifacts

    def swap_output(
        declared: object,
        files: object,
        output_root: Path,
        **kwargs: object,
    ) -> int:
        detached = output_root.with_name(f"{output_root.name}-detached")
        output_root.rename(detached)
        output_root.mkdir(mode=0o700)
        for source in detached.iterdir():
            target = output_root / source.name
            target.write_bytes(source.read_bytes())
            target.chmod(0o600)
        return original_verify(declared, files, output_root, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(producer, "_verify_artifacts", swap_output)
    with pytest.raises(producer.Phase66ModeratedEvidenceError, match="output path changed"):
        _prepare(private_closure)


def test_written_control_file_mutation_is_detected_before_success(
    private_closure: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_verify = producer._verify_artifacts

    def mutate_control(
        declared: object,
        files: object,
        output_root: Path,
        **kwargs: object,
    ) -> int:
        control = output_root / producer.RECORDS_FILENAME
        control.write_bytes(b"[]\n")
        control.chmod(0o600)
        return original_verify(declared, files, output_root, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(producer, "_verify_artifacts", mutate_control)
    with pytest.raises(producer.Phase66ModeratedEvidenceError, match="control or artifact"):
        _prepare(private_closure)


def test_output_schema_is_validated_before_directory_creation(
    private_closure: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(private_closure)

    def reject(_value: object) -> object:
        raise ValueError("schema rejected")

    monkeypatch.setattr(producer, "validate_phase66_evidence", reject)
    with pytest.raises(producer.Phase66ModeratedEvidenceError, match="authoritative schema"):
        _prepare(private_closure, inputs=inputs)
    assert not (private_closure / producer.OUTPUT_DIRECTORY_NAME).exists()


def test_tool_has_no_network_cloud_browser_or_provider_client_imports() -> None:
    source = Path(producer.__file__).read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".", 1)[0])
    assert imported.isdisjoint(
        {
            "boto3",
            "botocore",
            "httpx",
            "playwright",
            "requests",
            "urllib",
        }
    )
    assert "--task-contract-sha256" in producer._parser().format_help()
    assert "provider_write_gate" not in producer._parser().format_help()


def test_frozen_task_contract_extra_or_reordered_fields_are_rejected() -> None:
    path = producer.REPOSITORY_ROOT / producer.TASK_CONTRACT_RELATIVE_PATH
    value = json.loads(path.read_bytes())
    changed = deepcopy(value)
    changed["manual_journeys"] = list(reversed(changed["manual_journeys"]))
    with pytest.raises(ValidationError, match="task contract has drifted"):
        producer._FrozenTaskContract.model_validate_json(_canonical(changed))
    changed = deepcopy(value)
    changed["notes"] = "free text"
    with pytest.raises(ValidationError):
        producer._FrozenTaskContract.model_validate_json(_canonical(changed))
