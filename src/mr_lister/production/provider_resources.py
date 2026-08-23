"""Owner-bound AWS resources for the Phase 6 draft-only Printify worker.

The worker owns provider-call authorization.  This composition owns only fresh,
owner-bound credential resolution, exact-version source reads, and construction of
the already closed Printify clients.  It intentionally exposes no publication,
order, fulfillment, webhook, or deletion surface.
"""

from __future__ import annotations

import re
import sys
from base64 import b64encode
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from typing import IO, Any, Literal, Protocol, cast
from urllib.parse import urlsplit

from pydantic import model_validator

from mr_lister.contracts import ContractModel, ProductProfile
from mr_lister.control.economics import ProductCostEvidence, ProductVariantCostEvidence
from mr_lister.control.models import OwnerId, SourceArtifactRecord
from mr_lister.production.draft_sync import (
    PrintifyDraftOnlyClient,
    PrintifyDraftSynchronizer,
    RedirectSafePrintifyTransport,
    assert_printify_api_url,
)
from mr_lister.production.printify import (
    PrintifyAuthenticationError,
    PrintifyCatalogMismatchError,
    PrintifyHttpResponse,
    PrintifyInputError,
    PrintifyResolvedProfile,
    PrintifyTransport,
    PrintifyUploadedImage,
)
from mr_lister.production.printify_shipping import (
    PrintifyV2StandardShippingClient,
    ReadOnlyPrintifyV2Transport,
    StandardUsShippingEvidence,
    assert_printify_v2_standard_url,
)
from mr_lister.production.settings import PrintifyConnection

ProviderAuditPath = Literal[
    "/v1/shops.json",
    "/v1/catalog/blueprints.json",
    "/v1/catalog/blueprints/{blueprint_id}/print_providers.json",
    ("/v1/catalog/blueprints/{blueprint_id}/print_providers/{print_provider_id}/variants.json"),
    "/v1/shops/{shop_id}/products.json",
    "/v1/shops/{shop_id}/products/{product_id}.json",
    "/v1/uploads/images.json",
    "/v1/uploads.json",
    "/v1/uploads/{image_id}.json",
    (
        "/v2/catalog/blueprints/{blueprint_id}/print_providers/"
        "{print_provider_id}/shipping/standard.json"
    ),
]
ProviderAuditRecordPath = ProviderAuditPath | Literal["/outside-draft-boundary"]

_OWNER_ID = re.compile(r"^[a-f0-9]{64}$")
_UPLOAD_FILE_NAME = re.compile(r"^mr-lister-[a-f0-9]{24}-[a-f0-9]{16}\.png$")
_SAFE_USER_AGENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ /-]{0,127}$")
_V1_AUDIT_PATHS: tuple[tuple[re.Pattern[str], ProviderAuditPath], ...] = (
    (re.compile(r"^/v1/shops\.json$"), "/v1/shops.json"),
    (re.compile(r"^/v1/catalog/blueprints\.json$"), "/v1/catalog/blueprints.json"),
    (
        re.compile(r"^/v1/catalog/blueprints/[1-9][0-9]*/print_providers\.json$"),
        "/v1/catalog/blueprints/{blueprint_id}/print_providers.json",
    ),
    (
        re.compile(
            r"^/v1/catalog/blueprints/[1-9][0-9]*/print_providers/"
            r"[1-9][0-9]*/variants\.json$"
        ),
        ("/v1/catalog/blueprints/{blueprint_id}/print_providers/{print_provider_id}/variants.json"),
    ),
    (
        re.compile(r"^/v1/shops/[1-9][0-9]*/products\.json$"),
        "/v1/shops/{shop_id}/products.json",
    ),
    (
        re.compile(
            r"^/v1/shops/[1-9][0-9]*/products/"
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\.json$"
        ),
        "/v1/shops/{shop_id}/products/{product_id}.json",
    ),
    (re.compile(r"^/v1/uploads/images\.json$"), "/v1/uploads/images.json"),
    (re.compile(r"^/v1/uploads\.json$"), "/v1/uploads.json"),
    (
        re.compile(r"^/v1/uploads/[A-Za-z0-9][A-Za-z0-9_-]{0,127}\.json$"),
        "/v1/uploads/{image_id}.json",
    ),
)
_V2_STANDARD_SHIPPING_AUDIT_PATH: ProviderAuditPath = (
    "/v2/catalog/blueprints/{blueprint_id}/print_providers/"
    "{print_provider_id}/shipping/standard.json"
)


