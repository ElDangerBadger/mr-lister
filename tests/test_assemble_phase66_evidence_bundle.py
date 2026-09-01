from __future__ import annotations

import io
import json
import os
import zipfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from mr_lister.acceptance.phase6 import (
    PHASE66_FIRST_TIME_SELLER_TASK_SHA256,
    AcceptanceEvidenceClass,
    ArtifactFormat,
    ArtifactKind,
    phase66_acceptance_manifest,
    phase66_manifest_digest,
    validate_phase66_evidence,
)
from tools import assemble_phase66_evidence_bundle as assembler


def _digest(label: str) -> str:
    return sha256(label.encode("utf-8")).hexdigest()


def _render(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _json_artifact(gate_id: str, kind: ArtifactKind) -> bytes:
    return _render(
        {
            "artifact_contract": "phase6.6-sanitized-test-artifact-v1",
            "gate": gate_id,
            "kind": kind.value,
            "result": "passed",
        }
    )


def _trace_artifact(gate_id: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("trace.trace", b'{"type":"sanitized-browser-trace"}\n')
        archive.writestr("summary.json", _json_artifact(gate_id, ArtifactKind.BROWSER_TRACE))
    return output.getvalue()


def _provider_summary(gate_id: str) -> dict[str, object]:
    if gate_id == "provider.primary_same_job_canary":
        counts = (1, 1, 2, 1, "unpublished_unlocked")
    elif gate_id == "provider.concurrency_canary":
        counts = (0, 0, 1, 1, "unpublished_unlocked")
    else:
        counts = (0, 0, 0, 0, "not_created")
    return {
        "artwork_upload_count": counts[0],
        "product_post_count": counts[1],
        "product_put_count": counts[2],
        "product_get_count": counts[3],
        "forbidden_attempt_count": 0,
        "publish_attempt_count": 0,
        "order_attempt_count": 0,
        "fulfillment_attempt_count": 0,
        "final_state": counts[4],
    }


def _moderated_artifact(record: dict[str, Any], provider: dict[str, Any]) -> bytes:
    session = record["moderated_session"]
    provider_digest = sha256(
        json.dumps(
            validate_phase66_evidence(provider).model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return _render(
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
        }
    )


def _artifact_filename(kind: ArtifactKind) -> str:
    if kind is ArtifactKind.BROWSER_TRACE:
        return "browser_trace.zip"
    return f"{kind.value}.json"


@dataclass
class _Fragment:
    root: Path
    records: list[dict[str, Any]]
    artifact_files: list[dict[str, Any]]

    def authority(self) -> assembler.FragmentAuthority:
        records_payload = _render(self.records)
        files_payload = _render(self.artifact_files)
        _write_private(self.root / assembler.RECORDS_FILENAME, records_payload)
        _write_private(self.root / assembler.ARTIFACT_FILES_FILENAME, files_payload)
        return assembler.FragmentAuthority(
            root=self.root,
            records_sha256=sha256(records_payload).hexdigest(),
            artifact_files_sha256=sha256(files_payload).hexdigest(),
        )


@dataclass
class _CompleteFragments:
    workspace: Path
    fragments: list[_Fragment]
    source_digest: str
    deployment_digest: str

    def authorities(self) -> tuple[assembler.FragmentAuthority, ...]:
        return tuple(fragment.authority() for fragment in self.fragments)

    def fragment(self, gate_id: str) -> _Fragment:
        return next(
            fragment for fragment in self.fragments if fragment.records[0]["gate_id"] == gate_id
        )


def _write_private(path: Path, contents: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = path.parent
    while current.name and current != current.parent:
        if ".mr_lister_private" in current.parts:
            current.chmod(0o700)
        current = current.parent
    path.write_bytes(contents)
    path.chmod(0o600)


@pytest.fixture
def complete_fragments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _CompleteFragments:
    repository = tmp_path / "repository"
    workspace = repository / ".mr_lister_private" / "phase66-acceptance"
    workspace.mkdir(mode=0o700, parents=True)
    (repository / ".mr_lister_private").chmod(0o700)
    workspace.chmod(0o700)
    monkeypatch.setattr(assembler, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(assembler, "PRIVATE_WORKSPACE_ROOT", workspace)

    source_digest = _digest("source-commit")
    deployment_digest = _digest("deployment")
    primary_run = _digest("moderated-provider-run")
    primary_job = _digest("provider-primary-job")
    fragments: list[_Fragment] = []
    for gate_number, gate in enumerate(phase66_acceptance_manifest().gates):
        if not gate.blocking_phase6_exit:
            continue
        root = workspace / f"fragment-{gate_number:02d}"
        root.mkdir(mode=0o700)
        artifacts: list[dict[str, Any]] = []
        artifact_files: list[dict[str, Any]] = []
        for kind in gate.required_artifact_kinds:
            filename = _artifact_filename(kind)
            contents = (
                _trace_artifact(gate.gate_id)
                if kind is ArtifactKind.BROWSER_TRACE
                else _json_artifact(gate.gate_id, kind)
            )
            _write_private(root / filename, contents)
            digest = sha256(contents).hexdigest()
            artifact_format = (
                ArtifactFormat.ZIP if kind is ArtifactKind.BROWSER_TRACE else ArtifactFormat.JSON
            )
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
                    "relative_path": filename,
                }
            )

        run_digest = (
            primary_run
            if gate.gate_id
            in {
                "provider.primary_same_job_canary",
                "moderated.first_time_seller_exit",
            }
            else _digest(f"run-{gate.gate_id}")
        )
        record: dict[str, Any] = {
            "schema_version": "6.6.0",
            "manifest_digest": phase66_manifest_digest(),
            "run_digest": run_digest,
            "source_commit_digest": source_digest,
            "gate_id": gate.gate_id,
            "evidence_class": gate.evidence_class.value,
            "outcome": "passed",
            "recorded_at": f"2026-08-23T06:{gate_number:02d}:00Z",
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
            actor_count = 2 if gate.gate_id == "deployed.edge_auth_owner_smoke" else 1
            record.update(
                {
                    "deployment_digest": deployment_digest,
                    "actor_digests": [
                        _digest(f"{gate.gate_id}-actor-{number}") for number in range(actor_count)
                    ],
                }
            )
        elif gate.evidence_class is AcceptanceEvidenceClass.PROVIDER_DESTRUCTIVE:
            record.update(
                {
                    "deployment_digest": deployment_digest,
                    "actor_digests": [_digest(f"{gate.gate_id}-actor")],
                    "job_digest": (
                        primary_job
                        if gate.gate_id == "provider.primary_same_job_canary"
                        else _digest(f"{gate.gate_id}-job")
                    ),
                    "provider_gate_attestation": {
                        "run_gate_digest": _digest(f"{gate.gate_id}-run-gate"),
                        "provider_write_gate_digest": _digest(f"{gate.gate_id}-write-gate"),
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
                record["work_digest"] = _digest("primary-work")
                record["correlation_digest"] = _digest("primary-correlation")
        elif gate.evidence_class is AcceptanceEvidenceClass.MODERATED_USER:
            record.update(
                {
                    "deployment_digest": deployment_digest,
                    "actor_digests": [_digest("provider.primary_same_job_canary-actor")],
                    "job_digest": primary_job,
                    "work_digest": _digest("primary-work"),
                    "correlation_digest": _digest("primary-correlation"),
                    "moderated_session": {
                        "participant_digest": _digest("participant"),
                        "consent_record_digest": _digest("consent"),
                        "task_script_digest": PHASE66_FIRST_TIME_SELLER_TASK_SHA256,
                        "session_record_digest": _digest("session"),
                        "first_time_seller": True,
                        "external_documentation_used": False,
                        "operator_intervention_count": 0,
                        "completed_supported_flow": True,
                        "duration_seconds": 600,
                    },
                }
            )
        fragments.append(_Fragment(root=root, records=[record], artifact_files=artifact_files))
    provider_fragment = next(
        fragment
        for fragment in fragments
        if fragment.records[0]["gate_id"] == "provider.primary_same_job_canary"
    )
    moderated_fragment = next(
        fragment
        for fragment in fragments
        if fragment.records[0]["gate_id"] == "moderated.first_time_seller_exit"
    )
    moderated_record = moderated_fragment.records[0]
    moderated_evidence = next(
        artifact
        for artifact in moderated_record["artifacts"]
        if artifact["kind"] == ArtifactKind.MODERATED_SESSION_RECORD.value
    )
    moderated_reference = next(
        reference
        for reference in moderated_fragment.artifact_files
        if reference["artifact_digest"] == moderated_evidence["artifact_digest"]
    )
    moderated_contents = _moderated_artifact(moderated_record, provider_fragment.records[0])
    _write_private(
        moderated_fragment.root / moderated_reference["relative_path"], moderated_contents
    )
    moderated_digest = sha256(moderated_contents).hexdigest()
    moderated_evidence["artifact_digest"] = moderated_digest
    moderated_evidence["byte_count"] = len(moderated_contents)
    moderated_reference["artifact_digest"] = moderated_digest
    return _CompleteFragments(
        workspace=workspace,
        fragments=fragments,
        source_digest=source_digest,
        deployment_digest=deployment_digest,
    )


def _assemble(fixture: _CompleteFragments, output_name: str = "assembled") -> dict[str, object]:
    return assembler.assemble_phase66_evidence_bundle(
        output_root=fixture.workspace / output_name,
        fragments=fixture.authorities(),
        expected_source_commit_digest=fixture.source_digest,
        expected_deployment_digest=fixture.deployment_digest,
    )


def test_assembles_gate_namespaced_create_only_bundle_without_rewriting_records(
    complete_fragments: _CompleteFragments,
) -> None:
    original_records = [
        record for fragment in complete_fragments.fragments for record in fragment.records
    ]

    result = _assemble(complete_fragments)

    output = complete_fragments.workspace / "assembled"
    assembled_records = json.loads((output / assembler.RECORDS_FILENAME).read_bytes())
    artifact_files = json.loads((output / assembler.ARTIFACT_FILES_FILENAME).read_bytes())
    assert result["result"] == "passed"
    assert result["fragment_count"] == 11
    assert result["record_count"] == 11
    assert result["artifact_count"] == 20
    assert sorted(_render(record) for record in assembled_records) == sorted(
        _render(record) for record in original_records
    )
    assert all(
        reference["relative_path"].startswith(f"{assembler.GATE_DIRECTORY}/")
        for reference in artifact_files
    )
    for reference in artifact_files:
        gate_id = next(
            record["gate_id"]
            for record in assembled_records
            if any(
                artifact["artifact_digest"] == reference["artifact_digest"]
                for artifact in record["artifacts"]
            )
        )
        assert reference["relative_path"].startswith(f"gates/{gate_id}/")
        assert (output / reference["relative_path"]).stat().st_mode & 0o777 == 0o600
    assert output.stat().st_mode & 0o777 == 0o700
    assert str(complete_fragments.workspace) not in json.dumps(result)


def test_rejects_a_caller_control_digest_mismatch_before_creating_output(
    complete_fragments: _CompleteFragments,
) -> None:
    authorities = list(complete_fragments.authorities())
    authorities[0] = assembler.FragmentAuthority(
        root=authorities[0].root,
        records_sha256="0" * 64,
        artifact_files_sha256=authorities[0].artifact_files_sha256,
    )

    with pytest.raises(
        assembler.Phase66EvidenceBundleAssemblyError,
        match="caller SHA-256 authority",
    ):
        assembler.assemble_phase66_evidence_bundle(
            output_root=complete_fragments.workspace / "rejected",
            fragments=authorities,
            expected_source_commit_digest=complete_fragments.source_digest,
            expected_deployment_digest=complete_fragments.deployment_digest,
        )

    assert not (complete_fragments.workspace / "rejected").exists()


def test_rejects_stale_or_extra_fragment_files(
    complete_fragments: _CompleteFragments,
) -> None:
    extra = complete_fragments.fragments[0].root / "stale-summary.json"
    _write_private(extra, _json_artifact("offline.replay_matrix", ArtifactKind.TEST_REPORT))

    with pytest.raises(
        assembler.Phase66EvidenceBundleAssemblyError,
        match="stale, missing, or extra",
    ):
        _assemble(complete_fragments)


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_rejects_fragment_symlinks_and_hardlinks(
    complete_fragments: _CompleteFragments,
    alias_kind: str,
) -> None:
    fragment = complete_fragments.fragments[0]
    artifact = fragment.root / fragment.artifact_files[0]["relative_path"]
    outside = complete_fragments.workspace / f"{alias_kind}-authority.json"
    if alias_kind == "symlink":
        _write_private(outside, artifact.read_bytes())
        artifact.unlink()
        artifact.symlink_to(outside)
    else:
        os.link(artifact, outside)

    with pytest.raises(assembler.Phase66EvidenceBundleAssemblyError):
        _assemble(complete_fragments)


def test_rejects_duplicate_fragment_paths_and_artifact_digests(
    complete_fragments: _CompleteFragments,
) -> None:
    authorities = complete_fragments.authorities()
    with pytest.raises(
        assembler.Phase66EvidenceBundleAssemblyError,
        match="root paths must be unique",
    ):
        assembler.assemble_phase66_evidence_bundle(
            output_root=complete_fragments.workspace / "duplicate-root",
            fragments=(*authorities, authorities[0]),
            expected_source_commit_digest=complete_fragments.source_digest,
            expected_deployment_digest=complete_fragments.deployment_digest,
        )

    first = complete_fragments.fragment("offline.replay_matrix")
    second = complete_fragments.fragment("offline.concurrency_matrix")
    first_path = first.root / first.artifact_files[0]["relative_path"]
    second_path = second.root / second.artifact_files[0]["relative_path"]
    second_path.write_bytes(first_path.read_bytes())
    second_path.chmod(0o600)
    duplicated = first.records[0]["artifacts"][0].copy()
    second.records[0]["artifacts"][0] = duplicated
    second.artifact_files[0].update(
        {
            "artifact_digest": duplicated["artifact_digest"],
            "artifact_format": duplicated["artifact_format"],
            "kind": duplicated["kind"],
        }
    )

    with pytest.raises(
        assembler.Phase66EvidenceBundleAssemblyError,
        match="prerequisite-closed",
    ):
        _assemble(complete_fragments, "duplicate-digest")


def test_rejects_casefolded_output_ancestry_without_mutating_fragment(
    complete_fragments: _CompleteFragments,
) -> None:
    authorities = complete_fragments.authorities()
    fragment = complete_fragments.fragments[0]
    before = sorted(
        (path.relative_to(fragment.root).as_posix(), path.stat().st_mode, path.stat().st_size)
        for path in fragment.root.rglob("*")
    )
    aliased_output = fragment.root.with_name(fragment.root.name.swapcase()) / "assembled"

    with pytest.raises(
        assembler.Phase66EvidenceBundleAssemblyError,
        match="output root and fragment roots must be disjoint",
    ):
        assembler.assemble_phase66_evidence_bundle(
            output_root=aliased_output,
            fragments=authorities,
            expected_source_commit_digest=complete_fragments.source_digest,
            expected_deployment_digest=complete_fragments.deployment_digest,
        )

    after = sorted(
        (path.relative_to(fragment.root).as_posix(), path.stat().st_mode, path.stat().st_size)
        for path in fragment.root.rglob("*")
    )
    assert after == before
    assert not (fragment.root / "assembled").exists()


def test_create_output_rejects_fragment_parent_inode_before_mutation(
    complete_fragments: _CompleteFragments,
) -> None:
    fragment = complete_fragments.fragments[0]
    output = fragment.root / "physical-alias"
    identity = assembler._Metadata.from_stat(fragment.root.stat()).identity

    with pytest.raises(
        assembler.Phase66EvidenceBundleAssemblyError,
        match="output parent cannot alias a fragment directory",
    ):
        assembler._create_output_root(
            output,
            forbidden_parent_identities=frozenset({identity}),
        )

    assert not output.exists()


def test_rejects_duplicate_artifact_index_paths(
    complete_fragments: _CompleteFragments,
) -> None:
    fragment = complete_fragments.fragment("provider.primary_same_job_canary")
    fragment.artifact_files[1]["relative_path"] = fragment.artifact_files[0]["relative_path"]

    with pytest.raises(
        assembler.Phase66EvidenceBundleAssemblyError,
        match="artifact index, or artifact failed validation",
    ):
        _assemble(complete_fragments, "duplicate-artifact-path")


@pytest.mark.parametrize("drift", ["source", "deployment", "prerequisite", "missing"])
def test_rejects_source_deployment_prerequisite_and_fragment_set_drift(
    complete_fragments: _CompleteFragments,
    drift: str,
) -> None:
    fragments = complete_fragments.fragments
    if drift == "source":
        fragments[0].records[0]["source_commit_digest"] = _digest("stale-source")
        message = "stale for the expected source"
    elif drift == "deployment":
        complete_fragments.fragment("deployed.edge_auth_owner_smoke").records[0][
            "deployment_digest"
        ] = _digest("stale-deployment")
        message = "stale for the expected deployment"
    elif drift == "prerequisite":
        complete_fragments.fragment("offline.concurrency_matrix").records[0]["recorded_at"] = (
            "2026-08-23T05:59:59Z"
        )
        message = "prerequisite-closed"
    else:
        complete_fragments.fragments = fragments[:-1]
        message = "prerequisite-closed"

    with pytest.raises(assembler.Phase66EvidenceBundleAssemblyError, match=message):
        _assemble(complete_fragments, f"drift-{drift}")


def test_rejects_an_input_mutation_during_copy(
    complete_fragments: _CompleteFragments,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = assembler._copy_artifact
    mutated = False

    def copy_then_mutate(**arguments: Any) -> None:
        nonlocal mutated
        original(**arguments)
        if not mutated:
            fragment = arguments["fragment"]
            reference = arguments["reference"]
            source = fragment.root / reference.relative_path
            source.write_bytes(source.read_bytes() + b" ")
            source.chmod(0o600)
            mutated = True

    monkeypatch.setattr(assembler, "_copy_artifact", copy_then_mutate)

    with pytest.raises(
        assembler.Phase66EvidenceBundleAssemblyError,
        match="changed during bundle assembly",
    ):
        _assemble(complete_fragments)


def test_existing_output_is_never_overwritten(
    complete_fragments: _CompleteFragments,
) -> None:
    output = complete_fragments.workspace / "existing"
    output.mkdir(mode=0o700)
    sentinel = output / "sentinel"
    _write_private(sentinel, b"preserve-me")

    with pytest.raises(
        assembler.Phase66EvidenceBundleAssemblyError,
        match="fresh private directory",
    ):
        _assemble(complete_fragments, "existing")

    assert sentinel.read_bytes() == b"preserve-me"
