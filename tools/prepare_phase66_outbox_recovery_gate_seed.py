#!/usr/bin/env python3
"""Prepare one closed, deployment-bound Phase 6.6 outbox run-gate seed.

This offline command consumes only the exact sanitized deployment authority and the passed
upload-integrity evidence record.  It emits the narrow seed contract consumed by
``capture_phase66_outbox_recovery_baseline``.  The random namespace authority is reduced to a
SHA-256 digest before serialization; raw identity, storage, credential, provider, and browser
authority has no input or output field.
"""

from __future__ import annotations

import argparse
import secrets
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from mr_lister.acceptance.phase6 import (
    AcceptanceOutcome,
    DeployedNonDestructiveEvidenceRecord,
    validate_phase66_evidence,
)
from tools import capture_phase66_outbox_recovery_baseline as baseline_capture
from tools import phase66_deployed_outbox_recovery_smoke as smoke
from tools.prepare_phase66_edge_revalidation import _DeploymentAuthorityDocument

ENTROPY_BYTES: Final = 32


class Phase66OutboxGateSeedError(RuntimeError):
    """One closed authority, confinement, or immutable-output assertion failed."""


def _deployment(value: object) -> _DeploymentAuthorityDocument:
    try:
        deployment = _DeploymentAuthorityDocument.model_validate(value)
    except (ValidationError, ValueError):
        raise Phase66OutboxGateSeedError(
            "deployment authority does not match the exact sanitized contract"
        ) from None
    if deployment.authority.source_commit_digest != smoke.SOURCE_AUTHORITY_COMMIT_DIGEST:
        raise Phase66OutboxGateSeedError(
            "deployment authority does not bind the frozen Phase 6 source"
        )
    return deployment


def _prerequisite(
    value: object,
    *,
    deployment: _DeploymentAuthorityDocument,
) -> DeployedNonDestructiveEvidenceRecord:
    if not isinstance(value, list) or len(value) != 1:
        raise Phase66OutboxGateSeedError(
            "prerequisite authority must contain exactly one evidence record"
        )
    try:
        record = validate_phase66_evidence(value[0])
    except (TypeError, ValueError):
        raise Phase66OutboxGateSeedError("prerequisite evidence record is invalid") from None
    if (
        not isinstance(record, DeployedNonDestructiveEvidenceRecord)
        or record.gate_id != baseline_capture.PREREQUISITE_GATE_ID
        or record.outcome is not AcceptanceOutcome.PASSED
        or record.deployment_digest != deployment.deployment_digest
        or record.source_commit_digest != smoke.SOURCE_AUTHORITY_COMMIT_DIGEST
    ):
        raise Phase66OutboxGateSeedError(
            "prerequisite evidence does not bind the passed deployment/source authority"
        )
    deployment_time = datetime.strptime(
        deployment.captured_at,
        "%Y-%m-%dT%H:%M:%SZ",
    ).replace(tzinfo=UTC)
    if record.recorded_at < deployment_time:
        raise Phase66OutboxGateSeedError("prerequisite evidence predates deployment authority")
    return record


def _namespace_nonce(
    *,
    deployment_digest: str,
    prerequisite_digest: str,
    entropy: Callable[[int], bytes],
) -> str:
    random_bytes = entropy(ENTROPY_BYTES)
    if not isinstance(random_bytes, bytes) or len(random_bytes) != ENTROPY_BYTES:
        raise Phase66OutboxGateSeedError("gate-seed entropy is invalid")
    return smoke._digest_bytes(
        b"\0".join(
            (
                baseline_capture.GATE_SEED_CONTRACT.encode("ascii"),
                deployment_digest.encode("ascii"),
                prerequisite_digest.encode("ascii"),
                random_bytes,
            )
        )
    )


def prepare_phase66_outbox_recovery_gate_seed(
    *,
    deployment_authority_path: Path,
    deployment_authority_sha256: str,
    prerequisite_records_path: Path,
    prerequisite_records_sha256: str,
    output_path: Path,
    entropy: Callable[[int], bytes] = secrets.token_bytes,
) -> Mapping[str, object]:
    """Validate two sanitized authorities and create one exact private gate-seed file."""

    try:
        paths = tuple(
            baseline_capture._private_path(path)
            for path in (
                deployment_authority_path,
                prerequisite_records_path,
                output_path,
            )
        )
        if len(set(paths)) != len(paths):
            raise Phase66OutboxGateSeedError("gate-seed inputs and output must be distinct")
        deployment_value = baseline_capture._read_exact_json(
            deployment_authority_path,
            deployment_authority_sha256,
            "deployment authority",
        )
        prerequisite_value = baseline_capture._read_exact_json(
            prerequisite_records_path,
            prerequisite_records_sha256,
            "prerequisite records",
        )
    except baseline_capture.BaselineCaptureError as error:
        raise Phase66OutboxGateSeedError(str(error)) from None

    deployment = _deployment(deployment_value)
    prerequisite = _prerequisite(prerequisite_value, deployment=deployment)
    document: dict[str, object] = {
        "authorization_contract": smoke.GATE_CONTRACT,
        "deployment_digest": deployment.deployment_digest,
        "gate_id": smoke.GATE_ID,
        "gate_seed_contract": baseline_capture.GATE_SEED_CONTRACT,
        "method_authorization": dict(smoke._EXPECTED_METHOD_AUTHORIZATION),
        "namespace_nonce": _namespace_nonce(
            deployment_digest=deployment.deployment_digest,
            prerequisite_digest=prerequisite.run_digest,
            entropy=entropy,
        ),
        "prerequisite_evidence_run_digest": prerequisite.run_digest,
        "source_authority_commit": smoke.SOURCE_AUTHORITY_COMMIT,
        "source_authority_commit_digest": smoke.SOURCE_AUTHORITY_COMMIT_DIGEST,
    }
    # Validate against the exact downstream consumer before any bytes are created.
    baseline_capture._gate_seed(document, "0" * 64)
    try:
        byte_count, gate_seed_sha256 = baseline_capture._write_once(output_path, document)
    except baseline_capture.BaselineCaptureError as error:
        raise Phase66OutboxGateSeedError(str(error)) from None
    return {
        "byte_count": byte_count,
        "deployment_digest": deployment.deployment_digest,
        "gate_seed_sha256": gate_seed_sha256,
        "prerequisite_evidence_run_digest": prerequisite.run_digest,
        "result": "passed",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment-authority", required=True, type=Path)
    parser.add_argument("--deployment-authority-sha256", required=True)
    parser.add_argument("--prerequisite-records", required=True, type=Path)
    parser.add_argument("--prerequisite-records-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    entropy: Callable[[int], bytes] = secrets.token_bytes,
) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = prepare_phase66_outbox_recovery_gate_seed(
            deployment_authority_path=arguments.deployment_authority,
            deployment_authority_sha256=arguments.deployment_authority_sha256,
            prerequisite_records_path=arguments.prerequisite_records,
            prerequisite_records_sha256=arguments.prerequisite_records_sha256,
            output_path=arguments.output,
            entropy=entropy,
        )
    except Phase66OutboxGateSeedError as error:
        _parser().error(str(error))
    print(smoke._canonical_json(result).decode("utf-8"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        if isinstance(error, SystemExit):
            raise
        raise SystemExit(
            "phase66 outbox gate-seed preparation stopped: an external operation failed closed"
        ) from None
