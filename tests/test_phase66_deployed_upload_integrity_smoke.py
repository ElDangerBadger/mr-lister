from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from tools import phase66_deployed_upload_integrity_smoke as smoke

OWNER: Final = "a" * 64
SUBJECT: Final = "subject-secret"
JOB: Final = "job_secret"
BUCKET: Final = "private-bucket-secret"
SOURCE_KEY: Final = f"private/owners/{OWNER}/jobs/{JOB}/source/source.png"
SOURCE_VERSION: Final = "source-version-secret"
UPLOAD_ID: Final = "upload_secret"
UPLOAD_JOB: Final = "upload_job_secret"
RESERVED_KEY: Final = f"private/owners/{OWNER}/jobs/{UPLOAD_JOB}/source/source.png"
ISSUER: Final = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_secret"
CLIENT_ID: Final = "client-secret"
NOW: Final = datetime(2026, 8, 29, 22, 0, tzinfo=UTC)


def _authority() -> smoke.Authority:
    return smoke.Authority(
        owner_id=OWNER,
        subject=SUBJECT,
        job_id=JOB,
        bucket=BUCKET,
        source_key=SOURCE_KEY,
        source_version=SOURCE_VERSION,
        issuer=ISSUER,
        client_id=CLIENT_ID,
        upload_function="upload-function-secret",
        review_function="review-function-secret",
        table_name="table-secret",
        state_machine_arns=("state-machine-secret",),
    )


def _job() -> dict[str, Any]:
    return {
        "job_id": JOB,
        "owner_id": OWNER,
        "state": "failed_retryable",
        "updated_at": "2026-08-29T21:00:00Z",
    }


def _source() -> dict[str, Any]:
    return {
        "job_id": JOB,
        "owner_id": OWNER,
        "bucket": BUCKET,
        "object_key": SOURCE_KEY,
        "version_id": SOURCE_VERSION,
        "content_sha256": smoke.PRIMARY_SHA256,
        "fingerprint": "f" * 64,
    }


def _inventory() -> tuple[smoke.InventoryVersion, ...]:
    return (
        smoke.InventoryVersion(
            version_id=SOURCE_VERSION,
            is_latest=True,
            last_modified="2026-08-29T20:00:00Z",
            size_bytes=smoke.PRIMARY_SIZE,
            etag='"etag-secret"',
        ),
    )


def _before() -> smoke.Snapshot:
    job = _job()
    source = _source()
    return smoke.Snapshot(
        authority=_authority(),
        items=(
            ("CONTROL_JOB", job),
            ("SOURCE_ARTIFACT", source),
            ("DOMAIN_EVENT", {"job_id": JOB, "sequence": 1}),
            ("REVIEW", {"job_id": JOB, "review_version": 2}),
        ),
        selected_job=job,
        selected_source=source,
        inventory=_inventory(),
        execution_digests=("e" * 64,),
    )


def _after(*, mutate_event: bool = False) -> smoke.Snapshot:
    before = _before()
    items = [(kind, deepcopy(payload)) for kind, payload in before.items]
    if mutate_event:
        items[2][1]["sequence"] = 2
    items.extend(
        (
            (
                "UPLOAD_INTENT",
                {
                    "upload_id": UPLOAD_ID,
                    "job_id": UPLOAD_JOB,
                    "owner_id": OWNER,
                    "bucket": BUCKET,
                    "object_key": RESERVED_KEY,
                    "content_sha256": smoke.PRIMARY_SHA256,
                    "content_type": "image/png",
                    "size_bytes": smoke.PRIMARY_SIZE,
                    "status": "open",
                },
            ),
            (
                "UPLOAD_RECEIPT",
                {
                    "upload_id": UPLOAD_ID,
                    "job_id": UPLOAD_JOB,
                    "owner_id": OWNER,
                    "command_type": "create_upload",
                },
            ),
        )
    )
    return replace(before, items=tuple(items))