class OwnerPrintifyConnection(PrintifyConnection):
    """One resolved credential that proves its exact seller owner binding."""

    owner_id: OwnerId


class OwnerPrintifyConnectionResolver(Protocol):
    """Resolve one owner-specific connection without sharing an unbound secret."""

    def resolve(self, *, owner_id: str) -> OwnerPrintifyConnection: ...


class VersionedSourceObjectClient(Protocol):
    def get_object(self, **kwargs: Any) -> Mapping[str, Any]: ...


class ProviderRequestAuditRecord(ContractModel):
    """One identifier-free allowed or rejected provider-boundary attempt."""

    outcome: Literal["allowed", "rejected"] = "allowed"
    method: Literal["GET", "POST", "PUT", "REJECTED"]
    path: ProviderAuditRecordPath

    @model_validator(mode="after")
    def outcome_matches_the_sanitized_route(self) -> ProviderRequestAuditRecord:
        is_rejected_shape = self.method == "REJECTED" and self.path == "/outside-draft-boundary"
        if (self.outcome == "rejected") != is_rejected_shape:
            raise ValueError("Provider audit outcome and route classification do not match")
        return self


class ProviderRequestAuditSink(Protocol):
    def write(self, record: ProviderRequestAuditRecord) -> None: ...


class LoggingProviderRequestAuditSink:
    """Emit the bounded provider-request contract for CloudWatch capture."""

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = stream or sys.stdout

    def write(self, record: ProviderRequestAuditRecord) -> None:
        print(
            f"provider_request_audit={record.model_dump_json()}",
            file=self._stream,
            flush=True,
        )


class SanitizedProviderAuditTransport:
    """Audit a validated route template without exposing request headers or bodies."""

    def __init__(
        self,
        *,
        transport: PrintifyTransport,
        audit_sink: ProviderRequestAuditSink,
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
    ) -> PrintifyHttpResponse:
        try:
            record = provider_request_audit_record(method=method, url=url)
        except Exception:
            self._write_audit(
                ProviderRequestAuditRecord(
                    outcome="rejected",
                    method="REJECTED",
                    path="/outside-draft-boundary",
                )
            )
            raise PrintifyInputError(
                "Printify request is outside the audited draft-only boundary"
            ) from None
        self._write_audit(record)
        return self._transport.request(
            method=method,
            url=url,
            headers=headers,
            body=body,
            timeout_seconds=timeout_seconds,
        )

    def _write_audit(self, record: ProviderRequestAuditRecord) -> None:
        try:
            self._audit_sink.write(record)
        except Exception:
            raise PrintifyInputError("Provider request audit is unavailable") from None


