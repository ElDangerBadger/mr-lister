from __future__ import annotations

import base64
import inspect
import json
import shutil
import stat
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

import tools.render_phase6_agentcore_closure_update as closure_update
from tools.render_phase6_agentcore_direct_codezip import VerifiedAgentCoreArchive
from tools.verify_phase6_s3_release_object import VerifiedPhase6S3ReleaseObject

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 1, 5, 0, tzinfo=UTC)
REMOTE_VERSION_ID = "closure-agentcore-exact-version-123"
REMOTE_EVIDENCE_SHA256 = "e" * 64
WORKLOAD_ARN = (
    f"arn:aws:bedrock-agentcore:{closure_update.REGION}:"
    f"{closure_update.ACCOUNT_ID}:workload-identity-directory/default/"
    "workload-identity/mr-lister-phase6"
)


def _canonical(value: object) -> bytes:
    return closure_update._canonical_json(value)


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(_canonical(value))
    path.chmod(0o600)


def _runtime_response(*, version: str) -> dict[str, object]:
    predecessor = version == closure_update.CURRENT_VERSION
    release = (
        closure_update.PREDECESSOR_RELEASE_FINGERPRINT
        if predecessor
        else closure_update.CLOSURE_RELEASE_FINGERPRINT
    )
    key = closure_update.PREDECESSOR_KEY if predecessor else closure_update.CLOSURE_KEY
    version_id = closure_update.PREDECESSOR_S3_VERSION_ID if predecessor else REMOTE_VERSION_ID
    return {
        "agentRuntimeArn": closure_update.RUNTIME_ARN,
        "agentRuntimeArtifact": {
            "codeConfiguration": {
                "code": {
                    "s3": {
                        "bucket": closure_update.ARTIFACT_BUCKET,
                        "prefix": key,
                        "versionId": version_id,
                    }
                },
                "entryPoint": closure_update.ENTRY_POINT,
                "runtime": closure_update.PYTHON_RUNTIME,
            }
        },
        "agentRuntimeId": closure_update.RUNTIME_ID,
        "agentRuntimeName": closure_update.RUNTIME_NAME,
        "agentRuntimeVersion": version,
        "authorizerConfiguration": None,
        "capacityProviderConfiguration": None,
        "createdAt": "2026-08-31T23:00:00+00:00",
        "description": "Release-bound Phase 6 Strands preparation runtime",
        "environmentVariables": closure_update._environment(release),
        "failureReason": None,
        "filesystemConfigurations": [],
        "lastUpdatedAt": "2026-09-01T04:59:00+00:00",
        "lifecycleConfiguration": closure_update.LIFECYCLE_CONFIGURATION,
        "metadataConfiguration": closure_update.METADATA_CONFIGURATION,
        "networkConfiguration": closure_update.NETWORK_CONFIGURATION,
        "protocolConfiguration": closure_update.PROTOCOL_CONFIGURATION,
        "requestHeaderConfiguration": {"requestHeaderAllowlist": []},
        "roleArn": closure_update.ROLE_ARN,
        "status": "READY",
        "workloadIdentityDetails": {"workloadIdentityArn": WORKLOAD_ARN},
    }


def _predecessor_evidence() -> dict[str, object]:
    return {
        "accountId": closure_update.ACCOUNT_ID,
        "capturedAt": "2026-09-01T05:00:00+00:00",
        "format": closure_update.PREDECESSOR_EVIDENCE_FORMAT,
        "getAgentRuntime": {
            "request": {
                "agentRuntimeId": closure_update.RUNTIME_ID,
                "agentRuntimeVersion": closure_update.CURRENT_VERSION,
            },
            "response": _runtime_response(version=closure_update.CURRENT_VERSION),
        },
        "getAgentRuntimeEndpoint": {
            "request": {
                "agentRuntimeId": closure_update.RUNTIME_ID,
                "endpointName": closure_update.CURRENT_ENDPOINT_NAME,
            },
            "response": {
                "agentRuntimeArn": closure_update.RUNTIME_ARN,
                "agentRuntimeEndpointArn": closure_update.CURRENT_ENDPOINT_ARN,
                "createdAt": "2026-08-31T23:05:00+00:00",
                "description": "Immutable Phase 6 dev endpoint pinned to runtime version 2",
                "failureReason": None,
                "id": "endpoint-v2-123",
                "lastUpdatedAt": "2026-08-31T23:06:00+00:00",
                "liveVersion": closure_update.CURRENT_VERSION,
                "name": closure_update.CURRENT_ENDPOINT_NAME,
                "status": "READY",
                "targetVersion": closure_update.CURRENT_VERSION,
            },
        },
        "region": closure_update.REGION,
    }


