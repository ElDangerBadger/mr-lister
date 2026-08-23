from __future__ import annotations

import ast
from pathlib import Path

import mr_lister.control as control_package
from mr_lister.control.commands import (
    ApproveReviewCommand,
    CancelJobCommand,
    RecordWorkerFailureCommand,
    RetryJobCommand,
)
from mr_lister.control.fingerprints import canonical_fingerprint, review_etag


def test_composite_review_etag_binds_review_product_sync_and_pricing() -> None:
    basis = {
        "job_id": "job_phase6",
        "review_version": 2,
        "review_fingerprint": "a" * 64,
        "product_id": "product-1",
        "product_sync_fingerprint": "b" * 64,
        "pricing_snapshot_id": "pricing-1",
        "pricing_snapshot_fingerprint": "c" * 64,
    }
    original = review_etag(**basis)

    for field, replacement in (
        ("review_fingerprint", "d" * 64),
        ("product_id", "product-2"),
        ("product_sync_fingerprint", "e" * 64),
        ("pricing_snapshot_id", "pricing-2"),
        ("pricing_snapshot_fingerprint", "f" * 64),
    ):
        assert review_etag(**{**basis, field: replacement}) != original


def test_canonical_fingerprint_is_independent_of_mapping_order() -> None:
    assert canonical_fingerprint({"a": 1, "b": 2}) == canonical_fingerprint({"b": 2, "a": 1})


def test_cancel_before_review_requires_no_review_or_fingerprint() -> None:
    fields = set(CancelJobCommand.model_fields)

    assert fields == {
        "contract_version",
        "job_id",
        "owner_id",
        "expected_record_version",
        "idempotency_key",
    }


def test_retry_caller_cannot_choose_recovery_action_or_state() -> None:
    fields = set(RetryJobCommand.model_fields)

    assert "recovery_action" not in fields
    assert "resume_state" not in fields
    assert "target_state" not in fields


def test_worker_failure_signal_cannot_assign_retryability_or_state() -> None:
    fields = set(RecordWorkerFailureCommand.model_fields)

    assert fields == {
        "contract_version",
        "job_id",
        "work_request_id",
        "expected_record_version",
        "code",
    }


def test_approval_command_cannot_request_publication_or_supply_authority_records() -> None:
    fields = set(ApproveReviewCommand.model_fields)

    assert "publish" not in fields
    assert "product_id" not in fields
    assert "product_sync_fingerprint" not in fields
    assert "pricing_snapshot_id" not in fields


def test_control_package_has_no_provider_callback_or_commerce_surface() -> None:
    package_directory = Path(control_package.__file__).parent
    banned_import_prefixes = (
        "aiohttp",
        "httpx",
        "mr_lister.production",
        "requests",
        "urllib",
    )
    banned_callable_fragments = (
        "fulfill",
        "order",
        "printify",
        "publish",
        "send_task",
        "task_token",
        "tasktoken",
        "unpublish",
    )
    banned_source_fragments = (
        '"/fulfillment',
        '"/orders',
        '"/publish',
        "mr_lister.production",
        "printify",
        "send_task_failure",
        "send_task_success",
        "task_token",
        "tasktoken",
    )
    allowed_data_identifiers = {"provider_published", "printify_shop_id"}
    violations: list[str] = []

    for path in sorted(package_directory.rglob("*.py")):
        relative_path = path.relative_to(package_directory)
        source = path.read_text(encoding="utf-8")
        lowered_source = source.casefold().replace("printify_shop_id", "")
        for fragment in banned_source_fragments:
            if fragment in lowered_source:
                violations.append(f"{relative_path}: source contains {fragment!r}")

        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            imported_names: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                imported_names = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_names = (node.module,)
            for imported_name in imported_names:
                if imported_name.startswith(banned_import_prefixes):
                    violations.append(f"{relative_path}: imports {imported_name}")

            identifier: str | None = None
            if isinstance(node, ast.Name):
                identifier = node.id
            elif isinstance(node, ast.Attribute):
                identifier = node.attr
            elif isinstance(node, ast.arg):
                identifier = node.arg
            if (
                identifier is not None
                and identifier not in allowed_data_identifiers
                and any(fragment in identifier.casefold() for fragment in banned_callable_fragments)
            ):
                violations.append(f"{relative_path}: unsafe identifier {identifier}")

            callable_name: str | None = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                callable_name = node.name
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    callable_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    callable_name = node.func.attr
            if callable_name is not None and any(
                fragment in callable_name.casefold() for fragment in banned_callable_fragments
            ):
                violations.append(f"{relative_path}: unsafe callable surface {callable_name}")

    assert violations == []
