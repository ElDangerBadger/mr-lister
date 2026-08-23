"""Least-capability production composition for the three Phase 6 seller API roles.

The functions in this module deliberately stop one layer short of deployment.  They parse a
closed, drift-resistant environment contract and return lazy callables, but they neither replace
the scaffold handlers nor make an AWS request at import time.  SDK clients and immutable
configuration may be reused by a warm Lambda; request events and seller identity never are.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit

from mr_lister.cloud.api import (
    ReviewQueryApiAdapter,
    SellerCommandApiAdapter,
    UploadApiAdapter,
)
from mr_lister.cloud.artifacts import ExactKeyS3UploadArtifacts
from mr_lister.cloud.auth import SellerClaimsPolicy
from mr_lister.cloud.http import (
    InvalidRequestError,
    RouteNotFoundError,
    error_response,
    request_id_from_event,
)
from mr_lister.cloud.preview import (
    AuthenticatedPreviewLinkIssuer,
    ExactVersionArtworkPreviewService,
)
from mr_lister.control.dynamodb import DynamoDBSellerControlStore
from mr_lister.control.projection import SellerReviewProjectionService
from mr_lister.control.service import SellerControlService
from mr_lister.control.upload_service import UploadIntakeService
from mr_lister.review_profile import (
    ExactReviewProductProfile,
    FilesystemReviewProductAuthority,
    ReviewProfileNotFoundError,
)

AwsServiceName = Literal["dynamodb", "s3"]

SELLER_SCOPE = "mr-lister-api/seller"
SELLER_GROUP = "seller"
HEALTH_ROUTE_KEY = "GET /health"
UPLOAD_ROUTE_KEYS = frozenset(
    {
        "POST /v1/uploads",
        "GET /v1/uploads/{upload_id}",
        "POST /v1/uploads/{upload_id}/authorize",
        "POST /v1/uploads/{upload_id}/complete",
        "POST /v1/uploads/{upload_id}/cancel",
    }
)
QUERY_ROUTE_KEYS = frozenset(
    {
        "GET /v1/jobs",
        "GET /v1/jobs/{job_id}",
        "GET /v1/jobs/{job_id}/review",
        "GET /v1/jobs/{job_id}/artwork-preview",
    }
)
COMMAND_ROUTE_KEYS = frozenset(
    {
        "PUT /v1/jobs/{job_id}/review/listing",
        "POST /v1/jobs/{job_id}/economics/refresh",
        "POST /v1/jobs/{job_id}/approve",
        "POST /v1/jobs/{job_id}/cancel",
        "POST /v1/jobs/{job_id}/retry",
    }
)

_REGION = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+){1,2}-[1-9][0-9]?$")
_TABLE = re.compile(r"^mr-lister-phase6-(?P<environment>[a-z][a-z0-9-]{1,15})$")
_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_CLIENT_ID = re.compile(r"^[A-Za-z0-9]{1,128}$")
_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_USER_POOL_ID = re.compile(r"^[a-z0-9-]+_[A-Za-z0-9]{1,64}$")
_GENERIC_CONFIGURATION_ERROR = "Phase 6 API configuration is invalid"


class Phase6ApiConfigurationError(RuntimeError):
    """One generic, value-free failure for missing, malformed, or drifting settings."""


class AwsClientFactory(Protocol):
    """Construct one regional SDK client without performing a service operation."""

    def __call__(self, service_name: AwsServiceName, *, region_name: str) -> object: ...


class ApiHandler(Protocol):
    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class CommonApiConfiguration:
    region: str
    environment_name: str
    state_table: str
    release_fingerprint: str
    claims_policy: SellerClaimsPolicy


@dataclass(frozen=True, slots=True)
class ArtifactConfiguration:
    bucket: str
    bucket_owner_account_id: str
    origin: str


@dataclass(frozen=True, slots=True)
class PinnedProfileConfiguration:
    profile_path: Path
    exact: ExactReviewProductProfile

    @property
    def profile_id(self) -> str:
        return self.exact.profile.profile_id

    @property
    def profile_version(self) -> int:
        return self.exact.profile.profile_version


@dataclass(frozen=True, slots=True)
class UploadApiConfiguration:
    common: CommonApiConfiguration
    artifacts: ArtifactConfiguration
    profile: PinnedProfileConfiguration


@dataclass(frozen=True, slots=True)
class QueryApiConfiguration:
    common: CommonApiConfiguration
    artifacts: ArtifactConfiguration
    profile: PinnedProfileConfiguration
    application_origin: str


@dataclass(frozen=True, slots=True)
class CommandApiConfiguration:
    common: CommonApiConfiguration


class PinnedReviewProductAuthority:
    """Expose exactly the configured immutable profile and no directory-wide authority."""

    __slots__ = ("_exact",)

    def __init__(self, exact: ExactReviewProductProfile) -> None:
        self._exact = exact

    def get_exact(self, *, profile_id: str, profile_version: int) -> ExactReviewProductProfile:
        if (
            profile_id != self._exact.profile.profile_id
            or profile_version != self._exact.profile.profile_version
        ):
            raise ReviewProfileNotFoundError("The review product profile was not found")
        return self._exact


def load_upload_api_configuration(environment: Mapping[str, object]) -> UploadApiConfiguration:
    """Load only the upload role's exact, non-secret settings."""

    try:
        common = _common_configuration(environment)
        return UploadApiConfiguration(
            common=common,
            artifacts=_artifact_configuration(environment, common),
            profile=_profile_configuration(environment),
        )
    except Exception:
        pass
    raise Phase6ApiConfigurationError(_GENERIC_CONFIGURATION_ERROR)


