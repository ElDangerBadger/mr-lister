"""One bounded multimodal intelligence pass inside the real Strands agent loop.

The application, not Gemma, chooses the preparation tool and owns persistence.
This module has no job, owner, provider, approval, or publication capability.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Iterable
from typing import Any

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError
from strands import Agent
from strands.models import BedrockModel
from strands.types.content import Messages, SystemContentBlock
from strands.types.exceptions import (
    ContextWindowOverflowException,
    MaxTokensReachedException,
    ModelThrottledException,
)
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec

from mr_lister.contracts import ArtworkAnalysis, ContractModel, ListingIntelligence
from mr_lister.intelligence.bedrock import (
    _CONFIGURATION_ERROR_CODES,
    _repairable_quality_problems,
    _safe_validation_problems,
    _transparency_note,
)
from mr_lister.intelligence.images import prepare_bedrock_image
from mr_lister.intelligence.listing_draft import ListingCandidateDraft, finalize_listing_draft
from mr_lister.intelligence.prompts import ETSY_SEO_RELEASE_PROMPT_BUNDLE
from mr_lister.intelligence.schema import bedrock_output_schema
from mr_lister.intelligence.settings import BedrockSettings
from mr_lister.latency import latency_span
from mr_lister.workflow.errors import (
    IntelligenceConfigurationError,
    IntelligenceUnavailableError,
    InvalidGeneratedOutputError,
)
from mr_lister.workflow.models import ArtworkInput

MAX_UNIFIED_MODEL_CALLS = 2
MAX_UNIFIED_OUTPUT_TOKENS = 2_500
MAX_UNIFIED_TOTAL_TOKENS = 12_000
UNIFIED_SYSTEM_PROMPT = ETSY_SEO_RELEASE_PROMPT_BUNDLE.system


class UnifiedArtworkListing(ContractModel):
    """Internal response only; the existing application contracts are unchanged."""

    analysis: ArtworkAnalysis
    listing: ListingCandidateDraft


def unified_review_prompt() -> str:
    """Compose the released visual/copy rules without changing the rollback bundle."""

    # The old listing-only preamble denies access to the image. Replace that transport
    # framing, not the reviewed description, audience, hook, or candidate instructions.
    listing_rules = ETSY_SEO_RELEASE_PROMPT_BUNDLE.listing.split("Analyze before writing,", 1)[
        1
    ].split("\nApplication-provided artwork analysis:\n", 1)[0]
    return (
        "Interpret the supplied artwork and return one JSON object with analysis and listing. "
        "Produce the analysis first, then ground the listing in that analysis and the image.\n\n"
        + ETSY_SEO_RELEASE_PROMPT_BUNDLE.artwork
        + "\n\nAnalyze before writing,"
        + listing_rules
    )


class NativeJsonBedrockModel(BedrockModel):
    """Non-streaming native JSON with one explicit permit per actual inference.

    An SDK retry cannot consume another model request implicitly. The preparation
    function alone grants the second permit after a deterministic validation failure.
    Each instance belongs to one preparation request, never a shared warm runtime.
    """

    def __init__(self, **kwargs: Any) -> None:
        self._request_count = 0
        self._authorized_output_tokens: int | None = None
        kwargs["streaming"] = False
        kwargs["use_native_token_count"] = False
        super().__init__(**kwargs)

    @property
    def request_count(self) -> int:
        return self._request_count

    def authorize_inference(self, *, remaining_output_tokens: int) -> None:
        if (
            self._request_count >= MAX_UNIFIED_MODEL_CALLS
            or self._authorized_output_tokens is not None
            or not 1 <= remaining_output_tokens <= MAX_UNIFIED_OUTPUT_TOKENS
        ):
            raise InvalidGeneratedOutputError("Unified intelligence exhausted its bounded budget")
        self._authorized_output_tokens = min(
            int(self.config["max_tokens"]), remaining_output_tokens
        )

    def format_request(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt_content: list[SystemContentBlock] | None = None,
        tool_choice: ToolChoice | None = None,
        dynamic_trailing_blocks: int = 0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del tool_specs, kwargs  # Only the application selects its scoped preparation tool.
        output_tokens = self._authorized_output_tokens
        self._authorized_output_tokens = None
        if output_tokens is None or self._request_count >= MAX_UNIFIED_MODEL_CALLS:
            raise InvalidGeneratedOutputError("An additional model request was not authorized")
        if dynamic_trailing_blocks != 0:
            raise IntelligenceConfigurationError(
                "Unified intelligence does not support dynamic trailing blocks"
            )
        if (
            self.config.get("streaming") is not False
            or tool_choice is not None
            or any(
                "toolUse" in block or "toolResult" in block
                for message in messages
                for block in message["content"]
            )
        ):
            raise IntelligenceConfigurationError("Unified intelligence accepts no model tools")
        self._request_count += 1
        request = super().format_request(messages, None, system_prompt_content, None)
        request["inferenceConfig"]["maxTokens"] = output_tokens
        request["outputConfig"] = {
            "textFormat": {
                "type": "json_schema",
                "structure": {
                    "jsonSchema": {
                        "schema": json.dumps(
                            bedrock_output_schema(UnifiedArtworkListing), separators=(",", ":")
                        ),
                        "name": "mr_lister_unified_artwork_listing_v1",
                        "description": "Artwork analysis and ranked listing candidates",
                    }
                },
            }
        }
        return request

    def convert_non_streaming_to_streaming(
        self, response: dict[str, Any], **kwargs: Any
    ) -> Iterable[StreamEvent]:
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        if response.get("stopReason") == "tool_use" or any("toolUse" in block for block in blocks):
            raise InvalidGeneratedOutputError("Unified intelligence returned an unauthorized tool")
        usage = response.get("usage", {})
        if (
            any(
                isinstance(usage.get(key), bool)
                or not isinstance(usage.get(key), int)
                or usage[key] < 0
                for key in ("inputTokens", "outputTokens", "totalTokens")
            )
            or usage["totalTokens"] != usage["inputTokens"] + usage["outputTokens"]
        ):
            raise InvalidGeneratedOutputError("Unified intelligence returned invalid token usage")
        yield from super().convert_non_streaming_to_streaming(response, **kwargs)

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[StreamEvent, None]:
        # These are actual Bedrock calls, separate from the SDK's enclosing cycle trace.
        with latency_span(
            "unified_intelligence_model_invocation",
            component="bedrock_intelligence",
            attempt=self._request_count + 1,
            model_id=str(self.config["model_id"]),
        ):
            try:
                async for event in super().stream(*args, **kwargs):
                    yield event
            except (ContextWindowOverflowException, ModelThrottledException, BotoCoreError):
                # Do not trigger Strands context-recovery or provider-retry loops.
                raise IntelligenceUnavailableError("Unified intelligence is unavailable") from None
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") in _CONFIGURATION_ERROR_CODES:
                    raise IntelligenceConfigurationError(
                        "Unified intelligence configuration is unavailable"
                    ) from None
                raise IntelligenceUnavailableError("Unified intelligence is unavailable") from None


def build_unified_model(session: Any, settings: BedrockSettings) -> NativeJsonBedrockModel:
    """Build a fresh per-request model using the already-authorized Gemma capability."""

    if settings.model_id != "google.gemma-3-27b-it" or settings.output_mode != "native_json_schema":
        raise IntelligenceConfigurationError("Unified intelligence requires the pinned Gemma model")
    return NativeJsonBedrockModel(
        boto_session=session,
        boto_client_config=Config(
            connect_timeout=10,
            read_timeout=300,
            retries={"mode": "standard", "max_attempts": 0},
        ),
        model_id=settings.model_id,
        max_tokens=min(settings.max_tokens, MAX_UNIFIED_OUTPUT_TOKENS),
        temperature=settings.temperature,
        streaming=False,
        use_native_token_count=False,
    )


def prepare_unified_review(
    agent: Agent, artwork: ArtworkInput, content: bytes
) -> tuple[ArtworkAnalysis, ListingIntelligence]:
    """Run one real Agent inference, with one shared schema/tag repair at most.

    Callers retain immutable source/owner authority checks and construct the Agent
    with retry_strategy=None, NullConversationManager, and no structured_output_model.
    """

    del artwork  # Pinned source integrity is checked by the caller before this boundary.
    model = agent.model
    if not isinstance(model, NativeJsonBedrockModel) or model.request_count:
        raise IntelligenceConfigurationError("Unified intelligence needs a fresh request model")
    image = prepare_bedrock_image(content, max_side=1600, max_bytes=750_000)
    prompt: Any = [
        {"image": {"format": "png", "source": {"bytes": image.content}}},
        {"text": unified_review_prompt() + _transparency_note(image)},
    ]
    tag_repair_source: UnifiedArtworkListing | None = None
    for attempt in range(MAX_UNIFIED_MODEL_CALLS):
        usage = agent.event_loop_metrics.get_summary()["accumulated_usage"]
        remaining_output = MAX_UNIFIED_OUTPUT_TOKENS - usage["outputTokens"]
        remaining_total = MAX_UNIFIED_TOTAL_TOKENS - usage["totalTokens"]
        if remaining_total <= 0:
            raise InvalidGeneratedOutputError("Unified intelligence exhausted its bounded budget")
        model.authorize_inference(remaining_output_tokens=remaining_output)
        problems = ""
        try:
            result = agent(
                prompt,
                limits={
                    "turns": 1,
                    "output_tokens": remaining_output,
                    "total_tokens": remaining_total,
                },
            )
        except MaxTokensReachedException:
            problems = "- response: The JSON object was incomplete; return the complete contract."
        else:
            try:
                if result.stop_reason != "end_turn":
                    raise ValueError("The response did not complete its JSON object")
                accepted = UnifiedArtworkListing.model_validate_json(str(result))
            except (ValidationError, ValueError) as error:
                problems = _safe_validation_problems(error)
            else:
                if tag_repair_source is not None:
                    accepted = accepted.model_copy(
                        update={
                            "analysis": tag_repair_source.analysis,
                            "listing": accepted.listing.model_copy(
                                update=tag_repair_source.listing.model_dump(
                                    exclude={"tag_candidates"}
                                )
                            ),
                        }
                    )
                problems = _repairable_quality_problems(accepted.listing)
                if not problems:
                    usage = agent.event_loop_metrics.get_summary()["accumulated_usage"]
                    if (
                        usage["outputTokens"] > MAX_UNIFIED_OUTPUT_TOKENS
                        or usage["totalTokens"] > MAX_UNIFIED_TOTAL_TOKENS
                    ):
                        raise InvalidGeneratedOutputError(
                            "Unified intelligence exceeded its budget"
                        )
                    return accepted.analysis, finalize_listing_draft(accepted.listing)
                tag_repair_source = accepted
        if attempt + 1 == MAX_UNIFIED_MODEL_CALLS:
            break
        prompt = ETSY_SEO_RELEASE_PROMPT_BUNDLE.repair.format(problems=problems)
    raise InvalidGeneratedOutputError("Unified intelligence failed after one bounded repair")
