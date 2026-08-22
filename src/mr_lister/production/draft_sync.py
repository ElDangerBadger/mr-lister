"""Draft-only Printify synchronization for the Phase 6 control plane.

This module deliberately does not own workflow state.  The application store decides whether a
job is authorized for its single initial create, supplies the immutable product ID thereafter,
and commits returned evidence transactionally.  This boundary can only create an unpublished
draft, replace that same draft, or read products for reconciliation.
"""

from __future__ import annotations

import re
from base64 import b64encode
from collections.abc import Callable
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from mr_lister.contracts import ListingIntelligence, ProductProfile
from mr_lister.contracts.presentation import ProductMockupEvidence
from mr_lister.control.fingerprints import canonical_fingerprint
from mr_lister.control.models import PHASE6_MAX_SOURCE_ARTWORK_BYTES
from mr_lister.production.printify import (
    PrintifyCatalogClient,
    PrintifyCatalogMismatchError,
    PrintifyError,
    PrintifyHttpResponse,
    PrintifyInputError,
    PrintifyResolvedProfile,
    PrintifyTransport,
    PrintifyUnavailableError,
    PrintifyUploadedImage,
)

PRINTIFY_API_ORIGIN = "https://api.printify.com"
PRINTIFY_API_BASE_URL = f"{PRINTIFY_API_ORIGIN}/v1/"

SafeProviderId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
CorrelationToken = Annotated[str, StringConstraints(pattern=r"^ml-[a-f0-9]{24}$")]


class _DraftModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DraftVariant(_DraftModel):
    id: int = Field(gt=0)
    price: int = Field(gt=0)
    is_enabled: bool = True
    sku: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class DraftPlacementImage(_DraftModel):
    id: NonEmptyText
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    scale: float = Field(gt=0.0, le=1.0)
    angle: int = Field(ge=-360, le=360)


class DraftPlaceholder(_DraftModel):
    position: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    images: tuple[DraftPlacementImage, ...] = Field(min_length=1)


class DraftPrintArea(_DraftModel):
    variant_ids: tuple[int, ...] = Field(min_length=1)
    placeholders: tuple[DraftPlaceholder, ...] = Field(min_length=1)


