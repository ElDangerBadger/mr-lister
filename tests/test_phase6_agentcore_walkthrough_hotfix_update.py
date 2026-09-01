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

import tools.render_phase6_agentcore_walkthrough_hotfix_update as hotfix_update
from tools.render_phase6_agentcore_direct_codezip import VerifiedAgentCoreArchive
from tools.verify_phase6_s3_release_object import VerifiedPhase6S3ReleaseObject

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 1, 5, 0, tzinfo=UTC)
REMOTE_VERSION_ID = "hotfix-agentcore-exact-version-123"
REMOTE_EVIDENCE_SHA256 = "e" * 64
WORKLOAD_ARN = (
    f"arn:aws:bedrock-agentcore:{hotfix_update.REGION}:"
    f"{hotfix_update.ACCOUNT_ID}:workload-identity-directory/default/"
    "workload-identity/mr-lister-phase6"
)


def _canonical(value: object) -> bytes:
    return hotfix_update._canonical_json(value)


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(_canonical(value))
    path.chmod(0o600)


def _runtime_response(*, version: str) -> dict[str, object]:
    predecessor = version == hotfix_update.CURRENT_VERSION
    release = (
        hotfix_update.PREDECESSOR_RELEASE_FINGERPRINT
        if predecessor
        else hotfix_update.HOTFIX_RELEASE_FINGERPRINT
    )
    key = hotfix_update.PREDECESSOR_KEY if predecessor else hotfix_update.HOTFIX_KEY
    version_id = hotfix_update.PREDECESSOR_S3_VERSION_ID if predecessor else REMOTE_VERSION_ID
    return {
        "agentRuntimeArn": hotfix_update.RUNTIME_ARN,
        "agentRuntimeArtifact": {
            "codeConfiguration": {
                "code": {
                    "s3": {
                        "bucket": hotfix_update.ARTIFACT_BUCKET,
                        "prefix": key,
                        "versionId": version_id,
                    }
                },
                "entryPoint": hotfix_update.ENTRY_POINT,
                "runtime": hotfix_update.PYTHON_RUNTIME,
            }
        },
        "agentRuntimeId": hotfix_update.RUNTIME_ID,
        "agentRuntimeName": hotfix_update.RUNTIME_NAME,
        "agentRuntimeVersion": version,
        "authorizerConfiguration": None,
        "capacityProviderConfiguration": None,
        "createdAt": "2026-08-31T23:00:00+00:00",
        "description": "Release-bound Phase 6 Strands preparation runtime",
        "environmentVariables": hotfix_update._environment(release),
        "failureReason": None,
        "filesystemConfigurations": [],
        "lastUpdatedAt": "2026-09-01T04:59:00+00:00",
        "lifecycleConfiguration": hotfix_update.LIFECYCLE_CONFIGURATION,
        "metadataConfiguration": hotfix_update.METADATA_CONFIGURATION,
        "networkConfiguration": hotfix_update.NETWORK_CONFIGURATION,
        "protocolConfiguration": hotfix_update.PROTOCOL_CONFIGURATION,
        "requestHeaderConfiguration": {"requestHeaderAllowlist": []},
        "roleArn": hotfix_update.ROLE_ARN,
        "status": "READY",
        "workloadIdentityDetails": {"workloadIdentityArn": WORKLOAD_ARN},
    }


