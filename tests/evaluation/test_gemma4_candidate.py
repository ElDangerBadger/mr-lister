"""Offline guards for the opt-in Gemma 4 comparison; no AWS or commerce calls."""

from pathlib import Path

import pytest
import test_live_bedrock as evaluation
from pydantic import ValidationError

from mr_lister.intelligence.prompts import (
    BASELINE_PROMPT_BUNDLE,
    ETSY_SEO_RELEASE_PROMPT_BUNDLE,
)
from mr_lister.intelligence.settings import BedrockSettings

ROOT = Path(__file__).resolve().parents[2]


def candidate(**changes: object) -> BedrockSettings:
    values = {
        "transport": "mantle",
        "region": "us-west-2",
        "model_id": "google.gemma-4-31b",
        "output_mode": "native_json_schema",
    }
    return BedrockSettings.model_validate(values | changes)


def test_existing_configuration_still_defaults_to_converse() -> None:
    settings = BedrockSettings.model_validate_json(
        (ROOT / "config/bedrock/google_gemma_3_27b_it.json").read_bytes()
    )
    assert settings.transport == "converse"
    assert settings.model_id == "google.gemma-3-27b-it"


def test_candidate_requires_explicit_transport_and_released_two_call_prompt() -> None:
    settings = BedrockSettings.model_validate_json(
        (ROOT / "config/bedrock/google_gemma_4_31b_candidate.json").read_bytes()
    )
    assert settings.transport == "mantle"
    evaluation._validate_evaluation_configuration(
        settings, ETSY_SEO_RELEASE_PROMPT_BUNDLE, "two_call"
    )
    with pytest.raises(ValueError, match="exact released SEO reference"):
        evaluation._validate_evaluation_configuration(settings, BASELINE_PROMPT_BUNDLE, "two_call")
    with pytest.raises(ValueError, match="existing two-call workflow"):
        evaluation._validate_evaluation_configuration(
            settings, ETSY_SEO_RELEASE_PROMPT_BUNDLE, "one_call"
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"transport": "converse"},
        {"model_id": "google.gemma-3-27b-it"},
        {"region": "ap-southeast-1"},
        {"output_mode": "prompted_json"},
    ],
)
def test_candidate_rejects_unreviewed_transport_model_region_and_output_mode(changes) -> None:
    with pytest.raises(ValidationError):
        candidate(**changes)


def test_optional_latency_candidate_uses_the_same_explicit_transport() -> None:
    assert candidate(model_id="google.gemma-4-26b-a4b").transport == "mantle"


def test_legacy_evaluation_prompt_selection_is_unchanged() -> None:
    evaluation._validate_evaluation_configuration(
        BedrockSettings(), BASELINE_PROMPT_BUNDLE, "two_call"
    )