def load_query_api_configuration(environment: Mapping[str, object]) -> QueryApiConfiguration:
    """Load only the read role's exact, non-secret settings."""

    try:
        common = _common_configuration(environment)
        application_origin = _required(environment, "MR_LISTER_APPLICATION_ORIGIN")
        # Reuse the application boundary's own exact-origin validator.
        AuthenticatedPreviewLinkIssuer(application_origin=application_origin)
        return QueryApiConfiguration(
            common=common,
            artifacts=_artifact_configuration(environment, common),
            profile=_profile_configuration(environment),
            application_origin=application_origin,
        )
    except Exception:
        pass
    raise Phase6ApiConfigurationError(_GENERIC_CONFIGURATION_ERROR)


def load_command_api_configuration(environment: Mapping[str, object]) -> CommandApiConfiguration:
    """Load only the command role's table and authentication settings."""

    try:
        return CommandApiConfiguration(common=_common_configuration(environment))
    except Exception:
        pass
    raise Phase6ApiConfigurationError(_GENERIC_CONFIGURATION_ERROR)


def compose_upload_api_adapter(
    configuration: UploadApiConfiguration,
    *,
    client_factory: AwsClientFactory,
) -> UploadApiAdapter:
    """Construct only upload-role dependencies; no provider or workflow client is reachable."""

    common = configuration.common
    dynamodb = _client(
        client_factory,
        "dynamodb",
        common.region,
        required_methods=("get_item", "transact_write_items"),
    )
    s3 = _client(
        client_factory,
        "s3",
        common.region,
        required_methods=("generate_presigned_post", "get_object", "put_object_tagging"),
    )
    store = DynamoDBSellerControlStore(client=dynamodb, table_name=common.state_table)
    artifacts = ExactKeyS3UploadArtifacts(
        client=cast(Any, s3),
        bucket=configuration.artifacts.bucket,
        bucket_owner_account_id=configuration.artifacts.bucket_owner_account_id,
        artifact_origin=configuration.artifacts.origin,
    )
    profiles = PinnedReviewProductAuthority(configuration.profile.exact)
    uploads = UploadIntakeService(
        store=store,
        artifacts=artifacts,
        profiles=profiles,
        artifact_bucket=configuration.artifacts.bucket,
        profile_id=configuration.profile.profile_id,
        profile_version=configuration.profile.profile_version,
    )
    return UploadApiAdapter(claims_policy=common.claims_policy, uploads=uploads)


def compose_query_api_adapter(
    configuration: QueryApiConfiguration,
    *,
    client_factory: AwsClientFactory,
) -> ReviewQueryApiAdapter:
    """Construct only owner-scoped reads and exact-version S3 presigning."""

    common = configuration.common
    dynamodb = _client(
        client_factory,
        "dynamodb",
        common.region,
        required_methods=("get_item", "query"),
    )
    s3 = _client(
        client_factory,
        "s3",
        common.region,
        required_methods=("generate_presigned_url",),
    )
    store = DynamoDBSellerControlStore(client=dynamodb, table_name=common.state_table)
    profiles = PinnedReviewProductAuthority(configuration.profile.exact)
    preview_issuer = AuthenticatedPreviewLinkIssuer(
        application_origin=configuration.application_origin
    )
    reviews = SellerReviewProjectionService(
        store=store,
        profiles=profiles,
        preview_issuer=preview_issuer,
        preview_origin=configuration.application_origin,
    )
    previews = ExactVersionArtworkPreviewService(
        store=store,
        presigner=cast(Any, s3),
        artifact_bucket=configuration.artifacts.bucket,
        artifact_origin=configuration.artifacts.origin,
    )
    return ReviewQueryApiAdapter(
        claims_policy=common.claims_policy,
        store=store,
        reviews=reviews,
        previews=previews,
    )