def test_ready_endpoint_may_omit_target_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, closure, _ = _setup(tmp_path, monkeypatch)
    evidence = _predecessor_evidence()
    endpoint = evidence["getAgentRuntimeEndpoint"]["response"]  # type: ignore[index]
    endpoint.pop("targetVersion")  # type: ignore[union-attr]
    _write(closure / closure_update.PREDECESSOR_EVIDENCE_FILE, evidence)

    documents = closure_update.render_phase6_agentcore_runtime_update_documents(
        closure,
        repository_root=repository,
        now=NOW,
    )

    assert closure_update.RUNTIME_UPDATE_FILE in documents


def _role_evidence(repository: Path) -> dict[str, object]:
    trust, policy, tags = closure_update._expected_role_documents(repository)
    return {
        "accountId": closure_update.ACCOUNT_ID,
        "capturedAt": "2026-09-01T05:00:00+00:00",
        "format": closure_update.ROLE_EVIDENCE_FORMAT,
        "getRole": {
            "request": {"RoleName": closure_update.ROLE_NAME},
            "response": {
                "Role": {
                    "Arn": closure_update.ROLE_ARN,
                    "AssumeRolePolicyDocument": trust,
                    "CreateDate": "2026-08-24T21:00:00+00:00",
                    "Description": "Retained least-privilege execution role",
                    "MaxSessionDuration": 3600,
                    "Path": "/",
                    "RoleId": "AROATESTROLE1234567890",
                    "RoleName": closure_update.ROLE_NAME,
                    "Tags": list(reversed(tags)),
                }
            },
        },
        "getRolePolicy": {
            "request": {
                "PolicyName": closure_update.ROLE_POLICY_NAME,
                "RoleName": closure_update.ROLE_NAME,
            },
            "response": {
                "PolicyDocument": policy,
                "PolicyName": closure_update.ROLE_POLICY_NAME,
                "RoleName": closure_update.ROLE_NAME,
            },
        },
        "listAttachedRolePolicies": {
            "request": {"RoleName": closure_update.ROLE_NAME},
            "response": {"AttachedPolicies": [], "IsTruncated": False},
        },
        "listRolePolicies": {
            "request": {"RoleName": closure_update.ROLE_NAME},
            "response": {
                "IsTruncated": False,
                "PolicyNames": [closure_update.ROLE_POLICY_NAME],
            },
        },
        "region": closure_update.REGION,
    }


