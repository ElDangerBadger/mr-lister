"""Pinned Gemma 4 preparation boundary; user approval remains application-owned.

The v6 tag-coverage writer runs with frozen v3 inspection and verification, atomically
within the standard image budget. This adapter projects an accepted pair into existing
persisted contracts, without caching results,
creating provider products, granting approval, or publishing anything.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any, Protocol

from mr_lister.contracts import ArtworkAnalysis, ListingIntelligence
from mr_lister.intelligence.diagnostics import DiagnosticSink
from mr_lister.intelligence.harness_candidate import (
    HarnessResult,
    VerifiedProductContext,
    build_harness_candidate_adapter,
    candidate_prompt_bundles,
)
from mr_lister.intelligence.settings import BedrockSettings
from mr_lister.workflow.errors import IntelligenceConfigurationError, InvalidGeneratedOutputError
from mr_lister.workflow.models import ArtworkInput

PRODUCTION_HARNESS_REVISION = "v6"
PRODUCTION_HARNESS_PROMPT_FINGERPRINT = (
    "5e0e88750e43f8acbda97ff23cf9876dad34c47c1a37bc3d50cfcba062139b56"
)
UNCALIBRATED_CONFIDENCE_NOTE = "Interpretation confidence is not calibrated for this model."


class AtomicHarness(Protocol):
    def prepare(self, artwork: ArtworkInput, content: bytes) -> HarnessResult: ...


def _verify_prompt_pin() -> None:
    bundle = candidate_prompt_bundles(revision=PRODUCTION_HARNESS_REVISION)["full"]
    if bundle.fingerprint != PRODUCTION_HARNESS_PROMPT_FINGERPRINT:
        raise IntelligenceConfigurationError("Production intelligence prompt has drifted")


class HarnessProductionAdapter:
    """One invocation-local evidence/listing pair; no inter-request mutable state."""

    def __init__(self, harness: AtomicHarness) -> None:
        _verify_prompt_pin()
        self._harness = harness

    def prepare_listing(
        self, artwork: ArtworkInput, content: bytes
    ) -> tuple[ArtworkAnalysis, ListingIntelligence]:
        if (
            not isinstance(content, bytes)
            or len(content) != artwork.size_bytes
            or sha256(content).hexdigest() != artwork.content_sha256
        ):
            raise InvalidGeneratedOutputError("Prepared artwork source binding is invalid")
        result = HarnessResult.model_validate(self._harness.prepare(artwork, content))
        bundle = candidate_prompt_bundles(revision=PRODUCTION_HARNESS_REVISION)["full"]
        if (
            result.artwork_sha256 != artwork.content_sha256
            or result.prompt_version != bundle.version
            or result.prompt_fingerprint != PRODUCTION_HARNESS_PROMPT_FINGERPRINT
            or result.state != "accepted_for_evaluation"
            or result.listing is None
            or result.issues
            or result.subject_verification != "agrees"
            or result.brief.subject_status != "resolved"
            or result.brief.supported_subject is None
            or result.brief.unresolved_alternatives
        ):
            # A rejected/uncertain draft must not silently become an editable accepted pair.
            raise InvalidGeneratedOutputError("Artwork interpretation requires further review")
        analysis = ArtworkAnalysis(
            subject=result.brief.supported_subject,
            visual_elements=tuple(
                dict.fromkeys(
                    (
                        *result.brief.observable_features,
                        *result.brief.listing_details,
                    )
                )
            ),
            visible_text=result.brief.visible_text,
            safety_flags=(UNCALIBRATED_CONFIDENCE_NOTE,),
            # Required legacy storage field, not a probability or model confidence score.
            # The review projection recognizes this note and exposes confidence=null.
            confidence=0.0,
        )
        return analysis, ListingIntelligence.model_validate(result.listing)


def build_harness_production_adapter(
    settings: BedrockSettings,
    *,
    session: Any | None = None,
    diagnostics: DiagnosticSink | None = None,
) -> HarnessProductionAdapter:
    _verify_prompt_pin()
    if settings != BedrockSettings(
        transport="mantle",
        region="us-west-2",
        model_id="google.gemma-4-31b",
        output_mode="native_json_schema",
        max_tokens=2048,
        temperature=0.0,
        max_repair_attempts=2,
    ):
        raise IntelligenceConfigurationError("Production intelligence settings have drifted")
    return HarnessProductionAdapter(
        build_harness_candidate_adapter(
            settings,
            session=session,
            diagnostics=diagnostics,
            product_context=VerifiedProductContext(product_type="T-shirt"),
            revision=PRODUCTION_HARNESS_REVISION,
        )
    )
