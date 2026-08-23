"""Sealed Printify provider boundary for offline Phase 7.2 publication work.

This module is intentionally not composed into a handler, worker, role, or seller route.  It is a
separate client from the broader Phase 6 draft client and exposes only the three method/route
pairs frozen by publication contract 7.0.1.  Durable call claims account for budgets; a fresh,
one-use grant returned only by the winning execution-store commit is still required for every wire
request.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Annotated, Any, Literal, Protocol
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    StrictFloat,
    StrictInt,
    StringConstraints,
    model_validator,
)

from mr_lister.publication.execution_fingerprints import execution_record_fingerprint
from mr_lister.publication.execution_models import (
    ExpectedVariantEconomics,
    PublicationCallClaim,
    PublicationCallKind,
    PublicationCallPurpose,
    PublicationExternalEvidenceState,
    PublicationMutationClaim,
    PublicationPostOutcome,
    PublicationPreflightProof,
    PublicationProductReadEvidence,
    PublicationProviderAuditCategory,
    PublicationProviderAuditDecision,
    PublicationProviderAuditRecord,
    PublicationProviderAuthority,
    PublicationPublishEvidence,
    PublicationPublishResponseCategory,
    PublicationReadOutcome,
    PublicationShopPreflightEvidence,
)
from mr_lister.publication.execution_store import (
    FreshPublicationCallGrant,
    FreshPublicationMutationGrant,
)
from mr_lister.publication.fingerprints import (
    canonical_fingerprint,
)
from mr_lister.publication.models import OwnerId, SafeId

PRINTIFY_PUBLICATION_API_ORIGIN = "https://api.printify.com"
PRINTIFY_PUBLICATION_API_BASE_URL = f"{PRINTIFY_PUBLICATION_API_ORIGIN}/v1/"
MAX_PUBLICATION_RESPONSE_BYTES = 4 * 1024 * 1024

SHOP_ROUTE_TEMPLATE = "/v1/shops.json"
PRODUCT_ROUTE_TEMPLATE = "/v1/shops/{shop_id}/products/{product_id}.json"
PUBLISH_ROUTE_TEMPLATE = "/v1/shops/{shop_id}/products/{product_id}/publish.json"
OUTSIDE_ROUTE_TEMPLATE = "forbidden_operation"

PublicationAllowedAuditRoute = Literal[
    "/v1/shops.json",
    "/v1/shops/{shop_id}/products/{product_id}.json",
    "/v1/shops/{shop_id}/products/{product_id}/publish.json",
]

_PRODUCT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_PRODUCT_PATH = re.compile(
    r"^/v1/shops/([1-9][0-9]*)/products/([A-Za-z0-9][A-Za-z0-9_-]{0,127})\.json$"
)
_PUBLISH_PATH = re.compile(
    r"^/v1/shops/([1-9][0-9]*)/products/"
    r"([A-Za-z0-9][A-Za-z0-9_-]{0,127})/publish\.json$"
)
_SAFE_USER_AGENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ /-]{0,127}$")
_NUMERIC_ETSY_LISTING_ID = re.compile(r"^[1-9][0-9]{0,12}$")

_PUBLICATION_BODY = {
    field: True
    for field in (
        "title",
        "description",
        "images",
        "variants",
        "tags",
        "keyFeatures",
        "shipping_template",
    )
}
PUBLICATION_BODY_BYTES = json.dumps(
    _PUBLICATION_BODY,
    sort_keys=True,
    separators=(",", ":"),
).encode("ascii")


class PublicationProviderError(Exception):
    """Base class whose messages contain no provider response, identity, or credential."""


class PublicationProviderInputError(PublicationProviderError):
    """An attempted call differed from exact publication authority."""


class PublicationProviderUnavailableError(PublicationProviderError):
    """A read-only call may be retried only with another durable GET claim."""


class PublicationProviderAuthenticationError(PublicationProviderError):
    """The injected owner-bound credential was not accepted."""


class PublicationProviderResponseError(PublicationProviderError):
    """A completed provider response could not be used as structured evidence."""


class PublicationProviderPreflightError(PublicationProviderError):
    """Read-only evidence definitively failed a pre-mutation requirement."""


class PublicationAuditSink(Protocol):
    def write_allowed(
        self,
        *,
        record: PublicationProviderAuditRecord,
        call_claim: PublicationCallClaim,
    ) -> None: ...

    def write_rejected(self, record: PublicationProviderAuditRecord) -> None: ...


class PublicationHttpResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: StrictInt = Field(ge=100, le=599)
    body: bytes = Field(max_length=MAX_PUBLICATION_RESPONSE_BYTES)


class PublicationHttpTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> PublicationHttpResponse: ...


class _RejectRedirectHandler(HTTPRedirectHandler):
    """Never construct a follow-up request that could inherit the bearer credential."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


class RedirectSafePublicationTransport:
    """One-attempt urllib transport with exact origin and bounded response reads."""

    def __init__(self, *, opener: OpenerDirector | None = None) -> None:
        self._opener = opener or build_opener(_RejectRedirectHandler())

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> PublicationHttpResponse:
        assert_publication_api_url(method=method, url=url, body=body)
        request = Request(url=url, headers=headers, data=body, method=method)
        response_parts: tuple[Any, bytes] | None = None
        failure_message: str | None = None
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                response_parts = (response.status, self._read_bounded(response))
        except HTTPError as error:
            try:
                response_parts = (error.code, self._read_bounded(error))
            except PublicationProviderUnavailableError:
                failure_message = "Printify response exceeded the publication boundary"
            except Exception:
                failure_message = "Printify request did not complete"
        except PublicationProviderUnavailableError:
            failure_message = "Printify response exceeded the publication boundary"
        except Exception:
            failure_message = "Printify request did not complete"
        if failure_message is not None:
            # This raise is outside the dependency exception handler so neither the URL nor
            # any provider identity survives in __cause__ or __context__.
            raise PublicationProviderUnavailableError(failure_message) from None
        if response_parts is None:
            raise PublicationProviderUnavailableError("Printify request did not complete") from None
        try:
            validated = PublicationHttpResponse(
                status=response_parts[0],
                body=response_parts[1],
            )
        except Exception:
            validated = None
        if validated is None:
            raise PublicationProviderUnavailableError(
                "Printify transport returned an invalid response"
            ) from None
        return validated

    @staticmethod
    def _read_bounded(stream: Any) -> bytes:
        body = stream.read(MAX_PUBLICATION_RESPONSE_BYTES + 1)
        if len(body) > MAX_PUBLICATION_RESPONSE_BYTES:
            raise PublicationProviderUnavailableError(
                "Printify response exceeded the publication boundary"
            )
        return body


