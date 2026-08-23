from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from mr_lister.publication.contract import (
    PHASE7_PUBLICATION_CONTRACT_VERSION,
    PublicationActivationPhaseName,
    PublicationPermitState,
    PublicationState,
    phase7_publication_contract,
    phase7_publication_contract_bytes,
    phase7_publication_contract_digest,
    validate_phase7_publication_contract_json,
)

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "contracts" / "publication" / "phase7.0.1.json"
SUPERSEDED_ARTIFACT = ROOT / "contracts" / "publication" / "phase7.0.json"


def test_checked_contract_is_exact_deterministic_frozen_authority() -> None:
    expected = phase7_publication_contract_bytes()

    assert PHASE7_PUBLICATION_CONTRACT_VERSION == "7.0.1"
    assert not SUPERSEDED_ARTIFACT.exists()
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
    assert mutating[0].maximum_calls_per_root_attempt == 1
    assert mutating[0].bounded_by_fixed_deadline is True
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


def test_approved_is_a_bridge_into_a_separate_publication_aggregate() -> None:
    contract = phase7_publication_contract()

    assert contract.aggregate_boundary == "separate_publication_aggregate"
    assert contract.phase6_control_state_during_publication == "approved"
    assert contract.bridge_source_state is PublicationState.APPROVED
    assert PublicationState.APPROVED not in contract.persisted_aggregate_states
    assert set(contract.persisted_aggregate_states) == set(contract.states) - {
        PublicationState.APPROVED
    }
    assert contract.phase71_authority_prerequisites == (
        "approval_decision_id",
        "printify_shop_id",
    )
    assert set(contract.phase71_authority_prerequisites).issubset(contract.snapshot_fields)


