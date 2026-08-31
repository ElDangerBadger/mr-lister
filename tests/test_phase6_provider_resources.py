from __future__ import annotations

import json
from base64 import b64encode
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO, StringIO
from typing import Any
from urllib.parse import urlsplit

import pytest
from PIL import Image

from mr_lister.contracts import Placement, PlacementGroup, ProductProfile
from mr_lister.control.models import SourceArtifactRecord
from mr_lister.production.draft_sync import PrintifyDraftSynchronizer
from mr_lister.production.printify import (
    PrintifyAuthenticationError,
    PrintifyCatalogMismatchError,
    PrintifyHttpResponse,
    PrintifyInputError,
    PrintifyUploadedImage,
)
from mr_lister.production.provider_resources import (
    LoggingProviderRequestAuditSink,
    OwnerBoundProviderDraftResources,
    OwnerPrintifyConnection,
    ProviderRequestAuditRecord,
    SanitizedProviderAuditTransport,
    parse_current_product_costs,
    provider_request_audit_record,
)

NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
OWNER = "a" * 64
OTHER_OWNER = "b" * 64
BUCKET = "phase6-private"
BUCKET_OWNER = "123456789012"
JOB_ID = "job_provider_resources"
FILE_NAME = "mr-lister-" + "1" * 24 + "-" + "2" * 16 + ".png"


@dataclass(frozen=True)
class ExpectedRequest:
    method: str
    path: str
    payload: object
    status: int = 200


class ScriptedTransport:
    def __init__(self, expected: list[ExpectedRequest]) -> None:
        self.expected = list(expected)
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> PrintifyHttpResponse:
        call = {
            "method": method,
            "url": url,
            "headers": headers,
            "body": body,
            "timeout_seconds": timeout_seconds,
        }
        self.calls.append(call)
        if not self.expected:
            raise AssertionError(f"Unexpected provider request: {method} {url}")
        expected = self.expected.pop(0)
        assert method == expected.method
        assert urlsplit(url).path == expected.path
        return PrintifyHttpResponse(
            status=expected.status,
            body=json.dumps(expected.payload).encode(),
        )


class MemoryAudit:
    def __init__(self) -> None:
        self.records: list[ProviderRequestAuditRecord] = []

    def write(self, record: ProviderRequestAuditRecord) -> None:
        self.records.append(record)


class RotatingResolver:
    def __init__(
        self,
        *,
        owner_id: str = OWNER,
        shop_id: int = 42,
        tokens: tuple[str, ...] = ("token-one",),
    ) -> None:
        self.owner_id = owner_id
        self.shop_id = shop_id
        self.tokens = tokens
        self.calls: list[str] = []

    def resolve(self, *, owner_id: str) -> OwnerPrintifyConnection:
        index = min(len(self.calls), len(self.tokens) - 1)
        self.calls.append(owner_id)
        return OwnerPrintifyConnection(
            owner_id=self.owner_id,
            shop_id=self.shop_id,
            api_token=self.tokens[index],
        )


class TrackingBody:
    def __init__(self, content: bytes) -> None:
        self._stream = BytesIO(content)
        self.read_sizes: list[int] = []
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self._stream.read(size)

    def close(self) -> None:
        self.closed = True
        self._stream.close()


class SourceS3:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response


def _upload_payload(
    *,
    image_id: str = "image_private_1",
    size: int = 100,
    width: int = 1200,
    height: int = 1600,
) -> dict[str, object]:
    return {
        "id": image_id,
        "file_name": FILE_NAME,
        "width": width,
        "height": height,
        "size": size,
        "mime_type": "image/png",
    }


def _source_png() -> bytes:
    image = Image.new("RGBA", (3, 2), (24, 72, 108, 255))
    image.putdata(
        [
            (24, 72, 108, 0),
            (24, 72, 108, 64),
            (24, 72, 108, 128),
            (24, 72, 108, 192),
            (24, 72, 108, 224),
            (24, 72, 108, 255),
        ]
    )
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _shipping_resource(variant_id: int) -> dict[str, object]:
    return {
        "type": "variant_shipping_standard_us",
        "id": str(variant_id),
        "attributes": {
            "shippingType": "standard",
            "country": {"code": "US"},
            "variantId": variant_id,
            "shippingPlanId": "standard-plan",
            "handlingTime": {"from": 4, "to": 8},
            "shippingCost": {
                "firstItem": {"amount": 399, "currency": "USD"},
                "additionalItems": {"amount": 219, "currency": "USD"},
            },
        },
    }