class OwnerBoundProviderDraftResources:
    """Concrete, rotation-safe resources for ``ProviderDraftResources``."""

    def __init__(
        self,
        *,
        connection_resolver: OwnerPrintifyConnectionResolver,
        s3_client: VersionedSourceObjectClient,
        artifact_bucket: str,
        bucket_owner_account_id: str,
        audit_sink: ProviderRequestAuditSink | None = None,
        v1_transport_factory: Callable[[], PrintifyTransport] | None = None,
        v2_transport_factory: Callable[[], PrintifyTransport] | None = None,
        clock: Callable[[], datetime] | None = None,
        user_agent: str = "MrLister-Phase6",
        timeout_seconds: float = 15.0,
    ) -> None:
        if not artifact_bucket or not artifact_bucket.isascii() or "/" in artifact_bucket:
            raise ValueError("Artifact bucket configuration is invalid")
        if (
            len(bucket_owner_account_id) != 12
            or not bucket_owner_account_id.isascii()
            or not bucket_owner_account_id.isdigit()
        ):
            raise ValueError("Artifact bucket owner configuration is invalid")
        normalized_user_agent = user_agent.strip()
        if _SAFE_USER_AGENT.fullmatch(normalized_user_agent) is None:
            raise ValueError("A safe non-empty Printify User-Agent is required")
        if timeout_seconds <= 0:
            raise ValueError("Printify timeout must be positive")
        self._connection_resolver = connection_resolver
        self._s3 = s3_client
        self._artifact_bucket = artifact_bucket
        self._bucket_owner = bucket_owner_account_id
        self._audit_sink = audit_sink or LoggingProviderRequestAuditSink()
        self._v1_transport_factory = v1_transport_factory or RedirectSafePrintifyTransport
        self._v2_transport_factory = v2_transport_factory or ReadOnlyPrintifyV2Transport
        self._clock = clock or (lambda: datetime.now(UTC))
        self._user_agent = normalized_user_agent
        self._timeout_seconds = timeout_seconds

    def preflight(self, *, owner_id: str, profile: ProductProfile) -> PrintifyResolvedProfile:
        connection = self._resolve(owner_id)
        return self._v1_client(connection).preflight(
            shop_id=connection.shop_id,
            profile=profile,
        )

    def upload_source(
        self,
        *,
        owner_id: str,
        source: SourceArtifactRecord,
        file_name: str,
    ) -> PrintifyUploadedImage:
        if _UPLOAD_FILE_NAME.fullmatch(file_name) is None:
            raise PrintifyInputError("Phase 6 artwork upload requires its deterministic PNG name")
        content = self._read_source(owner_id=owner_id, source=source)
        connection = self._resolve(owner_id)
        return self._v1_client(connection).upload_artwork_contents(
            file_name=file_name,
            content_type=source.media_type,
            content=content,
        )

    def list_uploads(self, *, owner_id: str) -> tuple[PrintifyUploadedImage, ...]:
        connection = self._resolve(owner_id)
        return self._v1_client(connection).list_uploads()

    def get_upload(self, *, owner_id: str, image_id: str) -> PrintifyUploadedImage:
        connection = self._resolve(owner_id)
        return self._v1_client(connection).get_upload(image_id=image_id)

    def current_product_costs(
        self,
        *,
        owner_id: str,
        shop_id: int,
        product_id: str,
        product_sync_fingerprint: str,
        variant_ids: tuple[int, ...],
    ) -> ProductCostEvidence:
        connection = self._resolve_for_shop(owner_id=owner_id, shop_id=shop_id)
        product = self._v1_client(connection).get_draft(
            shop_id=shop_id,
            product_id=product_id,
        )
        return parse_current_product_costs(
            product,
            shop_id=shop_id,
            product_sync_fingerprint=product_sync_fingerprint,
            variant_ids=variant_ids,
            observed_at=self._clock(),
        )

    def standard_us_shipping(
        self,
        *,
        owner_id: str,
        blueprint_id: int,
        print_provider_id: int,
        variant_ids: tuple[int, ...],
    ) -> StandardUsShippingEvidence:
        connection = self._resolve(owner_id)
        return self._v2_client(connection).get_us_standard_shipping(
            blueprint_id=blueprint_id,
            print_provider_id=print_provider_id,
            variant_ids=variant_ids,
        )

    def synchronizer(self, *, owner_id: str, shop_id: int) -> PrintifyDraftSynchronizer:
        # The worker requests this before consuming its one-shot product-write permit.  Resolve
        # here, rather than lazily in synchronize(), so a missing credential remains a safe retry.
        connection = self._resolve_for_shop(owner_id=owner_id, shop_id=shop_id)
        return PrintifyDraftSynchronizer(
            client=self._v1_client(connection),
            shop_id=shop_id,
        )

    def _resolve(self, owner_id: str) -> OwnerPrintifyConnection:
        if _OWNER_ID.fullmatch(owner_id) is None:
            raise PrintifyAuthenticationError("Owner-bound Printify credential is unavailable")
        try:
            resolved = self._connection_resolver.resolve(owner_id=owner_id)
            connection = OwnerPrintifyConnection.model_validate(resolved)
        except Exception:
            raise PrintifyAuthenticationError(
                "Owner-bound Printify credential is unavailable"
            ) from None
        if connection.owner_id != owner_id:
            raise PrintifyAuthenticationError("Owner-bound Printify credential is unavailable")
        return connection

    def _resolve_for_shop(self, *, owner_id: str, shop_id: int) -> OwnerPrintifyConnection:
        connection = self._resolve(owner_id)
        if connection.shop_id != shop_id:
            raise PrintifyAuthenticationError("Owner-bound Printify shop is unavailable")
        return connection

    def _v1_client(self, connection: OwnerPrintifyConnection) -> PrintifyDraftOnlyClient:
        token = connection.api_token.get_secret_value()
        transport = SanitizedProviderAuditTransport(
            transport=self._v1_transport_factory(),
            audit_sink=self._audit_sink,
        )
        return PrintifyDraftOnlyClient(
            token_provider=lambda: token,
            user_agent=self._user_agent,
            timeout_seconds=self._timeout_seconds,
            transport=transport,
        )

    def _v2_client(self, connection: OwnerPrintifyConnection) -> PrintifyV2StandardShippingClient:
        token = connection.api_token.get_secret_value()
        transport = SanitizedProviderAuditTransport(
            transport=self._v2_transport_factory(),
            audit_sink=self._audit_sink,
        )
        return PrintifyV2StandardShippingClient(
            token_provider=lambda: token,
            user_agent=self._user_agent,
            timeout_seconds=self._timeout_seconds,
            transport=transport,
            clock=self._clock,
        )

    def _read_source(self, *, owner_id: str, source: SourceArtifactRecord) -> bytes:
        if source.owner_id != owner_id or source.bucket != self._artifact_bucket:
            raise PrintifyInputError("Pinned source does not belong to the configured owner")
        try:
            response = self._s3.get_object(
                Bucket=self._artifact_bucket,
                Key=source.object_key,
                VersionId=source.version_id,
                ChecksumMode="ENABLED",
                ExpectedBucketOwner=self._bucket_owner,
            )
        except Exception:
            raise PrintifyCatalogMismatchError("Pinned source could not be read exactly") from None
        if not isinstance(response, Mapping):
            raise PrintifyCatalogMismatchError("Pinned source response was malformed")
        body = response.get("Body")
        read = getattr(body, "read", None)
        if not callable(read):
            raise PrintifyCatalogMismatchError("Pinned source response was malformed")
        try:
            content = _read_exactly_bounded(cast(_ReadableBody, body), source.size_bytes)
        except Exception:
            raise PrintifyCatalogMismatchError(
                "Pinned source bytes did not match authority"
            ) from None
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        expected_checksum = b64encode(bytes.fromhex(source.content_sha256)).decode("ascii")
        if (
            response.get("VersionId") != source.version_id
            or response.get("ContentLength") != source.size_bytes
            or response.get("ContentType") != source.media_type
            or response.get("ChecksumSHA256") != expected_checksum
            or response.get("ServerSideEncryption") != "AES256"
            or sha256(content).hexdigest() != source.content_sha256
        ):
            raise PrintifyCatalogMismatchError("Pinned source integrity check failed")
        return content


