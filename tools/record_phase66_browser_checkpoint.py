#!/usr/bin/env python3
"""Record one sanitized, deployment-bound Phase 6.6 browser checkpoint.

The operator observation is a closed JSON document containing only the exact deployment/source
digests, canonical UTC time, two bounded job counts, and required pass/fail facts.  This offline
recorder neither controls a browser nor accepts identity, URL, token, cookie, or free-text fields.
It emits precisely the checkpoint schema consumed by the deployed edge/owner observation.
"""

from __future__ import annotations

import argparse
import secrets
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    StringConstraints,
    ValidationError,
    model_validator,
)

from tools import phase66_deployed_edge_auth_owner_observation as edge_observation
from tools import phase66_deployed_upload_integrity_smoke as private_io
from tools.prepare_phase66_edge_revalidation import _DeploymentAuthorityDocument

OPERATOR_OBSERVATION_FORMAT: Final = "phase6.6-operator-browser-edge-auth-owner-observation-v1"
MAX_AUTHORITY_WINDOW: Final = timedelta(hours=4)

type Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
type CanonicalTimestamp = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"),
]


class Phase66BrowserCheckpointError(RuntimeError):
    """One closed observation, confinement, time, or immutable-output assertion failed."""


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _OperatorObservation(_ClosedModel):
    format: Literal[OPERATOR_OBSERVATION_FORMAT]
    recorded_at: CanonicalTimestamp
    deployment_digest: Digest
    source_commit_digest: Literal[edge_observation.SOURCE_COMMIT_DIGEST]
    actor_a: edge_observation._BrowserActorA
    actor_b: edge_observation._BrowserActorB
    matrix: edge_observation._BrowserAuthMatrix

    @model_validator(mode="after")
    def timestamp_is_calendar_valid(self) -> _OperatorObservation:
        datetime.strptime(self.recorded_at, "%Y-%m-%dT%H:%M:%SZ")
        return self


def _private_path(path: Path) -> Path:
    try:
        return edge_observation._private_path(path)
    except edge_observation.Phase66EdgeObservationError:
        raise Phase66BrowserCheckpointError(
            "checkpoint paths must stay in the repository-private acceptance root"
        ) from None


def _read_exact_json(path: Path, expected_digest: str, label: str) -> object:
    if not edge_observation._is_digest(expected_digest):
        raise Phase66BrowserCheckpointError(f"{label} SHA-256 is invalid")
    candidate = _private_path(path)
    try:
        payload = private_io._read_private_file(
            candidate,
            max_bytes=edge_observation.MAX_INPUT_BYTES,
        )
        if not secrets.compare_digest(private_io._digest_bytes(payload), expected_digest):
            raise Phase66BrowserCheckpointError(f"{label} changed or does not match its SHA-256")
        return private_io._strict_json(payload, label)
    except private_io.SmokeError:
        raise Phase66BrowserCheckpointError(
            f"{label} must be one stable mode-0600 private JSON file"
        ) from None


def _deployment(value: object) -> _DeploymentAuthorityDocument:
    try:
        deployment = _DeploymentAuthorityDocument.model_validate(value)
    except (ValidationError, ValueError):
        raise Phase66BrowserCheckpointError(
            "deployment authority does not match the exact sanitized contract"
        ) from None
    if deployment.authority.source_commit_digest != edge_observation.SOURCE_COMMIT_DIGEST:
        raise Phase66BrowserCheckpointError(
            "deployment authority does not bind the frozen Phase 6 source"
        )
    return deployment