class SanitizedPublicationAuditTransport:
    """Fail closed around one transport and record only a route template and decision."""

    def __init__(
        self,
        *,
        transport: PublicationHttpTransport,
        audit_sink: PublicationAuditSink,
    ) -> None:
        self._transport = transport
        self._audit_sink = audit_sink

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
        call_claim: PublicationCallClaim | None = None,
    ) -> PublicationHttpResponse:
        try:
            route_template = assert_publication_api_url(method=method, url=url, body=body)
        except Exception:
            category = (
                PublicationProviderAuditCategory.FORBIDDEN_METHOD
                if method not in {"GET", "POST"}
                else PublicationProviderAuditCategory.FORBIDDEN_ROUTE
            )
            self._write_rejected(_rejected_audit_record(category))
            raise PublicationProviderInputError(
                "Printify request is outside the publication boundary"
            ) from None
        try:
            if call_claim is None:
                raise ValueError
            claim = PublicationCallClaim.model_validate(call_claim.model_dump(mode="python"))
            if (
                claim.method != method
                or claim.route_template != route_template
                or not _claim_matches_exact_url(claim=claim, url=url)
            ):
                raise ValueError
            record = _allowed_audit_record(claim)
        except Exception:
            self._write_rejected(
                _rejected_audit_record(PublicationProviderAuditCategory.CLAIM_MISMATCH)
            )
            raise PublicationProviderInputError(
                "Printify wire request lacks its exact durable call claim"
            ) from None
        self._write_allowed(record=record, call_claim=claim)
        return self._transport.request(
            method=method,
            url=url,
            headers=headers,
            body=body,
            timeout_seconds=timeout_seconds,
        )

    def record_rejected(self, category: PublicationProviderAuditCategory) -> None:
        self._write_rejected(_rejected_audit_record(category))

    def _write_allowed(
        self,
        *,
        record: PublicationProviderAuditRecord,
        call_claim: PublicationCallClaim,
    ) -> None:
        try:
            self._audit_sink.write_allowed(record=record, call_claim=call_claim)
        except Exception:
            raise PublicationProviderUnavailableError(
                "Publication provider audit is unavailable"
            ) from None

    def _write_rejected(self, record: PublicationProviderAuditRecord) -> None:
        try:
            self._audit_sink.write_rejected(record)
        except Exception:
            raise PublicationProviderUnavailableError(
                "Publication provider audit is unavailable"
            ) from None


def assert_publication_api_url(
    *,
    method: str,
    url: str,
    body: bytes | None,
) -> PublicationAllowedAuditRoute:
    """Return the sole sanitized template for an exact allowed request, else fail."""

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise PublicationProviderInputError("Printify publication URL was malformed") from error
    if (
        parsed.scheme != "https"
        or parsed.netloc != "api.printify.com"
        or parsed.hostname != "api.printify.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise PublicationProviderInputError(
            "Printify publication request escaped the exact HTTPS API origin"
        )
    if method == "GET" and body is None and parsed.path == SHOP_ROUTE_TEMPLATE:
        return SHOP_ROUTE_TEMPLATE
    if method == "GET" and body is None and _PRODUCT_PATH.fullmatch(parsed.path):
        return PRODUCT_ROUTE_TEMPLATE
    if method == "POST" and body == PUBLICATION_BODY_BYTES and _PUBLISH_PATH.fullmatch(parsed.path):
        return PUBLISH_ROUTE_TEMPLATE
    raise PublicationProviderInputError(
        "Printify method, route, query, or body is outside publication authority"
    )


def _claim_matches_exact_url(*, claim: PublicationCallClaim, url: str) -> bool:
    path = urlsplit(url).path
    if claim.call_kind is PublicationCallKind.SHOP_GET:
        return path == SHOP_ROUTE_TEMPLATE and claim.printify_product_id is None
    pattern = _PRODUCT_PATH if claim.call_kind is PublicationCallKind.PRODUCT_GET else _PUBLISH_PATH
    match = pattern.fullmatch(path)
    if match is None or claim.printify_product_id is None:
        return False
    return (
        int(match.group(1)) == claim.printify_shop_id
        and match.group(2) == claim.printify_product_id
    )


def _allowed_audit_record(claim: PublicationCallClaim) -> PublicationProviderAuditRecord:
    categories = {
        PublicationCallKind.SHOP_GET: PublicationProviderAuditCategory.SHOP_GET_ALLOWED,
        PublicationCallKind.PRODUCT_GET: PublicationProviderAuditCategory.PRODUCT_GET_ALLOWED,
        PublicationCallKind.PUBLISH_POST: PublicationProviderAuditCategory.PUBLISH_POST_ALLOWED,
    }
    values = {
        "decision": PublicationProviderAuditDecision.ALLOWED,
        "method_category": claim.method,
        "route_template": claim.route_template,
        "category": categories[claim.call_kind],
    }
    return PublicationProviderAuditRecord(
        **values,
        fingerprint=execution_record_fingerprint("provider_audit_record", values),
    )


def _rejected_audit_record(
    category: PublicationProviderAuditCategory,
) -> PublicationProviderAuditRecord:
    values = {
        "decision": PublicationProviderAuditDecision.REJECTED,
        "method_category": "FORBIDDEN",
        "route_template": OUTSIDE_ROUTE_TEMPLATE,
        "category": category,
    }
    return PublicationProviderAuditRecord(
        **values,
        fingerprint=execution_record_fingerprint("provider_audit_record", values),
    )