def _gate_document(before: smoke.Snapshot | None = None) -> dict[str, Any]:
    before = before or _before()
    source = before.selected_source
    authority = before.authority
    job_ids = sorted(
        smoke._digest_text(payload["job_id"])
        for entity, payload in before.items
        if entity == "CONTROL_JOB"
    )
    return {
        "authorization_contract": smoke.GATE_CONTRACT,
        "gate_id": smoke.GATE_ID,
        "source_authority_commit": smoke.SOURCE_AUTHORITY_COMMIT,
        "source_authority_commit_digest": smoke.SOURCE_AUTHORITY_COMMIT_DIGEST,
        "deployment_digest": "d" * 64,
        "prerequisite_evidence_run_digest": "b" * 64,
        "alternate_method_authorization": dict(smoke._EXPECTED_METHOD_AUTHORIZATION),
        "exact_write_budget": dict(smoke._EXPECTED_BUDGET),
        "canaries": {
            "primary": {
                "byte_count": smoke.PRIMARY_SIZE,
                "sha256": smoke.PRIMARY_SHA256,
            },
            "wrong_bytes": {
                "byte_count": smoke.PRIMARY_SIZE,
                "mutation": "xor_0x01_at_zero_based_file_offset_1048576",
                "sha256": smoke.WRONG_SHA256,
            },
            "overwrite": {
                "byte_count": smoke.OVERWRITE_SIZE,
                "sha256": smoke.OVERWRITE_SHA256,
            },
        },
        "baseline": {
            "actor_digest": smoke._digest_text(authority.owner_id),
            "bucket_versioning_enabled": True,
            "existing_job_count": 1,
            "existing_job_set_digest": smoke._digest_json(job_ids),
            "existing_job_states": ["failed_retryable"],
            "provider_record_count": 0,
            "running_execution_count": 0,
            "selected_inventory_count": 1,
            "selected_inventory_digest": smoke._inventory_digest(before.inventory),
            "selected_job_digest": smoke._digest_text(authority.job_id),
            "selected_job_record_digest": smoke._digest_json(before.selected_job),
            "selected_object_coordinate_digest": smoke._digest_text(
                authority.bucket + "\0" + authority.source_key
            ),
            "selected_pinned_is_latest": True,
            "selected_pinned_version_digest": smoke._digest_text(authority.source_version),
            "selected_source_authority_digest": smoke._digest_json(
                {
                    key: source[key]
                    for key in (
                        "bucket",
                        "object_key",
                        "version_id",
                        "content_sha256",
                        "fingerprint",
                    )
                }
            ),
            "selected_source_record_digest": smoke._digest_json(source),
            "selected_version_head_matches_exact_canary": True,
            "selected_version_tag_is_pinned": True,
            "table_record_count": len(before.items),
        },
    }


def _gate(document: dict[str, Any] | None = None) -> smoke.RunGate:
    return smoke.RunGate(digest="9" * 64, document=document or _gate_document())


def _upload_response() -> dict[str, Any]:
    checksum = smoke.base64.b64encode(bytes.fromhex(smoke.PRIMARY_SHA256)).decode()
    return {
        "statusCode": 201,
        "headers": {},
        "body": json.dumps(
            {
                "upload": {
                    "upload_id": UPLOAD_ID,
                    "job_id": UPLOAD_JOB,
                    "status": "open",
                    "record_version": 1,
                },
                "authorization": {
                    "upload_id": UPLOAD_ID,
                    "job_id": UPLOAD_JOB,
                    "authorization_generation": 1,
                    "method": "POST",
                    "url": f"https://{BUCKET}.s3.us-west-2.amazonaws.com/",
                    "form_fields": {
                        "key": RESERVED_KEY,
                        "Content-Type": "image/png",
                        "x-amz-checksum-algorithm": "SHA256",
                        "x-amz-checksum-sha256": checksum,
                        "x-amz-server-side-encryption": "AES256",
                        "x-amz-tagging": "mr-lister-state=staged",
                        "x-amz-algorithm": "AWS4-HMAC-SHA256",
                        "x-amz-credential": "presigned-credential-secret",
                        "x-amz-date": "20260829T220000Z",
                        "policy": "presigned-policy-secret",
                        "x-amz-signature": "1" * 64,
                    },
                    "content_sha256": smoke.PRIMARY_SHA256,
                    "size_bytes": smoke.PRIMARY_SIZE,
                    "issued_at": NOW.isoformat(),
                    "expires_at": (NOW + timedelta(seconds=60)).isoformat(),
                },
            }
        ),
        "isBase64Encoded": False,
    }