def _predecessor_evidence() -> dict[str, object]:
    return {
        "accountId": hotfix_update.ACCOUNT_ID,
        "capturedAt": "2026-09-01T05:00:00+00:00",
        "format": hotfix_update.PREDECESSOR_EVIDENCE_FORMAT,
        "getAgentRuntime": {
            "request": {
                "agentRuntimeId": hotfix_update.RUNTIME_ID,
                "agentRuntimeVersion": hotfix_update.CURRENT_VERSION,
            },
            "response": _runtime_response(version=hotfix_update.CURRENT_VERSION),
        },
        "getAgentRuntimeEndpoint": {
            "request": {
                "agentRuntimeId": hotfix_update.RUNTIME_ID,
                "endpointName": hotfix_update.CURRENT_ENDPOINT_NAME,
            },
            "response": {
                "agentRuntimeArn": hotfix_update.RUNTIME_ARN,
                "agentRuntimeEndpointArn": hotfix_update.CURRENT_ENDPOINT_ARN,
                "createdAt": "2026-08-31T23:05:00+00:00",
                "description": "Immutable Phase 6 dev endpoint pinned to runtime version 3",
                "failureReason": None,
                "id": "endpoint-v3-123",
                "lastUpdatedAt": "2026-08-31T23:06:00+00:00",
                "liveVersion": hotfix_update.CURRENT_VERSION,
                "name": hotfix_update.CURRENT_ENDPOINT_NAME,
                "status": "READY",
                "targetVersion": hotfix_update.CURRENT_VERSION,
            },
        },
        "region": hotfix_update.REGION,
    }


def test_ready_endpoint_may_omit_target_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, closure, _ = _setup(tmp_path, monkeypatch)
    evidence = _predecessor_evidence()
    endpoint = evidence["getAgentRuntimeEndpoint"]["response"]  # type: ignore[index]
    endpoint.pop("targetVersion")  # type: ignore[union-attr]
    _write(closure / hotfix_update.PREDECESSOR_EVIDENCE_FILE, evidence)

    documents = hotfix_update.render_phase6_agentcore_runtime_update_documents(
        closure,
        repository_root=repository,
        now=NOW,
    )

    assert hotfix_update.RUNTIME_UPDATE_FILE in documents


def _role_evidence(repository: Path) -> dict[str, object]:
    trust, policy, tags = hotfix_update._expected_role_documents(repository)
    return {
        "accountId": hotfix_update.ACCOUNT_ID,
        "capturedAt": "2026-09-01T05:00:00+00:00",
        "format": hotfix_update.ROLE_EVIDENCE_FORMAT,
        "getRole": {
            "request": {"RoleName": hotfix_update.ROLE_NAME},
            "response": {
                "Role": {
                    "Arn": hotfix_update.ROLE_ARN,
                    "AssumeRolePolicyDocument": trust,
                    "CreateDate": "2026-08-24T21:00:00+00:00",
                    "Description": "Retained least-privilege execution role",
                    "MaxSessionDuration": 3600,
                    "Path": "/",
                    "RoleId": "AROATESTROLE1234567890",
                    "RoleName": hotfix_update.ROLE_NAME,
                    "Tags": list(reversed(tags)),
                }
            },
        },
        "getRolePolicy": {
            "request": {
                "PolicyName": hotfix_update.ROLE_POLICY_NAME,
                "RoleName": hotfix_update.ROLE_NAME,
            },
            "response": {
                "PolicyDocument": policy,
                "PolicyName": hotfix_update.ROLE_POLICY_NAME,
                "RoleName": hotfix_update.ROLE_NAME,
            },
        },
        "listAttachedRolePolicies": {
            "request": {"RoleName": hotfix_update.ROLE_NAME},
            "response": {"AttachedPolicies": [], "IsTruncated": False},
        },
        "listRolePolicies": {
            "request": {"RoleName": hotfix_update.ROLE_NAME},
            "response": {
                "IsTruncated": False,
                "PolicyNames": [hotfix_update.ROLE_POLICY_NAME],
            },
        },
        "region": hotfix_update.REGION,
    }