class CanonicalPrintifyDraft(_DraftModel):
    """Complete desired provider payload plus a non-provider correlation authority."""

    correlation_token: CorrelationToken
    title: NonEmptyText
    description: NonEmptyText
    tags: tuple[NonEmptyText, ...] = Field(min_length=1)
    blueprint_id: int = Field(gt=0)
    print_provider_id: int = Field(gt=0)
    variants: tuple[DraftVariant, ...] = Field(min_length=1)
    print_areas: tuple[DraftPrintArea, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def correlation_and_variant_coverage_are_complete(self) -> CanonicalPrintifyDraft:
        variant_ids = tuple(variant.id for variant in self.variants)
        if len(set(variant_ids)) != len(variant_ids):
            raise ValueError("Canonical draft variants must have unique IDs")
        for variant in self.variants:
            if not variant.sku.startswith(f"{self.correlation_token}-"):
                raise ValueError("Every canonical variant SKU must carry the job correlation token")
        placed_ids = tuple(
            variant_id for print_area in self.print_areas for variant_id in print_area.variant_ids
        )
        if len(placed_ids) != len(set(placed_ids)) or set(placed_ids) != set(variant_ids):
            raise ValueError("Print areas must cover each canonical variant exactly once")
        return self

    def provider_payload(self) -> dict[str, Any]:
        """Return only fields supported by Printify's product create/update endpoints."""

        return self.model_dump(mode="json", exclude={"correlation_token"})

    @property
    def payload_fingerprint(self) -> str:
        return canonical_fingerprint(self.provider_payload())


class DraftSyncOperation(StrEnum):
    CREATED = "created"
    REPLACED = "replaced"


class CreateReconciliationOutcome(StrEnum):
    ZERO = "zero"
    ONE = "one"
    AMBIGUOUS = "ambiguous"


class UpdateReconciliationOutcome(StrEnum):
    APPLIED = "applied"
    PRIOR_PAYLOAD = "prior_payload"
    CONFLICT = "conflict"


class CreateAmbiguityReason(StrEnum):
    MULTIPLE_CORRELATED_PRODUCTS = "multiple_correlated_products"
    CANONICAL_CONFLICT = "canonical_conflict"


class DraftVariantEconomics(_DraftModel):
    variant_id: int = Field(gt=0, strict=True)
    retail_price_cents: int = Field(gt=0, strict=True)
    production_cost_cents: int = Field(ge=0, strict=True)


class DraftSynchronizationEvidence(_DraftModel):
    """Provider evidence for the application to validate and persist; never state authority."""

    operation: DraftSyncOperation
    product_id: SafeProviderId
    image_id: NonEmptyText
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    response_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_locked: bool
    provider_published: bool
    variants: tuple[DraftVariantEconomics, ...] = Field(min_length=1)
    mockups: tuple[ProductMockupEvidence, ...] = ()

    @model_validator(mode="after")
    def enabled_variants_are_unique(self) -> DraftSynchronizationEvidence:
        variant_ids = tuple(item.variant_id for item in self.variants)
        if len(variant_ids) != len(set(variant_ids)):
            raise ValueError("Provider variant economics must have unique IDs")
        return self

    @property
    def payload_fingerprint(self) -> str:
        """Alias the request fingerprint to the frozen ProductSyncRecord field name."""

        return self.request_fingerprint

    @property
    def enabled_variant_economics(self) -> tuple[DraftVariantEconomics, ...]:
        """Describe the ProductSyncRecord-compatible ``variants`` evidence explicitly."""

        return self.variants


class CreateReconciliationResult(_DraftModel):
    outcome: CreateReconciliationOutcome
    evidence: DraftSynchronizationEvidence | None = None
    ambiguity_reason: CreateAmbiguityReason | None = None

    @model_validator(mode="after")
    def result_shape_matches_outcome(self) -> CreateReconciliationResult:
        if self.outcome is CreateReconciliationOutcome.ONE:
            if self.evidence is None or self.ambiguity_reason is not None:
                raise ValueError("One-product reconciliation requires only product evidence")
        elif self.outcome is CreateReconciliationOutcome.AMBIGUOUS:
            if self.evidence is not None or self.ambiguity_reason is None:
                raise ValueError("Ambiguous reconciliation requires only a stable reason")
        elif self.evidence is not None or self.ambiguity_reason is not None:
            raise ValueError("Zero-product reconciliation has no product evidence")
        return self


class UpdateReconciliationResult(_DraftModel):
    outcome: UpdateReconciliationOutcome
    evidence: DraftSynchronizationEvidence | None = None

    @model_validator(mode="after")
    def evidence_only_confirms_applied_target(self) -> UpdateReconciliationResult:
        if (self.outcome is UpdateReconciliationOutcome.APPLIED) != (self.evidence is not None):
            raise ValueError("Only an applied update carries synchronized product evidence")
        return self


class PrintifyCreateOutcomeUnknown(PrintifyError):
    """The one authorized POST may have succeeded and must only be reconciled."""


class PrintifyUpdateOutcomeUnknown(PrintifyError):
    """The PUT may have succeeded and must be reconciled against the same product."""


class PrintifyUploadOutcomeUnknown(PrintifyError):
    """The only authorized artwork POST may have succeeded and requires GET-only recovery."""


_COLLECTION_PATH = re.compile(r"^shops/([1-9][0-9]*)/products\.json$")
_PRODUCT_PATH = re.compile(
    r"^shops/([1-9][0-9]*)/products/([A-Za-z0-9][A-Za-z0-9_-]{0,127})\.json$"
)
_SHOP_PATH = re.compile(r"^shops\.json$")
_BLUEPRINTS_PATH = re.compile(r"^catalog/blueprints\.json$")
_PROVIDERS_PATH = re.compile(r"^catalog/blueprints/([1-9][0-9]*)/print_providers\.json$")
_VARIANTS_PATH = re.compile(
    r"^catalog/blueprints/([1-9][0-9]*)/print_providers/"
    r"([1-9][0-9]*)/variants\.json$"
)
_UPLOAD_PATH = re.compile(r"^uploads/images\.json$")
_UPLOAD_COLLECTION_PATH = re.compile(r"^uploads\.json$")
_UPLOAD_ITEM_PATH = re.compile(r"^uploads/([A-Za-z0-9][A-Za-z0-9_-]{0,127})\.json$")
_UPLOAD_FILE_NAME = re.compile(r"^mr-lister-[a-f0-9]{24}-[a-f0-9]{16}\.png$")
_MAX_RECONCILIATION_PAGES = 20


def assert_draft_only_route(*, method: str, path: str) -> None:
    """Fail closed unless a method/path pair belongs to the draft-only product surface."""

    normalized_method = method.upper()
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path.startswith("/"):
        raise PrintifyInputError("Printify draft route must be a relative API path")
    normalized_path = parsed.path
    collection = _COLLECTION_PATH.fullmatch(normalized_path)
    product = _PRODUCT_PATH.fullmatch(normalized_path)
    upload_collection = _UPLOAD_COLLECTION_PATH.fullmatch(normalized_path)
    upload_item = _UPLOAD_ITEM_PATH.fullmatch(normalized_path)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if collection and normalized_method == "POST" and not query:
        return
    if collection and normalized_method == "GET" and query.get("limit") == ["50"]:
        page = query.get("page", [""])
        if len(query) == 2 and len(page) == 1 and page[0].isdigit():
            page_number = int(page[0])
            if 1 <= page_number <= _MAX_RECONCILIATION_PAGES:
                return
    if product and normalized_method in {"GET", "PUT"} and not query:
        return
    if upload_item and normalized_method == "GET" and not query:
        return
    if upload_collection and normalized_method == "GET" and query.get("limit") == ["100"]:
        page = query.get("page", [""])
        if len(query) == 2 and len(page) == 1 and page[0].isdigit():
            page_number = int(page[0])
            if 1 <= page_number <= _MAX_RECONCILIATION_PAGES:
                return
    if (
        normalized_method == "GET"
        and not query
        and any(
            pattern.fullmatch(normalized_path)
            for pattern in (_SHOP_PATH, _BLUEPRINTS_PATH, _PROVIDERS_PATH, _VARIANTS_PATH)
        )
    ):
        return
    if normalized_method == "POST" and not query and _UPLOAD_PATH.fullmatch(normalized_path):
        return
    raise PrintifyInputError("Printify request is outside the draft-only product boundary")


def assert_printify_api_url(*, method: str, url: str) -> None:
    """Require the exact HTTPS Printify API origin and a draft-only v1 route."""

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise PrintifyInputError("Printify API URL was malformed") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.printify.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.path.startswith("/v1/")
    ):
        raise PrintifyInputError("Printify API request escaped the exact HTTPS API origin")
    route = parsed.path.removeprefix("/v1/")
    if parsed.query:
        route = f"{route}?{parsed.query}"
    assert_draft_only_route(method=method, path=route)