def _clock_value(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise Phase66BrowserCheckpointError("checkpoint clock must be timezone-aware")
    return value.astimezone(UTC).replace(microsecond=0)


def _validate_authority_window(
    *,
    deployment: _DeploymentAuthorityDocument,
    observation: _OperatorObservation,
    now: datetime,
) -> None:
    deployed_at = datetime.strptime(deployment.captured_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )
    observed_at = datetime.strptime(observation.recorded_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )
    if (
        observed_at < deployed_at
        or observed_at > now
        or observed_at - deployed_at > MAX_AUTHORITY_WINDOW
        or now - observed_at > MAX_AUTHORITY_WINDOW
    ):
        raise Phase66BrowserCheckpointError(
            "browser observation is outside the exact deployment/recording window"
        )


def record_phase66_browser_checkpoint(
    *,
    deployment_authority_path: Path,
    deployment_authority_sha256: str,
    operator_observation_path: Path,
    operator_observation_sha256: str,
    output_path: Path,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Validate one closed operator observation and create the exact consumer checkpoint."""

    paths = tuple(
        _private_path(path)
        for path in (
            deployment_authority_path,
            operator_observation_path,
            output_path,
        )
    )
    if len(set(paths)) != len(paths):
        raise Phase66BrowserCheckpointError("checkpoint inputs and output must be distinct")
    deployment = _deployment(
        _read_exact_json(
            deployment_authority_path,
            deployment_authority_sha256,
            "deployment authority",
        )
    )
    try:
        observation = _OperatorObservation.model_validate(
            _read_exact_json(
                operator_observation_path,
                operator_observation_sha256,
                "operator observation",
            )
        )
    except (ValidationError, ValueError):
        raise Phase66BrowserCheckpointError(
            "operator observation does not match the exact closed contract"
        ) from None
    if (
        observation.deployment_digest != deployment.deployment_digest
        or observation.source_commit_digest != deployment.authority.source_commit_digest
    ):
        raise Phase66BrowserCheckpointError(
            "operator observation does not bind the deployment/source authority"
        )
    _validate_authority_window(
        deployment=deployment,
        observation=observation,
        now=_clock_value(clock),
    )

    checkpoint: dict[str, object] = {
        "format": edge_observation.BROWSER_CHECKPOINT_FORMAT,
        "recorded_at": observation.recorded_at,
        "deployment_digest": deployment.deployment_digest,
        "actor_a": observation.actor_a.model_dump(mode="json"),
        "actor_b": observation.actor_b.model_dump(mode="json"),
        "matrix": observation.matrix.model_dump(mode="json"),
    }
    try:
        validated = edge_observation._BrowserCheckpoint.model_validate(checkpoint)
    except (ValidationError, ValueError):
        raise Phase66BrowserCheckpointError(
            "emitted browser checkpoint does not match the exact consumer contract"
        ) from None
    checkpoint = validated.model_dump(mode="json")

    output = _private_path(output_path)
    try:
        with private_io._private_directory_descriptor(output.parent, create=True) as descriptor:
            byte_count, checkpoint_sha256 = private_io._write_once_private_json(
                descriptor,
                output.name,
                checkpoint,
            )
    except private_io.SmokeError:
        raise Phase66BrowserCheckpointError(
            "checkpoint output must be one fresh mode-0600 private JSON file"
        ) from None
    return {
        "byte_count": byte_count,
        "checkpoint_sha256": checkpoint_sha256,
        "deployment_digest": deployment.deployment_digest,
        "recorded_at": observation.recorded_at,
        "result": "passed",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment-authority", required=True, type=Path)
    parser.add_argument("--deployment-authority-sha256", required=True)
    parser.add_argument("--operator-observation", required=True, type=Path)
    parser.add_argument("--operator-observation-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = record_phase66_browser_checkpoint(
            deployment_authority_path=arguments.deployment_authority,
            deployment_authority_sha256=arguments.deployment_authority_sha256,
            operator_observation_path=arguments.operator_observation,
            operator_observation_sha256=arguments.operator_observation_sha256,
            output_path=arguments.output,
            clock=clock,
        )
    except Phase66BrowserCheckpointError as error:
        _parser().error(str(error))
    print(private_io._canonical_json(result).decode("utf-8"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        if isinstance(error, SystemExit):
            raise
        raise SystemExit(
            "phase66 browser-checkpoint recording stopped: an external operation failed closed"
        ) from None