def test_transition_role_policy_pairs_only_predecessor_and_target_archives(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    bootstrap = repository / "infra/agentcore/mrlisterphase6/direct-codezip-bootstrap.json"
    bootstrap.parent.mkdir(parents=True)
    shutil.copyfile(
        ROOT / "infra/agentcore/mrlisterphase6/direct-codezip-bootstrap.json",
        bootstrap,
    )

    _, policy, tags = hotfix_update._expected_role_documents(repository)
    statements = policy["Statement"]
    archive_statements = [
        statement
        for statement in statements
        if "AgentCoreDeploymentArchive" in str(statement.get("Sid"))
    ]

    assert archive_statements == [
        hotfix_update._archive_read_statement(
            sid="ReadOnlyPredecessorAgentCoreDeploymentArchive",
            key=hotfix_update.PREDECESSOR_KEY,
        ),
        hotfix_update._archive_read_statement(
            sid="ReadOnlyTargetAgentCoreDeploymentArchive",
            key=hotfix_update.HOTFIX_KEY,
        ),
    ]
    assert all(isinstance(statement["Resource"], str) for statement in archive_statements)
    assert {tag["Key"]: tag["Value"] for tag in tags}["ReleaseFingerprint"] == (
        hotfix_update.HOTFIX_RELEASE_FINGERPRINT
    )


def _target_evidence(
    runtime_documents: dict[Path, bytes],
    predecessor_sha256: str,
    role_sha256: str,
) -> dict[str, object]:
    return {
        "accountId": hotfix_update.ACCOUNT_ID,
        "capturedAt": "2026-09-01T05:00:00+00:00",
        "format": hotfix_update.TARGET_EVIDENCE_FORMAT,
        "getAgentRuntime": {
            "request": {
                "agentRuntimeId": hotfix_update.RUNTIME_ID,
                "agentRuntimeVersion": hotfix_update.TARGET_VERSION,
            },
            "response": _runtime_response(version=hotfix_update.TARGET_VERSION),
        },
        "objectEvidenceSHA256": REMOTE_EVIDENCE_SHA256,
        "predecessorEvidenceSHA256": predecessor_sha256,
        "region": hotfix_update.REGION,
        "roleEvidenceSHA256": role_sha256,
        "runtimeUpdateManifestSHA256": sha256(
            runtime_documents[hotfix_update.RUNTIME_MANIFEST_FILE]
        ).hexdigest(),
        "updateAgentRuntime": {
            "inputSHA256": sha256(runtime_documents[hotfix_update.RUNTIME_UPDATE_FILE]).hexdigest(),
            "response": {
                "agentRuntimeArn": hotfix_update.RUNTIME_ARN,
                "agentRuntimeId": hotfix_update.RUNTIME_ID,
                "agentRuntimeVersion": hotfix_update.TARGET_VERSION,
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
    closure = (
        repository / ".mr_lister_private" / "phase6-walkthrough-hotfix-f6a643b-20260901T152211Z"
    )
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
    _write(closure / hotfix_update.OBJECT_EVIDENCE_FILE, {"synthetic": "patched-verifier"})
    _write(closure / hotfix_update.PREDECESSOR_EVIDENCE_FILE, _predecessor_evidence())
    _write(closure / hotfix_update.ROLE_EVIDENCE_FILE, _role_evidence(repository))

    checksum = base64.b64encode(bytes.fromhex(hotfix_update.HOTFIX_ARCHIVE_SHA256)).decode()
    remote = VerifiedPhase6S3ReleaseObject(
        account_id=hotfix_update.ACCOUNT_ID,
        region=hotfix_update.REGION,
        environment=hotfix_update.ENVIRONMENT,
        component="agentcore",
        release_fingerprint=hotfix_update.HOTFIX_RELEASE_FINGERPRINT,
        archive_sha256=hotfix_update.HOTFIX_ARCHIVE_SHA256,
        size_bytes=hotfix_update.HOTFIX_ARCHIVE_SIZE,
        checksum_sha256_base64=checksum,
        bucket=hotfix_update.ARTIFACT_BUCKET,
        key=hotfix_update.HOTFIX_KEY,
        version_id=REMOTE_VERSION_ID,
        evidence_sha256=REMOTE_EVIDENCE_SHA256,
    )

    def verify_artifact(
        binding: object,
        *,
        deployment_root: Path,
        artifact_root: Path,
    ) -> VerifiedAgentCoreArchive:
        assert binding == hotfix_update._binding()
        assert deployment_root == closure / "phase6-deployment"
        assert artifact_root == artifacts
        return VerifiedAgentCoreArchive(
            sha256=hotfix_update.HOTFIX_ARCHIVE_SHA256,
            size_bytes=hotfix_update.HOTFIX_ARCHIVE_SIZE,
            checksum_sha256_base64=checksum,
            descriptor_sha256="d" * 64,
        )

    def verify_object(expectation: object, *, evidence_path: Path) -> VerifiedPhase6S3ReleaseObject:
        assert expectation == hotfix_update.Phase6S3ReleaseObjectExpectation(
            account_id=hotfix_update.ACCOUNT_ID,
            region=hotfix_update.REGION,
            environment=hotfix_update.ENVIRONMENT,
            component="agentcore",
            release_fingerprint=hotfix_update.HOTFIX_RELEASE_FINGERPRINT,
            archive_sha256=hotfix_update.HOTFIX_ARCHIVE_SHA256,
            size_bytes=hotfix_update.HOTFIX_ARCHIVE_SIZE,
        )
        assert evidence_path == closure / hotfix_update.OBJECT_EVIDENCE_FILE
        return remote

    monkeypatch.setattr(
        hotfix_update, "verify_phase6_agentcore_direct_codezip_artifact", verify_artifact
    )
    monkeypatch.setattr(hotfix_update, "verify_phase6_s3_release_object_evidence", verify_object)
    return repository, closure, remote


def _runtime_documents(
    repository: Path,
    closure: Path,
) -> dict[Path, bytes]:
    return hotfix_update.render_phase6_agentcore_runtime_update_documents(
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
    update = json.loads(first[hotfix_update.RUNTIME_UPDATE_FILE])
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
    assert update["agentRuntimeId"] == hotfix_update.RUNTIME_ID
    assert update["roleArn"] == hotfix_update.ROLE_ARN
    assert update["networkConfiguration"] == {"networkMode": "PUBLIC"}
    assert update["protocolConfiguration"] == {"serverProtocol": "HTTP"}
    assert update["lifecycleConfiguration"] == {
        "idleRuntimeSessionTimeout": 900,
        "maxLifetime": 3600,
    }
    assert update["metadataConfiguration"] == {"requireMMDSV2": True}
    assert update["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"] == {
        "bucket": hotfix_update.ARTIFACT_BUCKET,
        "prefix": hotfix_update.HOTFIX_KEY,
        "versionId": REMOTE_VERSION_ID,
    }
    environment = update["environmentVariables"]
    assert environment["MR_LISTER_RELEASE_FINGERPRINT"] == (
        hotfix_update.HOTFIX_RELEASE_FINGERPRINT
    )
    assert environment["MR_LISTER_STRANDS_CONTROLLER_MODEL_ID"] == (
        hotfix_update.CONTROLLER_MODEL_ID
    )
    assert environment["MR_LISTER_GEMMA_CONFIG_FINGERPRINT"] == (
        hotfix_update.GEMMA_CONFIG_FINGERPRINT
    )
    assert environment["MR_LISTER_PRODUCT_PROFILE_FINGERPRINT"] == (
        hotfix_update.PRODUCT_PROFILE_FINGERPRINT
    )
    manifest = json.loads(first[hotfix_update.RUNTIME_MANIFEST_FILE])
    assert manifest["binding"]["sourceCommit"] == hotfix_update.SOURCE_COMMIT
    assert manifest["binding"]["archiveSHA256"] == hotfix_update.HOTFIX_ARCHIVE_SHA256
    assert manifest["binding"]["predecessorReleaseFingerprint"] == (
        hotfix_update.PREDECESSOR_RELEASE_FINGERPRINT
    )
    assert manifest["binding"]["predecessorArchiveSHA256"] == (
        hotfix_update.PREDECESSOR_ARCHIVE_SHA256
    )
    assert manifest["binding"]["predecessorS3VersionId"] == (
        hotfix_update.PREDECESSOR_S3_VERSION_ID
    )
    assert manifest["binding"]["currentVersion"] == "3"
    assert manifest["binding"]["targetVersion"] == "4"
    assert manifest["binding"]["targetEndpointName"] == "phase6_v4_dev"
    assert manifest["authorization"] == "BLOCKED_UNTIL_SEPARATELY_REVIEWED"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("agentRuntimeVersion", "2"),
        ("roleArn", "arn:aws:iam::384627057108:role/other"),
        ("networkConfiguration", {"networkMode": "VPC"}),
        ("protocolConfiguration", {"serverProtocol": "MCP"}),
        ("lifecycleConfiguration", {"idleRuntimeSessionTimeout": 901, "maxLifetime": 3600}),
        ("metadataConfiguration", {"requireMMDSV2": False}),
    ),
)
def test_runtime_update_rejects_every_drifted_v3_runtime_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    repository, closure, _ = _setup(tmp_path, monkeypatch)
    path = closure / hotfix_update.PREDECESSOR_EVIDENCE_FILE
    evidence = json.loads(path.read_bytes())
    evidence["getAgentRuntime"]["response"][field] = value
    _write(path, evidence)

    with pytest.raises(hotfix_update.Phase6AgentCoreWalkthroughHotfixUpdateError):
        _runtime_documents(repository, closure)


@pytest.mark.parametrize(
    "drift",
    (
        "target_only",
        "predecessor_only",
        "extra_archive",
        "swapped",
        "cross_product",
        "broad",
        "predecessor_tag",
        "attached",
        "boundary",
    ),
)
def test_runtime_update_requires_exact_closed_retained_role_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    repository, closure, _ = _setup(tmp_path, monkeypatch)
    path = closure / hotfix_update.ROLE_EVIDENCE_FILE
    evidence = json.loads(path.read_bytes())
    statements = evidence["getRolePolicy"]["response"]["PolicyDocument"]["Statement"]
    predecessor = next(
        statement
        for statement in statements
        if statement["Sid"] == "ReadOnlyPredecessorAgentCoreDeploymentArchive"
    )
    target = next(
        statement
        for statement in statements
        if statement["Sid"] == "ReadOnlyTargetAgentCoreDeploymentArchive"
    )
    if drift == "target_only":
        statements.remove(predecessor)
    elif drift == "predecessor_only":
        statements.remove(target)
    elif drift == "extra_archive":
        statements.append(
            {
                **target,
                "Resource": f"arn:aws:s3:::{hotfix_update.ARTIFACT_BUCKET}/private/other.zip",
                "Sid": "ReadOnlyExtraAgentCoreDeploymentArchive",
            }
        )
    elif drift == "swapped":
        predecessor["Resource"], target["Resource"] = target["Resource"], predecessor["Resource"]
    elif drift == "cross_product":
        target["Resource"] = [predecessor["Resource"], target["Resource"]]
    elif drift == "broad":
        target["Resource"] = f"arn:aws:s3:::{hotfix_update.ARTIFACT_BUCKET}/*"
    elif drift == "predecessor_tag":
        tags = evidence["getRole"]["response"]["Role"]["Tags"]
        next(tag for tag in tags if tag["Key"] == "ReleaseFingerprint")["Value"] = (
            hotfix_update.PREDECESSOR_RELEASE_FINGERPRINT
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

    with pytest.raises(hotfix_update.Phase6AgentCoreWalkthroughHotfixUpdateError):
        _runtime_documents(repository, closure)


def test_observations_are_recent_and_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, closure, _ = _setup(tmp_path, monkeypatch)
    predecessor = closure / hotfix_update.PREDECESSOR_EVIDENCE_FILE
    evidence = json.loads(predecessor.read_bytes())
    evidence["capturedAt"] = (NOW - timedelta(minutes=16)).isoformat()
    _write(predecessor, evidence)
    with pytest.raises(hotfix_update.Phase6AgentCoreWalkthroughHotfixUpdateError):
        _runtime_documents(repository, closure)

    evidence["capturedAt"] = NOW.isoformat()
    predecessor.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(hotfix_update.Phase6AgentCoreWalkthroughHotfixUpdateError):
        _runtime_documents(repository, closure)


def test_runtime_outputs_are_private_create_only_and_byte_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, closure, _ = _setup(tmp_path, monkeypatch)

    written = hotfix_update.write_phase6_agentcore_runtime_update_documents(
        closure,
        repository_root=repository,
        now=NOW,
    )

    assert set(written) == {
        closure / hotfix_update.RUNTIME_UPDATE_FILE,
        closure / hotfix_update.RUNTIME_MANIFEST_FILE,
    }
    assert stat.S_IMODE((closure / hotfix_update.OUTPUT_DIRECTORY).stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in written)
    hotfix_update.verify_phase6_agentcore_runtime_update_documents(
        closure,
        repository_root=repository,
        now=NOW,
    )
    with pytest.raises(hotfix_update.Phase6AgentCoreWalkthroughHotfixUpdateError):
        hotfix_update.write_phase6_agentcore_runtime_update_documents(
            closure,
            repository_root=repository,
            now=NOW,
        )
    target = closure / hotfix_update.RUNTIME_UPDATE_FILE
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(hotfix_update.Phase6AgentCoreWalkthroughHotfixUpdateError):
        hotfix_update.verify_phase6_agentcore_runtime_update_documents(
            closure,
            repository_root=repository,
            now=NOW,
        )


def test_endpoint_is_second_stage_and_pinned_to_ready_v4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, closure, _ = _setup(tmp_path, monkeypatch)
    runtime_documents = _runtime_documents(repository, closure)
    with pytest.raises(hotfix_update.Phase6AgentCoreWalkthroughHotfixUpdateError):
        hotfix_update.render_phase6_agentcore_endpoint_documents(
            closure,
            repository_root=repository,
            now=NOW,
        )
    hotfix_update.write_phase6_agentcore_runtime_update_documents(
        closure,
        repository_root=repository,
        now=NOW,
    )
    predecessor_sha = sha256(
        (closure / hotfix_update.PREDECESSOR_EVIDENCE_FILE).read_bytes()
    ).hexdigest()
    role_sha = sha256((closure / hotfix_update.ROLE_EVIDENCE_FILE).read_bytes()).hexdigest()
    _write(
        closure / hotfix_update.TARGET_EVIDENCE_FILE,
        _target_evidence(runtime_documents, predecessor_sha, role_sha),
    )

    documents = hotfix_update.render_phase6_agentcore_endpoint_documents(
        closure,
        repository_root=repository,
        now=NOW,
    )
    endpoint = json.loads(documents[hotfix_update.ENDPOINT_CREATE_FILE])
    assert endpoint["agentRuntimeId"] == hotfix_update.RUNTIME_ID
    assert endpoint["agentRuntimeVersion"] == "4"
    assert endpoint["name"] == "phase6_v4_dev"
    assert endpoint["tags"]["ReleaseFingerprint"] == (hotfix_update.HOTFIX_RELEASE_FINGERPRINT)
    assert (
        endpoint["clientToken"]
        != json.loads(runtime_documents[hotfix_update.RUNTIME_UPDATE_FILE])["clientToken"]
    )

    written = hotfix_update.write_phase6_agentcore_endpoint_documents(
        closure,
        repository_root=repository,
        now=NOW,
    )
    assert set(written) == {
        closure / hotfix_update.ENDPOINT_CREATE_FILE,
        closure / hotfix_update.ENDPOINT_MANIFEST_FILE,
    }
    hotfix_update.verify_phase6_agentcore_endpoint_documents(
        closure,
        repository_root=repository,
        now=NOW,
    )


@pytest.mark.parametrize(
    ("location", "field", "value"),
    (
        ("update", "inputSHA256", "0" * 64),
        ("update_response", "agentRuntimeVersion", "3"),
        ("runtime", "status", "UPDATING"),
        ("runtime", "metadataConfiguration", {"requireMMDSV2": False}),
        ("top", "roleEvidenceSHA256", "0" * 64),
    ),
)
def test_endpoint_rejects_unjoined_or_not_ready_v4_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
    field: str,
    value: object,
) -> None:
    repository, closure, _ = _setup(tmp_path, monkeypatch)
    runtime_documents = _runtime_documents(repository, closure)
    hotfix_update.write_phase6_agentcore_runtime_update_documents(
        closure,
        repository_root=repository,
        now=NOW,
    )
    predecessor_sha = sha256(
        (closure / hotfix_update.PREDECESSOR_EVIDENCE_FILE).read_bytes()
    ).hexdigest()
    role_sha = sha256((closure / hotfix_update.ROLE_EVIDENCE_FILE).read_bytes()).hexdigest()
    evidence = _target_evidence(runtime_documents, predecessor_sha, role_sha)
    if location == "update":
        evidence["updateAgentRuntime"][field] = value  # type: ignore[index]
    elif location == "update_response":
        evidence["updateAgentRuntime"]["response"][field] = value  # type: ignore[index]
    elif location == "runtime":
        evidence["getAgentRuntime"]["response"][field] = value  # type: ignore[index]
    else:
        evidence[field] = value
    _write(closure / hotfix_update.TARGET_EVIDENCE_FILE, evidence)

    with pytest.raises(hotfix_update.Phase6AgentCoreWalkthroughHotfixUpdateError):
        hotfix_update.render_phase6_agentcore_endpoint_documents(
            closure,
            repository_root=repository,
            now=NOW,
        )


def test_renderer_has_no_aws_or_identity_override_and_confines_hotfix_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = inspect.getsource(hotfix_update)
    assert "import boto3" not in source
    assert "subprocess" not in source
    assert "--account-id" not in source
    assert "--runtime-id" not in source
    assert "--target-version" not in source

    repository, closure, _ = _setup(tmp_path, monkeypatch)
    outside = tmp_path / closure.name
    outside.mkdir()
    with pytest.raises(hotfix_update.Phase6AgentCoreWalkthroughHotfixUpdateError):
        hotfix_update.render_phase6_agentcore_runtime_update_documents(
            outside,
            repository_root=repository,
            now=NOW,
        )

    wrong_source = (
        repository / ".mr_lister_private/phase6-walkthrough-hotfix-e1d3fda-20260901T152211Z"
    )
    wrong_source.mkdir()
    with pytest.raises(hotfix_update.Phase6AgentCoreWalkthroughHotfixUpdateError):
        hotfix_update.render_phase6_agentcore_runtime_update_documents(
            wrong_source,
            repository_root=repository,
            now=NOW,
        )


def test_exact_hotfix_identity_is_not_caller_selectable() -> None:
    assert hotfix_update._binding() == hotfix_update.Phase6AgentCoreDirectCodeZipBinding(
        account_id="384627057108",
        release_fingerprint=("9bc5e1727cfcf68b40847d1a2e416300640779898c9bf884f6f9e442b0225d9e"),
        agentcore_archive_sha256=(
            "5a2821b40e39cf7fcdf77421ed6bce1b3b76907af6221930ea29dc5c6210c7a6"
        ),
    )
    assert hotfix_update.RUNTIME_ID == "mr_lister_phase6-4HoPmq2hCI"
    assert hotfix_update.SOURCE_COMMIT == "f6a643b19e47f02784e9b590949fddde1cf9c107"
    assert hotfix_update.CURRENT_VERSION == "3"
    assert hotfix_update.TARGET_VERSION == "4"
    assert hotfix_update.CURRENT_ENDPOINT_NAME == "phase6_v3_dev"
    assert hotfix_update.TARGET_ENDPOINT_NAME == "phase6_v4_dev"