class _ProviderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class OwnerBoundPrintifyCredential(_ProviderModel):
    """An already resolved credential; no secret manager is composed in this module."""

    owner_id: OwnerId
    bearer_token: SecretStr

    @model_validator(mode="after")
    def token_is_bounded_without_disclosure(self) -> OwnerBoundPrintifyCredential:
        token = self.bearer_token.get_secret_value()
        if (
            not token
            or len(token) > 4096
            or token != token.strip()
            or not token.isascii()
            or any(character.isspace() or ord(character) < 33 for character in token)
        ):
            raise ValueError("Printify bearer credential is invalid")
        return self


class CanonicalReadVariant(_ProviderModel):
    id: StrictInt = Field(gt=0)
    price: StrictInt = Field(gt=0)
    is_enabled: StrictBool
    sku: Annotated[
        str,
        StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
    ]


class CanonicalReadPlacementImage(_ProviderModel):
    id: SafeId
    x: StrictFloat
    y: StrictFloat
    scale: StrictFloat
    angle: StrictInt = Field(ge=-360, le=360)

    @model_validator(mode="after")
    def coordinates_are_finite_and_bounded(self) -> CanonicalReadPlacementImage:
        if (
            not all(math.isfinite(value) for value in (self.x, self.y, self.scale))
            or not 0 <= self.x <= 1
            or not 0 <= self.y <= 1
            or not 0 < self.scale <= 1
        ):
            raise ValueError("Printify placement coordinates are invalid")
        return self


class CanonicalReadPlaceholder(_ProviderModel):
    position: Annotated[
        str,
        StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$"),
    ]
    images: tuple[CanonicalReadPlacementImage, ...] = Field(min_length=1, max_length=20)