def compose_command_api_adapter(
    configuration: CommandApiConfiguration,
    *,
    client_factory: AwsClientFactory,
) -> SellerCommandApiAdapter:
    """Construct only the transaction store and deterministic seller command service."""

    common = configuration.common
    dynamodb = _client(
        client_factory,
        "dynamodb",
        common.region,
        required_methods=("get_item", "put_item", "transact_write_items"),
    )
    store = DynamoDBSellerControlStore(client=dynamodb, table_name=common.state_table)
    commands = SellerControlService(store=store)
    return SellerCommandApiAdapter(claims_policy=common.claims_policy, commands=commands)


def build_upload_api_handler(
    environment: Mapping[str, object],
    *,
    client_factory: AwsClientFactory | None = None,
) -> ApiHandler:
    """Return a route-closed callable whose SDK clients are created on first invocation."""

    configuration = load_upload_api_configuration(environment)
    factory = client_factory or default_aws_client_factory
    return _LazyRoleHandler(
        allowed_routes=UPLOAD_ROUTE_KEYS,
        builder=lambda: compose_upload_api_adapter(configuration, client_factory=factory),
    )


def build_query_api_handler(
    environment: Mapping[str, object],
    *,
    client_factory: AwsClientFactory | None = None,
) -> ApiHandler:
    """Return a lazy protected-query callable; public health is intentionally excluded."""

    configuration = load_query_api_configuration(environment)
    factory = client_factory or default_aws_client_factory
    return _LazyRoleHandler(
        allowed_routes=QUERY_ROUTE_KEYS,
        builder=lambda: compose_query_api_adapter(configuration, client_factory=factory),
    )


def build_command_api_handler(
    environment: Mapping[str, object],
    *,
    client_factory: AwsClientFactory | None = None,
) -> ApiHandler:
    """Return a lazy seller-command callable with no S3 or secret dependency."""

    configuration = load_command_api_configuration(environment)
    factory = client_factory or default_aws_client_factory
    return _LazyRoleHandler(
        allowed_routes=COMMAND_ROUTE_KEYS,
        builder=lambda: compose_command_api_adapter(configuration, client_factory=factory),
    )


def build_health_readiness_handler(
    environment: Mapping[str, object],
    *,
    client_factory: AwsClientFactory | None = None,
) -> ApiHandler:
    """Prove query dependency construction without reading data or exposing configuration."""

    configuration = load_query_api_configuration(environment)
    factory = client_factory or default_aws_client_factory
    return _HealthReadinessHandler(
        builder=lambda: compose_query_api_adapter(configuration, client_factory=factory)
    )


def default_aws_client_factory(service_name: AwsServiceName, *, region_name: str) -> object:
    """Create an SDK client lazily with deterministic regional SigV4 S3 signing."""

    import boto3

    if service_name == "s3":
        from botocore.config import Config

        return boto3.client(
            service_name,
            region_name=region_name,
            config=Config(
                signature_version="s3v4",
                s3={
                    "addressing_style": "virtual",
                    "us_east_1_regional_endpoint": "regional",
                },
            ),
        )
    if service_name == "dynamodb":
        return boto3.client(service_name, region_name=region_name)
    raise ValueError("Unsupported Phase 6 API AWS client")


class _LazyRoleHandler:
    """Thread-safe cold-start cache containing no request or identity material."""

    __slots__ = ("_allowed_routes", "_builder", "_delegate", "_lock")

    def __init__(
        self,
        *,
        allowed_routes: frozenset[str],
        builder: Callable[[], ApiHandler],
    ) -> None:
        self._allowed_routes = allowed_routes
        self._builder = builder
        self._delegate: ApiHandler | None = None
        self._lock = Lock()

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        request_id = request_id_from_event(event)
        if not isinstance(event, Mapping) or event.get("routeKey") not in self._allowed_routes:
            return error_response(RouteNotFoundError(), request_id=request_id)
        try:
            delegate = self._get_delegate()
            return delegate(event, context)
        except Exception:
            return error_response(RuntimeError(), request_id=request_id)

    def _get_delegate(self) -> ApiHandler:
        delegate = self._delegate
        if delegate is not None:
            return delegate
        with self._lock:
            if self._delegate is None:
                self._delegate = self._builder()
            return self._delegate