def test_permit_state_graph_is_complete_and_one_way() -> None:
    contract = phase7_publication_contract()

    assert {
        (transition.source, transition.target) for transition in contract.permit_transitions
    } == {
        (PublicationPermitState.AVAILABLE, PublicationPermitState.CONSUMED),
        (PublicationPermitState.AVAILABLE, PublicationPermitState.RETIRED),
    }
    consumed = next(
        transition
        for transition in contract.permit_transitions
        if transition.target is PublicationPermitState.CONSUMED
    )
    retired = next(
        transition
        for transition in contract.permit_transitions
        if transition.target is PublicationPermitState.RETIRED
    )
    assert consumed.maximum_publish_posts_authorized == 1
    assert retired.maximum_publish_posts_authorized == 0
    assert not any(
        transition.source in {PublicationPermitState.CONSUMED, PublicationPermitState.RETIRED}
        for transition in contract.permit_transitions
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


def test_get_observations_cannot_claim_provider_failure() -> None:
    contract = phase7_publication_contract()

    assert contract.read_observation_outcomes == (
        "positive_publication_proof",
        "publication_not_yet_proven",
        "conflicting_or_incomplete_evidence",
    )
    for source in {
        PublicationState.PUBLICATION_VERIFYING,
        PublicationState.PUBLICATION_RECONCILING,
    }:
        targets = {
            transition.target for transition in contract.transitions if transition.source is source
        }
        assert targets == {
            PublicationState.PUBLISHED,
            PublicationState.PUBLICATION_OUTCOME_UNKNOWN,
        }
    assert any(
        transition.source is PublicationState.PUBLICATION_REQUESTED
        and transition.target is PublicationState.PUBLICATION_FAILED
        and transition.authority == "consumed_permit_and_definitive_synchronous_rejection"
        and transition.provider_mutation_count == 1
        for transition in contract.transitions
    )


def test_call_budgets_are_per_root_attempt_and_deadline_cannot_move() -> None:
    contract = phase7_publication_contract()

    assert contract.maximum_root_attempts_per_job == 1
    assert contract.provider_call_budget_scope == "root_publication_attempt"
    assert contract.verification_deadline_anchor == "root_attempt_requested_at"
    assert contract.verification_deadline_seconds == 1800
    assert contract.verification_deadline_extension_allowed is False
    assert all(call.bounded_by_fixed_deadline for call in contract.provider_calls)
    assert {
        (call.method, call.route): call.maximum_calls_per_root_attempt
        for call in contract.provider_calls
    } == {
        ("GET", "/v1/shops.json"): 3,
        ("GET", "/v1/shops/{shop_id}/products/{product_id}.json"): 100,
        ("POST", "/v1/shops/{shop_id}/products/{product_id}/publish.json"): 1,
    }


def test_retention_is_derived_only_from_publication_terminal_time() -> None:
    contract = phase7_publication_contract()

    assert contract.retention_anchor == "publication_terminal_at"
    assert contract.source_release_after_terminal_days == 30
    assert contract.operational_ttl_after_terminal_days == 90
    assert contract.terminal_settlement_fields == (
        "terminal_at",
        "source_release_eligible_at",
        "operational_expires_at",
    )
    assert (
        contract.duplicate_prevention_retention_invariant
        == "job_aggregate_tombstone_until_operational_expiry"
    )
    assert not set(contract.terminal_settlement_fields).intersection(contract.snapshot_fields)


def test_activation_is_phased_and_currently_capability_free() -> None:
    contract = phase7_publication_contract()

    assert contract.publication_enabled is False
    assert (
        contract.current_activation_phase is PublicationActivationPhaseName.OFFLINE_IMPLEMENTATION
    )
    assert tuple(phase.name for phase in contract.activation_phases) == tuple(
        PublicationActivationPhaseName
    )
    assert (
        tuple(gate for phase in contract.activation_phases for gate in phase.required_gates)
        == contract.activation_gates
    )
    offline, read_only, canary, general = contract.activation_phases
    assert not offline.bounded_provider_mutation_allowed
    assert not offline.seller_publication_route_allowed
    assert not offline.requires_new_enabled_contract
    assert not read_only.bounded_provider_mutation_allowed
    assert not read_only.seller_publication_route_allowed
    assert not read_only.requires_new_enabled_contract
    assert canary.bounded_provider_mutation_allowed
    assert not canary.seller_publication_route_allowed
    assert not canary.requires_new_enabled_contract
    assert general.bounded_provider_mutation_allowed
    assert general.seller_publication_route_allowed
    assert general.requires_new_enabled_contract


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


def test_publication_core_has_no_capability_imports() -> None:
    """Keep the pure 7.0.1 authority reusable without any runtime capability."""

    forbidden = {
        "boto3",
        "botocore",
        "httpx",
        "requests",
        "urllib",
        "mr_lister.agent",
        "mr_lister.production",
        "mr_lister.cloud",
        "mr_lister.control",
        "mr_lister.intelligence",
        "mr_lister.workflow",
    }
    pure_files = {
        "__init__.py",
        "commands.py",
        "contract.py",
        "errors.py",
        "fingerprints.py",
        "models.py",
    }
    publication_root = ROOT / "src" / "mr_lister" / "publication"
    assert pure_files.issubset({path.name for path in publication_root.glob("*.py")})

    for filename in pure_files:
        path = publication_root / filename
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


def test_publication_persistence_and_service_cannot_acquire_runtime_capability() -> None:
    """Allow control evidence and botocore errors, but no client factory or provider surface."""

    publication_root = ROOT / "src" / "mr_lister" / "publication"
    adapter_paths = [
        path
        for path in publication_root.glob("*.py")
        if path.name
        not in {
            "__init__.py",
            "commands.py",
            "contract.py",
            "errors.py",
            "fingerprints.py",
            "models.py",
            # Phase 7.2's sealed three-route provider boundary intentionally owns its
            # redirect-safe urllib transport. The disabled-runtime boundary and focused
            # provider tests prove that it remains unexported, uncomposed, and route closed.
            "provider_boundary.py",
        }
    ]
    assert {path.name for path in adapter_paths}.issuperset({"store.py", "dynamodb.py"})

    forbidden_imports = {
        "boto3",
        "httpx",
        "requests",
        "urllib",
        "mr_lister.agent",
        "mr_lister.cloud",
        "mr_lister.intelligence",
        "mr_lister.production",
        "mr_lister.workflow",
    }
    forbidden_calls = {
        "client",
        "create_unpublished_product",
        "describe_execution",
        "describe_secret",
        "dispatch",
        "get_secret_value",
        "publish",
        "resource",
        "start_execution",
    }
    forbidden_definitions = {
        "consume_permit",
        "dispatch",
        "dispatch_due",
        "dispatch_one",
        "publish",
        "retire_permit",
    }

    for path in adapter_paths:
        tree = ast.parse(path.read_text())
        imported: set[str] = set()
        called: set[str] = set()
        defined: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called.add(node.func.attr)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)

        assert not any(
            module == denied or module.startswith(f"{denied}.")
            for module in imported
            for denied in forbidden_imports
        ), path.name
        assert called.isdisjoint(forbidden_calls), path.name
        assert defined.isdisjoint(forbidden_definitions), path.name
