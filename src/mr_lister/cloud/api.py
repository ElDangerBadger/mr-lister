"""Pure API Gateway HTTP API v2 adapters for the Phase 6 seller boundary.

The three adapters mirror the three least-capability Lambda roles in the Phase 6 stack.  They
translate only API Gateway's verified event shape into application-owned commands: identity is
derived from the JWT authorizer context, resource ownership is never accepted from the caller,
and every response is a closed, non-cacheable JSON envelope (except the bodyless preview redirect).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from urllib.parse import parse_qsl

from pydantic import ValidationError

from mr_lister.cloud.auth import AuthenticatedSeller, SellerClaimsPolicy, authenticate_seller
from mr_lister.cloud.browser_contracts import (
    BrowserContractModel,
    CreateUploadRequest,
    JobPageProjection,
    JobProgressProjection,
    JobSummaryProjection,
    RecordAuthorityRequest,
    RequestFieldError,
    ReviewAuthorityRequest,
    ReviseListingRequest,
    SellerCommandResponse,
    UploadAuthorizationProjection,
    UploadCommandProjection,
    UploadMutationResponse,
    UploadRecoveryProjection,
)
from mr_lister.cloud.http import (
    InvalidRequestError,
    RequestValidationError,
    RouteNotFoundError,
    error_response,
    parse_idempotency_key,
    parse_strong_if_match,
    request_id_from_event,
    require_exact_route_key,
)
from mr_lister.cloud.preview import PreviewRedirect, preview_redirect_response
from mr_lister.control.commands import (
    ApproveReviewCommand,
    CancelJobCommand,
    ListingRevision,
    RefreshEconomicsCommand,
    RetryJobCommand,
    ReviseListingCommand,
)
from mr_lister.control.models import (
    CommandResponse,
    ControlJobRecord,
)
from mr_lister.control.projection_models import SellerReviewProjection
from mr_lister.control.store import OwnerJobPage, decode_owner_job_cursor
from mr_lister.control.upload_models import UploadAuthorization, UploadCommandType, UploadIntent
from mr_lister.control.upload_service import UploadIntakeResult

_UPLOAD_ROUTES = frozenset(
    {
        "POST /v1/uploads",
        "GET /v1/uploads/{upload_id}",
        "POST /v1/uploads/{upload_id}/authorize",
        "POST /v1/uploads/{upload_id}/complete",
        "POST /v1/uploads/{upload_id}/cancel",
    }
)
_QUERY_ROUTES = frozenset(
    {
        "GET /v1/jobs",
        "GET /v1/jobs/{job_id}",
        "GET /v1/jobs/{job_id}/review",
        "GET /v1/jobs/{job_id}/artwork-preview",
    }
)
_COMMAND_ROUTES = frozenset(
    {
        "PUT /v1/jobs/{job_id}/review/listing",
        "POST /v1/jobs/{job_id}/economics/refresh",
        "POST /v1/jobs/{job_id}/approve",
        "POST /v1/jobs/{job_id}/cancel",
        "POST /v1/jobs/{job_id}/retry",
    }
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_CURSOR = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_MAX_JSON_BODY_BYTES = 512 * 1024


class UploadIntakePort(Protocol):
    def get_upload(self, *, owner_id: str, upload_id: str) -> UploadIntent: ...

    def create_upload(
        self,
        *,
        owner_id: str,
        idempotency_key: str,
        filename: str,
        content_type: str,
        content_sha256: str,
        size_bytes: int,
    ) -> UploadIntakeResult: ...

    def authorize_upload(
        self, *, owner_id: str, upload_id: str, idempotency_key: str
    ) -> UploadIntakeResult: ...

    def complete_upload(
        self, *, owner_id: str, upload_id: str, idempotency_key: str
    ) -> UploadIntakeResult: ...

    def cancel_upload(
        self, *, owner_id: str, upload_id: str, idempotency_key: str
    ) -> UploadIntakeResult: ...


class SellerQueryStore(Protocol):
    def list_jobs_for_owner(
        self,
        owner_id: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> OwnerJobPage: ...


class ReviewProjectionPort(Protocol):
    def get(self, *, owner_id: str, job_id: str) -> SellerReviewProjection: ...


class PreviewAuthorizationPort(Protocol):
    def authorize(self, *, owner_id: str, job_id: str) -> PreviewRedirect: ...


class SellerCommandPort(Protocol):
    def revise_listing(self, command: ReviseListingCommand) -> CommandResponse: ...

    def approve_review(self, command: ApproveReviewCommand) -> CommandResponse: ...

    def refresh_economics(self, command: RefreshEconomicsCommand) -> CommandResponse: ...

    def cancel_job(self, command: CancelJobCommand) -> CommandResponse: ...

    def retry_job(self, command: RetryJobCommand) -> CommandResponse: ...


class _ProtectedApiAdapter:
    """Authenticate before delegating to any application service."""

    _allowed_routes: frozenset[str]

    def __init__(self, *, claims_policy: SellerClaimsPolicy) -> None:
        self._claims_policy = claims_policy

    def handle(self, event: Mapping[str, Any], context: object | None = None) -> dict[str, Any]:
        del context
        request_id = request_id_from_event(event)
        try:
            route_key = require_exact_route_key(event)
            if route_key not in self._allowed_routes:
                raise RouteNotFoundError
            # Parsing body, headers, path parameters, and query data deliberately happens only
            # after the verified JWT context has been translated to the immutable owner identity.
            seller = authenticate_seller(event, policy=self._claims_policy)
            _require_http_api_v2(event)
            return self._dispatch(route_key, event, seller, request_id)
        except Exception as error:
            return error_response(error, request_id=request_id)

    def __call__(self, event: Mapping[str, Any], context: object | None = None) -> dict[str, Any]:
        return self.handle(event, context)

    def _dispatch(
        self,
        route_key: str,
        event: Mapping[str, Any],
        seller: AuthenticatedSeller,
        request_id: str,
    ) -> dict[str, Any]:
        raise NotImplementedError


class UploadApiAdapter(_ProtectedApiAdapter):
    """Translate the closed private direct-upload routes for the upload-only Lambda role."""

    _allowed_routes = _UPLOAD_ROUTES

    def __init__(
        self,
        *,
        claims_policy: SellerClaimsPolicy,
        uploads: UploadIntakePort,
    ) -> None:
        super().__init__(claims_policy=claims_policy)
        self._uploads = uploads

    def _dispatch(
        self,
        route_key: str,
        event: Mapping[str, Any],
        seller: AuthenticatedSeller,
        request_id: str,
    ) -> dict[str, Any]:
        _require_no_query(event)
        if route_key == "GET /v1/uploads/{upload_id}":
            upload_id = _resource_id(event, name="upload_id")
            _require_path(
                event,
                expected=f"/v1/uploads/{upload_id}",
                resource_name="upload_id",
            )
            _require_no_body(event)
            intent = self._uploads.get_upload(owner_id=seller.owner_id, upload_id=upload_id)
            projection = _upload_recovery_projection(
                intent,
                expected_owner_id=seller.owner_id,
                expected_upload_id=upload_id,
            )
            return _json_response(
                200,
                projection.model_dump(mode="json"),
                request_id=request_id,
            )

        idempotency_key = parse_idempotency_key(_headers(event))
        if route_key == "POST /v1/uploads":
            _require_path(event, expected="/v1/uploads")
            body = _parse_json_body(event, CreateUploadRequest)
            result = self._uploads.create_upload(
                owner_id=seller.owner_id,
                idempotency_key=idempotency_key,
                filename=body.filename,
                content_type=body.content_type,
                content_sha256=body.content_sha256,
                size_bytes=body.size_bytes,
            )
            return _upload_response(
                result,
                request_id=request_id,
                status_code=201,
                expected_owner_id=seller.owner_id,
                expected_upload_id=None,
                expected_command_type=UploadCommandType.CREATE_UPLOAD,
            )

        upload_id = _resource_id(event, name="upload_id")
        suffix = {
            "POST /v1/uploads/{upload_id}/authorize": "authorize",
            "POST /v1/uploads/{upload_id}/complete": "complete",
            "POST /v1/uploads/{upload_id}/cancel": "cancel",
        }[route_key]
        _require_path(
            event,
            expected=f"/v1/uploads/{upload_id}/{suffix}",
            resource_name="upload_id",
        )
        _require_no_body(event)
        operation: Callable[..., UploadIntakeResult] = {
            "authorize": self._uploads.authorize_upload,
            "complete": self._uploads.complete_upload,
            "cancel": self._uploads.cancel_upload,
        }[suffix]
        result = operation(
            owner_id=seller.owner_id,
            upload_id=upload_id,
            idempotency_key=idempotency_key,
        )
        status_code = 202 if suffix == "complete" else 200
        command_type = {
            "authorize": UploadCommandType.REAUTHORIZE_UPLOAD,
            "complete": UploadCommandType.COMPLETE_UPLOAD,
            "cancel": UploadCommandType.CANCEL_UPLOAD,
        }[suffix]
        return _upload_response(
            result,
            request_id=request_id,
            status_code=status_code,
            expected_owner_id=seller.owner_id,
            expected_upload_id=upload_id,
            expected_command_type=command_type,
        )


class ReviewQueryApiAdapter(_ProtectedApiAdapter):
    """Serve public health plus owner-scoped job, review, and preview reads."""

    _allowed_routes = _QUERY_ROUTES

    def __init__(
        self,
        *,
        claims_policy: SellerClaimsPolicy,
        store: SellerQueryStore,
        reviews: ReviewProjectionPort,
        previews: PreviewAuthorizationPort,
    ) -> None:
        super().__init__(claims_policy=claims_policy)
        self._store = store
        self._reviews = reviews
        self._previews = previews

    def handle(self, event: Mapping[str, Any], context: object | None = None) -> dict[str, Any]:
        del context
        request_id = request_id_from_event(event)
        try:
            route_key = require_exact_route_key(event)
            if route_key == "GET /health":
                _require_http_api_v2(event)
                _require_path(event, expected="/health")
                _require_no_query(event)
                _require_no_body(event)
                return _json_response(200, {"status": "ok"}, request_id=request_id)
            if route_key not in self._allowed_routes:
                raise RouteNotFoundError
            seller = authenticate_seller(event, policy=self._claims_policy)
            _require_http_api_v2(event)
            return self._dispatch(route_key, event, seller, request_id)
        except Exception as error:
            return error_response(error, request_id=request_id)

    def _dispatch(
        self,
        route_key: str,
        event: Mapping[str, Any],
        seller: AuthenticatedSeller,
        request_id: str,
    ) -> dict[str, Any]:
        _require_no_body(event)
        if route_key == "GET /v1/jobs":
            _require_path(event, expected="/v1/jobs")
            limit, cursor = _job_page_query(event)
            page = self._store.list_jobs_for_owner(
                seller.owner_id,
                limit=limit,
                cursor=cursor,
            )
            jobs = tuple(_owned_job(job, owner_id=seller.owner_id) for job in page.jobs)
            next_cursor = _trusted_page_cursor(page.next_cursor)
            page_projection = JobPageProjection(
                jobs=tuple(_job_summary(job) for job in jobs),
                next_cursor=next_cursor,
            )
            return _json_response(
                200,
                page_projection.model_dump(mode="json"),
                request_id=request_id,
            )

        _require_no_query(event)
        job_id = _resource_id(event, name="job_id")
        suffix = {
            "GET /v1/jobs/{job_id}": "",
            "GET /v1/jobs/{job_id}/review": "/review",
            "GET /v1/jobs/{job_id}/artwork-preview": "/artwork-preview",
        }[route_key]
        _require_path(
            event,
            expected=f"/v1/jobs/{job_id}{suffix}",
            resource_name="job_id",
        )
        if route_key == "GET /v1/jobs/{job_id}":
            review = self._reviews.get(owner_id=seller.owner_id, job_id=job_id)
            public_review = _public_review_projection(review, expected_job_id=job_id)
            progress = _job_progress_projection(public_review)
            return _json_response(
                200,
                progress.model_dump(mode="json"),
                request_id=request_id,
            )
        if route_key == "GET /v1/jobs/{job_id}/review":
            review = self._reviews.get(owner_id=seller.owner_id, job_id=job_id)
            public_review = _public_review_projection(review, expected_job_id=job_id)
            etag = _strong_etag(public_review.review_authority_etag)
            headers = {"ETag": etag} if etag is not None else None
            return _json_response(
                200,
                public_review.model_dump(mode="json"),
                request_id=request_id,
                extra_headers=headers,
            )
        redirect = self._previews.authorize(owner_id=seller.owner_id, job_id=job_id)
        return preview_redirect_response(redirect, request_id=request_id)


class SellerCommandApiAdapter(_ProtectedApiAdapter):
    """Translate only the closed pre-publication seller command surface."""

    _allowed_routes = _COMMAND_ROUTES

    def __init__(
        self,
        *,
        claims_policy: SellerClaimsPolicy,
        commands: SellerCommandPort,
    ) -> None:
        super().__init__(claims_policy=claims_policy)
        self._commands = commands

    def _dispatch(
        self,
        route_key: str,
        event: Mapping[str, Any],
        seller: AuthenticatedSeller,
        request_id: str,
    ) -> dict[str, Any]:
        _require_no_query(event)
        job_id = _resource_id(event, name="job_id")
        suffix = {
            "PUT /v1/jobs/{job_id}/review/listing": "/review/listing",
            "POST /v1/jobs/{job_id}/economics/refresh": "/economics/refresh",
            "POST /v1/jobs/{job_id}/approve": "/approve",
            "POST /v1/jobs/{job_id}/cancel": "/cancel",
            "POST /v1/jobs/{job_id}/retry": "/retry",
        }[route_key]
        _require_path(
            event,
            expected=f"/v1/jobs/{job_id}{suffix}",
            resource_name="job_id",
        )
        headers = _headers(event)
        idempotency_key = parse_idempotency_key(headers)

        if route_key == "PUT /v1/jobs/{job_id}/review/listing":
            body = _parse_json_body(event, ReviseListingRequest)
            response = self._commands.revise_listing(
                ReviseListingCommand(
                    owner_id=seller.owner_id,
                    job_id=job_id,
                    expected_record_version=body.expected_record_version,
                    expected_review_version=body.expected_review_version,
                    expected_review_fingerprint=body.expected_review_fingerprint,
                    expected_review_etag=parse_strong_if_match(headers),
                    idempotency_key=idempotency_key,
                    revision=ListingRevision(
                        title=body.listing.title,
                        description=body.listing.description,
                        tags=tuple(body.listing.tags),
                    ),
                )
            )
        elif route_key in {
            "POST /v1/jobs/{job_id}/economics/refresh",
            "POST /v1/jobs/{job_id}/approve",
        }:
            body = _parse_json_body(event, ReviewAuthorityRequest)
            authority = {
                "owner_id": seller.owner_id,
                "job_id": job_id,
                "expected_record_version": body.expected_record_version,
                "expected_review_version": body.expected_review_version,
                "expected_review_fingerprint": body.expected_review_fingerprint,
                "expected_review_etag": parse_strong_if_match(headers),
                "idempotency_key": idempotency_key,
            }
            if route_key == "POST /v1/jobs/{job_id}/economics/refresh":
                response = self._commands.refresh_economics(RefreshEconomicsCommand(**authority))
            else:
                response = self._commands.approve_review(ApproveReviewCommand(**authority))
        else:
            body = _parse_json_body(event, RecordAuthorityRequest)
            authority = {
                "owner_id": seller.owner_id,
                "job_id": job_id,
                "expected_record_version": body.expected_record_version,
                "idempotency_key": idempotency_key,
            }
            if route_key == "POST /v1/jobs/{job_id}/cancel":
                response = self._commands.cancel_job(CancelJobCommand(**authority))
            else:
                response = self._commands.retry_job(RetryJobCommand(**authority))
        return _json_response(
            200,
            _command_response(response, expected_job_id=job_id),
            request_id=request_id,
        )


def _headers(event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    headers = event.get("headers")
    if headers is None or isinstance(headers, Mapping):
        return headers
    raise InvalidRequestError


def _require_http_api_v2(event: Mapping[str, Any]) -> None:
    if event.get("version") != "2.0":
        raise InvalidRequestError


def _resource_id(event: Mapping[str, Any], *, name: str) -> str:
    parameters = event.get("pathParameters")
    if not isinstance(parameters, Mapping) or set(parameters) != {name}:
        raise InvalidRequestError
    value = parameters.get(name)
    if not isinstance(value, str) or not value.isascii() or _SAFE_ID.fullmatch(value) is None:
        raise InvalidRequestError
    return value


def _require_path(
    event: Mapping[str, Any],
    *,
    expected: str,
    resource_name: str | None = None,
) -> None:
    raw_path = event.get("rawPath")
    if not isinstance(raw_path, str) or raw_path != expected:
        raise InvalidRequestError
    parameters = event.get("pathParameters")
    if resource_name is None:
        if parameters is not None and (not isinstance(parameters, Mapping) or parameters):
            raise InvalidRequestError
    elif not isinstance(parameters, Mapping) or set(parameters) != {resource_name}:
        raise InvalidRequestError


def _require_no_body(event: Mapping[str, Any]) -> None:
    encoded = event.get("isBase64Encoded")
    if encoded is not None and encoded is not False:
        raise InvalidRequestError
    if event.get("body") not in (None, ""):
        raise InvalidRequestError


def _parse_json_body[ModelT: BrowserContractModel](
    event: Mapping[str, Any], model: type[ModelT]
) -> ModelT:
    encoded = event.get("isBase64Encoded")
    if encoded is not None and encoded is not False:
        raise InvalidRequestError
    content_type = _exact_header(_headers(event), "content-type")
    if content_type != "application/json":
        raise InvalidRequestError
    body = event.get("body")
    if not isinstance(body, str) or not body:
        raise InvalidRequestError
    try:
        encoded_body = body.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidRequestError from None
    if len(encoded_body) > _MAX_JSON_BODY_BYTES:
        raise InvalidRequestError
    try:
        decoded = json.loads(
            body,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError):
        raise InvalidRequestError from None
    if not isinstance(decoded, dict):
        raise InvalidRequestError
    try:
        return model.model_validate(decoded)
    except ValidationError as error:
        raise RequestValidationError(_request_field_errors(error)) from None


_FIELD_ERROR_PRESENTATION = {
    "missing": ("REQUIRED", "This field is required."),
    "extra_forbidden": ("UNEXPECTED_FIELD", "This field is not accepted."),
    "string_type": ("INVALID_TYPE", "Use the required value type."),
    "int_type": ("INVALID_TYPE", "Use the required value type."),
    "list_type": ("INVALID_TYPE", "Use the required value type."),
    "dict_type": ("INVALID_TYPE", "Use the required value type."),
    "literal_error": ("INVALID_FORMAT", "Use one of the allowed values."),
    "string_pattern_mismatch": ("INVALID_FORMAT", "Use the required format."),
    "string_too_short": ("INVALID_LENGTH", "Use the required length."),
    "string_too_long": ("INVALID_LENGTH", "Use the required length."),
    "too_short": ("INVALID_LENGTH", "Use the required number of items."),
    "too_long": ("INVALID_LENGTH", "Use the required number of items."),
    "greater_than": ("OUT_OF_RANGE", "Use a value within the allowed range."),
    "greater_than_equal": ("OUT_OF_RANGE", "Use a value within the allowed range."),
    "less_than": ("OUT_OF_RANGE", "Use a value within the allowed range."),
    "less_than_equal": ("OUT_OF_RANGE", "Use a value within the allowed range."),
}
_SAFE_FIELD_SEGMENT = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")


def _request_field_errors(error: ValidationError) -> tuple[RequestFieldError, ...]:
    """Reduce Pydantic diagnostics to bounded paths and fixed seller-safe messages."""

    result: list[RequestFieldError] = []
    for issue in error.errors(include_url=False, include_context=False, include_input=False)[:25]:
        path = "$"
        location = issue.get("loc", ())
        if isinstance(location, tuple):
            for segment in location:
                if (
                    isinstance(segment, int)
                    and not isinstance(segment, bool)
                    and 0 <= segment <= 999
                ):
                    path += f"[{segment}]"
                elif isinstance(segment, str) and _SAFE_FIELD_SEGMENT.fullmatch(segment):
                    path += f".{segment}"
        error_type = issue.get("type")
        code, message = _FIELD_ERROR_PRESENTATION.get(
            error_type if isinstance(error_type, str) else "",
            ("INVALID_VALUE", "Use a valid value."),
        )
        result.append(RequestFieldError(path=path, code=code, message=message))
    return tuple(result)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError("Non-finite JSON number")


def _exact_header(headers: Mapping[str, Any] | None, name: str) -> str:
    if not isinstance(headers, Mapping):
        raise InvalidRequestError
    matches = [
        (key, value)
        for key, value in headers.items()
        if isinstance(key, str) and key.casefold() == name
    ]
    if len(matches) != 1 or not isinstance(matches[0][1], str):
        raise InvalidRequestError
    return matches[0][1]


def _query(event: Mapping[str, Any]) -> dict[str, str]:
    raw_parameters = event.get("queryStringParameters")
    if raw_parameters is None:
        parameters: dict[str, str] = {}
    elif isinstance(raw_parameters, Mapping) and all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw_parameters.items()
    ):
        parameters = dict(raw_parameters)
    else:
        raise InvalidRequestError

    raw_query = event.get("rawQueryString")
    if raw_query is not None:
        if not isinstance(raw_query, str) or len(raw_query) > 1_024:
            raise InvalidRequestError
        try:
            pairs = parse_qsl(
                raw_query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=3,
            )
        except ValueError:
            raise InvalidRequestError from None
        if len({key for key, _value in pairs}) != len(pairs):
            raise InvalidRequestError
        if dict(pairs) != parameters:
            raise InvalidRequestError
    return parameters


def _require_no_query(event: Mapping[str, Any]) -> None:
    if _query(event):
        raise InvalidRequestError


def _job_page_query(event: Mapping[str, Any]) -> tuple[int, str | None]:
    parameters = _query(event)
    if not set(parameters) <= {"limit", "cursor"}:
        raise InvalidRequestError
    limit_text = parameters.get("limit", "25")
    if re.fullmatch(r"[1-9][0-9]{0,2}", limit_text) is None:
        raise InvalidRequestError
    limit = int(limit_text)
    if limit > 100:
        raise InvalidRequestError
    cursor = parameters.get("cursor")
    if cursor is not None:
        if _CURSOR.fullmatch(cursor) is None:
            raise InvalidRequestError
        try:
            decode_owner_job_cursor(cursor)
        except ValueError:
            raise InvalidRequestError from None
    return limit, cursor


def _job_summary(job: ControlJobRecord) -> JobSummaryProjection:
    return JobSummaryProjection(
        job_id=job.job_id,
        state=job.state,
        record_version=job.record_version,
        review_version=job.review_version,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _upload_response(
    result: UploadIntakeResult,
    *,
    request_id: str,
    status_code: int,
    expected_owner_id: str,
    expected_upload_id: str | None,
    expected_command_type: UploadCommandType,
) -> dict[str, Any]:
    receipt = result.receipt
    if (
        receipt.owner_id != expected_owner_id
        or receipt.command_type is not expected_command_type
        or (expected_upload_id is not None and receipt.upload_id != expected_upload_id)
    ):
        raise RuntimeError("Invalid upload authority")
    if result.authorization is not None and (
        result.authorization.owner_id != expected_owner_id
        or result.authorization.upload_id != receipt.upload_id
        or result.authorization.job_id != receipt.job_id
    ):
        raise RuntimeError("Invalid upload authorization")
    payload = UploadMutationResponse(
        upload=UploadCommandProjection(
            upload_id=receipt.upload_id,
            job_id=receipt.job_id,
            status=receipt.status,
            record_version=receipt.record_version,
        ),
        authorization=(
            _upload_authorization(result.authorization)
            if result.authorization is not None
            else None
        ),
    )
    return _json_response(
        status_code,
        payload.model_dump(mode="json"),
        request_id=request_id,
    )


def _upload_authorization(
    authorization: UploadAuthorization,
) -> UploadAuthorizationProjection:
    return UploadAuthorizationProjection(
        upload_id=authorization.upload_id,
        job_id=authorization.job_id,
        authorization_generation=authorization.authorization_generation,
        method="POST",
        url=authorization.url,
        form_fields=dict(authorization.form_fields),
        content_sha256=authorization.content_sha256,
        size_bytes=authorization.size_bytes,
        issued_at=authorization.issued_at,
        expires_at=authorization.expires_at,
    )


def _upload_recovery_projection(
    intent: UploadIntent,
    *,
    expected_owner_id: str,
    expected_upload_id: str,
) -> UploadRecoveryProjection:
    if intent.owner_id != expected_owner_id or intent.upload_id != expected_upload_id:
        raise RuntimeError("Invalid upload authority")
    try:
        return UploadRecoveryProjection(
            upload_id=intent.upload_id,
            job_id=intent.job_id,
            status=intent.status,
            filename=intent.filename,
            content_type="image/png",
            size_bytes=intent.size_bytes,
            record_version=intent.record_version,
            authorization_expires_at=intent.authorization_expires_at,
            intent_expires_at=intent.intent_expires_at,
            completed_at=intent.completed_at,
            cancelled_at=intent.cancelled_at,
            expired_at=intent.expired_at,
            created_at=intent.created_at,
            updated_at=intent.updated_at,
        )
    except ValidationError:
        raise RuntimeError("Invalid upload authority") from None


def _command_response(
    response: CommandResponse,
    *,
    expected_job_id: str,
) -> dict[str, Any]:
    if response.job_id != expected_job_id:
        raise RuntimeError("Invalid command authority")
    projection = SellerCommandResponse(
        job_id=response.job_id,
        state=response.state,
        record_version=response.record_version,
        review_version=response.review_version,
    )
    return projection.model_dump(mode="json")


def _strong_etag(authority: object) -> str | None:
    if authority is None:
        return None
    if not isinstance(authority, str) or re.fullmatch(r"[a-f0-9]{64}", authority) is None:
        raise RuntimeError("Invalid review authority")
    return f'"{authority}"'


def _public_review_projection(
    candidate: object,
    *,
    expected_job_id: str,
) -> SellerReviewProjection:
    """Reconstruct the exact public schema before crossing the internet boundary.

    The projection service is trusted application code, but a wiring error, subclass, or test
    double must not be able to add storage or ownership fields through a permissive ``model_dump``.
    Schema failure here is an internal authority failure, never caller validation feedback.
    """

    try:
        model_dump = getattr(candidate, "model_dump", None)
        if not callable(model_dump):
            raise TypeError
        payload = model_dump(mode="json")
        projection = SellerReviewProjection.model_validate(payload)
    except Exception:
        raise RuntimeError("Invalid review authority") from None
    if projection.job_id != expected_job_id:
        raise RuntimeError("Invalid review authority")
    return projection


def _job_progress_projection(review: SellerReviewProjection) -> JobProgressProjection:
    try:
        return JobProgressProjection(
            job_id=review.job_id,
            record_version=review.record_version,
            review_version=review.review_version,
            display_state=review.display_state,
            stage=review.stage,
            actions=review.actions,
            failure=review.failure,
            provider_outcome_unconfirmed=review.provider_outcome_unconfirmed,
            created_at=review.created_at,
            updated_at=review.updated_at,
        )
    except ValidationError:
        raise RuntimeError("Invalid review authority") from None


def _owned_job(
    job: ControlJobRecord,
    *,
    owner_id: str,
    expected_job_id: str | None = None,
) -> ControlJobRecord:
    if job.owner_id != owner_id or (expected_job_id is not None and job.job_id != expected_job_id):
        raise RuntimeError("Invalid job authority")
    return job


def _trusted_page_cursor(cursor: object) -> str | None:
    if cursor is None:
        return None
    if not isinstance(cursor, str) or _CURSOR.fullmatch(cursor) is None:
        raise RuntimeError("Invalid job page authority")
    try:
        decode_owner_job_cursor(cursor)
    except ValueError:
        raise RuntimeError("Invalid job page authority") from None
    return cursor


def _json_response(
    status_code: int,
    payload: Mapping[str, Any],
    *,
    request_id: str,
    extra_headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    headers = {
        "Cache-Control": "no-store",
        "Content-Type": "application/json",
        "X-Content-Type-Options": "nosniff",
        "X-Request-Id": request_id,
    }
    if extra_headers is not None:
        if any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or "\r" in key
            or "\n" in key
            or "\r" in value
            or "\n" in value
            for key, value in extra_headers.items()
        ):
            raise RuntimeError("Invalid response header")
        headers.update(extra_headers)
    return {
        "statusCode": status_code,
        "headers": headers,
        "body": json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ),
        "isBase64Encoded": False,
    }


__all__ = [
    "PreviewAuthorizationPort",
    "ReviewProjectionPort",
    "ReviewQueryApiAdapter",
    "SellerCommandApiAdapter",
    "SellerCommandPort",
    "SellerQueryStore",
    "UploadApiAdapter",
    "UploadIntakePort",
]