class CanonicalReadPrintArea(_ProviderModel):
    variant_ids: tuple[StrictInt, ...] = Field(min_length=1, max_length=100)
    placeholders: tuple[CanonicalReadPlaceholder, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def variants_are_positive_and_unique(self) -> CanonicalReadPrintArea:
        if any(value <= 0 for value in self.variant_ids) or len(set(self.variant_ids)) != len(
            self.variant_ids
        ):
            raise ValueError("Printify print-area variants are invalid")
        return self


class CanonicalProductReadback(_ProviderModel):
    title: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=250_000)]
    tags: tuple[Annotated[str, StringConstraints(min_length=1, max_length=255)], ...] = Field(
        min_length=1,
        max_length=100,
    )
    blueprint_id: StrictInt = Field(gt=0)
    print_provider_id: StrictInt = Field(gt=0)
    variants: tuple[CanonicalReadVariant, ...] = Field(min_length=1, max_length=100)
    print_areas: tuple[CanonicalReadPrintArea, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def canonical_collections_are_unique(self) -> CanonicalProductReadback:
        variant_ids = tuple(item.id for item in self.variants)
        if len(set(variant_ids)) != len(variant_ids):
            raise ValueError("Printify product repeated a variant")
        return self


ExternalEvidenceState = PublicationExternalEvidenceState
PublishResponseCategory = PublicationPublishResponseCategory
PrintifyShopPreflightObservation = PublicationShopPreflightEvidence
PrintifyProductObservation = PublicationProductReadEvidence
PrintifyPublishObservation = PublicationPublishEvidence


class PublicationProviderBoundary(Protocol):
    """Capability-narrow interface for a future dedicated execution service."""

    def preflight_shop(
        self,
        *,
        call_claim: PublicationCallClaim,
        fresh_grant: FreshPublicationCallGrant,
    ) -> PrintifyShopPreflightObservation: ...

    def preflight_exact_product(
        self,
        *,
        call_claim: PublicationCallClaim,
        fresh_grant: FreshPublicationCallGrant,
    ) -> PrintifyProductObservation: ...

    def poll_exact_product(
        self,
        *,
        call_claim: PublicationCallClaim,
        fresh_grant: FreshPublicationCallGrant,
    ) -> PrintifyProductObservation: ...

    def publish_exact_product(
        self,
        *,
        call_claim: PublicationCallClaim,
        mutation_claim: PublicationMutationClaim,
        preflight_proof: PublicationPreflightProof,
        fresh_grant: FreshPublicationMutationGrant,
    ) -> PrintifyPublishObservation: ...


class PrintifyPublicationBoundary:
    """Three-route, exact-authority Printify publication boundary."""

    def __init__(
        self,
        *,
        authority: PublicationProviderAuthority,
        credential: OwnerBoundPrintifyCredential,
        transport: PublicationHttpTransport,
        audit_sink: PublicationAuditSink,
        clock: Any | None = None,
        timeout_seconds: float = 15.0,
        user_agent: str = "MrLister-Phase7-Offline",
    ) -> None:
        try:
            self._authority = PublicationProviderAuthority.model_validate(
                authority.model_dump(mode="python")
            )
            self._credential = OwnerBoundPrintifyCredential.model_validate(
                credential.model_dump(mode="python")
            )
        except Exception:
            raise PublicationProviderInputError(
                "Publication provider authority is invalid"
            ) from None
        if self._credential.owner_id != self._authority.owner_id:
            raise PublicationProviderInputError(
                "Publication credential does not match owner authority"
            )
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
            or timeout_seconds > 60
        ):
            raise ValueError("Publication provider timeout must be between zero and 60 seconds")
        if _SAFE_USER_AGENT.fullmatch(user_agent) is None:
            raise ValueError("Publication provider User-Agent is invalid")
        self._transport = SanitizedPublicationAuditTransport(
            transport=transport,
            audit_sink=audit_sink,
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._timeout_seconds = float(timeout_seconds)
        self._user_agent = user_agent
        self._claim_lock = Lock()
        self._used_call_claim_ids: set[str] = set()
        self._publish_started = False

    def preflight_shop(
        self,
        *,
        call_claim: PublicationCallClaim,
        fresh_grant: FreshPublicationCallGrant,
    ) -> PrintifyShopPreflightObservation:
        claim = self._claim_wire_call(
            call_claim=call_claim,
            fresh_grant=fresh_grant,
            expected_kind=PublicationCallKind.SHOP_GET,
            allowed_purposes={PublicationCallPurpose.SHOP_PREFLIGHT},
            method="GET",
            route_template=SHOP_ROUTE_TEMPLATE,
        )
        response = self._request(
            call_claim=claim,
            method="GET",
            path="shops.json",
            body=None,
        )
        payload = self._require_get_json(response, missing_product_is_definitive=False)
        if not isinstance(payload, list) or len(payload) > 512:
            raise PublicationProviderResponseError("Printify shops response was invalid")
        matching_channels: list[str] = []
        seen_ids: set[int] = set()
        for row in payload:
            if not isinstance(row, Mapping):
                raise PublicationProviderResponseError("Printify shops response was invalid")
            shop_id = row.get("id")
            channel = row.get("sales_channel")
            title = row.get("title")
            if (
                isinstance(shop_id, bool)
                or not isinstance(shop_id, int)
                or shop_id <= 0
                or not isinstance(channel, str)
                or not channel
                or len(channel) > 128
                or (title is not None and (not isinstance(title, str) or len(title) > 512))
                or shop_id in seen_ids
            ):
                raise PublicationProviderResponseError("Printify shops response was invalid")
            seen_ids.add(shop_id)
            if shop_id == self._authority.printify_shop_id:
                matching_channels.append(channel)
        if matching_channels != [self._authority.expected_sales_channel]:
            raise PublicationProviderPreflightError(
                "Configured Printify shop is not exactly connected to Etsy"
            )
        values = {
            "call_claim_id": claim.authorization_id,
            "call_claim_fingerprint": claim.fingerprint,
            "provider_authority_id": self._authority.provider_authority_id,
            "provider_authority_fingerprint": self._authority.fingerprint,
            "printify_shop_id": self._authority.printify_shop_id,
            "sales_channel": "etsy",
            "sanitized_response_fingerprint": canonical_fingerprint(
                {"exact_shop": True, "expected_sales_channel": "etsy"}
            ),
            "observed_at": self._now(),
        }
        return PrintifyShopPreflightObservation(
            **values,
            fingerprint=execution_record_fingerprint("shop_preflight_evidence", values),
        )

    def preflight_exact_product(
        self,
        *,
        call_claim: PublicationCallClaim,
        fresh_grant: FreshPublicationCallGrant,
    ) -> PrintifyProductObservation:
        observation = self._read_exact_product(
            call_claim=call_claim,
            fresh_grant=fresh_grant,
            allowed_purposes={PublicationCallPurpose.PRODUCT_PREFLIGHT},
        )
        if not observation.preflight_satisfied:
            raise PublicationProviderPreflightError(
                "Exact Printify product failed publication preflight"
            )
        return observation

    def poll_exact_product(
        self,
        *,
        call_claim: PublicationCallClaim,
        fresh_grant: FreshPublicationCallGrant,
    ) -> PrintifyProductObservation:
        return self._read_exact_product(
            call_claim=call_claim,
            fresh_grant=fresh_grant,
            allowed_purposes={
                PublicationCallPurpose.VERIFICATION,
                PublicationCallPurpose.RECONCILIATION,
            },
        )

    def publish_exact_product(
        self,
        *,
        call_claim: PublicationCallClaim,
        mutation_claim: PublicationMutationClaim,
        preflight_proof: PublicationPreflightProof,
        fresh_grant: FreshPublicationMutationGrant,
    ) -> PrintifyPublishObservation:
        try:
            claim = self._validate_claim(
                call_claim,
                expected_kind=PublicationCallKind.PUBLISH_POST,
                allowed_purposes={PublicationCallPurpose.PUBLISH},
                method="POST",
                route_template=PUBLISH_ROUTE_TEMPLATE,
            )
        except Exception:
            self._record_rejected(PublicationProviderAuditCategory.CLAIM_MISMATCH)
            raise PublicationProviderInputError(
                "Publish call claim differs from exact provider authority"
            ) from None
        try:
            mutation = PublicationMutationClaim.model_validate(
                mutation_claim.model_dump(mode="python")
            )
            proof = PublicationPreflightProof.model_validate(
                preflight_proof.model_dump(mode="python")
            )
            self._validate_mutation_links(claim=claim, mutation=mutation, proof=proof)
        except Exception:
            self._record_rejected(PublicationProviderAuditCategory.MUTATION_CLAIM_MISMATCH)
            raise PublicationProviderInputError(
                "Publish mutation records do not match exact preflight authority"
            ) from None
        try:
            self._consume_grant(
                claim=claim,
                fresh_grant=fresh_grant,
                mutation_claim=mutation,
                is_publish=True,
            )
        except Exception:
            self._record_rejected(PublicationProviderAuditCategory.STALE_OR_REPLAYED_GRANT)
            raise PublicationProviderInputError(
                "Publish call lacks fresh exact mutation authority"
            ) from None

        fallback_observed_at = claim.authorized_at
        try:
            response = self._request(
                call_claim=claim,
                method="POST",
                path=(
                    f"shops/{self._authority.printify_shop_id}/products/"
                    f"{self._authority.printify_product_id}/publish.json"
                ),
                body=PUBLICATION_BODY_BYTES,
            )
        except Exception:
            return self._publish_evidence(
                claim=claim,
                mutation=mutation,
                outcome=PublicationPostOutcome.AMBIGUOUS,
                response_category=PublishResponseCategory.TRANSPORT_FAILURE,
                sanitized_response_fingerprint=None,
                observed_at=fallback_observed_at,
            )

        if not 200 <= response.status < 300:
            category = PublishResponseCategory.NON_2XX
            outcome = PublicationPostOutcome.AMBIGUOUS
        elif not _valid_publish_success_body(response):
            category = PublishResponseCategory.MALFORMED_2XX
            outcome = PublicationPostOutcome.AMBIGUOUS
        else:
            category = PublishResponseCategory.VALIDATED_2XX
            outcome = PublicationPostOutcome.DEFINITELY_ACCEPTED
        fingerprint = canonical_fingerprint(
            {
                "response_category": category,
                "status": response.status,
            }
        )
        try:
            return self._publish_evidence(
                claim=claim,
                mutation=mutation,
                outcome=outcome,
                response_category=category,
                sanitized_response_fingerprint=fingerprint,
                observed_at=self._now(),
            )
        except Exception:
            return self._publish_evidence(
                claim=claim,
                mutation=mutation,
                outcome=PublicationPostOutcome.AMBIGUOUS,
                response_category=PublishResponseCategory.TRANSPORT_FAILURE,
                sanitized_response_fingerprint=None,
                observed_at=fallback_observed_at,
            )

    def _publish_evidence(
        self,
        *,
        claim: PublicationCallClaim,
        mutation: PublicationMutationClaim,
        outcome: PublicationPostOutcome,
        response_category: PublicationPublishResponseCategory,
        sanitized_response_fingerprint: str | None,
        observed_at: datetime,
    ) -> PrintifyPublishObservation:
        values = {
            "call_claim_id": claim.authorization_id,
            "call_claim_fingerprint": claim.fingerprint,
            "mutation_claim_id": mutation.mutation_claim_id,
            "mutation_claim_fingerprint": mutation.fingerprint,
            "provider_authority_id": self._authority.provider_authority_id,
            "provider_authority_fingerprint": self._authority.fingerprint,
            "outcome": outcome,
            "response_category": response_category,
            "sanitized_response_fingerprint": sanitized_response_fingerprint,
            "observed_at": observed_at,
        }
        return PrintifyPublishObservation(
            **values,
            fingerprint=execution_record_fingerprint("publish_evidence", values),
        )

    def _read_exact_product(
        self,
        *,
        call_claim: PublicationCallClaim,
        fresh_grant: FreshPublicationCallGrant,
        allowed_purposes: set[PublicationCallPurpose],
    ) -> PrintifyProductObservation:
        claim = self._claim_wire_call(
            call_claim=call_claim,
            fresh_grant=fresh_grant,
            expected_kind=PublicationCallKind.PRODUCT_GET,
            allowed_purposes=allowed_purposes,
            method="GET",
            route_template=PRODUCT_ROUTE_TEMPLATE,
        )
        response = self._request(
            call_claim=claim,
            method="GET",
            path=(
                f"shops/{self._authority.printify_shop_id}/products/"
                f"{self._authority.printify_product_id}.json"
            ),
            body=None,
        )
        if response.status == 404 and claim.purpose is not PublicationCallPurpose.PRODUCT_PREFLIGHT:
            return self._missing_product_observation(claim)
        payload = self._require_get_json(response, missing_product_is_definitive=True)
        if not isinstance(payload, Mapping):
            raise PublicationProviderResponseError("Printify product response was invalid")
        return self._parse_product_observation(claim=claim, payload=payload)

    def _parse_product_observation(
        self,
        *,
        claim: PublicationCallClaim,
        payload: Mapping[str, Any],
    ) -> PrintifyProductObservation:
        try:
            product_id = payload["id"]
            shop_id = payload["shop_id"]
            locked = payload["is_locked"]
            visible = payload["visible"]
            if product_id != self._authority.printify_product_id:
                raise ValueError
            if type(shop_id) is not int or shop_id != self._authority.printify_shop_id:
                raise ValueError
            if type(locked) is not bool or type(visible) is not bool:
                raise ValueError
            canonical = _canonical_product_readback(payload)
            economics = _variant_economics(payload)
            placement_ids = _placement_image_ids(canonical)
            mockup_fingerprints = _mockup_fingerprints(payload)
        except Exception:
            raise PublicationProviderResponseError(
                "Printify product response was invalid"
            ) from None

        canonical_payload_fingerprint = canonical_fingerprint(canonical.model_dump(mode="json"))
        canonical_match = (
            canonical_payload_fingerprint == self._authority.product_payload_fingerprint
        )
        economics_match = economics == self._authority.expected_variant_economics
        placement_match = placement_ids == {self._authority.printify_image_id}
        mockups_match = set(mockup_fingerprints) == set(
            self._authority.expected_mockup_fingerprints
        ) and len(mockup_fingerprints) == len(self._authority.expected_mockup_fingerprints)
        external_state, numeric_listing_id = _external_evidence(payload)
        complete_content = canonical_match and economics_match and placement_match and mockups_match
        observed_at = self._now()
        positive = (
            complete_content
            and not locked
            and visible
            and external_state is ExternalEvidenceState.SINGLE_NUMERIC_ETSY_REFERENCE
            and numeric_listing_id is not None
            and observed_at < self._authority.verification_deadline
        )
        if positive:
            outcome = PublicationReadOutcome.POSITIVE_PROOF
        elif (
            external_state is ExternalEvidenceState.CONFLICTING_OR_INCOMPLETE
            or not complete_content
        ):
            outcome = PublicationReadOutcome.CONFLICTING_OR_INCOMPLETE
            numeric_listing_id = None
        else:
            outcome = PublicationReadOutcome.NOT_YET_PROVEN
            numeric_listing_id = None
        values = {
            "call_claim_id": claim.authorization_id,
            "call_claim_fingerprint": claim.fingerprint,
            "provider_authority_id": self._authority.provider_authority_id,
            "provider_authority_fingerprint": self._authority.fingerprint,
            "printify_shop_id": shop_id,
            "printify_product_id": product_id,
            "sanitized_response_fingerprint": canonical_fingerprint(
                {
                    "canonical_payload_fingerprint": canonical_payload_fingerprint,
                    "variant_economics": economics,
                    "placement_image_ids": tuple(sorted(placement_ids)),
                    "mockup_fingerprints": tuple(sorted(mockup_fingerprints)),
                    "is_locked": locked,
                    "visible": visible,
                    "external_evidence": external_state,
                    "numeric_listing_id": numeric_listing_id,
                }
            ),
            "product_present": True,
            "canonical_payload_fingerprint": canonical_payload_fingerprint,
            "canonical_content_match": canonical_match,
            "exact_variant_economics": economics_match,
            "exact_placement_image": placement_match,
            "exact_mockups": mockups_match,
            "is_locked": locked,
            "visible": visible,
            "external_evidence": external_state,
            "numeric_listing_id": numeric_listing_id,
            "read_outcome": outcome,
            "observed_at": observed_at,
        }
        return PrintifyProductObservation(
            **values,
            fingerprint=execution_record_fingerprint("product_read_evidence", values),
        )

    def _missing_product_observation(
        self,
        claim: PublicationCallClaim,
    ) -> PrintifyProductObservation:
        values = {
            "call_claim_id": claim.authorization_id,
            "call_claim_fingerprint": claim.fingerprint,
            "provider_authority_id": self._authority.provider_authority_id,
            "provider_authority_fingerprint": self._authority.fingerprint,
            "printify_shop_id": self._authority.printify_shop_id,
            "printify_product_id": self._authority.printify_product_id,
            "sanitized_response_fingerprint": canonical_fingerprint(
                {"read_category": "exact_product_missing"}
            ),
            "product_present": False,
            "canonical_payload_fingerprint": None,
            "canonical_content_match": False,
            "exact_variant_economics": False,
            "exact_placement_image": False,
            "exact_mockups": False,
            "is_locked": None,
            "visible": None,
            "external_evidence": ExternalEvidenceState.CONFLICTING_OR_INCOMPLETE,
            "numeric_listing_id": None,
            "read_outcome": PublicationReadOutcome.CONFLICTING_OR_INCOMPLETE,
            "observed_at": self._now(),
        }
        return PrintifyProductObservation(
            **values,
            fingerprint=execution_record_fingerprint("product_read_evidence", values),
        )

    def _claim_wire_call(
        self,
        *,
        call_claim: PublicationCallClaim,
        fresh_grant: FreshPublicationCallGrant,
        expected_kind: PublicationCallKind,
        allowed_purposes: set[PublicationCallPurpose],
        method: Literal["GET", "POST"],
        route_template: PublicationAllowedAuditRoute,
    ) -> PublicationCallClaim:
        try:
            claim = self._validate_claim(
                call_claim,
                expected_kind=expected_kind,
                allowed_purposes=allowed_purposes,
                method=method,
                route_template=route_template,
            )
        except Exception:
            self._record_rejected(PublicationProviderAuditCategory.CLAIM_MISMATCH)
            raise PublicationProviderInputError(
                "Provider call claim differs from exact provider authority"
            ) from None
        try:
            self._consume_grant(
                claim=claim,
                fresh_grant=fresh_grant,
                mutation_claim=None,
                is_publish=False,
            )
            return claim
        except Exception:
            self._record_rejected(PublicationProviderAuditCategory.STALE_OR_REPLAYED_GRANT)
            raise PublicationProviderInputError(
                "Provider call lacks fresh exact authority"
            ) from None

    def _validate_claim(
        self,
        call_claim: PublicationCallClaim,
        *,
        expected_kind: PublicationCallKind,
        allowed_purposes: set[PublicationCallPurpose],
        method: Literal["GET", "POST"],
        route_template: PublicationAllowedAuditRoute,
    ) -> PublicationCallClaim:
        claim = PublicationCallClaim.model_validate(call_claim.model_dump(mode="python"))
        authority = self._authority
        current_time = self._now()
        if (
            claim.call_kind is not expected_kind
            or claim.purpose not in allowed_purposes
            or claim.method != method
            or claim.route_template != route_template
            or claim.owner_id != authority.owner_id
            or claim.job_id != authority.job_id
            or claim.aggregate_id != authority.aggregate_id
            or claim.attempt_id != authority.attempt_id
            or claim.snapshot_id != authority.snapshot_id
            or claim.snapshot_fingerprint != authority.snapshot_fingerprint
            or claim.permit_id != authority.permit_id
            or claim.work_request_id != authority.work_request_id
            or claim.printify_shop_id != authority.printify_shop_id
            or claim.verification_deadline != authority.verification_deadline
            or claim.authorized_at > current_time
            or (
                expected_kind is PublicationCallKind.SHOP_GET
                and claim.printify_product_id is not None
            )
            or (
                expected_kind is not PublicationCallKind.SHOP_GET
                and claim.printify_product_id != authority.printify_product_id
            )
            or current_time >= authority.verification_deadline
            or (
                expected_kind is PublicationCallKind.PUBLISH_POST
                and current_time >= authority.pricing_fresh_until
            )
        ):
            raise PublicationProviderInputError(
                "Provider call differs from exact root-attempt authority"
            )
        return claim

    def _validate_mutation_links(
        self,
        *,
        claim: PublicationCallClaim,
        mutation: PublicationMutationClaim,
        proof: PublicationPreflightProof,
    ) -> None:
        authority = self._authority
        if (
            mutation.call_claim_id != claim.authorization_id
            or mutation.call_claim_fingerprint != claim.fingerprint
            or mutation.aggregate_id != authority.aggregate_id
            or mutation.attempt_id != authority.attempt_id
            or mutation.snapshot_id != authority.snapshot_id
            or mutation.snapshot_fingerprint != authority.snapshot_fingerprint
            or mutation.permit_id != authority.permit_id
            or mutation.work_request_id != authority.work_request_id
            or mutation.preflight_proof_id != proof.proof_id
            or mutation.preflight_proof_fingerprint != proof.fingerprint
            or mutation.consumed_permit_fingerprint != claim.permit_fingerprint
            or mutation.publication_body_fingerprint != authority.publication_body_fingerprint
            or mutation.verification_deadline != authority.verification_deadline
            or proof.aggregate_id != authority.aggregate_id
            or proof.attempt_id != authority.attempt_id
            or proof.snapshot_id != authority.snapshot_id
            or proof.snapshot_fingerprint != authority.snapshot_fingerprint
            or proof.provider_authority_id != authority.provider_authority_id
            or proof.provider_authority_fingerprint != authority.fingerprint
            or proof.printify_shop_id != authority.printify_shop_id
            or proof.printify_product_id != authority.printify_product_id
            or proof.publication_body_fingerprint != authority.publication_body_fingerprint
            or proof.verification_deadline != authority.verification_deadline
            or proof.proven_at > mutation.authorized_at
        ):
            raise PublicationProviderInputError(
                "Publish mutation records do not match exact preflight authority"
            )

    def _consume_grant(
        self,
        *,
        claim: PublicationCallClaim,
        fresh_grant: FreshPublicationCallGrant,
        mutation_claim: PublicationMutationClaim | None,
        is_publish: bool,
    ) -> None:
        with self._claim_lock:
            if claim.authorization_id in self._used_call_claim_ids or (
                is_publish and self._publish_started
            ):
                raise PublicationProviderInputError("Fresh provider call grant was already used")
            if mutation_claim is None:
                fresh_grant.consume_once(claim)
            else:
                if not isinstance(fresh_grant, FreshPublicationMutationGrant):
                    raise PublicationProviderInputError(
                        "Publish call requires fresh mutation authority"
                    )
                fresh_grant.consume_once(claim, mutation_claim)
            self._used_call_claim_ids.add(claim.authorization_id)
            if is_publish:
                self._publish_started = True

    def _request(
        self,
        *,
        call_claim: PublicationCallClaim,
        method: Literal["GET", "POST"],
        path: str,
        body: bytes | None,
    ) -> PublicationHttpResponse:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._credential.bearer_token.get_secret_value()}",
            "User-Agent": self._user_agent,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = self._transport.request(
                method=method,
                url=f"{PRINTIFY_PUBLICATION_API_BASE_URL}{path}",
                headers=headers,
                body=body,
                timeout_seconds=self._timeout_seconds,
                call_claim=call_claim,
            )
        except PublicationProviderError:
            raise
        except Exception:
            raise PublicationProviderUnavailableError("Printify request did not complete") from None
        try:
            return PublicationHttpResponse.model_validate(response.model_dump(mode="python"))
        except Exception:
            raise PublicationProviderResponseError(
                "Printify transport returned an invalid response"
            ) from None

    @staticmethod
    def _require_get_json(
        response: PublicationHttpResponse,
        *,
        missing_product_is_definitive: bool,
    ) -> Any:
        if response.status in {401, 403}:
            raise PublicationProviderAuthenticationError(
                "Printify credential could not read publication authority"
            )
        if response.status == 404 and missing_product_is_definitive:
            raise PublicationProviderPreflightError("Exact Printify product was not found")
        if response.status != 200:
            raise PublicationProviderUnavailableError(
                "Printify read did not return a usable response"
            )
        return _decode_json(response.body)

    def _record_rejected(self, category: PublicationProviderAuditCategory) -> None:
        self._transport.record_rejected(category)

    def _now(self) -> datetime:
        value = self._clock()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != timedelta(0)
        ):
            raise PublicationProviderInputError("Publication provider clock must be UTC-aware")
        return value.astimezone(UTC)