class _RejectRedirectHandler(HTTPRedirectHandler):
    """Never construct a second request that could inherit a bearer header."""

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


class RedirectSafePrintifyTransport:
    """urllib transport that validates origin and returns 3xx without following it."""

    MAX_RESPONSE_BYTES = 8 * 1024 * 1024

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
    ) -> PrintifyHttpResponse:
        assert_printify_api_url(method=method, url=url)
        request = Request(url=url, headers=headers, data=body, method=method)
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                return PrintifyHttpResponse(
                    status=response.status,
                    body=self._read_bounded(response),
                )
        except HTTPError as error:
            return PrintifyHttpResponse(status=error.code, body=self._read_bounded(error))
        except (URLError, TimeoutError) as error:
            raise PrintifyUnavailableError("Printify request did not complete") from error

    def _read_bounded(self, stream: Any) -> bytes:
        body = stream.read(self.MAX_RESPONSE_BYTES + 1)
        if len(body) > self.MAX_RESPONSE_BYTES:
            raise PrintifyUnavailableError("Printify response exceeded the safe read boundary")
        return body


class PrintifyDraftOnlyClient(PrintifyCatalogClient):
    """Printify client with no publication, deletion, order, fulfillment, or webhook method."""

    MAX_BASE64_SOURCE_BYTES = PHASE6_MAX_SOURCE_ARTWORK_BYTES

    def __init__(
        self,
        *,
        token_provider: Callable[[], str],
        user_agent: str = "MrLister",
        base_url: str = PRINTIFY_API_BASE_URL,
        timeout_seconds: float = 15.0,
        transport: PrintifyTransport | None = None,
    ) -> None:
        if base_url != PRINTIFY_API_BASE_URL:
            raise ValueError("Phase 6 Printify client requires the exact production API base URL")
        super().__init__(
            token_provider=token_provider,
            user_agent=user_agent,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            transport=transport or RedirectSafePrintifyTransport(),
        )

    def create_draft(self, *, shop_id: int, draft: CanonicalPrintifyDraft) -> dict[str, Any]:
        path = self._collection_path(shop_id)
        try:
            payload = self._request_json(method="POST", path=path, payload=draft.provider_payload())
        except (PrintifyCatalogMismatchError, PrintifyUnavailableError) as error:
            # Even a provider 4xx or malformed response is reconciled after a POST.  The stricter
            # classification costs a read but guarantees an unexpected provider response can
            # never become authority for a blind second create.
            raise PrintifyCreateOutcomeUnknown(
                "Initial product creation outcome is unknown; reconcile without another POST"
            ) from error
        try:
            return self._parse_product(payload, expected_product_id=None)
        except PrintifyCatalogMismatchError as error:
            raise PrintifyCreateOutcomeUnknown(
                "Initial product creation returned unconfirmed evidence; "
                "reconcile without another POST"
            ) from error

    def upload_artwork_contents(
        self, *, file_name: str, content_type: str, content: bytes
    ) -> PrintifyUploadedImage:
        """Upload one bounded PNG through the same redirect-safe credential boundary."""

        self._require_upload_file_name(file_name)
        if content_type != "image/png":
            raise PrintifyInputError("Phase 6 artwork upload requires PNG content")
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise PrintifyInputError("Artwork does not contain a valid PNG signature")
        if not content or len(content) > self.MAX_BASE64_SOURCE_BYTES:
            raise PrintifyInputError("Artwork exceeds the safe base64 upload boundary")
        try:
            payload = self._request_json(
                method="POST",
                path="uploads/images.json",
                payload={"file_name": file_name, "contents": b64encode(content).decode("ascii")},
            )
            return self._parse_upload(payload)
        except (PrintifyCatalogMismatchError, PrintifyUnavailableError) as error:
            raise PrintifyUploadOutcomeUnknown(
                "Artwork upload outcome is unknown; reconcile without another POST"
            ) from error

    def get_upload(self, *, image_id: str) -> PrintifyUploadedImage:
        if not isinstance(image_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", image_id
        ):
            raise PrintifyInputError("Printify image ID is invalid")
        payload = self._request_json(method="GET", path=f"uploads/{image_id}.json")
        upload = self._parse_upload(payload)
        if upload.image_id != image_id:
            raise PrintifyCatalogMismatchError("Printify upload response changed image identity")
        return upload

    def list_uploads(self) -> tuple[PrintifyUploadedImage, ...]:
        """Read a bounded exact upload listing for deterministic-name reconciliation."""

        uploads: list[PrintifyUploadedImage] = []
        image_ids: set[str] = set()
        for page in range(1, _MAX_RECONCILIATION_PAGES + 1):
            payload = self._request_json(method="GET", path=f"uploads.json?page={page}&limit=100")
            last_page = None
            if isinstance(payload, dict):
                last_page = payload.get("last_page")
                payload = payload.get("data")
            if not isinstance(payload, list):
                raise PrintifyCatalogMismatchError("Printify upload list was not a collection")
            for item in payload:
                upload = self._parse_upload(item)
                if upload.image_id in image_ids:
                    raise PrintifyCatalogMismatchError(
                        "Printify pagination repeated an upload identity"
                    )
                image_ids.add(upload.image_id)
                uploads.append(upload)
            if last_page is not None:
                if (
                    isinstance(last_page, bool)
                    or not isinstance(last_page, int)
                    or last_page < page
                    or last_page > _MAX_RECONCILIATION_PAGES
                ):
                    raise PrintifyCatalogMismatchError(
                        "Printify upload pagination metadata was malformed"
                    )
                if page >= last_page:
                    return tuple(uploads)
            elif len(payload) < 100:
                return tuple(uploads)
        raise PrintifyCatalogMismatchError(
            "Printify upload reconciliation exceeded its bounded page limit"
        )

    def replace_draft(
        self,
        *,
        shop_id: int,
        product_id: str,
        draft: CanonicalPrintifyDraft,
    ) -> dict[str, Any]:
        path = self._product_path(shop_id, product_id)
        try:
            payload = self._request_json(method="PUT", path=path, payload=draft.provider_payload())
        except (PrintifyCatalogMismatchError, PrintifyUnavailableError) as error:
            raise PrintifyUpdateOutcomeUnknown(
                "Product update outcome is unknown; reconcile the same product without creating"
            ) from error
        try:
            return self._parse_product(payload, expected_product_id=product_id)
        except PrintifyCatalogMismatchError as error:
            raise PrintifyUpdateOutcomeUnknown(
                "Product update returned unconfirmed evidence; reconcile the same product"
            ) from error

    def get_draft(self, *, shop_id: int, product_id: str) -> dict[str, Any]:
        payload = self._request_json(method="GET", path=self._product_path(shop_id, product_id))
        return self._parse_product(payload, expected_product_id=product_id)

    def list_recent_drafts(self, *, shop_id: int) -> tuple[dict[str, Any], ...]:
        products: list[dict[str, Any]] = []
        product_ids: set[str] = set()
        for page in range(1, _MAX_RECONCILIATION_PAGES + 1):
            path = f"{self._collection_path(shop_id)}?page={page}&limit=50"
            payload = self._request_json(method="GET", path=path)
            last_page = None
            if isinstance(payload, dict):
                last_page = payload.get("last_page")
                payload = payload.get("data")
            if not isinstance(payload, list):
                raise PrintifyCatalogMismatchError("Printify product list was not a collection")
            for item in payload:
                product = self._parse_product(item, expected_product_id=None)
                if product["id"] in product_ids:
                    raise PrintifyCatalogMismatchError(
                        "Printify pagination repeated a product identity"
                    )
                product_ids.add(product["id"])
                products.append(product)
            if last_page is not None:
                if (
                    isinstance(last_page, bool)
                    or not isinstance(last_page, int)
                    or last_page < page
                ):
                    raise PrintifyCatalogMismatchError(
                        "Printify product pagination metadata was malformed"
                    )
                if page >= last_page:
                    return tuple(products)
            elif len(payload) < 50:
                return tuple(products)
        raise PrintifyCatalogMismatchError(
            "Printify product reconciliation exceeded its bounded page limit"
        )

    def _request_json(
        self,
        *,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        # Override the inherited primitive so even accidental internal use cannot bypass the
        # Phase 6 route policy.
        assert_draft_only_route(method=method, path=path)
        assert_printify_api_url(method=method, url=urljoin(PRINTIFY_API_BASE_URL, path))
        return super()._request_json(method=method, path=path, payload=payload)

    @staticmethod
    def _collection_path(shop_id: int) -> str:
        if isinstance(shop_id, bool) or not isinstance(shop_id, int) or shop_id <= 0:
            raise PrintifyInputError("Printify shop ID must be positive")
        return f"shops/{shop_id}/products.json"

    @staticmethod
    def _product_path(shop_id: int, product_id: str) -> str:
        collection = PrintifyDraftOnlyClient._collection_path(shop_id)
        if not isinstance(product_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", product_id
        ):
            raise PrintifyInputError("Printify product ID is invalid")
        return f"{collection.removesuffix('.json')}/{product_id}.json"

    @staticmethod
    def _require_upload_file_name(file_name: str) -> None:
        if not isinstance(file_name, str) or not _UPLOAD_FILE_NAME.fullmatch(file_name):
            raise PrintifyInputError("Phase 6 artwork upload requires its deterministic PNG name")

    @staticmethod
    def _parse_product(payload: Any, expected_product_id: str | None) -> dict[str, Any]:
        product = PrintifyCatalogClient._require_mapping(payload, "product")
        product_id = product.get("id")
        if not isinstance(product_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", product_id
        ):
            raise PrintifyCatalogMismatchError("Printify product response omitted a valid ID")
        if expected_product_id is not None and product_id != expected_product_id:
            raise PrintifyCatalogMismatchError("Printify product response changed product identity")
        return product

    @staticmethod
    def _parse_upload(payload: Any) -> PrintifyUploadedImage:
        upload = PrintifyCatalogClient._require_mapping(payload, "upload response")
        try:
            return PrintifyUploadedImage(
                image_id=upload["id"],
                file_name=upload["file_name"],
                width=upload["width"],
                height=upload["height"],
                size_bytes=upload["size"],
                mime_type=upload["mime_type"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PrintifyCatalogMismatchError(
                "Printify returned an invalid upload record"
            ) from error


class PrintifyDraftSynchronizer:
    """Execute only the provider operation selected by application-owned product authority."""

    def __init__(self, *, client: PrintifyDraftOnlyClient, shop_id: int) -> None:
        if isinstance(shop_id, bool) or not isinstance(shop_id, int) or shop_id <= 0:
            raise ValueError("Printify shop ID must be positive")
        self._client = client
        self._shop_id = shop_id

    def synchronize(
        self,
        *,
        job_id: str,
        draft: CanonicalPrintifyDraft,
        product_id: str | None,
        prior_draft: CanonicalPrintifyDraft | None = None,
    ) -> DraftSynchronizationEvidence:
        """Perform one authorized POST or update the exact application-owned product ID."""

        self._require_job_correlation(job_id, draft)
        if product_id is None:
            if prior_draft is not None:
                raise PrintifyInputError("Initial product creation cannot carry a prior draft")
            created = self._client.create_draft(shop_id=self._shop_id, draft=draft)
            try:
                product = self._client.get_draft(
                    shop_id=self._shop_id,
                    product_id=created["id"],
                )
                self._require_editable_identity(product=product, draft=draft)
                return self._evidence(
                    operation=DraftSyncOperation.CREATED,
                    product=product,
                    draft=draft,
                )
            except (PrintifyCatalogMismatchError, PrintifyUnavailableError) as error:
                raise PrintifyCreateOutcomeUnknown(
                    "Initial product creation readback was incomplete; "
                    "reconcile without another POST"
                ) from error

        if prior_draft is None:
            raise PrintifyInputError("Product updates require the exact prior draft authority")
        self._require_job_correlation(job_id, prior_draft)
        current = self._client.get_draft(shop_id=self._shop_id, product_id=product_id)
        self._require_editable_identity(product=current, draft=draft)
        if not self._contains_canonical(current, prior_draft.provider_payload()):
            raise PrintifyCatalogMismatchError(
                "Printify product no longer matches the exact prior application draft"
            )
        self._client.replace_draft(
            shop_id=self._shop_id,
            product_id=product_id,
            draft=draft,
        )
        try:
            updated = self._client.get_draft(
                shop_id=self._shop_id,
                product_id=product_id,
            )
            self._require_editable_identity(product=updated, draft=draft)
            return self._evidence(
                operation=DraftSyncOperation.REPLACED,
                product=updated,
                draft=draft,
            )
        except (PrintifyCatalogMismatchError, PrintifyUnavailableError) as error:
            raise PrintifyUpdateOutcomeUnknown(
                "Product update readback was incomplete; reconcile the same product"
            ) from error

    def reconcile_initial_create(
        self,
        *,
        job_id: str,
        draft: CanonicalPrintifyDraft,
    ) -> CreateReconciliationResult:
        """Classify GET-only initial-create evidence without ever authorizing another POST."""

        self._require_job_correlation(job_id, draft)
        products = self._client.list_recent_drafts(shop_id=self._shop_id)
        correlated = [
            product
            for product in products
            if self._product_carries_token(product, draft.correlation_token)
        ]
        if not correlated:
            return CreateReconciliationResult(outcome=CreateReconciliationOutcome.ZERO)
        if len(correlated) != 1:
            return CreateReconciliationResult(
                outcome=CreateReconciliationOutcome.AMBIGUOUS,
                ambiguity_reason=CreateAmbiguityReason.MULTIPLE_CORRELATED_PRODUCTS,
            )
        # The collection response is sufficient only for correlation.  Read the exact candidate
        # before comparing the complete canonical payload and collecting economics/mockups.
        product = self._client.get_draft(
            shop_id=self._shop_id,
            product_id=correlated[0]["id"],
        )
        try:
            self._require_editable_identity(product=product, draft=draft)
        except PrintifyCatalogMismatchError:
            return CreateReconciliationResult(
                outcome=CreateReconciliationOutcome.AMBIGUOUS,
                ambiguity_reason=CreateAmbiguityReason.CANONICAL_CONFLICT,
            )
        if not self._contains_canonical(product, draft.provider_payload()):
            return CreateReconciliationResult(
                outcome=CreateReconciliationOutcome.AMBIGUOUS,
                ambiguity_reason=CreateAmbiguityReason.CANONICAL_CONFLICT,
            )
        try:
            evidence = self._evidence(
                operation=DraftSyncOperation.CREATED,
                product=product,
                draft=draft,
            )
        except PrintifyCatalogMismatchError:
            return CreateReconciliationResult(
                outcome=CreateReconciliationOutcome.AMBIGUOUS,
                ambiguity_reason=CreateAmbiguityReason.CANONICAL_CONFLICT,
            )
        return CreateReconciliationResult(
            outcome=CreateReconciliationOutcome.ONE,
            evidence=evidence,
        )

    def reconcile_update(
        self,
        *,
        job_id: str,
        product_id: str,
        target_draft: CanonicalPrintifyDraft,
        prior_draft: CanonicalPrintifyDraft,
    ) -> UpdateReconciliationResult:
        """GET one immutable product and classify an ambiguous PUT without writing."""

        self._require_job_correlation(job_id, target_draft)
        self._require_job_correlation(job_id, prior_draft)
        product = self._client.get_draft(shop_id=self._shop_id, product_id=product_id)
        self._require_editable_identity(product=product, draft=target_draft)
        if self._contains_canonical(product, target_draft.provider_payload()):
            try:
                evidence = self._evidence(
                    operation=DraftSyncOperation.REPLACED,
                    product=product,
                    draft=target_draft,
                )
            except PrintifyCatalogMismatchError:
                return UpdateReconciliationResult(outcome=UpdateReconciliationOutcome.CONFLICT)
            return UpdateReconciliationResult(
                outcome=UpdateReconciliationOutcome.APPLIED,
                evidence=evidence,
            )
        if self._contains_canonical(product, prior_draft.provider_payload()):
            return UpdateReconciliationResult(outcome=UpdateReconciliationOutcome.PRIOR_PAYLOAD)
        return UpdateReconciliationResult(outcome=UpdateReconciliationOutcome.CONFLICT)

    @staticmethod
    def _require_job_correlation(job_id: str, draft: CanonicalPrintifyDraft) -> None:
        if not isinstance(job_id, str) or not job_id.strip():
            raise PrintifyInputError("A job ID is required for product synchronization")
        if draft.correlation_token != job_correlation_token(job_id):
            raise PrintifyInputError("Canonical draft does not belong to the supplied job")

    def _require_editable_identity(
        self, *, product: dict[str, Any], draft: CanonicalPrintifyDraft
    ) -> None:
        shop_id = product.get("shop_id")
        if shop_id is not None and shop_id != self._shop_id:
            raise PrintifyCatalogMismatchError("Printify product belongs to another shop")
        if product.get("blueprint_id") != draft.blueprint_id:
            raise PrintifyCatalogMismatchError("Printify product blueprint changed")
        if product.get("print_provider_id") != draft.print_provider_id:
            raise PrintifyCatalogMismatchError("Printify product provider changed")
        if self._locked(product) or self._published(product):
            raise PrintifyCatalogMismatchError(
                "Printify product is not an editable unpublished draft"
            )

    @staticmethod
    def _product_carries_token(product: dict[str, Any], token: str) -> bool:
        variants = product.get("variants")
        if not isinstance(variants, list):
            return False
        prefix = f"{token}-"
        return any(
            isinstance(variant, dict)
            and isinstance((sku := variant.get("sku")), str)
            and sku.startswith(prefix)
            for variant in variants
        )

    @classmethod
    def _evidence(
        cls,
        *,
        operation: DraftSyncOperation,
        product: dict[str, Any],
        draft: CanonicalPrintifyDraft,
    ) -> DraftSynchronizationEvidence:
        canonical_payload = draft.provider_payload()
        if not cls._contains_canonical(product, canonical_payload):
            raise PrintifyCatalogMismatchError(
                "Printify product did not match the complete canonical payload"
            )
        variant_economics = cls._variant_economics(product, draft)
        image_id = cls._canonical_image_id(draft)
        mockups = cls._mockups(product, expected_variant_ids={item.id for item in draft.variants})
        provider_locked = cls._locked(product)
        provider_published = cls._published(product)
        response_fingerprint = canonical_fingerprint(
            {
                "product_id": product["id"],
                "shop_id": product.get("shop_id"),
                "canonical_readback": {key: product[key] for key in canonical_payload},
                "image_id": image_id,
                "variants": [item.model_dump(mode="json") for item in variant_economics],
                "mockups": [item.model_dump(mode="json") for item in mockups],
                "provider_locked": provider_locked,
                "provider_published": provider_published,
            }
        )
        return DraftSynchronizationEvidence(
            operation=operation,
            product_id=product["id"],
            image_id=image_id,
            request_fingerprint=draft.payload_fingerprint,
            response_fingerprint=response_fingerprint,
            provider_locked=provider_locked,
            provider_published=provider_published,
            variants=variant_economics,
            mockups=mockups,
        )

    @staticmethod
    def _variant_economics(
        product: dict[str, Any], draft: CanonicalPrintifyDraft
    ) -> tuple[DraftVariantEconomics, ...]:
        raw_variants = product.get("variants")
        if not isinstance(raw_variants, list):
            raise PrintifyCatalogMismatchError("Printify product variants were malformed")
        by_id: dict[int, dict[str, Any]] = {}
        for raw_variant in raw_variants:
            if not isinstance(raw_variant, dict):
                raise PrintifyCatalogMismatchError("Printify product variant was malformed")
            variant_id = raw_variant.get("id")
            if isinstance(variant_id, bool) or not isinstance(variant_id, int) or variant_id <= 0:
                raise PrintifyCatalogMismatchError("Printify product variant ID was malformed")
            if variant_id in by_id:
                raise PrintifyCatalogMismatchError("Printify product repeated a variant ID")
            by_id[variant_id] = raw_variant
        expected_ids = {variant.id for variant in draft.variants}
        if set(by_id) != expected_ids:
            raise PrintifyCatalogMismatchError("Printify product variant set changed")

        economics = []
        for expected in draft.variants:
            actual = by_id[expected.id]
            price = actual.get("price")
            cost = actual.get("cost")
            enabled = actual.get("is_enabled")
            if (
                isinstance(price, bool)
                or not isinstance(price, int)
                or price <= 0
                or isinstance(cost, bool)
                or not isinstance(cost, int)
                or cost < 0
                or not isinstance(enabled, bool)
                or not enabled
            ):
                raise PrintifyCatalogMismatchError(
                    "Printify enabled variant economics were malformed"
                )
            economics.append(
                DraftVariantEconomics(
                    variant_id=expected.id,
                    retail_price_cents=price,
                    production_cost_cents=cost,
                )
            )
        return tuple(economics)

    @staticmethod
    def _canonical_image_id(draft: CanonicalPrintifyDraft) -> str:
        image_ids = {
            image.id
            for print_area in draft.print_areas
            for placeholder in print_area.placeholders
            for image in placeholder.images
        }
        if len(image_ids) != 1:
            raise PrintifyCatalogMismatchError(
                "Phase 6 synchronization requires one canonical provider image ID"
            )
        return next(iter(image_ids))

    @staticmethod
    def _mockups(
        product: dict[str, Any], *, expected_variant_ids: set[int]
    ) -> tuple[ProductMockupEvidence, ...]:
        images = product.get("images", [])
        if not isinstance(images, list):
            raise PrintifyCatalogMismatchError("Printify product mockups were malformed")
        mockups: list[ProductMockupEvidence] = []
        for image in images:
            if not isinstance(image, dict):
                raise PrintifyCatalogMismatchError("Printify product mockup was malformed")
            source = image.get("src")
            if source is None:
                continue
            if not isinstance(source, str) or not source.strip():
                raise PrintifyCatalogMismatchError("Printify product mockup URL was malformed")
            position = image.get("position")
            if position is not None and not isinstance(position, str):
                raise PrintifyCatalogMismatchError("Printify product mockup position was malformed")
            raw_variant_ids = image.get("variant_ids", [])
            if not isinstance(raw_variant_ids, list) or any(
                type(variant_id) is not int or variant_id <= 0 for variant_id in raw_variant_ids
            ):
                raise PrintifyCatalogMismatchError(
                    "Printify product mockup variants were malformed"
                )
            variant_ids = tuple(raw_variant_ids)
            if len(set(variant_ids)) != len(variant_ids) or not set(variant_ids).issubset(
                expected_variant_ids
            ):
                raise PrintifyCatalogMismatchError(
                    "Printify product mockup variants were malformed"
                )
            if any(mockup.url == source for mockup in mockups):
                raise PrintifyCatalogMismatchError("Printify product repeated a mockup URL")
            try:
                mockups.append(
                    ProductMockupEvidence(
                        url=source,
                        position=position,
                        variant_ids=variant_ids,
                    )
                )
            except ValueError as error:
                raise PrintifyCatalogMismatchError(
                    "Printify product mockup evidence was malformed"
                ) from error
        return tuple(mockups)

    @staticmethod
    def _locked(product: dict[str, Any]) -> bool:
        locked = product.get("is_locked", False)
        if not isinstance(locked, bool):
            raise PrintifyCatalogMismatchError("Printify product lock state was malformed")
        return locked

    @staticmethod
    def _published(product: dict[str, Any]) -> bool:
        # ``visible`` is a draft's sales-channel visibility preference and defaults true; it is
        # not publication evidence.  Printify documents the external object as the linkage to a
        # published sales-channel listing.
        external = product.get("external", {})
        if not isinstance(external, dict):
            raise PrintifyCatalogMismatchError("Printify product external state was malformed")
        return bool(external)

    @classmethod
    def _contains_canonical(cls, actual: Any, expected: Any) -> bool:
        if isinstance(expected, dict):
            return isinstance(actual, dict) and all(
                key in actual and cls._contains_canonical(actual[key], value)
                for key, value in expected.items()
            )
        if isinstance(expected, list):
            return (
                isinstance(actual, list)
                and len(actual) == len(expected)
                and all(
                    cls._contains_canonical(actual_item, expected_item)
                    for actual_item, expected_item in zip(actual, expected, strict=True)
                )
            )
        return actual == expected


def job_correlation_token(job_id: str) -> str:
    """Return a deterministic, non-secret token safe to place in provider variant SKUs."""

    if not isinstance(job_id, str) or not job_id.strip():
        raise PrintifyInputError("A job ID is required for product correlation")
    digest = sha256(f"mr-lister:provider-draft:{job_id}".encode()).hexdigest()[:24]
    return f"ml-{digest}"


def build_canonical_draft(
    *,
    job_id: str,
    listing: ListingIntelligence,
    profile: ProductProfile,
    resolved: PrintifyResolvedProfile,
    image_id: str,
) -> CanonicalPrintifyDraft:
    """Build the Phase 6 create/replace payload from the proven Phase 5 contracts."""

    if profile.publish_enabled:
        raise PrintifyInputError("Product synchronization requires publication to remain disabled")
    if not isinstance(image_id, str) or not image_id.strip():
        raise PrintifyInputError("A Printify image ID is required")
    if (
        resolved.profile_id != profile.profile_id
        or resolved.profile_version != profile.profile_version
        or resolved.blueprint_id != profile.blueprint_id
        or resolved.print_provider_id != profile.print_provider_id
    ):
        raise PrintifyInputError("Resolved catalog does not match the product profile")

    token = job_correlation_token(job_id)
    variants = tuple(
        DraftVariant(
            id=variant.variant_id,
            price=variant.retail_price_cents,
            is_enabled=True,
            sku=f"{token}-{variant.variant_id}",
        )
        for variant in resolved.variants
    )
    print_areas = tuple(
        DraftPrintArea(
            variant_ids=tuple(
                variant.variant_id
                for variant in resolved.variants
                if variant.placement_group_id == group.group_id
            ),
            placeholders=(
                DraftPlaceholder(
                    position=group.position,
                    images=(
                        DraftPlacementImage(
                            id=image_id,
                            x=group.placement.x,
                            y=group.placement.y,
                            scale=group.placement.scale,
                            angle=group.angle,
                        ),
                    ),
                ),
            ),
        )
        for group in profile.placement_groups
    )
    return CanonicalPrintifyDraft(
        correlation_token=token,
        title=listing.title,
        description=listing.description,
        tags=listing.tags,
        blueprint_id=profile.blueprint_id,
        print_provider_id=profile.print_provider_id,
        variants=variants,
        print_areas=print_areas,
    )