def _review_response() -> dict[str, Any]:
    return {
        "statusCode": 302,
        "headers": {
            "Location": (
                f"https://{BUCKET}.s3.us-west-2.amazonaws.com/source.png"
                f"?versionId={SOURCE_VERSION}&X-Amz-Expires=300"
            )
        },
        "body": "",
        "isBase64Encoded": False,
    }


class FakeBackend:
    def __init__(self, *, after: smoke.Snapshot | None = None) -> None:
        self.before = _before()
        self.after = after or _after()
        self.calls: list[tuple[str, object]] = []
        self.post_statuses = [403, 403, 403]
        self.prove_error: Exception | None = None
        self.during = (
            replace(self.before.inventory[0], is_latest=False),
            smoke.InventoryVersion(
                version_id="temporary-version-secret",
                is_latest=True,
                last_modified="2026-08-29T22:01:00Z",
                size_bytes=smoke.OVERWRITE_SIZE,
                etag='"temporary-etag-secret"',
            ),
        )

    def prepare(self, gate: smoke.RunGate, primary: bytes) -> smoke.Snapshot:
        self.calls.append(("prepare", (gate.digest, smoke._digest_bytes(primary))))
        return self.before

    def invoke_upload(
        self, authority: smoke.Authority, event: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.calls.append(("invoke_upload", event))
        return _upload_response()

    def invoke_review(
        self, authority: smoke.Authority, event: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.calls.append(("invoke_review", event))
        return _review_response()

    def post_form(
        self,
        url: str,
        fields: Mapping[str, str],
        content: bytes,
        *,
        content_type: str,
    ) -> int:
        self.calls.append(
            (
                "post_form",
                {
                    "url": url,
                    "fields": dict(fields),
                    "content_digest": smoke._digest_bytes(content),
                    "content_type": content_type,
                },
            )
        )
        return self.post_statuses.pop(0)

    def count_exact_versions(self, authority: smoke.Authority, key: str) -> int:
        self.calls.append(("count_exact_versions", key))
        return 0

    def get_preview(self, url: str) -> bytes:
        self.calls.append(("get_preview", url))
        return smoke.exact_canaries()[0]

    def put_temporary(self, authority: smoke.Authority, content: bytes) -> str:
        self.calls.append(("put_temporary", smoke._digest_bytes(content)))
        return "temporary-version-secret"

    def prove_temporary(self, authority: smoke.Authority, version_id: str, content: bytes) -> None:
        self.calls.append(("prove_temporary", version_id))
        if self.prove_error is not None:
            raise self.prove_error

    def delete_temporary(self, authority: smoke.Authority, version_id: str) -> None:
        self.calls.append(("delete_temporary", version_id))

    def inventory(self, authority: smoke.Authority) -> tuple[smoke.InventoryVersion, ...]:
        self.calls.append(("inventory", None))
        inventory_calls = sum(name == "inventory" for name, _value in self.calls)
        return self.during if inventory_calls == 1 else self.before.inventory

    def snapshot(self, authority: smoke.Authority) -> smoke.Snapshot:
        self.calls.append(("snapshot", None))
        return self.after

    def wait_until(self, timestamp: datetime) -> None:
        self.calls.append(("wait_until", timestamp))


def _private_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    private = repository / ".mr_lister_private" / "phase66-acceptance"
    private.mkdir(parents=True, mode=0o700)
    repository.chmod(0o700)
    (repository / ".mr_lister_private").chmod(0o700)
    private.chmod(0o700)
    monkeypatch.setattr(smoke, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(smoke, "PRIVATE_ROOT", private)
    return private


def _write_gate(private: Path, document: dict[str, Any] | None = None) -> tuple[Path, str]:
    path = private / "run" / "gate.json"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    payload = smoke._canonical_json(document or _gate_document(), pretty=True)
    path.write_bytes(payload)
    path.chmod(0o600)
    return path, smoke._digest_bytes(payload)


def test_default_cli_is_local_only_and_never_constructs_live_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    gate_path, gate_digest = _write_gate(private)
    constructed = False

    def forbidden_factory() -> FakeBackend:
        nonlocal constructed
        constructed = True
        raise AssertionError("default preflight constructed a live backend")

    assert (
        smoke.main(
            ["--gate", str(gate_path), "--gate-sha256", gate_digest],
            backend_factory=forbidden_factory,
        )
        == 0
    )

    assert constructed is False
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "local_preflight"
    assert result["network_calls"] == result["mutations"] == 0


def test_gate_requires_exact_digest_code_authority_method_and_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    document = _gate_document()
    gate_path, gate_digest = _write_gate(private, document)

    assert smoke.load_run_gate(gate_path, gate_digest).digest == gate_digest
    with pytest.raises(smoke.SmokeError, match="does not match"):
        smoke.load_run_gate(gate_path, "0" * 64)

    document["source_authority_commit"] = "0" * 40
    gate_path, gate_digest = _write_gate(private, document)
    with pytest.raises(smoke.SmokeError, match="source authority commit"):
        smoke.load_run_gate(gate_path, gate_digest)


def test_live_path_executes_exact_budget_and_emits_only_sanitized_private_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    backend = FakeBackend()
    output = private / "run" / "evidence"

    result = smoke.run_live(_gate(), backend, output)

    names = [name for name, _value in backend.calls]
    assert Counter(names)["invoke_upload"] == 1
    assert Counter(names)["invoke_review"] == 2
    assert Counter(names)["post_form"] == 3
    assert Counter(names)["put_temporary"] == 1
    assert Counter(names)["delete_temporary"] == 1
    upload_events = [value for name, value in backend.calls if name == "invoke_upload"]
    assert [event["routeKey"] for event in upload_events] == ["POST /v1/uploads"]
    assert result["status"] == "passed"
    canary = json.loads((output / "canary-summary.json").read_bytes())
    audit = json.loads((output / "log-audit.json").read_bytes())
    assert canary["artifact_contract"] == smoke.RAW_CANARY_CONTRACT
    assert audit["artifact_contract"] == smoke.RAW_LOG_CONTRACT
    assert canary["execution_authority"] == audit["execution_authority"]
    assert result["execution_digest"] == canary["execution_authority"]["execution_digest"]
    retained = b"".join(path.read_bytes() for path in sorted(output.iterdir()))
    for secret in (
        OWNER,
        SUBJECT,
        JOB,
        BUCKET,
        SOURCE_KEY,
        SOURCE_VERSION,
        RESERVED_KEY,
        CLIENT_ID,
        "presigned-policy-secret",
        str(output),
    ):
        assert secret.encode() not in retained
    assert {path.stat().st_mode & 0o777 for path in output.iterdir()} == {0o600}


def test_each_live_run_gets_one_distinct_shared_execution_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    first_output = private / "first"
    second_output = private / "second"
    first = smoke.run_live(_gate(), FakeBackend(), first_output)
    second = smoke.run_live(_gate(), FakeBackend(), second_output)
    first_canary = json.loads((first_output / "canary-summary.json").read_bytes())
    first_log = json.loads((first_output / "log-audit.json").read_bytes())
    second_canary = json.loads((second_output / "canary-summary.json").read_bytes())
    second_log = json.loads((second_output / "log-audit.json").read_bytes())

    assert first_canary["execution_authority"] == first_log["execution_authority"]
    assert second_canary["execution_authority"] == second_log["execution_authority"]
    assert first["execution_digest"] != second["execution_digest"]
    assert first_canary["execution_authority"]["completed_at"].endswith("Z")


def test_live_run_requires_fresh_output_before_backend_operations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    output = private / "existing"
    output.mkdir(mode=0o700)
    marker = output / "marker"
    marker.write_bytes(b"unchanged")
    marker.chmod(0o600)
    backend = FakeBackend()

    with pytest.raises(smoke.SmokeError, match="fresh"):
        smoke.run_live(_gate(), backend, output)

    assert backend.calls == []
    assert marker.read_bytes() == b"unchanged"


def test_failed_temporary_proof_still_cleans_only_returned_exact_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    backend = FakeBackend()
    backend.prove_error = smoke.SmokeError("proof failed")

    with pytest.raises(smoke.SmokeError, match="proof failed"):
        smoke.run_live(_gate(), backend, private / "proof-failure")

    deletes = [value for name, value in backend.calls if name == "delete_temporary"]
    assert deletes == ["temporary-version-secret"]


def test_existing_domain_event_content_mutation_fails_exact_record_delta(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    backend = FakeBackend(after=_after(mutate_event=True))

    with pytest.raises(smoke.SmokeError, match="existing DynamoDB record changed"):
        smoke.run_live(_gate(), backend, private / "delta-failure")


def test_negative_probe_success_stops_before_any_temporary_overwrite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    backend = FakeBackend()
    backend.post_statuses[0] = 204

    with pytest.raises(smoke.SmokeError, match="definitive rejection"):
        smoke.run_live(_gate(), backend, private / "probe-failure")

    names = [name for name, _value in backend.calls]
    assert "put_temporary" not in names
    assert "delete_temporary" not in names


def test_private_gate_rejects_group_readable_file_and_out_of_root_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    gate_path, gate_digest = _write_gate(private)
    gate_path.chmod(0o640)
    with pytest.raises(smoke.SmokeError, match="mode-0600"):
        smoke.load_run_gate(gate_path, gate_digest)

    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    outside.chmod(0o600)
    with pytest.raises(smoke.SmokeError, match="repository workspace"):
        smoke.load_run_gate(outside, smoke._digest_bytes(b"{}"))


def test_private_gate_open_is_anchored_across_parent_symlink_swap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    gate_path, gate_digest = _write_gate(private)
    parent = gate_path.parent
    relocated = parent.with_name("run-relocated")
    outside = private / "swap-target"
    outside.mkdir(mode=0o700)
    decoy = outside / gate_path.name
    decoy.write_bytes(b"{}\n")
    decoy.chmod(0o600)
    real_open = smoke.os.open
    swapped = False

    def swapping_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == gate_path.name and dir_fd is not None and not swapped:
            swapped = True
            parent.rename(relocated)
            parent.symlink_to(outside, target_is_directory=True)
            try:
                return real_open(path, flags, mode, dir_fd=dir_fd)
            finally:
                parent.unlink()
                relocated.rename(parent)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(smoke.os, "open", swapping_open)
    assert smoke.load_run_gate(gate_path, gate_digest).digest == gate_digest
    assert swapped is True
    assert decoy.read_bytes() == b"{}\n"


def test_live_cli_requires_both_output_root_and_exact_environment_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    private = _private_workspace(monkeypatch, tmp_path)
    gate_path, gate_digest = _write_gate(private)
    args = ["--gate", str(gate_path), "--gate-sha256", gate_digest, "--live"]

    with pytest.raises(smoke.SmokeError, match="output root"):
        smoke.main(args, backend_factory=FakeBackend)

    output = private / "output"
    with pytest.raises(smoke.SmokeError, match="environment switch"):
        smoke.main([*args, "--output-root", str(output)], backend_factory=FakeBackend)
    monkeypatch.setenv(smoke.LIVE_ENVIRONMENT_SWITCH, smoke.LIVE_ENVIRONMENT_VALUE)
    assert smoke.main([*args, "--output-root", str(output)], backend_factory=FakeBackend) == 0


def test_sensitive_authority_dataclasses_have_redacted_repr() -> None:
    authority_repr = repr(_authority())
    grant_repr = repr(
        smoke.UploadGrant(
            upload_id=UPLOAD_ID,
            job_id=UPLOAD_JOB,
            url="https://presigned-secret",
            fields={"policy": "presigned-secret"},
            key=RESERVED_KEY,
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=60),
        )
    )
    inventory_repr = repr(_inventory()[0])

    for secret in (OWNER, SUBJECT, JOB, BUCKET, SOURCE_VERSION, RESERVED_KEY, "presigned-secret"):
        assert secret not in authority_repr + grant_repr + inventory_repr


def test_upload_grant_rejects_multipart_header_injection() -> None:
    response = _upload_response()
    body = json.loads(response["body"])
    body["authorization"]["form_fields"]["policy"] = "secret\r\ninjected: true"
    response["body"] = json.dumps(body)

    with pytest.raises(smoke.SmokeError, match="integrity conditions"):
        smoke._parse_upload_grant(response, _authority())


def test_final_delta_helper_detects_any_execution_or_provider_delta() -> None:
    grant = smoke._parse_upload_grant(_upload_response(), _authority())
    changed_execution = replace(_after(), execution_digests=("different",))
    with pytest.raises(smoke.SmokeError, match="workflow execution"):
        smoke._verify_final_delta(_before(), changed_execution, grant)

    provider_items = (*_after().items, ("PROVIDER_CALL", {"attempt": 1}))
    provider_after = replace(_after(), items=provider_items)
    assert provider_after.entity_counts["PROVIDER_CALL"] == 1
    with pytest.raises(smoke.SmokeError, match="DynamoDB delta"):
        smoke._verify_final_delta(_before(), provider_after, grant)


@pytest.mark.parametrize("logical_id", tuple(smoke._EXPECTED_LAMBDA_HANDLERS))
def test_live_configuration_binds_exact_code_hash_handler_and_environment(
    logical_id: str,
) -> None:
    outputs = {
        "StateTableName": "table-secret",
        "ArtifactBucketName": BUCKET,
        "ArtifactBucketBrowserOrigin": (f"https://{BUCKET}.s3.us-west-2.amazonaws.com"),
        "SellerUserPoolId": "us-west-2_secret",
        "SellerUserPoolClientId": CLIENT_ID,
        "SellerApplicationOrigin": "https://massskutiny.com",
    }
    release = smoke._EXPECTED_LAMBDA_RELEASE_FINGERPRINT[logical_id]
    configuration = {
        "State": "Active",
        "LastUpdateStatus": "Successful",
        "Handler": smoke._EXPECTED_LAMBDA_HANDLERS[logical_id],
        "CodeSha256": smoke._EXPECTED_LAMBDA_CODE_SHA256[logical_id],
        "Runtime": "python3.12",
        "Timeout": 30,
        "MemorySize": 512,
        "Architectures": ["arm64"],
        "PackageType": "Zip",
        "Environment": {"Variables": smoke._expected_lambda_environment(outputs, release)},
    }

    class LambdaClient:
        def get_function_configuration(self, *, FunctionName: str) -> dict[str, Any]:
            assert FunctionName == "function-secret"
            return deepcopy(configuration)

    backend = smoke.AwsBackend.__new__(smoke.AwsBackend)
    backend._lambda = LambdaClient()
    backend._configuration(
        "function-secret",
        smoke._EXPECTED_LAMBDA_HANDLERS[logical_id],
        smoke._EXPECTED_LAMBDA_CODE_SHA256[logical_id],
        release,
        outputs,
    )

    configuration["CodeSha256"] = "drifted"
    with pytest.raises(smoke.SmokeError, match="code/environment"):
        backend._configuration(
            "function-secret",
            smoke._EXPECTED_LAMBDA_HANDLERS[logical_id],
            smoke._EXPECTED_LAMBDA_CODE_SHA256[logical_id],
            release,
            outputs,
        )
