#!/usr/bin/env python3
"""Capture the mutation-free run gate for the deployed Phase 6.6 outbox smoke.

The command accepts three exact, owner-only authorities: a gate-seed document, the
sanitized deployment authority, and the passed upload-integrity prerequisite record.
Only after all local bindings pass does it construct the read-only AWS backend.  The
backend re-verifies the deployed Lambda/configuration/workflow envelope, including the
dispatcher stream filter that excludes REMOVE events, and captures bounded inventories.

No Lambda is invoked and no DynamoDB, S3, Step Functions, provider, browser, identity,
or secret mutation exists in this path.  The only write is one sanitized, mode-0600 JSON
file in the exact schema consumed by ``load_run_gate`` beneath the repository-private
Phase 6.6 acceptance root.
"""

from __future__ import annotations

import argparse
import os
import secrets
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

from pydantic import ValidationError

from mr_lister.acceptance.phase6 import (
    AcceptanceOutcome,
    DeployedNonDestructiveEvidenceRecord,
    validate_phase66_evidence,
)
from tools import phase66_deployed_outbox_recovery_smoke as smoke
from tools.prepare_phase66_edge_revalidation import _DeploymentAuthorityDocument

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
PRIVATE_ROOT: Final = REPOSITORY_ROOT / ".mr_lister_private" / "phase66-acceptance"

GATE_SEED_CONTRACT: Final = "phase6.6-deployed-outbox-recovery-gate-seed-v2"
PREREQUISITE_GATE_ID: Final = "deployed.upload_integrity_smoke"
MAX_INPUT_BYTES: Final = 4 * 1024 * 1024


class BaselineCaptureError(RuntimeError):
    """One local authority, deployed read, confinement, or output check failed."""


@dataclass(frozen=True, slots=True)
class GateSeed:
    digest: str
    deployment_digest: str
    prerequisite_digest: str
    document: Mapping[str, Any]


class BaselineBackend(Protocol):
    def capture_baseline(
        self, canary: smoke.CanaryAuthority
    ) -> tuple[smoke.LiveSnapshot, bool]: ...


def _private_path(path: Path) -> Path:
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(PRIVATE_ROOT)
    except ValueError:
        raise BaselineCaptureError(
            "baseline authorities must stay in the repository-private acceptance root"
        ) from None
    if not relative.parts:
        raise BaselineCaptureError("baseline authority path must name one private child")
    return candidate


def _open_repository_root() -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    root = Path(os.path.abspath(REPOSITORY_ROOT))
    descriptor: int | None = None
    try:
        if not root.is_absolute() or root.parts[0] != os.sep:
            raise OSError
        descriptor = os.open(os.sep, flags)
        for component in root.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError
        result = descriptor
        descriptor = None
        return result
    except OSError:
        raise BaselineCaptureError("repository root is not one stable directory chain") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def _private_directory_descriptor(path: Path, *, create: bool) -> Iterator[int]:
    directory = Path(os.path.abspath(path))
    try:
        directory.relative_to(PRIVATE_ROOT)
    except ValueError:
        raise BaselineCaptureError(
            "private authority directory is outside the acceptance root"
        ) from None
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = _open_repository_root()
        for component in directory.relative_to(REPOSITORY_ROOT).parts:
            next_descriptor: int | None = None
            try:
                try:
                    next_descriptor = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    next_descriptor = os.open(component, flags, dir_fd=descriptor)
                metadata = os.fstat(next_descriptor)
                if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
                    raise OSError
            except OSError:
                if next_descriptor is not None:
                    os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor
    except OSError:
        raise BaselineCaptureError(
            "private authority directory is not one confined directory chain"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_exact_json(path: Path, expected_digest: str, label: str) -> object:
    if not smoke._is_digest(expected_digest):
        raise BaselineCaptureError(f"{label} SHA-256 is invalid")
    candidate = _private_path(path)
    descriptor: int | None = None
    with _private_directory_descriptor(candidate.parent, create=False) as parent_descriptor:
        try:
            descriptor = os.open(
                candidate.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent_descriptor,
            )
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_mode & 0o077
                or not 1 <= before.st_size <= MAX_INPUT_BYTES
            ):
                raise OSError
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 1024 * 1024))
                if not chunk:
                    raise OSError
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
        except (OSError, BaselineCaptureError):
            raise BaselineCaptureError(
                f"{label} must be one stable mode-0600 private regular file"
            ) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
    payload = b"".join(chunks)
    if (
        len(payload) != before.st_size
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        or not secrets.compare_digest(smoke._digest_bytes(payload), expected_digest)
    ):
        raise BaselineCaptureError(f"{label} changed or does not match its SHA-256")
    try:
        return smoke._strict_json(payload, label)
    except smoke.SmokeError:
        raise BaselineCaptureError(f"{label} is not strict JSON") from None