class _HealthReadinessHandler:
    """A separate public health boundary over the read role's construction graph."""

    __slots__ = ("_builder", "_dependency", "_lock")

    def __init__(self, *, builder: Callable[[], object]) -> None:
        self._builder = builder
        self._dependency: object | None = None
        self._lock = Lock()

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, Any]:
        del context
        request_id = request_id_from_event(event)
        if not isinstance(event, Mapping) or event.get("routeKey") != HEALTH_ROUTE_KEY:
            return error_response(RouteNotFoundError(), request_id=request_id)
        try:
            _validate_health_event(event)
        except InvalidRequestError as error:
            return error_response(error, request_id=request_id)
        try:
            self._get_dependency()
        except Exception:
            return _health_response(503, "unavailable")
        return _health_response(200, "ok")

    def _get_dependency(self) -> object:
        dependency = self._dependency
        if dependency is not None:
            return dependency
        with self._lock:
            if self._dependency is None:
                self._dependency = self._builder()
            return self._dependency


def _common_configuration(environment: Mapping[str, object]) -> CommonApiConfiguration:
    region = _required(environment, "AWS_REGION")
    if _REGION.fullmatch(region) is None:
        raise ValueError
    state_table = _required(environment, "MR_LISTER_STATE_TABLE")
    table_match = _TABLE.fullmatch(state_table)
    if table_match is None:
        raise ValueError
    release_fingerprint = _required(environment, "MR_LISTER_RELEASE_FINGERPRINT")
    if _FINGERPRINT.fullmatch(release_fingerprint) is None or release_fingerprint == "0" * 64:
        raise ValueError
    issuer = _required(environment, "MR_LISTER_COGNITO_ISSUER")
    _validate_cognito_issuer(issuer, region)
    client_id = _required(environment, "MR_LISTER_COGNITO_CLIENT_ID")
    if _CLIENT_ID.fullmatch(client_id) is None:
        raise ValueError
    scope = _required(environment, "MR_LISTER_COGNITO_SCOPE")
    group = _required(environment, "MR_LISTER_COGNITO_GROUP")
    if scope != SELLER_SCOPE or group != SELLER_GROUP:
        raise ValueError
    return CommonApiConfiguration(
        region=region,
        environment_name=table_match.group("environment"),
        state_table=state_table,
        release_fingerprint=release_fingerprint,
        claims_policy=SellerClaimsPolicy(
            issuer=issuer,
            client_id=client_id,
            required_scope=scope,
            required_group=group,
        ),
    )


def _artifact_configuration(
    environment: Mapping[str, object],
    common: CommonApiConfiguration,
) -> ArtifactConfiguration:
    account_id = _required(environment, "MR_LISTER_ARTIFACT_BUCKET_OWNER_ACCOUNT_ID")
    if _ACCOUNT_ID.fullmatch(account_id) is None or account_id == "0" * 12:
        raise ValueError
    bucket = _required(environment, "MR_LISTER_ARTIFACT_BUCKET")
    expected_bucket = (
        f"mr-lister-phase6-artifacts-{common.environment_name}-{account_id}-{common.region}"
    )
    if bucket != expected_bucket or len(bucket) > 63:
        raise ValueError
    suffix = _aws_dns_suffix(common.region)
    expected_origin = f"https://{bucket}.s3.{common.region}.{suffix}"
    origin = _required(environment, "MR_LISTER_ARTIFACT_ORIGIN")
    if origin != expected_origin:
        raise ValueError
    return ArtifactConfiguration(
        bucket=bucket,
        bucket_owner_account_id=account_id,
        origin=origin,
    )