def _profile() -> ProductProfile:
    return ProductProfile(
        profile_id="gildan_64000_swiftpod",
        profile_version=2,
        blueprint_id=145,
        print_provider_id=39,
        colors=("Black",),
        sizes=("S",),
        retail_price_cents=2999,
        placement_groups=(
            PlacementGroup(
                group_id="standard",
                sizes=("S",),
                canvas_width=3021,
                canvas_height=3927,
                placement=Placement(x=0.5, y=0.25, scale=0.65),
            ),
        ),
    )


def _source(content: bytes) -> SourceArtifactRecord:
    digest = sha256(content).hexdigest()
    return SourceArtifactRecord(
        job_id=JOB_ID,
        owner_id=OWNER,
        fingerprint="3" * 64,
        bucket=BUCKET,
        object_key=f"private/owners/{OWNER}/jobs/{JOB_ID}/source/source.png",
        version_id="version-exact-1",
        content_sha256=digest,
        size_bytes=len(content),
        media_type="image/png",
        product_profile_id="gildan_64000_swiftpod",
        product_profile_version=2,
        product_profile_fingerprint="4" * 64,
        created_at=NOW,
    )


def _s3_response(source: SourceArtifactRecord, body: TrackingBody) -> dict[str, Any]:
    return {
        "Body": body,
        "VersionId": source.version_id,
        "ContentLength": source.size_bytes,
        "ContentType": source.media_type,
        "ChecksumSHA256": b64encode(bytes.fromhex(source.content_sha256)).decode("ascii"),
        "ServerSideEncryption": "AES256",
    }


def _resources(
    *,
    resolver: RotatingResolver,
    transport: ScriptedTransport,
    audit: MemoryAudit,
    s3: SourceS3 | None = None,
    v2_transport: ScriptedTransport | None = None,
) -> OwnerBoundProviderDraftResources:
    return OwnerBoundProviderDraftResources(
        connection_resolver=resolver,
        s3_client=s3 or SourceS3({}),
        artifact_bucket=BUCKET,
        bucket_owner_account_id=BUCKET_OWNER,
        audit_sink=audit,
        v1_transport_factory=lambda: transport,
        v2_transport_factory=lambda: v2_transport or transport,
        clock=lambda: NOW,
    )


def test_connection_resolution_is_owner_bound_fresh_and_rotation_safe() -> None:
    responses = [
        ExpectedRequest("GET", "/v1/uploads/image_private_1.json", _upload_payload()),
        ExpectedRequest("GET", "/v1/uploads/image_private_1.json", _upload_payload()),
    ]
    transport = ScriptedTransport(responses)
    resolver = RotatingResolver(tokens=("token-one", "token-two"))
    audit = MemoryAudit()
    resources = _resources(resolver=resolver, transport=transport, audit=audit)

    first = resources.get_upload(owner_id=OWNER, image_id="image_private_1")
    second = resources.get_upload(owner_id=OWNER, image_id="image_private_1")

    assert first == second
    assert resolver.calls == [OWNER, OWNER]
    assert [call["headers"]["Authorization"] for call in transport.calls] == [
        "Bearer token-one",
        "Bearer token-two",
    ]
    assert [record.model_dump() for record in audit.records] == [
        {
            "outcome": "allowed",
            "method": "GET",
            "path": "/v1/uploads/{image_id}.json",
        },
        {
            "outcome": "allowed",
            "method": "GET",
            "path": "/v1/uploads/{image_id}.json",
        },
    ]
    serialized_audit = "".join(record.model_dump_json() for record in audit.records)
    assert "token-" not in serialized_audit
    assert "image_private_1" not in serialized_audit
    assert OWNER not in serialized_audit


def test_resolver_cannot_return_another_owners_connection() -> None:
    resolver = RotatingResolver(owner_id=OTHER_OWNER)
    transport = ScriptedTransport([])
    audit = MemoryAudit()
    resources = _resources(resolver=resolver, transport=transport, audit=audit)

    with pytest.raises(PrintifyAuthenticationError, match="unavailable"):
        resources.list_uploads(owner_id=OWNER)

    assert resolver.calls == [OWNER]
    assert transport.calls == []
    assert audit.records == []


