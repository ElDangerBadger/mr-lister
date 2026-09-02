"""Adversarial checks for the exact Phase 7.17 publish-once binding preparation."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

import tools.prepare_phase717_publish_once as operator
from mr_lister.publication.canary_runtime import (
    PublicationCanaryBinding,
    PublicationCanaryInvocation,
    PublicationCanaryMode,
    build_publication_canary_binding,
)
from mr_lister.publication.execution_fingerprints import execution_record_fingerprint
from mr_lister.publication.execution_models import PublicationExecutionAuthority
from tests.test_phase71_publication_store import OWNER_ID
from tests.test_phase72_publication_execution import Harness


@dataclass
class Backend:
    authority: PublicationExecutionAuthority
    calls: list[tuple[str, str]]

    def load_execution_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationExecutionAuthority:
        self.calls.append((owner_id, aggregate_id))
        return self.authority


@pytest.fixture
def prepared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Harness, Backend, Path, Path, bytes, bytes]:
    harness = Harness()
    pristine = harness.authority
    old_binding = build_publication_canary_binding(
        pristine,
        mode=PublicationCanaryMode.READ_ONLY_PREFLIGHT,
    )
    invocation = PublicationCanaryInvocation(
        owner_id=OWNER_ID,
        aggregate_id=harness.aggregate_id,
    )
    binding_raw = operator._canonical_json(old_binding.model_dump(mode="json"), pretty=True)
    invocation_raw = operator._canonical_json(
        {"aggregate_id": invocation.aggregate_id, "owner_id": invocation.owner_id},
        pretty=True,
    )

    read_root = tmp_path / "phase712-canary-operator"
    read_root.mkdir(mode=0o700)
    source = read_root / "executed"
    source.mkdir(mode=0o700)
    (source / operator.BINDING_FILENAME).write_bytes(binding_raw)
    (source / operator.INVOCATION_FILENAME).write_bytes(invocation_raw)
    (source / operator.BINDING_FILENAME).chmod(0o600)
    (source / operator.INVOCATION_FILENAME).chmod(0o600)

    write_root = tmp_path / "phase717-publish-once"
    monkeypatch.setattr(operator, "READ_ONLY_PRIVATE_ROOT", read_root)
    monkeypatch.setattr(operator, "PRIVATE_ROOT", write_root)

    harness.dispatch_and_reconstruct()
    harness.complete_preflight()
    backend = Backend(harness.authority, [])
    return harness, backend, source, write_root, binding_raw, invocation_raw


def _prepare(
    harness: Harness,
    backend: Backend,
    source: Path,
    output: Path,
    binding_raw: bytes,
    invocation_raw: bytes,
    *,
    now=None,  # type: ignore[no-untyped-def]
) -> dict[str, object]:
    return operator.prepare_publish_once(
        read_only_root=source,
        read_only_binding_sha256=operator._digest(binding_raw),
        private_invocation_sha256=operator._digest(invocation_raw),
        output_root=output,
        backend_factory=lambda: backend,
        clock=lambda: harness.clock.now if now is None else now,
    )


def test_exact_preflight_mints_only_sanitized_binding_and_same_private_invocation(
    prepared: tuple[Harness, Backend, Path, Path, bytes, bytes],
) -> None:
    harness, backend, source, write_root, binding_raw, invocation_raw = prepared
    output = write_root / "approved"

    result = _prepare(
        harness,
        backend,
        source,
        output,
        binding_raw,
        invocation_raw,
    )

    assert backend.calls == [(OWNER_ID, harness.aggregate_id)]
    assert result["status"] == "bound_publish_once"
    assert result["mode"] == "publish_once"
    assert result["aws_mutations"] == 0
    assert result["provider_calls"] == 0
    assert result["provider_posts"] == 0
    assert {path.name for path in output.iterdir()} == {
        operator.BINDING_FILENAME,
        operator.INVOCATION_FILENAME,
    }
    assert (output / operator.INVOCATION_FILENAME).read_bytes() == invocation_raw
    assert (output.stat().st_mode & 0o077) == 0
    assert ((output / operator.BINDING_FILENAME).stat().st_mode & 0o077) == 0
    assert ((output / operator.INVOCATION_FILENAME).stat().st_mode & 0o077) == 0

    new_raw = (output / operator.BINDING_FILENAME).read_bytes()
    new = PublicationCanaryBinding.model_validate_json(new_raw, strict=True)
    old = PublicationCanaryBinding.model_validate_json(binding_raw, strict=True)
    assert new.mode is PublicationCanaryMode.PUBLISH_ONCE
    assert new.required_preflight_proof_fingerprint == harness.authority.preflight_proof.fingerprint
    assert all(
        getattr(old, field) == getattr(new, field) for field in operator._STABLE_BINDING_FIELDS
    )
    public = new_raw + operator._canonical_json(result)
    for raw in operator._private_identity_values(harness.authority):
        assert raw.encode() not in public


def test_main_uses_injected_backend_and_prints_only_sanitized_json(
    prepared: tuple[Harness, Backend, Path, Path, bytes, bytes],
    capsys: pytest.CaptureFixture[str],
) -> None:
    harness, backend, source, write_root, binding_raw, invocation_raw = prepared

    assert (
        operator.main(
            [
                "--read-only-root",
                str(source),
                "--read-only-binding-sha256",
                operator._digest(binding_raw),
                "--private-invocation-sha256",
                operator._digest(invocation_raw),
                "--output-root",
                str(write_root / "main-output"),
            ],
            backend_factory=lambda: backend,
            clock=lambda: harness.clock.now,
        )
        == 0
    )

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "bound_publish_once"
    rendered = json.dumps(result, sort_keys=True)
    assert OWNER_ID not in rendered
    assert harness.aggregate_id not in rendered


@pytest.mark.parametrize("which", ["binding", "invocation"])
def test_sha_drift_refuses_before_backend_or_output_creation(
    prepared: tuple[Harness, Backend, Path, Path, bytes, bytes],
    which: str,
) -> None:
    harness, backend, source, write_root, binding_raw, invocation_raw = prepared
    output = write_root / "must-not-exist"

    with pytest.raises(operator.Phase717PublishOncePreparationError, match="refused safely"):
        operator.prepare_publish_once(
            read_only_root=source,
            read_only_binding_sha256=(
                "0" * 64 if which == "binding" else operator._digest(binding_raw)
            ),
            private_invocation_sha256=(
                "1" * 64 if which == "invocation" else operator._digest(invocation_raw)
            ),
            output_root=output,
            backend_factory=lambda: backend,
            clock=lambda: harness.clock.now,
        )

    assert backend.calls == []
    assert not output.exists()


def test_foreign_private_invocation_cannot_be_rebound_even_with_approved_hash(
    prepared: tuple[Harness, Backend, Path, Path, bytes, bytes],
) -> None:
    harness, backend, source, write_root, binding_raw, _invocation_raw = prepared
    foreign = PublicationCanaryInvocation(
        owner_id="f" * 64,
        aggregate_id=harness.aggregate_id,
    )
    foreign_raw = operator._canonical_json(
        {"aggregate_id": foreign.aggregate_id, "owner_id": foreign.owner_id},
        pretty=True,
    )
    (source / operator.INVOCATION_FILENAME).write_bytes(foreign_raw)
    (source / operator.INVOCATION_FILENAME).chmod(0o600)

    with pytest.raises(operator.Phase717PublishOncePreparationError, match="refused safely"):
        operator.prepare_publish_once(
            read_only_root=source,
            read_only_binding_sha256=operator._digest(binding_raw),
            private_invocation_sha256=operator._digest(foreign_raw),
            output_root=write_root / "foreign",
            backend_factory=lambda: backend,
            clock=lambda: harness.clock.now,
        )

    assert backend.calls == []


def test_incomplete_or_nonexact_authority_writes_nothing(
    prepared: tuple[Harness, Backend, Path, Path, bytes, bytes],
) -> None:
    harness, backend, source, write_root, binding_raw, invocation_raw = prepared
    backend.authority = harness.authority.model_copy(update={"preflight_proof": None})
    output = write_root / "incomplete"

    with pytest.raises(operator.Phase717PublishOncePreparationError, match="refused safely"):
        _prepare(
            harness,
            backend,
            source,
            output,
            binding_raw,
            invocation_raw,
        )

    assert backend.calls == [(OWNER_ID, harness.aggregate_id)]
    assert output.is_dir()
    assert list(output.iterdir()) == []


def test_consumed_permit_and_existing_mutation_are_never_minted(
    prepared: tuple[Harness, Backend, Path, Path, bytes, bytes],
) -> None:
    harness, backend, source, write_root, binding_raw, invocation_raw = prepared
    harness.claim_publish()
    backend.authority = harness.authority
    output = write_root / "post-authority"

    with pytest.raises(operator.Phase717PublishOncePreparationError, match="refused safely"):
        _prepare(
            harness,
            backend,
            source,
            output,
            binding_raw,
            invocation_raw,
        )

    assert output.is_dir()
    assert list(output.iterdir()) == []


def test_deadline_is_strict_and_cannot_be_extended_during_mint(
    prepared: tuple[Harness, Backend, Path, Path, bytes, bytes],
) -> None:
    harness, backend, source, write_root, binding_raw, invocation_raw = prepared
    output = write_root / "expired"

    with pytest.raises(operator.Phase717PublishOncePreparationError, match="refused safely"):
        _prepare(
            harness,
            backend,
            source,
            output,
            binding_raw,
            invocation_raw,
            now=harness.authority.snapshot.verification_deadline,
        )

    assert output.is_dir()
    assert list(output.iterdir()) == []


def test_immutable_read_only_binding_drift_is_rejected_after_strong_read(
    prepared: tuple[Harness, Backend, Path, Path, bytes, bytes],
) -> None:
    harness, backend, source, write_root, binding_raw, invocation_raw = prepared
    old = PublicationCanaryBinding.model_validate_json(binding_raw, strict=True)
    values = old.model_dump(mode="python", exclude={"fingerprint"})
    values["snapshot_fingerprint"] = "e" * 64
    drifted = PublicationCanaryBinding(
        **values,
        fingerprint=execution_record_fingerprint("publication_canary_binding", values),
    )
    drifted_raw = operator._canonical_json(drifted.model_dump(mode="json"), pretty=True)
    (source / operator.BINDING_FILENAME).write_bytes(drifted_raw)
    (source / operator.BINDING_FILENAME).chmod(0o600)
    output = write_root / "drifted"

    with pytest.raises(operator.Phase717PublishOncePreparationError, match="refused safely"):
        _prepare(
            harness,
            backend,
            source,
            output,
            drifted_raw,
            invocation_raw,
        )

    assert output.is_dir()
    assert list(output.iterdir()) == []


def test_private_file_permissions_and_create_once_output_are_enforced(
    prepared: tuple[Harness, Backend, Path, Path, bytes, bytes],
) -> None:
    harness, backend, source, write_root, binding_raw, invocation_raw = prepared
    (source / operator.INVOCATION_FILENAME).chmod(0o644)

    with pytest.raises(operator.Phase717PublishOncePreparationError, match="refused safely"):
        _prepare(
            harness,
            backend,
            source,
            write_root / "bad-mode",
            binding_raw,
            invocation_raw,
        )
    assert backend.calls == []

    (source / operator.INVOCATION_FILENAME).chmod(0o600)
    occupied = write_root / "occupied"
    occupied.mkdir(mode=0o700, parents=True)
    with pytest.raises(operator.Phase717PublishOncePreparationError, match="refused safely"):
        _prepare(
            harness,
            backend,
            source,
            occupied,
            binding_raw,
            invocation_raw,
        )
    assert backend.calls == []


def test_source_closure_has_only_fixed_read_aws_and_no_provider_or_mutation_surface() -> None:
    source = Path(operator.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    client_services = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "client"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    called_methods = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert client_services == {"dynamodb", "sts"}
    assert called_methods.isdisjoint(
        {
            "delete_item",
            "invoke",
            "put_item",
            "send_message",
            "transact_write_items",
            "update_item",
        }
    )
    assert imported_modules.isdisjoint(
        {
            "mr_lister.publication.credential_resolver",
            "mr_lister.publication.provider_client",
            "mr_lister.publication.provider_transport",
        }
    )
    assert operator.PROFILE == "mr-lister-dev"
    assert operator.ACCOUNT_ID == "384627057108"
    assert operator.REGION == "us-west-2"
    assert operator.STATE_TABLE == "mr-lister-phase6-dev"


def test_clock_before_durable_proof_is_rejected(
    prepared: tuple[Harness, Backend, Path, Path, bytes, bytes],
) -> None:
    harness, backend, source, write_root, binding_raw, invocation_raw = prepared
    proof = harness.authority.preflight_proof
    assert proof is not None
    output = write_root / "clock-regression"

    with pytest.raises(operator.Phase717PublishOncePreparationError, match="refused safely"):
        _prepare(
            harness,
            backend,
            source,
            output,
            binding_raw,
            invocation_raw,
            now=proof.proven_at - timedelta(microseconds=1),
        )

    assert output.is_dir()
    assert list(output.iterdir()) == []