class _ReadableBody(Protocol):
    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


def _read_exactly_bounded(body: _ReadableBody, expected_size: int) -> bytes:
    content = bytearray()
    upper_bound = expected_size + 1
    while len(content) < upper_bound:
        chunk = body.read(min(64 * 1024, upper_bound - len(content)))
        if not isinstance(chunk, bytes):
            raise TypeError("Pinned source body returned non-bytes content")
        if not chunk:
            break
        content.extend(chunk)
    if len(content) != expected_size:
        raise ValueError("Pinned source body size differs from durable authority")
    return bytes(content)


def parse_current_product_costs(
    product: Any,
    *,
    shop_id: int,
    product_sync_fingerprint: str,
    variant_ids: tuple[int, ...],
    observed_at: datetime,
) -> ProductCostEvidence:
    """Validate one exact unpublished product GET as current variant-cost evidence."""

    if not isinstance(product, dict):
        raise PrintifyCatalogMismatchError("Printify product response was malformed")
    observed_shop = product.get("shop_id")
    if observed_shop is not None and observed_shop != shop_id:
        raise PrintifyCatalogMismatchError("Printify product response changed shop identity")
    locked = product.get("is_locked", False)
    external = product.get("external", {})
    if not isinstance(locked, bool) or not isinstance(external, dict):
        raise PrintifyCatalogMismatchError("Printify product draft state was malformed")
    if locked or external:
        raise PrintifyCatalogMismatchError("Printify product is not an editable unpublished draft")
    if (
        not variant_ids
        or len(set(variant_ids)) != len(variant_ids)
        or any(type(variant_id) is not int or variant_id <= 0 for variant_id in variant_ids)
    ):
        raise PrintifyInputError("Configured product variant IDs are invalid")
    raw_variants = product.get("variants")
    if not isinstance(raw_variants, list):
        raise PrintifyCatalogMismatchError("Printify product variants were malformed")
    by_id: dict[int, dict[str, Any]] = {}
    for raw_variant in raw_variants:
        if not isinstance(raw_variant, dict):
            raise PrintifyCatalogMismatchError("Printify product variant was malformed")
        variant_id = raw_variant.get("id")
        if type(variant_id) is not int or variant_id <= 0 or variant_id in by_id:
            raise PrintifyCatalogMismatchError("Printify product variant identity was malformed")
        by_id[variant_id] = raw_variant
    if set(by_id) != set(variant_ids):
        raise PrintifyCatalogMismatchError("Printify product variant set changed")
    variants: list[ProductVariantCostEvidence] = []
    for variant_id in variant_ids:
        raw_variant = by_id[variant_id]
        price = raw_variant.get("price")
        cost = raw_variant.get("cost")
        enabled = raw_variant.get("is_enabled")
        if (
            type(price) is not int
            or price <= 0
            or type(cost) is not int
            or cost < 0
            or enabled is not True
        ):
            raise PrintifyCatalogMismatchError("Printify enabled variant economics were malformed")
        variants.append(
            ProductVariantCostEvidence(
                variant_id=variant_id,
                retail_price_cents=price,
                production_cost_cents=cost,
            )
        )
    try:
        return ProductCostEvidence(
            product_sync_fingerprint=product_sync_fingerprint,
            observed_at=observed_at,
            variants=tuple(variants),
        )
    except ValueError:
        raise PrintifyInputError("Product cost evidence authority is invalid") from None