def test_requested_shop_must_match_the_owner_bound_connection() -> None:
    resolver = RotatingResolver(shop_id=42)
    transport = ScriptedTransport([])
    audit = MemoryAudit()
    resources = _resources(resolver=resolver, transport=transport, audit=audit)

    with pytest.raises(PrintifyAuthenticationError, match="shop"):
        resources.synchronizer(owner_id=OWNER, shop_id=43)

    assert transport.calls == []
    assert audit.records == []


def test_preflight_uses_the_owner_shop_and_audits_only_catalog_templates() -> None:
    transport = ScriptedTransport(
        [
            ExpectedRequest("GET", "/v1/shops.json", [{"id": 42}]),
            ExpectedRequest("GET", "/v1/catalog/blueprints.json", [{"id": 145}]),
            ExpectedRequest(
                "GET",
                "/v1/catalog/blueprints/145/print_providers.json",
                [{"id": 39}],
            ),
            ExpectedRequest(
                "GET",
                "/v1/catalog/blueprints/145/print_providers/39/variants.json",
                {
                    "variants": [
                        {
                            "id": 1001,
                            "options": {"color": "Black", "size": "S"},
                            "placeholders": [
                                {
                                    "position": "front",
                                    "decoration_method": "dtg",
                                    "width": 3021,
                                    "height": 3927,
                                }
                            ],
                        }
                    ]
                },
            ),
        ]
    )
    resolver = RotatingResolver()
    audit = MemoryAudit()
    resources = _resources(resolver=resolver, transport=transport, audit=audit)

    resolved = resources.preflight(owner_id=OWNER, profile=_profile())

    assert resolved.shop_id == 42
    assert resolved.variants[0].variant_id == 1001
    assert resolver.calls == [OWNER]
    assert [record.path for record in audit.records] == [
        "/v1/shops.json",
        "/v1/catalog/blueprints.json",
        "/v1/catalog/blueprints/{blueprint_id}/print_providers.json",
        ("/v1/catalog/blueprints/{blueprint_id}/print_providers/{print_provider_id}/variants.json"),
    ]


def test_upload_reads_only_the_exact_s3_version_and_bounds_the_body() -> None:
    content = _source_png()
    source = _source(content)
    body = TrackingBody(content)
    s3 = SourceS3(_s3_response(source, body))
    transport = ScriptedTransport(
        [
            ExpectedRequest(
                "POST",
                "/v1/uploads/images.json",
                _upload_payload(size=len(content), width=3, height=2),
            )
        ]
    )
    resolver = RotatingResolver(tokens=("rotated-token",))
    audit = MemoryAudit()
    resources = _resources(resolver=resolver, transport=transport, audit=audit, s3=s3)

    uploaded = resources.upload_source(owner_id=OWNER, source=source, file_name=FILE_NAME)

    assert uploaded.image_id == "image_private_1"
    assert s3.calls == [
        {
            "Bucket": BUCKET,
            "Key": source.object_key,
            "VersionId": "version-exact-1",
            "ChecksumMode": "ENABLED",
            "ExpectedBucketOwner": BUCKET_OWNER,
        }
    ]
    assert body.closed is True
    assert max(body.read_sizes) <= source.size_bytes + 1
    assert resolver.calls == [OWNER]
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer rotated-token"
    assert audit.records == [
        ProviderRequestAuditRecord(method="POST", path="/v1/uploads/images.json")
    ]
    audit_json = audit.records[0].model_dump_json()
    assert source.content_sha256 not in audit_json
    assert b64encode(content).decode("ascii") not in audit_json
    assert "rotated-token" not in audit_json


def test_upload_rejects_provider_geometry_that_differs_from_the_exact_png() -> None:
    content = _source_png()
    source = _source(content)
    body = TrackingBody(content)
    transport = ScriptedTransport(
        [
            ExpectedRequest(
                "POST",
                "/v1/uploads/images.json",
                _upload_payload(size=len(content), width=2, height=3),
            )
        ]
    )
    resources = _resources(
        resolver=RotatingResolver(),
        transport=transport,
        audit=MemoryAudit(),
        s3=SourceS3(_s3_response(source, body)),
    )

    with pytest.raises(PrintifyCatalogMismatchError, match="geometry"):
        resources.upload_source(owner_id=OWNER, source=source, file_name=FILE_NAME)

    assert body.closed is True


