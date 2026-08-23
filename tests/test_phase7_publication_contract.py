from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from mr_lister.publication.contract import (
    PublicationPermitState,
    PublicationState,
    phase7_publication_contract,
    phase7_publication_contract_bytes,
    phase7_publication_contract_digest,
    validate_phase7_publication_contract_json,
)

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "contracts" / "publication" / "phase7.0.json"


def test_checked_contract_is_exact_deterministic_frozen_authority() -> None:
    expected = phase7_publication_contract_bytes()

    assert ARTIFACT.read_bytes() == expected
    assert validate_phase7_publication_contract_json(expected) == phase7_publication_contract()
    assert len(phase7_publication_contract_digest()) == 64


def test_contract_is_disabled_and_one_shot() -> None:
    contract = phase7_publication_contract()

    assert contract.publication_enabled is False
    assert contract.maximum_publish_posts_per_job == 1
    assert contract.verification_deadline_seconds == 1800
    assert contract.permit_states == tuple(PublicationPermitState)
    mutating = [call for call in contract.provider_calls if call.mutating]
    assert len(mutating) == 1
    assert mutating[0].method == "POST"
    assert mutating[0].maximum_calls == 1
    assert mutating[0].route.endswith("/publish.json")
    assert contract.publication_body_fields == (
        "title",
        "description",
        "images",
        "variants",
        "tags",
        "keyFeatures",
        "shipping_template",
    )


def test_no_post_consumption_transition_can_issue_another_mutation() -> None:
    contract = phase7_publication_contract()

    for transition in contract.transitions:
        if transition.source in {
            PublicationState.PUBLICATION_VERIFYING,
            PublicationState.PUBLICATION_RECONCILING,
        }:
            assert transition.provider_mutation_count == 0
    assert not any(
        transition.source in set(contract.terminal_states) for transition in contract.transitions
    )


def test_provider_surface_is_exact_and_excludes_custom_status_and_commerce() -> None:
    contract = phase7_publication_contract()

    assert {(call.method, call.route) for call in contract.provider_calls} == {
        ("GET", "/v1/shops.json"),
        ("GET", "/v1/shops/{shop_id}/products/{product_id}.json"),
        ("POST", "/v1/shops/{shop_id}/products/{product_id}/publish.json"),
    }
    assert {
        "publishing_succeeded",
        "publishing_failed",
        "unpublish",
        "order",
        "fulfillment",
    }.issubset(contract.forbidden_provider_operations)


def test_contract_requires_exact_authority_and_positive_verification() -> None:
    contract = phase7_publication_contract()

    assert {
        "approval_fingerprint",
        "review_fingerprint",
        "product_sync_fingerprint",
        "pricing_evidence_fingerprint",
        "pricing_fresh_until",
        "profile_fingerprint",
        "release_manifest_fingerprint",
        "publication_body_fingerprint",
    }.issubset(contract.snapshot_fields)
    assert {
        "unlocked",
        "visible",
        "canonical_content_match",
        "single_etsy_external_id",
        "safe_etsy_link",
    }.issubset(contract.positive_verification_fields)


def test_changed_serialized_contract_fails_semantic_validation() -> None:
    payload = json.loads(phase7_publication_contract_bytes())
    payload["publication_enabled"] = True

    with pytest.raises(ValidationError):
        validate_phase7_publication_contract_json(json.dumps(payload))


def test_publication_package_has_no_capability_imports() -> None:
    forbidden = {
        "boto3",
        "botocore",
        "requests",
        "urllib",
        "mr_lister.production",
        "mr_lister.cloud",
        "mr_lister.control.store",
    }
    for path in (ROOT / "src" / "mr_lister" / "publication").glob("*.py"):
        tree = ast.parse(path.read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not any(
            module == denied or module.startswith(f"{denied}.")
            for module in imported
            for denied in forbidden
        )
