"""Gemma 4 candidate using AWS Mantle's chat-completions wire format.

This adapter is not selected by the production composition. Its live canary passed,
but semantic qualification and deployed runtime compatibility remain promotion gates.
No provider API key, mutable endpoint, automatic model fallback or hidden HTTP retry exists.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from re import fullmatch
from time import monotonic
from typing import Any, Protocol

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSPreparedRequest, AWSRequest, AWSResponse
from botocore.exceptions import BotoCoreError
from botocore.httpsession import URLLib3Session

from mr_lister.intelligence.bedrock import BedrockListingIntelligenceAdapter
from mr_lister.intelligence.diagnostics import BedrockDiagnosticRecord, DiagnosticSink
from mr_lister.intelligence.images import BedrockImage, prepare_bedrock_image
from mr_lister.intelligence.prompts import BASELINE_PROMPT_BUNDLE, PromptBundle
from mr_lister.intelligence.settings import BedrockSettings
from mr_lister.workflow.errors import (
    IntelligenceConfigurationError,
    IntelligenceUnavailableError,
    InvalidGeneratedOutputError,
)

MAX_REQUEST_BYTES = 3_500_000
MAX_RESPONSE_BYTES = 1_000_000
MANTLE_REGIONS = frozenset({"us-east-1", "us-east-2", "us-west-2", "eu-central-1"})
SIGNING_SERVICE = "bedrock-mantle"
MAX_OPERATION_SECONDS = 300.0
_OPERATION_DEADLINE: ContextVar[float | None] = ContextVar(
    "mantle_operation_deadline", default=None
)


def _remaining_seconds() -> float:
    deadline = _OPERATION_DEADLINE.get()
    remaining = MAX_OPERATION_SECONDS if deadline is None else deadline - monotonic()
    if remaining <= 0:
        raise IntelligenceUnavailableError("Mantle operation exceeded its bounded time budget")
    return remaining


class MantleClient(Protocol):
    def complete(self, request: Mapping[str, Any]) -> dict[str, Any]: ...


class MantleSender(Protocol):
    def send(self, request: AWSPreparedRequest) -> AWSResponse: ...


def _encoded_request(request: Mapping[str, Any]) -> bytes:
    try:
        body = json.dumps(
            request, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise IntelligenceConfigurationError("Mantle request is not serializable") from None
    if len(body) > MAX_REQUEST_BYTES:
        raise IntelligenceConfigurationError("Mantle request exceeds the bounded byte budget")
    return body


class _QuietSigV4Auth(SigV4Auth):
    """Use botocore's signing primitives without its credential-bearing debug output."""

    def add_auth(self, request: AWSRequest) -> None:
        request.context["timestamp"] = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self._modify_request_before_signing(request)
        canonical_request = self.canonical_request(request)
        string_to_sign = self.string_to_sign(request, canonical_request)
        signature = self.signature(string_to_sign, request)
        self._inject_signature_to_request(request, signature)