def _canonical_product_readback(payload: Mapping[str, Any]) -> CanonicalProductReadback:
    variants = []
    raw_variants = payload.get("variants")
    if not isinstance(raw_variants, list):
        raise ValueError
    for item in raw_variants:
        if not isinstance(item, Mapping):
            raise ValueError
        variants.append(
            CanonicalReadVariant(
                id=item.get("id"),
                price=item.get("price"),
                is_enabled=item.get("is_enabled"),
                sku=item.get("sku"),
            )
        )
    print_areas = []
    raw_areas = payload.get("print_areas")
    if not isinstance(raw_areas, list):
        raise ValueError
    for raw_area in raw_areas:
        if not isinstance(raw_area, Mapping):
            raise ValueError
        raw_placeholders = raw_area.get("placeholders")
        if not isinstance(raw_placeholders, list):
            raise ValueError
        placeholders = []
        for raw_placeholder in raw_placeholders:
            if not isinstance(raw_placeholder, Mapping):
                raise ValueError
            raw_images = raw_placeholder.get("images")
            if not isinstance(raw_images, list):
                raise ValueError
            images = []
            for raw_image in raw_images:
                if not isinstance(raw_image, Mapping):
                    raise ValueError
                images.append(
                    CanonicalReadPlacementImage(
                        id=raw_image.get("id"),
                        x=_strict_coordinate(raw_image.get("x")),
                        y=_strict_coordinate(raw_image.get("y")),
                        scale=_strict_coordinate(raw_image.get("scale")),
                        angle=raw_image.get("angle"),
                    )
                )
            placeholders.append(
                CanonicalReadPlaceholder(
                    position=raw_placeholder.get("position"),
                    images=tuple(images),
                )
            )
        variant_ids = raw_area.get("variant_ids")
        if not isinstance(variant_ids, list):
            raise ValueError
        print_areas.append(
            CanonicalReadPrintArea(
                variant_ids=tuple(variant_ids),
                placeholders=tuple(placeholders),
            )
        )
    tags = payload.get("tags")
    if not isinstance(tags, list):
        raise ValueError
    return CanonicalProductReadback(
        title=payload.get("title"),
        description=payload.get("description"),
        tags=tuple(tags),
        blueprint_id=payload.get("blueprint_id"),
        print_provider_id=payload.get("print_provider_id"),
        variants=tuple(variants),
        print_areas=tuple(print_areas),
    )