def provider_request_audit_record(*, method: str, url: str) -> ProviderRequestAuditRecord:
    """Reduce one validated provider URL to an identifier-free route template."""

    parsed = urlsplit(url)
    if parsed.hostname == "api.printify.com" and parsed.path.startswith("/v1/"):
        assert_printify_api_url(method=method, url=url)
        for pattern, template in _V1_AUDIT_PATHS:
            if pattern.fullmatch(parsed.path):
                return ProviderRequestAuditRecord(method=method.upper(), path=template)
    elif parsed.hostname == "api.printify.com" and parsed.path.startswith("/v2/"):
        assert_printify_v2_standard_url(method=method, url=url)
        return ProviderRequestAuditRecord(
            method="GET",
            path=_V2_STANDARD_SHIPPING_AUDIT_PATH,
        )
    raise PrintifyInputError("Printify request is outside the audited draft-only boundary")


__all__ = [
    "LoggingProviderRequestAuditSink",
    "OwnerBoundProviderDraftResources",
    "OwnerPrintifyConnection",
    "OwnerPrintifyConnectionResolver",
    "ProviderAuditPath",
    "ProviderAuditRecordPath",
    "ProviderRequestAuditRecord",
    "ProviderRequestAuditSink",
    "SanitizedProviderAuditTransport",
    "VersionedSourceObjectClient",
    "parse_current_product_costs",
    "provider_request_audit_record",
]
