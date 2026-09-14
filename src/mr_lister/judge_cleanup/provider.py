"""Fresh primary credentials, exact published-product GET/DELETE, no redirects."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from mr_lister.judge_cleanup.models import CleanupConfig, PublicationEvidence
from mr_lister.judge_cleanup.service import CleanupRunError, ProviderMismatchError
from mr_lister.production.printify import PrintifyHttpResponse
from mr_lister.publication.provider_boundary import (
    _canonical_product_readback,
    _decode_json,
    _external_evidence,
    _mockup_fingerprints,
    _placement_image_ids,
    canonical_fingerprint,
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ExactProductTransport:
    """Deletion is deliberately isolated from the existing draft/publication transports."""

    def __init__(self, opener: Any = None) -> None:
        self.opener = opener or build_opener(_NoRedirect())

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> PrintifyHttpResponse:
        import re

        if (
            method not in {"GET", "DELETE"}
            or body is not None
            or timeout_seconds != 5
            or re.fullmatch(
                r"https://api\.printify\.com/v1/shops/[1-9][0-9]*/products/[A-Za-z0-9][A-Za-z0-9_-]{0,127}\.json",
                url,
            )
            is None
        ):
            raise ProviderMismatchError("Cleanup transport is outside its exact product boundary")
        result = None
        try:
            request = Request(url, headers=headers, method=method)
            try:
                response = self.opener.open(request, timeout=timeout_seconds)
            except HTTPError as error:
                response = error
            with response:
                payload = response.read(8 * 1024 * 1024 + 1)
                if len(payload) <= 8 * 1024 * 1024:
                    result = PrintifyHttpResponse(status=response.code, body=payload)
        except Exception:
            pass
        if result is None:
            raise CleanupRunError("Cleanup provider request was unavailable")
        return result


class PrintifyCleanupProvider:
    def __init__(
        self,
        *,
        config: CleanupConfig,
        resolver: Any,
        transport: Any = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = CleanupConfig.model_validate_json(config.model_dump_json())
        self.resolver = resolver
        self.transport = transport or ExactProductTransport()
        self.clock = clock or (lambda: datetime.now(UTC))

    def _request(self, evidence: PublicationEvidence, method: str) -> PrintifyHttpResponse:
        evidence = PublicationEvidence.model_validate_json(evidence.model_dump_json())
        evidence.require_campaign(self.config)
        connection = self.resolver.resolve_primary(owner_id=self.config.primary_owner_id)
        if (
            connection.owner_id != self.config.primary_owner_id
            or connection.shop_id != self.config.printify_shop_id
        ):
            raise ProviderMismatchError("Cleanup credential binding changed")
        s = evidence.snapshot
        response = self.transport.request(
            method=method,
            url=f"https://api.printify.com/v1/shops/{s.printify_shop_id}/products/{s.printify_product_id}.json",
            headers={
                "Authorization": f"Bearer {connection.api_token.get_secret_value()}",
                "User-Agent": "MrListerJudgeCleanup/1",
                "Accept": "application/json",
            },
            body=None,
            timeout_seconds=5.0,
        )
        if (
            type(response.status) is not int
            or not isinstance(response.body, bytes)
            or len(response.body) > 8 * 1024 * 1024
        ):
            raise CleanupRunError("Cleanup provider response was invalid")
        return response

    def present(self, evidence: PublicationEvidence) -> bool:
        response = self._request(evidence, "GET")
        if response.status == 404:
            return False
        if response.status != 200:
            raise CleanupRunError("Cleanup provider read was unavailable")
        try:
            payload = _decode_json(response.body)
            authority = evidence.provider
            if (
                payload["id"] != authority.printify_product_id
                or type(payload["shop_id"]) is not int
                or payload["shop_id"] != authority.printify_shop_id
                or payload["is_locked"] is not False
                or payload["visible"] is not True
            ):
                raise ValueError
            _, listing_id = _external_evidence(payload)
            canonical, skus_match = _canonical_product_readback(
                payload,
                expected_variant_ids=tuple(
                    v.variant_id for v in authority.expected_variant_economics
                ),
                job_id=authority.job_id,
                include_shipping=authority.expected_free_shipping is not None,
            )
            if (
                listing_id != evidence.result.numeric_listing_id
                or not skus_match
                or canonical_fingerprint(canonical.model_dump(mode="json"))
                != authority.product_payload_fingerprint
                or _placement_image_ids(canonical) != {authority.printify_image_id}
                or set(_mockup_fingerprints(payload)) != set(authority.expected_mockup_fingerprints)
            ):
                raise ValueError
        except Exception:
            raise ProviderMismatchError(
                "Cleanup provider product no longer matches publication"
            ) from None
        return True

    def delete(self, evidence: PublicationEvidence) -> None:
        if self.config.dry_run or self.clock() < evidence.deadline:
            raise ProviderMismatchError("Cleanup deletion is disabled or not due")
        response = self._request(evidence, "DELETE")
        if response.status not in {200, 204, 404}:
            raise CleanupRunError("Cleanup provider deletion needs reconciliation")