def _variant_economics(payload: Mapping[str, Any]) -> tuple[ExpectedVariantEconomics, ...]:
    raw_variants = payload.get("variants")
    if not isinstance(raw_variants, list):
        raise ValueError
    return tuple(
        ExpectedVariantEconomics(
            variant_id=item.get("id"),
            retail_price_cents=item.get("price"),
            production_cost_cents=item.get("cost"),
        )
        for item in raw_variants
        if isinstance(item, Mapping)
    )


def _placement_image_ids(canonical: CanonicalProductReadback) -> set[str]:
    return {
        image.id
        for area in canonical.print_areas
        for placeholder in area.placeholders
        for image in placeholder.images
    }


def printify_mockup_fingerprint(
    *,
    url: str,
    position: str | None,
    variant_ids: tuple[int, ...],
) -> str:
    """Hash one safe mockup without returning its provider URL in an observation."""

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("Printify mockup URL is invalid") from error
    if (
        not isinstance(url, str)
        or len(url) > 2048
        or not url.isascii()
        or parsed.scheme != "https"
        or parsed.netloc != "images.printify.com"
        or parsed.hostname != "images.printify.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
        or parsed.path == "/"
        or parsed.fragment
        or (position is not None and (not isinstance(position, str) or len(position) > 64))
        or any(type(value) is not int or value <= 0 for value in variant_ids)
        or len(set(variant_ids)) != len(variant_ids)
    ):
        raise ValueError("Printify mockup evidence is invalid")
    return canonical_fingerprint(
        {
            "url": url,
            "position": position,
            "variant_ids": variant_ids,
        }
    )