def _gate_seed(value: object, digest: str) -> GateSeed:
    try:
        document = smoke._mapping(value, "gate seed")
    except smoke.SmokeError:
        raise BaselineCaptureError("gate seed is not an exact JSON object") from None
    expected_fields = {
        "authorization_contract",
        "deployment_digest",
        "gate_id",
        "gate_seed_contract",
        "method_authorization",
        "namespace_nonce",
        "prerequisite_evidence_run_digest",
        "source_authority_commit",
        "source_authority_commit_digest",
    }
    if set(document) != expected_fields:
        raise BaselineCaptureError("gate seed is not the exact closed authority object")
    if (
        document.get("gate_seed_contract") != GATE_SEED_CONTRACT
        or document.get("authorization_contract") != smoke.GATE_CONTRACT
        or document.get("gate_id") != smoke.GATE_ID
        or document.get("source_authority_commit") != smoke.SOURCE_AUTHORITY_COMMIT
        or document.get("source_authority_commit_digest") != smoke.SOURCE_AUTHORITY_COMMIT_DIGEST
        or document.get("method_authorization") != smoke._EXPECTED_METHOD_AUTHORIZATION
    ):
        raise BaselineCaptureError("gate seed does not bind the frozen smoke authority")
    for name in (
        "deployment_digest",
        "namespace_nonce",
        "prerequisite_evidence_run_digest",
    ):
        if not smoke._is_digest(document.get(name)):
            raise BaselineCaptureError("gate seed digest authority is invalid")
    deployment_digest = document["deployment_digest"]
    prerequisite_digest = document["prerequisite_evidence_run_digest"]
    assert isinstance(deployment_digest, str) and isinstance(prerequisite_digest, str)
    return GateSeed(
        digest=digest,
        deployment_digest=deployment_digest,
        prerequisite_digest=prerequisite_digest,
        document=document,
    )


def _deployment(value: object, seed: GateSeed) -> _DeploymentAuthorityDocument:
    try:
        deployment = _DeploymentAuthorityDocument.model_validate(value)
    except (ValidationError, ValueError):
        raise BaselineCaptureError(
            "deployment authority does not match the exact sanitized contract"
        ) from None
    if (
        deployment.deployment_digest != seed.deployment_digest
        or deployment.authority.source_commit_digest != smoke.SOURCE_AUTHORITY_COMMIT_DIGEST
    ):
        raise BaselineCaptureError("deployment authority does not bind the gate seed")
    return deployment


def _prerequisite(
    value: object,
    seed: GateSeed,
) -> DeployedNonDestructiveEvidenceRecord:
    if not isinstance(value, list) or len(value) != 1:
        raise BaselineCaptureError(
            "prerequisite authority must contain exactly one evidence record"
        )
    try:
        record = validate_phase66_evidence(value[0])
    except (TypeError, ValueError):
        raise BaselineCaptureError("prerequisite evidence record is invalid") from None
    if (
        not isinstance(record, DeployedNonDestructiveEvidenceRecord)
        or record.gate_id != PREREQUISITE_GATE_ID
        or record.outcome is not AcceptanceOutcome.PASSED
        or record.run_digest != seed.prerequisite_digest
        or record.deployment_digest != seed.deployment_digest
        or record.source_commit_digest != smoke.SOURCE_AUTHORITY_COMMIT_DIGEST
    ):
        raise BaselineCaptureError(
            "prerequisite evidence does not bind the passed deployment/source authority"
        )
    return record


