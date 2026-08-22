"""Phase 6 authenticated cloud-transport boundaries.

This package translates already verified API Gateway identity and HTTP metadata.  It deliberately
does not own seller lifecycle transitions, persistence authority, provider access, or publication.
"""

from mr_lister.cloud.auth import (
    AccessDeniedError,
    AuthenticatedSeller,
    AuthenticationRequiredError,
    SellerClaimsPolicy,
    authenticate_and_invoke,
    authenticate_seller,
)
from mr_lister.cloud.http import (
    ALL_ROUTE_KEYS,
    PROTECTED_ROUTE_KEYS,
    PUBLIC_ROUTE_KEYS,
    InvalidRequestError,
    PreconditionRequiredError,
    build_safe_request_log,
    error_response,
    parse_idempotency_key,
    parse_strong_if_match,
    request_id_from_event,
    require_exact_route_key,
)

__all__ = [
    "ALL_ROUTE_KEYS",
    "PROTECTED_ROUTE_KEYS",
    "PUBLIC_ROUTE_KEYS",
    "AccessDeniedError",
    "AuthenticatedSeller",
    "AuthenticationRequiredError",
    "InvalidRequestError",
    "PreconditionRequiredError",
    "SellerClaimsPolicy",
    "authenticate_and_invoke",
    "authenticate_seller",
    "build_safe_request_log",
    "error_response",
    "parse_idempotency_key",
    "parse_strong_if_match",
    "request_id_from_event",
    "require_exact_route_key",
]