def _mockup_fingerprints(payload: Mapping[str, Any]) -> tuple[str, ...]:
    images = payload.get("images")
    if not isinstance(images, list) or not 1 <= len(images) <= 20:
        raise ValueError
    fingerprints = []
    for image in images:
        if not isinstance(image, Mapping):
            raise ValueError
        raw_variant_ids = image.get("variant_ids", [])
        if not isinstance(raw_variant_ids, list):
            raise ValueError
        fingerprints.append(
            printify_mockup_fingerprint(
                url=image.get("src"),
                position=image.get("position"),
                variant_ids=tuple(raw_variant_ids),
            )
        )
    if len(set(fingerprints)) != len(fingerprints):
        raise ValueError
    return tuple(fingerprints)


_MISSING = object()


def _external_evidence(
    payload: Mapping[str, Any],
) -> tuple[ExternalEvidenceState, int | None]:
    """Classify the documented external-reference array without trusting its handle."""

    external = payload.get("external", _MISSING)
    if external is _MISSING or external == [] or external == {}:
        return ExternalEvidenceState.ABSENT, None
    if not isinstance(external, list) or len(external) != 1:
        return ExternalEvidenceState.CONFLICTING_OR_INCOMPLETE, None
    reference = external[0]
    if not isinstance(reference, Mapping):
        return ExternalEvidenceState.CONFLICTING_OR_INCOMPLETE, None
    identifier = reference.get("id")
    if not isinstance(identifier, str) or not _NUMERIC_ETSY_LISTING_ID.fullmatch(identifier):
        return ExternalEvidenceState.CONFLICTING_OR_INCOMPLETE, None
    return ExternalEvidenceState.SINGLE_NUMERIC_ETSY_REFERENCE, int(identifier)


