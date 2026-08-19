"""Amazon Bedrock Converse adapter for Phase 2 listing intelligence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol, TypeVar

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    ParamValidationError,
    PartialCredentialsError,
)
from pydantic import BaseModel, ValidationError

from mr_lister.contracts import ArtworkAnalysis, ListingIntelligence
from mr_lister.intelligence.diagnostics import (
    BedrockDiagnosticRecord,
    DiagnosticSink,
    NoOpDiagnosticSink,
)
from mr_lister.intelligence.images import BedrockImage, prepare_bedrock_image
from mr_lister.intelligence.listing_draft import (
    ListingCandidateDraft,
    finalize_listing_draft,
    select_etsy_tags,
)
from mr_lister.intelligence.prompts import (
    ARTWORK_PROMPT,
    LISTING_PROMPT,
    PROMPT_VERSION,
    REPAIR_PROMPT,
    SYSTEM_PROMPT,
)
from mr_lister.intelligence.schema import bedrock_output_schema
from mr_lister.intelligence.settings import BedrockSettings
from mr_lister.workflow.errors import (
    IntelligenceConfigurationError,
    IntelligenceUnavailableError,
    InvalidGeneratedOutputError,
)
from mr_lister.workflow.models import ArtworkInput
from mr_lister.workflow.validation import find_repeated_tag_keyword_locations

ContractT = TypeVar("ContractT", bound=BaseModel)

_RETRYABLE_ERROR_CODES = {
    "InternalServerException",
    "ModelErrorException",
    "ModelNotReadyException",
    "ModelTimeoutException",
    "ServiceQuotaExceededException",
    "ServiceUnavailableException",
    "ThrottlingException",
}
_CONFIGURATION_ERROR_CODES = {
    "AccessDeniedException",
    "ExpiredToken",
    "ExpiredTokenException",
    "IncompleteSignature",
    "IncompleteSignatureException",
    "InvalidClientTokenId",
    "InvalidSignatureException",
    "MissingAuthenticationToken",
    "MissingAuthenticationTokenException",
    "ResourceNotFoundException",
    "SignatureDoesNotMatch",
    "UnrecognizedClientException",
    "ValidationException",
}


class ConverseClient(Protocol):
    def converse(self, **kwargs: Any) -> dict[str, Any]: ...


def build_bedrock_adapter(
    settings: BedrockSettings,
    *,
    session: boto3.Session | None = None,
    diagnostics: DiagnosticSink | None = None,
) -> BedrockListingIntelligenceAdapter:
    """Build an adapter using the AWS default credential chain."""

    active_session = session or boto3.Session(region_name=settings.region)
    client = active_session.client(
        "bedrock-runtime",
        region_name=settings.region,
        config=Config(
            connect_timeout=10,
            read_timeout=300,
            retries={"mode": "standard", "max_attempts": 4},
        ),
    )
    return BedrockListingIntelligenceAdapter(
        client=client,
        settings=settings,
        diagnostics=diagnostics,
    )


class BedrockListingIntelligenceAdapter:
    """Multimodal Bedrock adapter that returns only application-owned contracts."""

    def __init__(
        self,
        *,
        client: ConverseClient,
        settings: BedrockSettings,
        diagnostics: DiagnosticSink | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._diagnostics = diagnostics or NoOpDiagnosticSink()

    def inspect_artwork(self, artwork: ArtworkInput, content: bytes) -> ArtworkAnalysis:
        image = prepare_bedrock_image(content)
        prompt = ARTWORK_PROMPT + _transparency_note(image)
        return self._invoke_contract(
            operation="inspect_artwork",
            contract=ArtworkAnalysis,
            schema_name="mr_lister_artwork_analysis_v1",
            prompt=prompt,
            image=image,
            artwork_sha256=artwork.content_sha256,
        )

    def draft_listing(
        self,
        artwork: ArtworkInput,
        content: bytes,
        analysis: ArtworkAnalysis,
    ) -> ListingIntelligence:
        del content
        prompt = LISTING_PROMPT.format(analysis_json=analysis.model_dump_json())
        draft = self._invoke_contract(
            operation="draft_listing",
            contract=ListingCandidateDraft,
            schema_name="mr_lister_listing_candidate_draft_v1",
            prompt=prompt,
            image=None,
            artwork_sha256=artwork.content_sha256,
        )
        return finalize_listing_draft(draft)

    def _invoke_contract(
        self,
        *,
        operation: str,
        contract: type[ContractT],
        schema_name: str,
        prompt: str,
        image: BedrockImage | None,
        artwork_sha256: str,
    ) -> ContractT:
        schema = bedrock_output_schema(contract)
        if self._settings.output_mode == "prompted_json":
            prompt = _prompt_with_schema(prompt, schema)
        content_blocks: list[dict[str, Any]] = []
        if image is not None:
            content_blocks.append({"image": {"format": "png", "source": {"bytes": image.content}}})
        content_blocks.append({"text": prompt})
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": content_blocks,
            }
        ]

        for attempt in range(self._settings.max_repair_attempts + 1):
            response = self._converse(
                operation=operation,
                attempt=attempt + 1,
                schema_name=schema_name,
                schema=schema,
                messages=messages,
                image=image,
                artwork_sha256=artwork_sha256,
            )
            raw_output = ""
            contract_payload = ""
            try:
                raw_output = _response_text(response)
                stop_reason = str(response.get("stopReason", "missing"))
                if stop_reason != "end_turn":
                    raise ValueError(f"Model stopped with {stop_reason}")
                contract_payload = (
                    _unwrap_single_json_fence(raw_output)
                    if self._settings.output_mode == "prompted_json"
                    else raw_output
                )
                accepted = contract.model_validate_json(contract_payload)
            except (ValidationError, ValueError) as error:
                problems = _safe_validation_problems(error)
                self._emit_response_diagnostic(
                    operation=operation,
                    attempt=attempt + 1,
                    response=response,
                    status="invalid_output",
                    raw_output=raw_output,
                    image=image,
                    artwork_sha256=artwork_sha256,
                    error_type=type(error).__name__,
                    error_message="Model output failed application validation",
                    validation_problems=problems,
                )
                if attempt >= self._settings.max_repair_attempts:
                    break
                repair_messages = [*messages]
                if raw_output:
                    repair_messages.append({"role": "assistant", "content": [{"text": raw_output}]})
                repair_messages.append(
                    {
                        "role": "user",
                        "content": [{"text": REPAIR_PROMPT.format(problems=problems)}],
                    }
                )
                messages = repair_messages
                continue

            quality_problems = _repairable_quality_problems(accepted)
            if quality_problems:
                self._emit_response_diagnostic(
                    operation=operation,
                    attempt=attempt + 1,
                    response=response,
                    status="invalid_output",
                    raw_output=raw_output,
                    image=image,
                    artwork_sha256=artwork_sha256,
                    error_type="QualityValidationError",
                    error_message="Model output missed a repairable listing quality target",
                    validation_problems=quality_problems,
                )
                if attempt >= self._settings.max_repair_attempts:
                    break
                messages = [
                    *messages,
                    {"role": "assistant", "content": [{"text": raw_output}]},
                    {
                        "role": "user",
                        "content": [{"text": REPAIR_PROMPT.format(problems=quality_problems)}],
                    },
                ]
                continue

            self._emit_response_diagnostic(
                operation=operation,
                attempt=attempt + 1,
                response=response,
                status="accepted",
                raw_output=raw_output,
                image=image,
                artwork_sha256=artwork_sha256,
                validation_problems=quality_problems or None,
            )
            return accepted

        raise InvalidGeneratedOutputError(
            "Bedrock output remained outside the application contract after bounded repair"
        )

    def _converse(
        self,
        *,
        operation: str,
        attempt: int,
        schema_name: str,
        schema: Mapping[str, Any],
        messages: list[dict[str, Any]],
        image: BedrockImage | None,
        artwork_sha256: str,
    ) -> dict[str, Any]:
        try:
            request: dict[str, Any] = {
                "modelId": self._settings.model_id,
                "system": [{"text": SYSTEM_PROMPT}],
                "messages": messages,
                "inferenceConfig": {
                    "maxTokens": self._settings.max_tokens,
                    "temperature": self._settings.temperature,
                },
            }
            if self._settings.output_mode == "native_json_schema":
                request["outputConfig"] = {
                    "textFormat": {
                        "type": "json_schema",
                        "structure": {
                            "jsonSchema": {
                                "schema": json.dumps(schema, separators=(",", ":")),
                                "name": schema_name,
                                "description": (
                                    "A bounded Mr Lister application intelligence contract"
                                ),
                            }
                        },
                    }
                }
            return self._client.converse(**request)
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", "ClientError"))
            request_id = error.response.get("ResponseMetadata", {}).get("RequestId")
            self._diagnostics.emit(
                BedrockDiagnosticRecord(
                    operation=operation,
                    model_id=self._settings.model_id,
                    status="provider_error",
                    prompt_version=PROMPT_VERSION,
                    attempt=attempt,
                    request_id=request_id,
                    artwork_sha256=artwork_sha256,
                    metadata={
                        **_image_metadata(image),
                    },
                    error_type=type(error).__name__,
                    error_code=code,
                    error_message="Bedrock rejected or could not complete the invocation",
                )
            )
            if code in _CONFIGURATION_ERROR_CODES:
                raise IntelligenceConfigurationError(
                    "Bedrock model access or invocation configuration is not ready"
                ) from error
            if code in _RETRYABLE_ERROR_CODES:
                raise IntelligenceUnavailableError(
                    "Bedrock is temporarily unavailable; retry the job safely"
                ) from error
            raise IntelligenceConfigurationError(
                "Bedrock returned an unclassified non-retryable invocation error"
            ) from error
        except (NoCredentialsError, PartialCredentialsError, ParamValidationError) as error:
            self._emit_sdk_error_diagnostic(
                operation=operation,
                attempt=attempt,
                artwork_sha256=artwork_sha256,
                image=image,
                error=error,
            )
            raise IntelligenceConfigurationError(
                "AWS credentials or Bedrock request configuration is not ready"
            ) from error
        except BotoCoreError as error:
            self._emit_sdk_error_diagnostic(
                operation=operation,
                attempt=attempt,
                artwork_sha256=artwork_sha256,
                image=image,
                error=error,
            )
            raise IntelligenceUnavailableError(
                "AWS transport is temporarily unavailable; retry the job safely"
            ) from error

    def _emit_sdk_error_diagnostic(
        self,
        *,
        operation: str,
        attempt: int,
        artwork_sha256: str,
        image: BedrockImage | None,
        error: BotoCoreError,
    ) -> None:
        self._diagnostics.emit(
            BedrockDiagnosticRecord(
                operation=operation,
                model_id=self._settings.model_id,
                status="provider_error",
                prompt_version=PROMPT_VERSION,
                attempt=attempt,
                artwork_sha256=artwork_sha256,
                metadata={
                    **_image_metadata(image),
                },
                error_type=type(error).__name__,
                error_message="AWS SDK failed before a result was accepted",
            )
        )

    def _emit_response_diagnostic(
        self,
        *,
        operation: str,
        attempt: int,
        response: Mapping[str, Any],
        status: str,
        raw_output: str,
        image: BedrockImage | None,
        artwork_sha256: str,
        error_type: str | None = None,
        error_message: str | None = None,
        validation_problems: str | None = None,
    ) -> None:
        response_metadata = response.get("ResponseMetadata", {})
        metrics = response.get("metrics", {})
        self._diagnostics.emit(
            BedrockDiagnosticRecord(
                operation=operation,
                model_id=self._settings.model_id,
                status=status,
                prompt_version=PROMPT_VERSION,
                attempt=attempt,
                latency_ms=metrics.get("latencyMs"),
                request_id=response_metadata.get("RequestId"),
                artwork_sha256=artwork_sha256,
                usage=response.get("usage", {}),
                metadata={
                    "stop_reason": response.get("stopReason"),
                    "validation_problems": validation_problems,
                    **_image_metadata(image),
                },
                error_type=error_type,
                error_message=error_message,
                raw_model_output=raw_output,
            )
        )


def _response_text(response: Mapping[str, Any]) -> str:
    try:
        blocks = response["output"]["message"]["content"]
    except (KeyError, TypeError) as error:
        raise ValueError("Bedrock response did not contain a model message") from error
    return "".join(str(block["text"]) for block in blocks if "text" in block)


def _safe_validation_problems(error: Exception) -> str:
    if isinstance(error, ValidationError):
        problems = []
        for item in error.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        ):
            location = ".".join(str(part) for part in item["loc"]) or "response"
            problems.append(f"- {location}: {item['msg']} ({item['type']})")
        return "\n".join(problems)
    return f"- response: {error}"


def _repairable_quality_problems(contract: BaseModel) -> str:
    if not isinstance(contract, ListingCandidateDraft):
        return ""
    try:
        select_etsy_tags(contract.tag_candidates)
    except ValueError:
        collisions = find_repeated_tag_keyword_locations(contract.tag_candidates)
        repeated = ", ".join(collisions) or "insufficient alternative vocabulary"
        return (
            "- tag_candidates: The ranked pool cannot produce 13 tags without meaningful "
            f"keyword reuse. Add relevant alternative phrases using distinct vocabulary; the "
            f"most constraining repeated roots include: {repeated}. Keep 18 to 30 unique "
            "candidates and do not remove listing fields. (candidate_selection)"
        )
    else:
        return ""


def _unwrap_single_json_fence(raw_output: str) -> str:
    """Remove only a solitary JSON fence emitted by a prompted-output model."""

    stripped = raw_output.strip()
    if not stripped.startswith("```"):
        return raw_output
    lines = stripped.splitlines()
    if len(lines) < 3 or lines[0].strip().casefold() not in {"```", "```json"}:
        raise ValueError("Prompted JSON used an unsupported code-fence opener")
    if lines[-1].strip() != "```":
        raise ValueError("Prompted JSON code fence was not closed")
    payload = "\n".join(lines[1:-1]).strip()
    if not payload or "```" in payload:
        raise ValueError("Prompted JSON contained nested or empty code fences")
    return payload


def _image_metadata(image: BedrockImage | None) -> dict[str, object]:
    if image is None:
        return {"image_included": False}
    return {
        "image_included": True,
        "image_dimensions": {"width": image.width, "height": image.height},
        "source_dimensions": {
            "width": image.source_width,
            "height": image.source_height,
        },
        "image_size_bytes": len(image.content),
        "transparency_composited": image.transparency_composited,
    }


def _transparency_note(image: BedrockImage) -> str:
    if not image.transparency_composited:
        return ""
    return (
        "\nThe inspection copy uses a gray checkerboard only to reveal transparent pixels. "
        "That checkerboard is not part of the seller's artwork."
    )


def _prompt_with_schema(prompt: str, schema: Mapping[str, Any]) -> str:
    """Supply the same bounded contract to models without native structured output."""

    encoded_schema = json.dumps(schema, separators=(",", ":"), sort_keys=True)
    return (
        f"{prompt}\n\nReturn only one JSON object matching this JSON Schema exactly. "
        "Do not add markdown or commentary.\n"
        f"{encoded_schema}"
    )