def _target_evidence(
    runtime_documents: dict[Path, bytes],
    predecessor_sha256: str,
    role_sha256: str,
) -> dict[str, object]:
    return {
        "accountId": closure_update.ACCOUNT_ID,
        "capturedAt": "2026-09-01T05:00:00+00:00",
        "format": closure_update.TARGET_EVIDENCE_FORMAT,
        "getAgentRuntime": {
            "request": {
                "agentRuntimeId": closure_update.RUNTIME_ID,
                "agentRuntimeVersion": closure_update.TARGET_VERSION,
            },
            "response": _runtime_response(version=closure_update.TARGET_VERSION),
        },
        "objectEvidenceSHA256": REMOTE_EVIDENCE_SHA256,
        "predecessorEvidenceSHA256": predecessor_sha256,
        "region": closure_update.REGION,
        "roleEvidenceSHA256": role_sha256,
        "runtimeUpdateManifestSHA256": sha256(
            runtime_documents[closure_update.RUNTIME_MANIFEST_FILE]
        ).hexdigest(),
        "updateAgentRuntime": {
            "inputSHA256": sha256(
                runtime_documents[closure_update.RUNTIME_UPDATE_FILE]
            ).hexdigest(),
            "response": {
                "agentRuntimeArn": closure_update.RUNTIME_ARN,
                "agentRuntimeId": closure_update.RUNTIME_ID,
                "agentRuntimeVersion": closure_update.TARGET_VERSION,
                "createdAt": "2026-08-24T21:30:00+00:00",
                "lastUpdatedAt": "2026-09-01T04:59:30+00:00",
                "status": "UPDATING",
                "workloadIdentityDetails": {"workloadIdentityArn": WORKLOAD_ARN},
            },
        },
    }


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, VerifiedPhase6S3ReleaseObject]:
    repository = tmp_path / "repository"
    closure = repository / ".mr_lister_private" / "phase6-closure-fff69db-20260901T025616Z"
    agentcore = closure / "phase6-deployment/agentcore"
    artifacts = closure / "phase6-artifacts"
    artifacts.mkdir(parents=True)
    for relative in (
        "config/bedrock/google_gemma_3_27b_it.json",
        "config/product_profiles/gildan_64000_swiftpod.json",
    ):
        destination = agentcore / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    bootstrap = repository / "infra/agentcore/mrlisterphase6/direct-codezip-bootstrap.json"
    bootstrap.parent.mkdir(parents=True)
    shutil.copyfile(
        ROOT / "infra/agentcore/mrlisterphase6/direct-codezip-bootstrap.json",
        bootstrap,
    )
    _write(closure / closure_update.OBJECT_EVIDENCE_FILE, {"synthetic": "patched-verifier"})
    _write(closure / closure_update.PREDECESSOR_EVIDENCE_FILE, _predecessor_evidence())
    _write(closure / closure_update.ROLE_EVIDENCE_FILE, _role_evidence(repository))

    checksum = base64.b64encode(bytes.fromhex(closure_update.CLOSURE_ARCHIVE_SHA256)).decode()
    remote = VerifiedPhase6S3ReleaseObject(
        account_id=closure_update.ACCOUNT_ID,
        region=closure_update.REGION,
        environment=closure_update.ENVIRONMENT,
        component="agentcore",
        release_fingerprint=closure_update.CLOSURE_RELEASE_FINGERPRINT,
        archive_sha256=closure_update.CLOSURE_ARCHIVE_SHA256,
        size_bytes=closure_update.CLOSURE_ARCHIVE_SIZE,
        checksum_sha256_base64=checksum,
        bucket=closure_update.ARTIFACT_BUCKET,
        key=closure_update.CLOSURE_KEY,
        version_id=REMOTE_VERSION_ID,
        evidence_sha256=REMOTE_EVIDENCE_SHA256,
    )

    def verify_artifact(
        binding: object,
        *,
        deployment_root: Path,
        artifact_root: Path,
    ) -> VerifiedAgentCoreArchive:
        assert binding == closure_update._binding()
        assert deployment_root == closure / "phase6-deployment"
        assert artifact_root == artifacts
        return VerifiedAgentCoreArchive(
            sha256=closure_update.CLOSURE_ARCHIVE_SHA256,
            size_bytes=closure_update.CLOSURE_ARCHIVE_SIZE,
            checksum_sha256_base64=checksum,
            descriptor_sha256="d" * 64,
        )

    def verify_object(expectation: object, *, evidence_path: Path) -> VerifiedPhase6S3ReleaseObject:
        assert expectation == closure_update.Phase6S3ReleaseObjectExpectation(
            account_id=closure_update.ACCOUNT_ID,
            region=closure_update.REGION,
            environment=closure_update.ENVIRONMENT,
            component="agentcore",
            release_fingerprint=closure_update.CLOSURE_RELEASE_FINGERPRINT,
            archive_sha256=closure_update.CLOSURE_ARCHIVE_SHA256,
            size_bytes=closure_update.CLOSURE_ARCHIVE_SIZE,
        )
        assert evidence_path == closure / closure_update.OBJECT_EVIDENCE_FILE
        return remote

    monkeypatch.setattr(
        closure_update, "verify_phase6_agentcore_direct_codezip_artifact", verify_artifact
    )
    monkeypatch.setattr(closure_update, "verify_phase6_s3_release_object_evidence", verify_object)
    return repository, closure, remote