class SigV4MantleClient:
    """One signed HTTP attempt per call, with refreshable temporary AWS credentials.

    URLLib3Session uses Retry(False), which disables redirect following and HTTP retries.
    The response is streamed and read at most MAX_RESPONSE_BYTES + 1 before closing.
    Application repair limits remain owned by the shared intelligence contract adapter.
    """

    def __init__(
        self,
        *,
        session: Any,
        region: str,
        sender: MantleSender | None = None,
    ) -> None:
        if region not in MANTLE_REGIONS:
            raise IntelligenceConfigurationError("Mantle region is not supported by this candidate")
        self._session = session
        self._region = region
        self._url = f"https://bedrock-mantle.{region}.api.aws/openai/v1/chat/completions"
        self._sender = sender or URLLib3Session(timeout=(10, 300), max_pool_connections=2)

    def complete(self, request: Mapping[str, Any]) -> dict[str, Any]:
        # Serialize before credentials or I/O, including every repair's full history.
        body = _encoded_request(request)
        remaining = _remaining_seconds()
        try:
            provider_credentials = self._session.get_credentials()
            credentials = (
                provider_credentials.get_frozen_credentials() if provider_credentials else None
            )
            if (
                credentials is None
                or not credentials.access_key
                or not credentials.secret_key
                or not credentials.token
            ):
                raise IntelligenceConfigurationError("Temporary AWS credentials are required")
            signed = AWSRequest(
                method="POST",
                url=self._url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
                stream_output=True,
            )
            # Repair attempts consume the same operation budget instead of each resetting it.
            signed.context["read_timeout"] = max(0.001, remaining - min(10.0, remaining / 2))
            _QuietSigV4Auth(credentials, SIGNING_SERVICE, self._region).add_auth(signed)
            prepared = signed.prepare()
        except IntelligenceConfigurationError:
            raise
        except Exception:
            raise IntelligenceConfigurationError("Mantle AWS signing is unavailable") from None

        try:
            response = self._sender.send(prepared)
        except Exception:
            raise IntelligenceUnavailableError(
                "Mantle transport is temporarily unavailable"
            ) from None

        try:
            status = response.status_code
            if status in {408, 429, 500, 502, 503, 504}:
                raise IntelligenceUnavailableError("Mantle is temporarily unavailable")
            if status != 200:
                # Never include response bodies, signed requests or arbitrary provider messages.
                raise IntelligenceConfigurationError("Mantle rejected the invocation configuration")
            body = response.raw.read(MAX_RESPONSE_BYTES + 1)
            _remaining_seconds()
            if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
                raise InvalidGeneratedOutputError("Mantle response exceeds the bounded byte budget")
        except (
            IntelligenceConfigurationError,
            IntelligenceUnavailableError,
            InvalidGeneratedOutputError,
        ):
            raise
        except Exception:
            raise IntelligenceUnavailableError("Mantle response could not be read") from None
        finally:
            try:
                response.raw.close()
            except Exception:
                pass
        try:
            result = json.loads(
                body, parse_constant=_reject_constant, object_pairs_hook=_unique_object
            )
            if not isinstance(result, dict):
                raise ValueError
        except (ValueError, TypeError, UnicodeError):
            raise InvalidGeneratedOutputError(
                "Mantle returned an invalid response envelope"
            ) from None
        # Metadata is owned by our transport, never taken from model output or an error body.
        result.pop("_mr_lister_request_id", None)
        request_id = response.headers.get("x-amzn-requestid") or response.headers.get(
            "x-amzn-request-id"
        )
        if isinstance(request_id, str) and fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:/+=-]{0,255}", request_id
        ):
            result["_mr_lister_request_id"] = request_id
        return result


def _reject_constant(_: str) -> None:
    raise ValueError("Non-finite JSON is not allowed")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate response member")
        result[key] = value
    return result


def _chat_messages(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    system = request.get("system", [])
    if system:
        messages.append({"role": "system", "content": "\n".join(block["text"] for block in system)})
    for message in request["messages"]:
        blocks: list[dict[str, Any]] = []
        for block in message["content"]:
            if "text" in block:
                blocks.append({"type": "text", "text": block["text"]})
            elif "image" in block:
                image = block["image"]
                if image["format"] != "png" or message["role"] != "user":
                    raise IntelligenceConfigurationError("Mantle image request is invalid")
                encoded = base64.b64encode(image["source"]["bytes"]).decode("ascii")
                blocks.append(
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}
                )
            else:
                raise IntelligenceConfigurationError("Mantle request contains unsupported content")
        messages.append({"role": message["role"], "content": blocks})
    return messages


