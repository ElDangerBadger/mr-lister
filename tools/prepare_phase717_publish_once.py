#!/usr/bin/env python3
"""Mint one exact ``publish_once`` canary binding from completed read-only authority.

The operator accepts only the two create-once artifacts emitted by the Phase 7.12 request
preparation, verifies both supplied SHA-256 approvals, and strongly re-reads the same owner and
aggregate from the fixed development table.  It emits a new sanitized binding plus the unchanged
private invocation.  It has no provider, secret, Lambda, S3, publication POST, or DynamoDB write
surface.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Final, Protocol

from mr_lister.publication.canary_runtime import (
    PublicationCanaryBinding,
    PublicationCanaryInvocation,
    PublicationCanaryMode,
    build_publication_canary_binding,
)
from mr_lister.publication.contract import PublicationPermitState, PublicationState
from mr_lister.publication.execution_dynamodb import DynamoDBPublicationExecutionStore
from mr_lister.publication.execution_fingerprints import safe_identity_digest
from mr_lister.publication.execution_models import (
    PublicationCallKind,
    PublicationExecutionAuthority,
    PublicationExecutionWorkStatus,
)

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
READ_ONLY_PRIVATE_ROOT: Final = REPOSITORY_ROOT / ".mr_lister_private" / "phase712-canary-operator"
PRIVATE_ROOT: Final = REPOSITORY_ROOT / ".mr_lister_private" / "phase717-publish-once"

PROFILE: Final = "mr-lister-dev"
ACCOUNT_ID: Final = "384627057108"
REGION: Final = "us-west-2"
CALLER_ARN: Final = f"arn:aws:iam::{ACCOUNT_ID}:user/{PROFILE}"
STATE_TABLE: Final = "mr-lister-phase6-dev"
STATE_TABLE_ARN: Final = f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/{STATE_TABLE}"

BINDING_FILENAME: Final = "canary-binding.json"
INVOCATION_FILENAME: Final = "invocation.local.json"
MAX_PRIVATE_BYTES: Final = 128 * 1024

_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_STABLE_BINDING_FIELDS: Final = (
    "owner_id_digest",
    "aggregate_id_digest",
    "job_id_digest",
    "snapshot_fingerprint",
    "permit_id_digest",
    "work_request_id_digest",
    "work_input_fingerprint",
    "release_manifest_fingerprint",
    "verification_deadline",
)


class Phase717PublishOncePreparationError(RuntimeError):
    """Value-free refusal for stale, foreign, expired, or incomplete canary authority."""


class ReadBackend(Protocol):
    """The sole live boundary: one complete strong execution-authority read."""

    def load_execution_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationExecutionAuthority: ...


class _StrongReadDynamoDBClient:
    """Expose only the two strongly consistent operations used by the execution store."""

    __slots__ = ("_client",)

    def __init__(self, client: object) -> None:
        self._client = client

    def get_item(self, **values: object) -> object:
        return self._client.get_item(**values)  # type: ignore[attr-defined, no-any-return]

    def query(self, **values: object) -> object:
        return self._client.query(**values)  # type: ignore[attr-defined, no-any-return]


class AwsReadBackend:
    """Fixed-account development read adapter with no DynamoDB mutation method."""

    __slots__ = ("_dynamodb", "_execution", "_sts")

    def __init__(self) -> None:
        import boto3

        session = boto3.Session(profile_name=PROFILE, region_name=REGION)
        self._sts = session.client("sts", region_name=REGION)
        self._dynamodb = session.client("dynamodb", region_name=REGION)
        self._execution = DynamoDBPublicationExecutionStore(
            client=_StrongReadDynamoDBClient(self._dynamodb),
            table_name=STATE_TABLE,
        )

    def load_execution_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationExecutionAuthority:
        identity = _mapping(self._sts.get_caller_identity())
        table = _mapping(self._dynamodb.describe_table(TableName=STATE_TABLE)).get("Table")
        table = _mapping(table)
        if (
            identity.get("Account") != ACCOUNT_ID
            or identity.get("Arn") != CALLER_ARN
            or table.get("TableName") != STATE_TABLE
            or table.get("TableArn") != STATE_TABLE_ARN
            or table.get("TableStatus") != "ACTIVE"
        ):
            raise ValueError
        return self._execution.load_execution_authority(owner_id, aggregate_id)


def prepare_publish_once(
    *,
    read_only_root: Path,
    read_only_binding_sha256: str,
    private_invocation_sha256: str,
    output_root: Path,
    backend_factory: Callable[[], ReadBackend],
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    """Strong-read and freeze the exact post-preflight authority without invoking it."""

    try:
        if (
            _DIGEST.fullmatch(read_only_binding_sha256) is None
            or _DIGEST.fullmatch(private_invocation_sha256) is None
        ):
            raise ValueError
        source = _existing_private_directory(read_only_root, READ_ONLY_PRIVATE_ROOT)
        old_binding_raw = _read_private_file(source, BINDING_FILENAME)
        invocation_raw = _read_private_file(source, INVOCATION_FILENAME)
        if (
            _digest(old_binding_raw) != read_only_binding_sha256
            or _digest(invocation_raw) != private_invocation_sha256
        ):
            raise ValueError

        old_binding = _binding(old_binding_raw)
        invocation = _invocation(invocation_raw)
        _require_read_only_pair(old_binding, invocation)

        # Prove the destination is fresh before crossing the live read boundary.
        destination = _fresh_private_directory(output_root)
        authority = _exact_authority(
            backend_factory().load_execution_authority(
                invocation.owner_id,
                invocation.aggregate_id,
            )
        )
        now = _utc_now(clock)
        _require_publish_once_authority(authority, now=now)
        new_binding = build_publication_canary_binding(
            authority,
            mode=PublicationCanaryMode.PUBLISH_ONCE,
        )
        _require_immutable_binding(old_binding, new_binding)

        binding_raw = _canonical_json(new_binding.model_dump(mode="json"), pretty=True)
        private_values = _private_identity_values(authority)
        _assert_sanitized(binding_raw, private_values)
        result = {
            "aws_mutations": 0,
            "binding_sha256": _digest(binding_raw),
            "invocation_sha256": _digest(invocation_raw),
            "mode": PublicationCanaryMode.PUBLISH_ONCE.value,
            "provider_calls": 0,
            "provider_posts": 0,
            "status": "bound_publish_once",
            "verification_window_remaining_seconds": int(
                (authority.snapshot.verification_deadline - now).total_seconds()
            ),
        }
        _assert_sanitized(_canonical_json(result), private_values)
        _write_once(destination, BINDING_FILENAME, binding_raw)
        _write_once(destination, INVOCATION_FILENAME, invocation_raw)
        return result
    except Phase717PublishOncePreparationError:
        raise
    except Exception:
        raise Phase717PublishOncePreparationError(
            "Phase 7.17 publish-once preparation refused safely"
        ) from None


def _binding(raw: bytes) -> PublicationCanaryBinding:
    _strict_json(raw, "read-only binding")
    binding = PublicationCanaryBinding.model_validate_json(raw, strict=True)
    if raw != _canonical_json(binding.model_dump(mode="json"), pretty=True):
        raise ValueError
    return binding


def _invocation(raw: bytes) -> PublicationCanaryInvocation:
    value = _strict_json(raw, "private invocation")
    if not isinstance(value, Mapping) or set(value) != {"aggregate_id", "owner_id"}:
        raise ValueError
    invocation = PublicationCanaryInvocation.model_validate_json(raw, strict=True)
    canonical = {
        "aggregate_id": invocation.aggregate_id,
        "owner_id": invocation.owner_id,
    }
    if raw != _canonical_json(canonical, pretty=True):
        raise ValueError
    return invocation


def _require_read_only_pair(
    binding: PublicationCanaryBinding,
    invocation: PublicationCanaryInvocation,
) -> None:
    if (
        binding.mode is not PublicationCanaryMode.READ_ONLY_PREFLIGHT
        or binding.required_preflight_proof_fingerprint is not None
        or safe_identity_digest("owner_id", invocation.owner_id) != binding.owner_id_digest
        or safe_identity_digest("publication_aggregate_id", invocation.aggregate_id)
        != binding.aggregate_id_digest
    ):
        raise ValueError


def _require_publish_once_authority(
    authority: PublicationExecutionAuthority,
    *,
    now: datetime,
) -> None:
    proof = authority.preflight_proof
    if (
        authority.aggregate.state is not PublicationState.PUBLICATION_REQUESTED
        or authority.permit.status is not PublicationPermitState.AVAILABLE
        or authority.work.status is not PublicationExecutionWorkStatus.DISPATCHED
        or proof is None
        or authority.provider_authority is None
        or authority.mutation_claim is not None
        or authority.post_observation is not None
        or authority.attempt.publish_post_call_count != 0
        or any(
            claim.call_kind is PublicationCallKind.PUBLISH_POST
            or claim.method == "POST"
            or claim.mutation_authorized
            for claim in authority.call_claims
        )
        or now < proof.proven_at
        or now >= authority.snapshot.verification_deadline
    ):
        raise ValueError


def _require_immutable_binding(
    old: PublicationCanaryBinding,
    new: PublicationCanaryBinding,
) -> None:
    proof = new.required_preflight_proof_fingerprint
    if (
        new.mode is not PublicationCanaryMode.PUBLISH_ONCE
        or proof is None
        or any(getattr(old, field) != getattr(new, field) for field in _STABLE_BINDING_FIELDS)
    ):
        raise ValueError


def _exact_authority(value: object) -> PublicationExecutionAuthority:
    if not isinstance(value, PublicationExecutionAuthority):
        raise TypeError
    exact = PublicationExecutionAuthority.model_validate(value.model_dump(mode="python"))
    if exact != value:
        raise ValueError
    return exact


def _private_identity_values(authority: PublicationExecutionAuthority) -> tuple[str, ...]:
    values: set[str] = set()

    def visit(value: object, key: str | None = None) -> None:
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                visit(child, str(child_key))
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child, key)
        elif key is not None and (
            key.endswith("_id")
            or key in {"printify_shop_id", "printify_product_id", "printify_image_id"}
        ):
            values.add(str(value))

    visit(authority.model_dump(mode="json"))
    return tuple(
        sorted((value for value in values if value), key=lambda value: (-len(value), value))
    )


def _assert_sanitized(payload: bytes, private_values: Sequence[str]) -> None:
    if any(value.encode() in payload for value in private_values):
        raise ValueError


def _canonical_json(value: object, *, pretty: bool = False) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _strict_json(raw: bytes, label: str) -> object:
    def reject_constant(_value: str) -> None:
        raise ValueError

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        return json.loads(
            raw,
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except Exception:
        raise ValueError(f"invalid {label}") from None


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError
    return value


def _utc_now(clock: Callable[[], datetime] | None) -> datetime:
    value = (clock or (lambda: datetime.now(UTC)))()
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError
    return value.astimezone(UTC)


def _digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _private_child(path: Path, root: Path) -> Path:
    candidate = _absolute(path)
    exact_root = _absolute(root)
    try:
        relative = candidate.relative_to(exact_root)
    except ValueError:
        raise ValueError from None
    if len(relative.parts) != 1 or candidate == exact_root:
        raise ValueError
    return candidate


def _directory_is_private(path: Path) -> bool:
    if path.is_symlink():
        return False
    try:
        metadata = path.stat()
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and stat.S_IMODE(metadata.st_mode) == 0o700


def _existing_private_directory(path: Path, root: Path) -> Path:
    exact_root = _absolute(root)
    candidate = _private_child(path, exact_root)
    if not _directory_is_private(exact_root) or not _directory_is_private(candidate):
        raise ValueError
    return candidate


def _ensure_private_root() -> Path:
    parent = PRIVATE_ROOT.parent
    if parent.is_symlink():
        raise ValueError
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if PRIVATE_ROOT.is_symlink():
        raise ValueError
    PRIVATE_ROOT.mkdir(mode=0o700, exist_ok=True)
    PRIVATE_ROOT.chmod(0o700)
    if not _directory_is_private(PRIVATE_ROOT):
        raise ValueError
    return _absolute(PRIVATE_ROOT)


def _fresh_private_directory(path: Path) -> Path:
    root = _ensure_private_root()
    candidate = _private_child(path, root)
    if candidate.exists() or candidate.is_symlink():
        raise ValueError
    candidate.mkdir(mode=0o700)
    if not _directory_is_private(candidate):
        raise ValueError
    return candidate


def _read_private_file(directory: Path, name: str) -> bytes:
    if name not in {BINDING_FILENAME, INVOCATION_FILENAME}:
        raise ValueError
    descriptor: int | None = None
    directory_descriptor: int | None = None
    try:
        directory_descriptor = os.open(
            directory,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory_descriptor,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or not 1 <= before.st_size <= MAX_PRIVATE_BYTES
        ):
            raise OSError
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise OSError
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise OSError
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise OSError
        return b"".join(chunks)
    except OSError:
        raise ValueError from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)


def _write_once(directory: Path, name: str, payload: bytes) -> None:
    if name not in {BINDING_FILENAME, INVOCATION_FILENAME} or not _directory_is_private(directory):
        raise ValueError
    directory_descriptor: int | None = None
    descriptor: int | None = None
    try:
        directory_descriptor = os.open(
            directory,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_descriptor,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = None
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        raise ValueError from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--read-only-root", type=Path, required=True)
    parser.add_argument("--read-only-binding-sha256", required=True)
    parser.add_argument("--private-invocation-sha256", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    backend_factory: Callable[[], ReadBackend] = AwsReadBackend,
    clock: Callable[[], datetime] | None = None,
) -> int:
    arguments = _parser().parse_args(argv)
    result = prepare_publish_once(
        read_only_root=arguments.read_only_root,
        read_only_binding_sha256=arguments.read_only_binding_sha256,
        private_invocation_sha256=arguments.private_invocation_sha256,
        output_root=arguments.output_root,
        backend_factory=backend_factory,
        clock=clock,
    )
    print(_canonical_json(result).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