def _runtime_documents(
    repository: Path,
    closure: Path,
) -> dict[Path, bytes]:
    return closure_update.render_phase6_agentcore_runtime_update_documents(
        closure,
        repository_root=repository,
        now=NOW,
    )


def test_runtime_update_is_exact_versioned_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, closure, _ = _setup(tmp_path, monkeypatch)

    first = _runtime_documents(repository, closure)
    second = _runtime_documents(repository, closure)

    assert first == second
    update = json.loads(first[closure_update.RUNTIME_UPDATE_FILE])
    assert set(update) == {
        "agentRuntimeArtifact",
        "agentRuntimeId",
        "clientToken",
        "description",
        "environmentVariables",
        "lifecycleConfiguration",
        "metadataConfiguration",
        "networkConfiguration",
        "protocolConfiguration",
        "roleArn",
    }
    assert update["agentRuntimeId"] == closure_update.RUNTIME_ID
    assert update["roleArn"] == closure_update.ROLE_ARN
    assert update["networkConfiguration"] == {"networkMode": "PUBLIC"}
    assert update["protocolConfiguration"] == {"serverProtocol": "HTTP"}
    assert update["lifecycleConfiguration"] == {
        "idleRuntimeSessionTimeout": 900,
        "maxLifetime": 3600,
    }
    assert update["metadataConfiguration"] == {"requireMMDSV2": True}
    assert update["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"] == {
        "bucket": closure_update.ARTIFACT_BUCKET,
        "prefix": closure_update.CLOSURE_KEY,
        "versionId": REMOTE_VERSION_ID,
    }
    environment = update["environmentVariables"]
    assert environment["MR_LISTER_RELEASE_FINGERPRINT"] == (
        closure_update.CLOSURE_RELEASE_FINGERPRINT
    )
    assert environment["MR_LISTER_STRANDS_CONTROLLER_MODEL_ID"] == (
        closure_update.CONTROLLER_MODEL_ID
    )
    assert environment["MR_LISTER_GEMMA_CONFIG_FINGERPRINT"] == (
        closure_update.GEMMA_CONFIG_FINGERPRINT
    )
    assert environment["MR_LISTER_PRODUCT_PROFILE_FINGERPRINT"] == (
        closure_update.PRODUCT_PROFILE_FINGERPRINT
    )
    manifest = json.loads(first[closure_update.RUNTIME_MANIFEST_FILE])
    assert manifest["binding"]["currentVersion"] == "2"
    assert manifest["binding"]["targetVersion"] == "3"
    assert manifest["binding"]["targetEndpointName"] == "phase6_v3_dev"
    assert manifest["authorization"] == "BLOCKED_UNTIL_SEPARATELY_REVIEWED"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("agentRuntimeVersion", "1"),
        ("roleArn", "arn:aws:iam::384627057108:role/other"),
        ("networkConfiguration", {"networkMode": "VPC"}),
        ("protocolConfiguration", {"serverProtocol": "MCP"}),
        ("lifecycleConfiguration", {"idleRuntimeSessionTimeout": 901, "maxLifetime": 3600}),
        ("metadataConfiguration", {"requireMMDSV2": False}),
    ),
)
def test_runtime_update_rejects_every_drifted_v2_runtime_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    repository, closure, _ = _setup(tmp_path, monkeypatch)
    path = closure / closure_update.PREDECESSOR_EVIDENCE_FILE
    evidence = json.loads(path.read_bytes())
    evidence["getAgentRuntime"]["response"][field] = value
    _write(path, evidence)

    with pytest.raises(closure_update.Phase6AgentCoreClosureUpdateError):
        _runtime_documents(repository, closure)