def _strict_coordinate(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError
    return converted


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _decode_json(body: bytes) -> Any:
    if not body:
        raise PublicationProviderResponseError("Printify response omitted JSON")
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("Invalid number")),
        )
        _validate_json_shape(value)
        return value
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise PublicationProviderResponseError("Printify response contained invalid JSON") from None


def _validate_json_shape(value: Any, *, depth: int = 0) -> None:
    if depth > 32:
        raise ValueError("JSON nesting exceeded")
    if isinstance(value, dict):
        if len(value) > 512:
            raise ValueError("JSON object exceeded")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 512:
                raise ValueError("JSON key exceeded")
            _validate_json_shape(item, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 2048:
            raise ValueError("JSON array exceeded")
        for item in value:
            _validate_json_shape(item, depth=depth + 1)
    elif isinstance(value, str):
        if len(value) > 300_000:
            raise ValueError("JSON string exceeded")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON number was non-finite")
    elif value is not None and not isinstance(value, (bool, int)):
        raise ValueError("JSON value was invalid")


def _valid_publish_success_body(response: PublicationHttpResponse) -> bool:
    if response.status == 204:
        return response.body == b""
    if not response.body:
        return True
    try:
        payload = _decode_json(response.body)
    except PublicationProviderResponseError:
        return False
    return isinstance(payload, Mapping)


__all__ = [
    "ExpectedVariantEconomics",
    "ExternalEvidenceState",
    "FreshPublicationCallGrant",
    "FreshPublicationMutationGrant",
    "MAX_PUBLICATION_RESPONSE_BYTES",
    "OUTSIDE_ROUTE_TEMPLATE",
    "OwnerBoundPrintifyCredential",
    "PRODUCT_ROUTE_TEMPLATE",
    "PUBLICATION_BODY_BYTES",
    "PUBLISH_ROUTE_TEMPLATE",
    "PrintifyProductObservation",
    "PrintifyPublicationBoundary",
    "PrintifyPublishObservation",
    "PrintifyShopPreflightObservation",
    "PublicationAuditSink",
    "PublicationHttpResponse",
    "PublicationHttpTransport",
    "PublicationProviderAuthenticationError",
    "PublicationProviderAuthority",
    "PublicationProviderBoundary",
    "PublicationProviderError",
    "PublicationProviderInputError",
    "PublicationProviderPreflightError",
    "PublicationProviderAuditRecord",
    "PublicationProviderResponseError",
    "PublicationProviderUnavailableError",
    "PublishResponseCategory",
    "RedirectSafePublicationTransport",
    "SHOP_ROUTE_TEMPLATE",
    "SanitizedPublicationAuditTransport",
    "assert_publication_api_url",
    "printify_mockup_fingerprint",
]