def _verify_baseline(baseline: Mapping[str, object]) -> None:
    if (
        baseline.get("provider_record_count") != 0
        or baseline.get("existing_dispatched_work_count") != 0
        or baseline.get("running_execution_count") != 0
        or baseline.get("retention_checkpoint_present") is not True
        or baseline.get("synthetic_namespace_absent") is not True
        or baseline.get("synthetic_namespace_seed") is None
    ):
        raise BaselineCaptureError("deployed state is not a provider-zero inert baseline")
    source_count = baseline.get("retention_source_version_count")
    if (
        type(source_count) is not int
        or not 1 <= source_count <= smoke.MAX_SOURCE_VERSIONS
        or baseline.get("retention_referenced_version_count") != source_count
        or baseline.get("retention_pinned_version_count") != source_count
    ):
        raise BaselineCaptureError(
            "source-version inventory is not complete, referenced, pinned, and bounded"
        )


def _verify_sanitized_output(
    payload: bytes,
    snapshot: smoke.LiveSnapshot,
    canary: smoke.CanaryAuthority,
) -> None:
    raw_authorities = (
        *canary.sensitive_values,
        snapshot.authority.table_name,
        snapshot.authority.artifact_bucket,
        *snapshot.authority.functions.values(),
        *snapshot.authority.state_machine_arns.values(),
    )
    if any(value and value.encode() in payload for value in raw_authorities):
        raise BaselineCaptureError("sanitized baseline retained raw deployed authority")