@pytest.mark.parametrize("drift", ("old_object", "wildcard", "attached", "boundary"))
def test_runtime_update_requires_exact_closed_retained_role_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    repository, closure, _ = _setup(tmp_path, monkeypatch)
    path = closure / closure_update.ROLE_EVIDENCE_FILE
    evidence = json.loads(path.read_bytes())
    if drift in {"old_object", "wildcard"}:
        statements = evidence["getRolePolicy"]["response"]["PolicyDocument"]["Statement"]
        exact = next(
            statement
            for statement in statements
            if statement["Sid"] == "ReadOnlyExactAgentCoreDeploymentArchive"
        )
        exact["Resource"] = f"arn:aws:s3:::{closure_update.ARTIFACT_BUCKET}/" + (
            closure_update.PREDECESSOR_KEY if drift == "old_object" else "*"
        )
    elif drift == "attached":
        evidence["listAttachedRolePolicies"]["response"]["AttachedPolicies"] = [
            {
                "PolicyArn": "arn:aws:iam::aws:policy/ReadOnlyAccess",
                "PolicyName": "ReadOnlyAccess",
            }
        ]
    else:
        evidence["getRole"]["response"]["Role"]["PermissionsBoundary"] = {
            "PermissionsBoundaryArn": "arn:aws:iam::384627057108:policy/example",
            "PermissionsBoundaryType": "Policy",
        }
    _write(path, evidence)

    with pytest.raises(closure_update.Phase6AgentCoreClosureUpdateError):
        _runtime_documents(repository, closure)


def test_observations_are_recent_and_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, closure, _ = _setup(tmp_path, monkeypatch)
    predecessor = closure / closure_update.PREDECESSOR_EVIDENCE_FILE
    evidence = json.loads(predecessor.read_bytes())
    evidence["capturedAt"] = (NOW - timedelta(minutes=16)).isoformat()
    _write(predecessor, evidence)
    with pytest.raises(closure_update.Phase6AgentCoreClosureUpdateError):
        _runtime_documents(repository, closure)

    evidence["capturedAt"] = NOW.isoformat()
    predecessor.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(closure_update.Phase6AgentCoreClosureUpdateError):
        _runtime_documents(repository, closure)


def test_runtime_outputs_are_private_create_only_and_byte_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, closure, _ = _setup(tmp_path, monkeypatch)

    written = closure_update.write_phase6_agentcore_runtime_update_documents(
        closure,
        repository_root=repository,
        now=NOW,
    )

    assert set(written) == {
        closure / closure_update.RUNTIME_UPDATE_FILE,
        closure / closure_update.RUNTIME_MANIFEST_FILE,
    }
    assert stat.S_IMODE((closure / closure_update.OUTPUT_DIRECTORY).stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in written)
    closure_update.verify_phase6_agentcore_runtime_update_documents(
        closure,
        repository_root=repository,
        now=NOW,
    )
    with pytest.raises(closure_update.Phase6AgentCoreClosureUpdateError):
        closure_update.write_phase6_agentcore_runtime_update_documents(
            closure,
            repository_root=repository,
            now=NOW,
        )
    target = closure / closure_update.RUNTIME_UPDATE_FILE
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(closure_update.Phase6AgentCoreClosureUpdateError):
        closure_update.verify_phase6_agentcore_runtime_update_documents(
            closure,
            repository_root=repository,
            now=NOW,
        )


def test_endpoint_is_second_stage_and_pinned_to_ready_v3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, closure, _ = _setup(tmp_path, monkeypatch)
    runtime_documents = _runtime_documents(repository, closure)
    with pytest.raises(closure_update.Phase6AgentCoreClosureUpdateError):
        closure_update.render_phase6_agentcore_endpoint_documents(
            closure,
            repository_root=repository,
            now=NOW,
        )
    closure_update.write_phase6_agentcore_runtime_update_documents(
        closure,
        repository_root=repository,
        now=NOW,
    )
    predecessor_sha = sha256(
        (closure / closure_update.PREDECESSOR_EVIDENCE_FILE).read_bytes()
    ).hexdigest()
    role_sha = sha256((closure / closure_update.ROLE_EVIDENCE_FILE).read_bytes()).hexdigest()
    _write(
        closure / closure_update.TARGET_EVIDENCE_FILE,
        _target_evidence(runtime_documents, predecessor_sha, role_sha),
    )

    documents = closure_update.render_phase6_agentcore_endpoint_documents(
        closure,
        repository_root=repository,
        now=NOW,
    )
    endpoint = json.loads(documents[closure_update.ENDPOINT_CREATE_FILE])
    assert endpoint["agentRuntimeId"] == closure_update.RUNTIME_ID
    assert endpoint["agentRuntimeVersion"] == "3"
    assert endpoint["name"] == "phase6_v3_dev"
    assert endpoint["tags"]["ReleaseFingerprint"] == (closure_update.CLOSURE_RELEASE_FINGERPRINT)
    assert (
        endpoint["clientToken"]
        != json.loads(runtime_documents[closure_update.RUNTIME_UPDATE_FILE])["clientToken"]
    )

    written = closure_update.write_phase6_agentcore_endpoint_documents(
        closure,
        repository_root=repository,
        now=NOW,
    )
    assert set(written) == {
        closure / closure_update.ENDPOINT_CREATE_FILE,
        closure / closure_update.ENDPOINT_MANIFEST_FILE,
    }
    closure_update.verify_phase6_agentcore_endpoint_documents(
        closure,
        repository_root=repository,
        now=NOW,
    )