def _profile_configuration(environment: Mapping[str, object]) -> PinnedProfileConfiguration:
    profile_id = _required(environment, "MR_LISTER_PRODUCT_PROFILE_ID")
    if _PROFILE_ID.fullmatch(profile_id) is None:
        raise ValueError
    version_text = _required(environment, "MR_LISTER_PRODUCT_PROFILE_VERSION")
    if re.fullmatch(r"[1-9][0-9]{0,5}", version_text) is None:
        raise ValueError
    profile_version = int(version_text)
    expected_fingerprint = _required(environment, "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT")
    if _FINGERPRINT.fullmatch(expected_fingerprint) is None or expected_fingerprint == "0" * 64:
        raise ValueError
    path_text = _required(environment, "MR_LISTER_PRODUCT_PROFILE_PATH")
    if not path_text.isascii() or len(path_text) > 4_096 or "\\" in path_text:
        raise ValueError
    profile_path = Path(path_text)
    if (
        not profile_path.is_absolute()
        or profile_path.as_posix() != path_text
        or profile_path.name != f"{profile_id}.json"
        or profile_path.resolve(strict=True) != profile_path
    ):
        raise ValueError
    stat = profile_path.stat()
    if not profile_path.is_file() or not 1 <= stat.st_size <= 1024 * 1024:
        raise ValueError
    authority = FilesystemReviewProductAuthority(profile_directory=profile_path.parent)
    exact = authority.get_exact(profile_id=profile_id, profile_version=profile_version)
    if exact.fingerprint != expected_fingerprint:
        raise ValueError
    return PinnedProfileConfiguration(profile_path=profile_path, exact=exact)


def _validate_cognito_issuer(issuer: str, region: str) -> None:
    parsed = urlsplit(issuer)
    suffix = _aws_dns_suffix(region)
    expected_host = f"cognito-idp.{region}.{suffix}"
    pool_id = parsed.path.removeprefix("/")
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_host
        or parsed.netloc != expected_host
        or parsed.path != f"/{pool_id}"
        or parsed.query
        or parsed.fragment
        or _USER_POOL_ID.fullmatch(pool_id) is None
        or not pool_id.startswith(f"{region}_")
    ):
        raise ValueError


def _aws_dns_suffix(region: str) -> str:
    return "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"


def _required(environment: Mapping[str, object], name: str) -> str:
    value = environment.get(name)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4_096
        or value != value.strip()
        or "\x00" in value
    ):
        raise ValueError
    return value


def _client(
    factory: AwsClientFactory,
    service_name: AwsServiceName,
    region_name: str,
    *,
    required_methods: tuple[str, ...],
) -> object:
    client = factory(service_name, region_name=region_name)
    if any(not callable(getattr(client, method, None)) for method in required_methods):
        raise RuntimeError("Phase 6 API dependency is unavailable")
    return client


def _validate_health_event(event: Mapping[str, Any]) -> None:
    parameters = event.get("pathParameters")
    query = event.get("queryStringParameters")
    if (
        event.get("version") != "2.0"
        or event.get("rawPath") != "/health"
        or event.get("rawQueryString", "") != ""
        or (parameters is not None and (not isinstance(parameters, Mapping) or parameters))
        or (query is not None and (not isinstance(query, Mapping) or query))
        or event.get("body") not in (None, "")
        or event.get("isBase64Encoded") not in (None, False)
    ):
        raise InvalidRequestError


def _health_response(status_code: int, status: str) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Cache-Control": "no-store",
            "Content-Type": "application/json",
            "X-Content-Type-Options": "nosniff",
        },
        "body": json.dumps({"status": status}, separators=(",", ":")),
        "isBase64Encoded": False,
    }


__all__ = [
    "COMMAND_ROUTE_KEYS",
    "HEALTH_ROUTE_KEY",
    "QUERY_ROUTE_KEYS",
    "SELLER_GROUP",
    "SELLER_SCOPE",
    "UPLOAD_ROUTE_KEYS",
    "ApiHandler",
    "ArtifactConfiguration",
    "AwsClientFactory",
    "CommandApiConfiguration",
    "CommonApiConfiguration",
    "Phase6ApiConfigurationError",
    "PinnedProfileConfiguration",
    "PinnedReviewProductAuthority",
    "QueryApiConfiguration",
    "UploadApiConfiguration",
    "build_command_api_handler",
    "build_health_readiness_handler",
    "build_query_api_handler",
    "build_upload_api_handler",
    "compose_command_api_adapter",
    "compose_query_api_adapter",
    "compose_upload_api_adapter",
    "default_aws_client_factory",
    "load_command_api_configuration",
    "load_query_api_configuration",
    "load_upload_api_configuration",
]