def test_reconciliation_geometry_check_rereads_the_exact_source_version() -> None:
    content = _source_png()
    source = _source(content)
    body = TrackingBody(content)
    s3 = SourceS3(_s3_response(source, body))
    resources = _resources(
        resolver=RotatingResolver(),
        transport=ScriptedTransport([]),
        audit=MemoryAudit(),
        s3=s3,
    )
    upload = PrintifyUploadedImage(
        image_id="image_private_1",
        file_name=FILE_NAME,
        width=3,
        height=2,
        size_bytes=len(content),
        mime_type="image/png",
    )

    assert (
        resources.verify_upload_source_geometry(
            owner_id=OWNER,
            source=source,
            upload=upload,
        )
        == upload
    )
    assert s3.calls == [
        {
            "Bucket": BUCKET,
            "Key": source.object_key,
            "VersionId": source.version_id,
            "ChecksumMode": "ENABLED",
            "ExpectedBucketOwner": BUCKET_OWNER,
        }
    ]
    assert body.closed is True


def test_oversized_source_stream_fails_before_credentials_or_provider_call() -> None:
    content = _source_png()
    source = _source(content)
    body = TrackingBody(content + b"x")
    s3 = SourceS3(_s3_response(source, body))
    transport = ScriptedTransport([])
    resolver = RotatingResolver()
    audit = MemoryAudit()
    resources = _resources(resolver=resolver, transport=transport, audit=audit, s3=s3)

    with pytest.raises(PrintifyCatalogMismatchError, match="bytes"):
        resources.upload_source(owner_id=OWNER, source=source, file_name=FILE_NAME)

    assert body.closed is True
    assert resolver.calls == []
    assert transport.calls == []
    assert audit.records == []


def test_source_response_must_confirm_the_requested_version() -> None:
    content = _source_png()
    source = _source(content)
    body = TrackingBody(content)
    response = _s3_response(source, body)
    response["VersionId"] = "different-version"
    transport = ScriptedTransport([])
    resolver = RotatingResolver()
    audit = MemoryAudit()
    resources = _resources(
        resolver=resolver,
        transport=transport,
        audit=audit,
        s3=SourceS3(response),
    )

    with pytest.raises(PrintifyCatalogMismatchError, match="integrity"):
        resources.upload_source(owner_id=OWNER, source=source, file_name=FILE_NAME)

    assert body.closed is True
    assert resolver.calls == []
    assert transport.calls == []
    assert audit.records == []


def test_current_product_costs_use_one_exact_draft_get() -> None:
    product = {
        "id": "product_private_1",
        "shop_id": 42,
        "is_locked": False,
        "external": {},
        "variants": [
            {"id": 1001, "price": 2999, "cost": 1200, "is_enabled": True},
            {"id": 1002, "price": 2999, "cost": 1250, "is_enabled": True},
        ],
    }
    transport = ScriptedTransport(
        [
            ExpectedRequest(
                "GET",
                "/v1/shops/42/products/product_private_1.json",
                product,
            )
        ]
    )
    resolver = RotatingResolver()
    audit = MemoryAudit()
    resources = _resources(resolver=resolver, transport=transport, audit=audit)

    evidence = resources.current_product_costs(
        owner_id=OWNER,
        shop_id=42,
        product_id="product_private_1",
        product_sync_fingerprint="5" * 64,
        variant_ids=(1002, 1001),
    )

    assert evidence.observed_at == NOW
    assert [item.variant_id for item in evidence.variants] == [1002, 1001]
    assert [item.production_cost_cents for item in evidence.variants] == [1250, 1200]
    assert audit.records == [
        ProviderRequestAuditRecord(
            method="GET",
            path="/v1/shops/{shop_id}/products/{product_id}.json",
        )
    ]
    assert "product_private_1" not in audit.records[0].model_dump_json()


@pytest.mark.parametrize(
    "update",
    [
        {"external": {"id": "published-listing"}},
        {"is_locked": True},
        {
            "variants": [
                {"id": 1001, "price": 2999, "cost": 1200, "is_enabled": True},
                {"id": 1002, "price": 2999, "cost": 1250, "is_enabled": True},
            ]
        },
    ],
)
def test_product_cost_parser_rejects_non_draft_or_changed_variant_authority(
    update: dict[str, object],
) -> None:
    product = {
        "id": "product_private_1",
        "shop_id": 42,
        "is_locked": False,
        "external": {},
        "variants": [{"id": 1001, "price": 2999, "cost": 1200, "is_enabled": True}],
        **update,
    }

    with pytest.raises(PrintifyCatalogMismatchError):
        parse_current_product_costs(
            product,
            shop_id=42,
            product_sync_fingerprint="5" * 64,
            variant_ids=(1001,),
            observed_at=NOW,
        )