@pytest.mark.parametrize(
    ("location", "field", "value"),
    (
        ("update", "inputSHA256", "0" * 64),
        ("update_response", "agentRuntimeVersion", "2"),
        ("runtime", "status", "UPDATING"),
        ("runtime", "metadataConfiguration", {"requireMMDSV2": False}),
        ("top", "roleEvidenceSHA256", "0" * 64),
    ),
)
def test_endpoint_rejects_unjoined_or_not_ready_v3_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
    field: str,
    value: object,
) -> None:
    repository, closure, _ = _setup(tmp_path, monkeypatch)
    runtime_documents = _runtime_documents(repository, closure)
    closure_update.write_phase6_agentcore_runtime_update_documents(
        closure,
        repository_root=repository,
        now=NOW,
    )
    predecessor_sha = sha256(
        (closure / closure_update.PREDECESSOR_EVIDENCE_FILE).read_bytes()
    ).hexdigest()
    role_sha = sha256((closure / closure_update.ROLE_EVIDENCE_FILE).read_bytes()).hexdigest()
    evidence = _target_evidence(runtime_documents, predecessor_sha, role_sha)
    if location == "update":
        evidence["updateAgentRuntime"][field] = value  # type: ignore[index]
    elif location == "update_response":
        evidence["updateAgentRuntime"]["response"][field] = value  # type: ignore[index]
    elif location == "runtime":
        evidence["getAgentRuntime"]["response"][field] = value  # type: ignore[index]
    else:
        evidence[field] = value
    _write(closure / closure_update.TARGET_EVIDENCE_FILE, evidence)

    with pytest.raises(closure_update.Phase6AgentCoreClosureUpdateError):
        closure_update.render_phase6_agentcore_endpoint_documents(
            closure,
            repository_root=repository,
            now=NOW,
        )


def test_renderer_has_no_aws_or_identity_override_and_confines_closure_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = inspect.getsource(closure_update)
    assert "import boto3" not in source
    assert "subprocess" not in source
    assert "--account-id" not in source
    assert "--runtime-id" not in source
    assert "--target-version" not in source

    repository, closure, _ = _setup(tmp_path, monkeypatch)
    outside = tmp_path / closure.name
    outside.mkdir()
    with pytest.raises(closure_update.Phase6AgentCoreClosureUpdateError):
        closure_update.render_phase6_agentcore_runtime_update_documents(
            outside,
            repository_root=repository,
            now=NOW,
        )


def test_exact_closure_identity_is_not_caller_selectable() -> None:
    assert closure_update._binding() == closure_update.Phase6AgentCoreDirectCodeZipBinding(
        account_id="384627057108",
        release_fingerprint=("f34ab73042014fccce2cb3733624f005a4ccc10bb065b39c3e20befd3c33923f"),
        agentcore_archive_sha256=(
            "443f62fe01a2ebd54c8ff4b551eab94c829a878b42333973a6731e1cdd105f8b"
        ),
    )
    assert closure_update.RUNTIME_ID == "mr_lister_phase6-4HoPmq2hCI"
    assert closure_update.CURRENT_VERSION == "2"
    assert closure_update.TARGET_VERSION == "3"