def _write_once(path: Path, value: object) -> tuple[int, str]:
    candidate = _private_path(path)
    if candidate.name in {"", ".", ".."} or "/" in candidate.name or "\x00" in candidate.name:
        raise BaselineCaptureError("gate output filename is invalid")
    payload = smoke._canonical_json(value, pretty=True)
    temporary = f".{candidate.name}.{secrets.token_hex(12)}.tmp"
    with _private_directory_descriptor(candidate.parent, create=True) as directory_descriptor:
        directory_identity = os.fstat(directory_descriptor)
        descriptor: int | None = None
        linked = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_descriptor,
            )
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                descriptor = None
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.link(
                temporary,
                candidate.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            linked = True
            os.unlink(temporary, dir_fd=directory_descriptor)
            os.fsync(directory_descriptor)
            with _private_directory_descriptor(
                candidate.parent, create=False
            ) as verification_descriptor:
                verified_identity = os.fstat(verification_descriptor)
                if (directory_identity.st_dev, directory_identity.st_ino) != (
                    verified_identity.st_dev,
                    verified_identity.st_ino,
                ):
                    raise OSError
        except (OSError, BaselineCaptureError):
            if linked:
                try:
                    os.unlink(candidate.name, dir_fd=directory_descriptor)
                    os.fsync(directory_descriptor)
                except OSError:
                    pass
            raise BaselineCaptureError(
                "gate output must be one fresh mode-0600 private file"
            ) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
    return len(payload), smoke._digest_bytes(payload)


def capture_phase66_outbox_recovery_baseline(
    *,
    gate_seed_path: Path,
    gate_seed_sha256: str,
    deployment_authority_path: Path,
    deployment_authority_sha256: str,
    prerequisite_records_path: Path,
    prerequisite_records_sha256: str,
    output_path: Path,
    backend_factory: Callable[[], BaselineBackend] = smoke.AwsBackend,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Mapping[str, object]:
    """Validate local authorities, capture AWS reads, and write one exact run gate."""

    paths = tuple(
        _private_path(path)
        for path in (
            gate_seed_path,
            deployment_authority_path,
            prerequisite_records_path,
            output_path,
        )
    )
    if len(set(paths)) != len(paths):
        raise BaselineCaptureError("baseline inputs and output must be distinct")
    seed_value = _read_exact_json(gate_seed_path, gate_seed_sha256, "gate seed")
    deployment_value = _read_exact_json(
        deployment_authority_path,
        deployment_authority_sha256,
        "deployment authority",
    )
    prerequisite_value = _read_exact_json(
        prerequisite_records_path,
        prerequisite_records_sha256,
        "prerequisite records",
    )
    seed = _gate_seed(seed_value, gate_seed_sha256)
    deployment = _deployment(deployment_value, seed)
    prerequisite = _prerequisite(prerequisite_value, seed)
    captured_at = clock()
    if not isinstance(captured_at, datetime) or captured_at.utcoffset() is None:
        raise BaselineCaptureError("baseline capture clock must be timezone-aware")
    captured_at = captured_at.astimezone(UTC).replace(microsecond=0)
    if captured_at < prerequisite.recorded_at or captured_at < datetime.strptime(
        deployment.captured_at, "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=UTC):
        raise BaselineCaptureError("baseline capture predates a required authority")

    # Every local check above completes before the AWS-backed reader is constructed.
    canary = smoke.derive_canary(seed.digest)
    snapshot, synthetic_absent = backend_factory().capture_baseline(canary)
    baseline = snapshot.gate_baseline(
        synthetic_namespace_absent=synthetic_absent,
        synthetic_namespace_seed=seed.digest,
    )
    _verify_baseline(baseline)
    document = {
        "authorization_contract": smoke.GATE_CONTRACT,
        "baseline": baseline,
        "deployment_digest": seed.deployment_digest,
        "exact_write_budget": {
            **smoke._FIXED_WRITE_BUDGET,
            "s3_version_tag_writes": baseline["retention_source_version_count"],
        },
        "gate_id": smoke.GATE_ID,
        "method_authorization": dict(smoke._EXPECTED_METHOD_AUTHORIZATION),
        "prerequisite_evidence_run_digest": seed.prerequisite_digest,
        "source_authority_commit": smoke.SOURCE_AUTHORITY_COMMIT,
        "source_authority_commit_digest": smoke.SOURCE_AUTHORITY_COMMIT_DIGEST,
        "synthetic_namespace_seed": seed.digest,
    }
    rendered = smoke._canonical_json(document)
    _verify_sanitized_output(rendered, snapshot, canary)
    byte_count, gate_sha256 = _write_once(output_path, document)
    return {
        "byte_count": byte_count,
        "deployment_digest": seed.deployment_digest,
        "gate_sha256": gate_sha256,
        "gate_seed_digest": seed.digest,
        "prerequisite_evidence_run_digest": seed.prerequisite_digest,
        "result": "passed",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate-seed", required=True, type=Path)
    parser.add_argument("--gate-seed-sha256", required=True)
    parser.add_argument("--deployment-authority", required=True, type=Path)
    parser.add_argument("--deployment-authority-sha256", required=True)
    parser.add_argument("--prerequisite-records", required=True, type=Path)
    parser.add_argument("--prerequisite-records-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    backend_factory: Callable[[], BaselineBackend] = smoke.AwsBackend,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    arguments = _parser().parse_args(argv)
    result = capture_phase66_outbox_recovery_baseline(
        gate_seed_path=arguments.gate_seed,
        gate_seed_sha256=arguments.gate_seed_sha256,
        deployment_authority_path=arguments.deployment_authority,
        deployment_authority_sha256=arguments.deployment_authority_sha256,
        prerequisite_records_path=arguments.prerequisite_records,
        prerequisite_records_sha256=arguments.prerequisite_records_sha256,
        output_path=arguments.output,
        backend_factory=backend_factory,
        clock=clock,
    )
    print(smoke._canonical_json(result).decode())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BaselineCaptureError as error:
        raise SystemExit(f"phase66 outbox baseline capture stopped: {error}") from None
    except Exception:
        raise SystemExit(
            "phase66 outbox baseline capture stopped: an external read failed closed"
        ) from None