def test_standard_shipping_uses_only_the_existing_read_only_v2_client() -> None:
    v1_transport = ScriptedTransport([])
    v2_transport = ScriptedTransport(
        [
            ExpectedRequest(
                "GET",
                ("/v2/catalog/blueprints/145/print_providers/39/shipping/standard.json"),
                {"data": [_shipping_resource(1001)]},
            )
        ]
    )
    resolver = RotatingResolver(tokens=("shipping-token",))
    audit = MemoryAudit()
    resources = _resources(
        resolver=resolver,
        transport=v1_transport,
        audit=audit,
        v2_transport=v2_transport,
    )

    evidence = resources.standard_us_shipping(
        owner_id=OWNER,
        blueprint_id=145,
        print_provider_id=39,
        variant_ids=(1001,),
    )

    assert evidence.variants[0].first_item_cents == 399
    assert v1_transport.calls == []
    assert v2_transport.calls[0]["headers"]["Authorization"] == "Bearer shipping-token"
    assert audit.records == [
        ProviderRequestAuditRecord(
            method="GET",
            path=(
                "/v2/catalog/blueprints/{blueprint_id}/print_providers/"
                "{print_provider_id}/shipping/standard.json"
            ),
        )
    ]


def test_synchronizer_constructs_only_the_existing_draft_only_boundary() -> None:
    resolver = RotatingResolver()
    transport = ScriptedTransport([])
    audit = MemoryAudit()
    resources = _resources(resolver=resolver, transport=transport, audit=audit)

    synchronizer = resources.synchronizer(owner_id=OWNER, shop_id=42)

    assert isinstance(synchronizer, PrintifyDraftSynchronizer)
    assert resolver.calls == [OWNER]
    for forbidden in (
        "publish",
        "publish_product",
        "create_order",
        "delete",
        "delete_product",
        "fulfill",
    ):
        assert not hasattr(resources, forbidden)
        assert not hasattr(synchronizer, forbidden)


def test_audit_transport_rejects_non_draft_routes_and_records_a_safe_denial() -> None:
    transport = ScriptedTransport([])
    audit = MemoryAudit()
    audited = SanitizedProviderAuditTransport(transport=transport, audit_sink=audit)

    with pytest.raises(PrintifyInputError, match="draft-only"):
        audited.request(
            method="POST",
            url="https://api.printify.com/v1/shops/42/products/product_1/publish.json",
            headers={"Authorization": "Bearer must-not-be-logged"},
            body=b'{"publish":true}',
            timeout_seconds=15,
        )

    assert transport.calls == []
    assert audit.records == [
        ProviderRequestAuditRecord(
            outcome="rejected",
            method="REJECTED",
            path="/outside-draft-boundary",
        )
    ]
    serialized = audit.records[0].model_dump_json()
    assert "product_1" not in serialized
    assert "publish" not in serialized
    assert "Authorization" not in serialized


def test_logging_sink_and_route_reducer_emit_only_allowlisted_method_and_template() -> None:
    record = provider_request_audit_record(
        method="GET",
        url="https://api.printify.com/v1/uploads/private_image_id.json",
    )
    stream = StringIO()

    LoggingProviderRequestAuditSink(stream).write(record)

    assert stream.getvalue() == (
        'provider_request_audit={"outcome":"allowed","method":"GET",'
        '"path":"/v1/uploads/{image_id}.json"}\n'
    )
    assert "private_image_id" not in stream.getvalue()
    assert "Authorization" not in stream.getvalue()


def test_audit_record_schema_rejects_non_allowlisted_methods_and_paths() -> None:
    with pytest.raises(ValueError):
        ProviderRequestAuditRecord.model_validate(
            {"method": "DELETE", "path": "/v1/uploads/{image_id}.json"}
        )
    with pytest.raises(ValueError):
        ProviderRequestAuditRecord.model_validate(
            {"method": "GET", "path": "/v1/shops/{shop_id}/orders.json"}
        )
    with pytest.raises(ValueError, match="do not match"):
        ProviderRequestAuditRecord.model_validate(
            {
                "outcome": "allowed",
                "method": "REJECTED",
                "path": "/outside-draft-boundary",
            }
        )
    with pytest.raises(ValueError, match="do not match"):
        ProviderRequestAuditRecord.model_validate(
            {
                "outcome": "rejected",
                "method": "GET",
                "path": "/v1/shops.json",
            }
        )