def _normalize_response(response: Mapping[str, Any]) -> dict[str, Any]:
    """Only complete assistant content can enter the existing Pydantic validator.

    Reasoning text and other metadata are never concatenated into listing text. Truncated,
    refused and tool-bearing completions fail rather than accepting a plausible JSON fragment.
    """

    try:
        choices = response["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError
        choice = choices[0]
        message = choice["message"]
        if (
            choice.get("finish_reason") != "stop"
            or message.get("role") != "assistant"
            or message.get("refusal") not in (None, "")
            or message.get("tool_calls") not in (None, [])
            or message.get("function_call") is not None
        ):
            raise ValueError
        content = message["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError
        normalized_usage: dict[str, int] = {}
        if "usage" in response and response["usage"] is not None:
            usage = response["usage"]
            for source, target in (
                ("prompt_tokens", "inputTokens"),
                ("completion_tokens", "outputTokens"),
                ("total_tokens", "totalTokens"),
            ):
                count = usage[source]
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise ValueError
                normalized_usage[target] = count
            if (
                normalized_usage["totalTokens"]
                != normalized_usage["inputTokens"] + normalized_usage["outputTokens"]
            ):
                raise ValueError
    except (KeyError, ValueError, TypeError, AttributeError):
        raise InvalidGeneratedOutputError(
            "Mantle returned no complete authorized assistant content"
        ) from None
    result: dict[str, Any] = {
        "output": {"message": {"role": "assistant", "content": [{"text": content}]}},
        "stopReason": "end_turn",
        "usage": normalized_usage,
    }
    request_id = response.get("_mr_lister_request_id")
    if isinstance(request_id, str):
        result["ResponseMetadata"] = {"RequestId": request_id}
    return result


class _MantleConverseBridge:
    """Internal wire bridge, not a claim that Gemma 4 supports Bedrock Converse.

    Keeping this seam lets both transports use exactly the same prompt, schema validation,
    quality repair and tag-selection code. It never calls a Converse endpoint.
    """

    def __init__(self, client: MantleClient) -> None:
        self._client = client

    def converse(self, **request: Any) -> dict[str, Any]:
        try:
            definition = request["outputConfig"]["textFormat"]["structure"]["jsonSchema"]
            payload = {
                "model": request["modelId"],
                "messages": _chat_messages(request),
                "max_tokens": request["inferenceConfig"]["maxTokens"],
                "temperature": request["inferenceConfig"]["temperature"],
                "stream": False,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": definition["name"],
                        "schema": json.loads(definition["schema"]),
                        "strict": True,
                    },
                },
            }
        except (KeyError, TypeError, ValueError):
            raise IntelligenceConfigurationError(
                "Mantle native-schema request is invalid"
            ) from None
        _encoded_request(payload)
        _remaining_seconds()
        response = self._client.complete(payload)
        _remaining_seconds()
        return _normalize_response(response)


class MantleListingIntelligenceAdapter(BedrockListingIntelligenceAdapter):
    """Gemma 4 candidate that preserves the current application-owned contract pipeline."""

    def __init__(
        self,
        *,
        client: MantleClient,
        settings: BedrockSettings,
        diagnostics: DiagnosticSink | None = None,
        prompt_bundle: PromptBundle = BASELINE_PROMPT_BUNDLE,
    ) -> None:
        if settings.transport != "mantle" or settings.output_mode != "native_json_schema":
            raise IntelligenceConfigurationError(
                "Mantle requires its explicit native-schema configuration"
            )
        super().__init__(
            client=_MantleConverseBridge(client),
            settings=settings,
            diagnostics=diagnostics,
            prompt_bundle=prompt_bundle,
        )

    def _invoke_contract(self, **kwargs: Any) -> Any:
        deadline = _OPERATION_DEADLINE.set(monotonic() + MAX_OPERATION_SECONDS)
        try:
            return super()._invoke_contract(**kwargs)
        finally:
            _OPERATION_DEADLINE.reset(deadline)

    def _prepare_inspection_image(self, content: bytes) -> BedrockImage:
        return prepare_bedrock_image(content, max_side=1600, max_bytes=750_000)

    def _converse(self, **kwargs: Any) -> dict[str, Any]:
        try:
            return super()._converse(**kwargs)
        except (
            IntelligenceConfigurationError,
            IntelligenceUnavailableError,
            InvalidGeneratedOutputError,
        ) as error:
            self._diagnostics.emit(
                BedrockDiagnosticRecord(
                    operation=kwargs["operation"],
                    model_id=self._settings.model_id,
                    status="provider_error",
                    prompt_version=self._prompt_bundle.version,
                    attempt=kwargs["attempt"],
                    artwork_sha256=kwargs["artwork_sha256"],
                    metadata={
                        "prompt_fingerprint": self._prompt_bundle.fingerprint,
                        "transport": "mantle",
                    },
                    error_type=type(error).__name__,
                    error_message="Mantle invocation did not produce an accepted result",
                )
            )
            raise error from None
        except BotoCoreError:
            raise IntelligenceUnavailableError(
                "Mantle transport is temporarily unavailable"
            ) from None


def build_mantle_adapter(
    settings: BedrockSettings,
    *,
    session: boto3.Session | None = None,
    diagnostics: DiagnosticSink | None = None,
    prompt_bundle: PromptBundle = BASELINE_PROMPT_BUNDLE,
) -> MantleListingIntelligenceAdapter:
    active_session = session or boto3.Session(region_name=settings.region)
    return MantleListingIntelligenceAdapter(
        client=SigV4MantleClient(session=active_session, region=settings.region),
        settings=settings,
        diagnostics=diagnostics,
        prompt_bundle=prompt_bundle,
    )
