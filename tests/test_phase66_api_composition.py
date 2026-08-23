from __future__ import annotations

import ast
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

import mr_lister.cloud.phase6_composition as composition
from mr_lister.cloud.api import (
    ReviewQueryApiAdapter,
    SellerCommandApiAdapter,
    UploadApiAdapter,
)
from mr_lister.cloud.artifacts import ExactKeyS3UploadArtifacts
from mr_lister.cloud.http import PROTECTED_ROUTE_KEYS
from mr_lister.cloud.preview import (
    AuthenticatedPreviewLinkIssuer,
    ExactVersionArtworkPreviewService,
)
from mr_lister.control.dynamodb import DynamoDBSellerControlStore
from mr_lister.control.projection import SellerReviewProjectionService
from mr_lister.control.service import SellerControlService
from mr_lister.control.upload_service import UploadIntakeService
from mr_lister.review_profile import ReviewProfileNotFoundError

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = (ROOT / "config/product_profiles/gildan_64000_swiftpod.json").resolve()
PROFILE_FINGERPRINT = "5de1257141cfdacb1731f68bb9113712957483b33d5b0f7115afdba86eb7476c"
REGION = "us-west-2"
ACCOUNT_ID = "123456789012"
BUCKET = f"mr-lister-phase6-artifacts-dev-{ACCOUNT_ID}-{REGION}"
ARTIFACT_ORIGIN = f"https://{BUCKET}.s3.{REGION}.amazonaws.com"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{REGION}_Phase66Pool"


def exact_environment() -> dict[str, object]:
    return {
        "AWS_REGION": REGION,
        "MR_LISTER_STATE_TABLE": "mr-lister-phase6-dev",
        "MR_LISTER_RELEASE_FINGERPRINT": "a" * 64,
        "MR_LISTER_COGNITO_ISSUER": ISSUER,
        "MR_LISTER_COGNITO_CLIENT_ID": "phase66client123",
        "MR_LISTER_COGNITO_SCOPE": composition.SELLER_SCOPE,
        "MR_LISTER_COGNITO_GROUP": composition.SELLER_GROUP,
        "MR_LISTER_ARTIFACT_BUCKET_OWNER_ACCOUNT_ID": ACCOUNT_ID,
        "MR_LISTER_ARTIFACT_BUCKET": BUCKET,
        "MR_LISTER_ARTIFACT_ORIGIN": ARTIFACT_ORIGIN,
        "MR_LISTER_PRODUCT_PROFILE_ID": "gildan_64000_swiftpod",
        "MR_LISTER_PRODUCT_PROFILE_VERSION": "2",
        "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT": PROFILE_FINGERPRINT,
        "MR_LISTER_PRODUCT_PROFILE_PATH": str(PROFILE_PATH),
        "MR_LISTER_APPLICATION_ORIGIN": "https://seller.example.com",
    }


class RecordingDynamoClient:
    def __init__(self) -> None:
        self.operations: list[str] = []

    def get_item(self, **_kwargs: object) -> object:
        self.operations.append("get_item")
        raise AssertionError("No data operation was expected")

    def query(self, **_kwargs: object) -> object:
        self.operations.append("query")
        raise AssertionError("No data operation was expected")

    def put_item(self, **_kwargs: object) -> object:
        self.operations.append("put_item")
        raise AssertionError("No data operation was expected")

    def transact_write_items(self, **_kwargs: object) -> object:
        self.operations.append("transact_write_items")
        raise AssertionError("No data operation was expected")


class RecordingS3Client:
    def __init__(self) -> None:
        self.operations: list[str] = []

    def generate_presigned_post(self, **_kwargs: object) -> object:
        self.operations.append("generate_presigned_post")
        raise AssertionError("No data operation was expected")

    def get_object(self, **_kwargs: object) -> object:
        self.operations.append("get_object")
        raise AssertionError("No data operation was expected")

    def put_object_tagging(self, **_kwargs: object) -> object:
        self.operations.append("put_object_tagging")
        raise AssertionError("No data operation was expected")

    def generate_presigned_url(self, *_args: object, **_kwargs: object) -> object:
        self.operations.append("generate_presigned_url")
        raise AssertionError("No data operation was expected")


class RecordingClientFactory:
    def __init__(self) -> None:
        self.dynamodb = RecordingDynamoClient()
        self.s3 = RecordingS3Client()
        self.calls: list[tuple[str, str]] = []

    def __call__(self, service_name: str, *, region_name: str) -> object:
        self.calls.append((service_name, region_name))
        return {"dynamodb": self.dynamodb, "s3": self.s3}[service_name]


def api_event(
    route_key: str,
    *,
    raw_path: str | None = None,
    body: object | None = None,
    subject: str | None = None,
) -> dict[str, object]:
    event: dict[str, object] = {
        "version": "2.0",
        "routeKey": route_key,
        "rawPath": raw_path or route_key.split(" ", 1)[1],
        "rawQueryString": "",
        "queryStringParameters": None,
        "requestContext": {"requestId": "phase66-request"},
        "isBase64Encoded": False,
    }
    if body is not None:
        event["body"] = body if isinstance(body, str) else json.dumps(body)
    if subject is not None:
        event["requestContext"] = {
            "requestId": "phase66-request",
            "authorizer": {
                "jwt": {
                    "claims": {
                        "iss": ISSUER,
                        "sub": subject,
                        "token_use": "access",
                        "client_id": "phase66client123",
                        "scope": composition.SELLER_SCOPE,
                        "cognito:groups": '["seller"]',
                    }
                }
            },
        }
    return event


def response_body(response: Mapping[str, object]) -> dict[str, object]:
    body = response["body"]
    assert isinstance(body, str)
    decoded = json.loads(body)
    assert isinstance(decoded, dict)
    return decoded


def test_exact_role_route_sets_match_the_existing_adapters_and_protected_contract() -> None:
    assert composition.UPLOAD_ROUTE_KEYS == UploadApiAdapter._allowed_routes
    assert composition.QUERY_ROUTE_KEYS == ReviewQueryApiAdapter._allowed_routes
    assert composition.COMMAND_ROUTE_KEYS == SellerCommandApiAdapter._allowed_routes
    assert (
        composition.UPLOAD_ROUTE_KEYS
        | composition.QUERY_ROUTE_KEYS
        | composition.COMMAND_ROUTE_KEYS
    ) == PROTECTED_ROUTE_KEYS
    assert not (
        composition.UPLOAD_ROUTE_KEYS & composition.QUERY_ROUTE_KEYS
        or composition.UPLOAD_ROUTE_KEYS & composition.COMMAND_ROUTE_KEYS
        or composition.QUERY_ROUTE_KEYS & composition.COMMAND_ROUTE_KEYS
    )


def test_upload_composition_wires_only_store_s3_profile_and_upload_boundary() -> None:
    factory = RecordingClientFactory()
    configuration = composition.load_upload_api_configuration(exact_environment())

    adapter = composition.compose_upload_api_adapter(configuration, client_factory=factory)

    assert isinstance(adapter, UploadApiAdapter)
    assert isinstance(adapter._uploads, UploadIntakeService)
    assert isinstance(adapter._uploads._store, DynamoDBSellerControlStore)
    assert isinstance(adapter._uploads._artifacts, ExactKeyS3UploadArtifacts)
    assert isinstance(adapter._uploads._profiles, composition.PinnedReviewProductAuthority)
    assert adapter._uploads._store._client is factory.dynamodb
    assert adapter._uploads._artifacts._client is factory.s3
    assert factory.calls == [("dynamodb", REGION), ("s3", REGION)]
    assert not factory.dynamodb.operations
    assert not factory.s3.operations


def test_query_composition_wires_only_owner_reads_projection_and_exact_preview() -> None:
    factory = RecordingClientFactory()
    configuration = composition.load_query_api_configuration(exact_environment())

    adapter = composition.compose_query_api_adapter(configuration, client_factory=factory)

    assert isinstance(adapter, ReviewQueryApiAdapter)
    assert isinstance(adapter._store, DynamoDBSellerControlStore)
    assert isinstance(adapter._reviews, SellerReviewProjectionService)
    assert isinstance(adapter._reviews._preview_issuer, AuthenticatedPreviewLinkIssuer)
    assert isinstance(adapter._previews, ExactVersionArtworkPreviewService)
    assert adapter._store._client is factory.dynamodb
    assert adapter._previews._presigner is factory.s3
    assert factory.calls == [("dynamodb", REGION), ("s3", REGION)]
    assert not factory.dynamodb.operations
    assert not factory.s3.operations


def test_command_composition_constructs_no_s3_or_secret_dependency() -> None:
    factory = RecordingClientFactory()
    configuration = composition.load_command_api_configuration(exact_environment())

    adapter = composition.compose_command_api_adapter(configuration, client_factory=factory)

    assert isinstance(adapter, SellerCommandApiAdapter)
    assert isinstance(adapter._commands, SellerControlService)
    assert isinstance(adapter._commands.store, DynamoDBSellerControlStore)
    assert adapter._commands.store._client is factory.dynamodb
    assert factory.calls == [("dynamodb", REGION)]
    assert not factory.dynamodb.operations


def test_command_composition_requires_the_replay_nudge_put_boundary() -> None:
    class MissingPutClient:
        def get_item(self, **_kwargs: object) -> object:
            raise AssertionError

        def transact_write_items(self, **_kwargs: object) -> object:
            raise AssertionError

    def incomplete_factory(service_name: str, *, region_name: str) -> object:
        assert service_name == "dynamodb"
        assert region_name == REGION
        return MissingPutClient()

    configuration = composition.load_command_api_configuration(exact_environment())

    with pytest.raises(RuntimeError, match="dependency"):
        composition.compose_command_api_adapter(
            configuration,
            client_factory=incomplete_factory,
        )


def test_profile_authority_exposes_only_the_pinned_id_version_and_fingerprint() -> None:
    configuration = composition.load_upload_api_configuration(exact_environment())
    authority = composition.PinnedReviewProductAuthority(configuration.profile.exact)

    exact = authority.get_exact(profile_id="gildan_64000_swiftpod", profile_version=2)

    assert exact is configuration.profile.exact
    assert exact.fingerprint == PROFILE_FINGERPRINT
    with pytest.raises(ReviewProfileNotFoundError):
        authority.get_exact(profile_id="synthetic_gildan_5000", profile_version=1)


QUERY_CONFIGURATION_KEYS = frozenset(exact_environment())


@pytest.mark.parametrize("missing", sorted(QUERY_CONFIGURATION_KEYS))
def test_every_missing_query_setting_fails_with_one_value_free_error(missing: str) -> None:
    environment = exact_environment()
    environment.pop(missing)
    environment["AWS_LAMBDA_FUNCTION_NAME"] = "ignored-platform-extra"

    with pytest.raises(composition.Phase6ApiConfigurationError) as caught:
        composition.load_query_api_configuration(environment)

    assert str(caught.value) == "Phase 6 API configuration is invalid"
    assert missing not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AWS_REGION", "eu-west-1"),
        ("MR_LISTER_STATE_TABLE", "mr-lister-phase6-prod"),
        ("MR_LISTER_RELEASE_FINGERPRINT", "0" * 64),
        ("MR_LISTER_RELEASE_FINGERPRINT", "A" * 64),
        (
            "MR_LISTER_COGNITO_ISSUER",
            "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_OtherPool",
        ),
        ("MR_LISTER_COGNITO_CLIENT_ID", " phase66client123"),
        ("MR_LISTER_COGNITO_SCOPE", "mr-lister-api/admin"),
        ("MR_LISTER_COGNITO_GROUP", "administrator"),
        ("MR_LISTER_ARTIFACT_BUCKET_OWNER_ACCOUNT_ID", "999999999999"),
        ("MR_LISTER_ARTIFACT_BUCKET", BUCKET.replace("-dev-", "-prod-")),
        ("MR_LISTER_ARTIFACT_ORIGIN", f"https://{BUCKET}.s3.amazonaws.com"),
        ("MR_LISTER_PRODUCT_PROFILE_ID", "synthetic_gildan_5000"),
        ("MR_LISTER_PRODUCT_PROFILE_VERSION", "1"),
        ("MR_LISTER_PRODUCT_PROFILE_FINGERPRINT", "b" * 64),
        (
            "MR_LISTER_PRODUCT_PROFILE_PATH",
            str((ROOT / "config/product_profiles/synthetic_gildan_5000.json").resolve()),
        ),
        ("MR_LISTER_APPLICATION_ORIGIN", "https://seller.example.com/path"),
    ],
)
def test_malformed_or_cross_setting_drift_fails_with_the_same_error(
    name: str,
    value: str,
) -> None:
    environment = exact_environment()
    environment[name] = value

    with pytest.raises(composition.Phase6ApiConfigurationError) as caught:
        composition.load_query_api_configuration(environment)

    assert str(caught.value) == "Phase 6 API configuration is invalid"
    assert value not in str(caught.value)


def test_role_parsers_ignore_lambda_extras_and_read_only_their_required_settings() -> None:
    environment = exact_environment()
    environment.update(
        {
            "AWS_LAMBDA_FUNCTION_NAME": "phase66-command",
            "AWS_EXECUTION_ENV": "AWS_Lambda_python3.12",
            "UNRELATED_SECRET": "must-not-be-retained",
        }
    )
    command_environment = {
        key: value
        for key, value in environment.items()
        if key
        in {
            "AWS_REGION",
            "MR_LISTER_STATE_TABLE",
            "MR_LISTER_RELEASE_FINGERPRINT",
            "MR_LISTER_COGNITO_ISSUER",
            "MR_LISTER_COGNITO_CLIENT_ID",
            "MR_LISTER_COGNITO_SCOPE",
            "MR_LISTER_COGNITO_GROUP",
            "AWS_LAMBDA_FUNCTION_NAME",
            "UNRELATED_SECRET",
        }
    }
    upload_environment = dict(environment)
    upload_environment.pop("MR_LISTER_APPLICATION_ORIGIN")

    command = composition.load_command_api_configuration(command_environment)
    upload = composition.load_upload_api_configuration(upload_environment)

    assert command.common.state_table == "mr-lister-phase6-dev"
    assert upload.artifacts.bucket == BUCKET
    assert "must-not-be-retained" not in repr(command)
    assert "must-not-be-retained" not in repr(upload)


@pytest.mark.parametrize(
    ("builder_name", "route_key"),
    [
        ("build_upload_api_handler", "POST /v1/uploads"),
        ("build_query_api_handler", "GET /v1/jobs"),
        ("build_command_api_handler", "POST /v1/jobs/{job_id}/cancel"),
    ],
)
def test_role_factories_are_lazy_and_delegate_the_exact_event_and_context(
    monkeypatch: pytest.MonkeyPatch,
    builder_name: str,
    route_key: str,
) -> None:
    delegated: list[tuple[Mapping[str, Any], object | None]] = []
    compositions = {
        "build_upload_api_handler": "compose_upload_api_adapter",
        "build_query_api_handler": "compose_query_api_adapter",
        "build_command_api_handler": "compose_command_api_adapter",
    }

    def delegate(event: Mapping[str, Any], context: object | None = None) -> dict[str, Any]:
        delegated.append((event, context))
        return {"statusCode": 204, "headers": {}, "body": "", "isBase64Encoded": False}

    composed: list[object] = []

    def fake_compose(configuration: object, *, client_factory: object) -> Callable[..., object]:
        composed.append((configuration, client_factory))
        return delegate

    monkeypatch.setattr(composition, compositions[builder_name], fake_compose)
    factory = RecordingClientFactory()
    handler = getattr(composition, builder_name)(exact_environment(), client_factory=factory)
    event = api_event(route_key)
    context = object()

    assert not composed
    response = handler(event, context)

    assert response["statusCode"] == 204
    assert len(composed) == 1
    assert delegated == [(event, context)]
    assert not factory.calls


def test_unrecognized_route_fails_before_dependency_construction() -> None:
    factory = RecordingClientFactory()
    handler = composition.build_upload_api_handler(exact_environment(), client_factory=factory)

    response = handler(api_event("POST /v1/jobs/job/approve"))

    assert response["statusCode"] == 404
    assert response_body(response)["error"]["code"] == "NOT_FOUND"
    assert not factory.calls


def test_authentication_precedes_body_parsing_and_no_request_identity_is_cached() -> None:
    factory = RecordingClientFactory()
    handler = composition.build_upload_api_handler(exact_environment(), client_factory=factory)
    first = api_event("POST /v1/uploads", body="TOKEN-IN-MALFORMED-BODY")
    second = api_event("POST /v1/uploads", body="A-DIFFERENT-MALFORMED-BODY")

    first_response = handler(first)
    second_response = handler(second)

    assert first_response["statusCode"] == second_response["statusCode"] == 401
    assert response_body(first_response)["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert response_body(second_response)["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert factory.calls == [("dynamodb", REGION), ("s3", REGION)]
    assert not factory.dynamodb.operations
    assert not factory.s3.operations
    assert "TOKEN-IN-MALFORMED-BODY" not in repr(handler)
    assert "A-DIFFERENT-MALFORMED-BODY" not in repr(handler)


def test_warm_handler_reuses_only_delegate_and_forwards_each_new_event(monkeypatch) -> None:
    seen_subjects: list[str] = []

    def delegate(event: Mapping[str, Any], _context: object | None = None) -> dict[str, Any]:
        request_context = event["requestContext"]
        assert isinstance(request_context, Mapping)
        authorizer = request_context["authorizer"]
        assert isinstance(authorizer, Mapping)
        jwt = authorizer["jwt"]
        assert isinstance(jwt, Mapping)
        claims = jwt["claims"]
        assert isinstance(claims, Mapping)
        subject = claims["sub"]
        assert isinstance(subject, str)
        seen_subjects.append(subject)
        return {"statusCode": 200, "headers": {}, "body": "{}", "isBase64Encoded": False}

    builds = 0

    def fake_compose(*_args: object, **_kwargs: object) -> Callable[..., object]:
        nonlocal builds
        builds += 1
        return delegate

    monkeypatch.setattr(composition, "compose_query_api_adapter", fake_compose)
    handler = composition.build_query_api_handler(
        exact_environment(), client_factory=RecordingClientFactory()
    )

    handler(api_event("GET /v1/jobs", subject="seller-one"))
    handler(api_event("GET /v1/jobs", subject="seller-two"))

    assert builds == 1
    assert seen_subjects == ["seller-one", "seller-two"]


def test_dependency_exceptions_and_values_never_reach_a_protected_response() -> None:
    class ExplodingFactory:
        def __call__(self, _service_name: str, *, region_name: str) -> object:
            raise RuntimeError(f"TOKEN-SUPER-SECRET-{region_name}")

    handler = composition.build_command_api_handler(
        exact_environment(), client_factory=ExplodingFactory()
    )

    response = handler(api_event("POST /v1/jobs/{job_id}/cancel"))
    serialized = json.dumps(response)

    assert response["statusCode"] == 500
    assert response_body(response)["error"]["code"] == "INTERNAL_ERROR"
    assert "TOKEN-SUPER-SECRET" not in serialized
    assert REGION not in serialized


def test_health_is_separate_minimal_and_constructs_read_dependencies_without_calls() -> None:
    factory = RecordingClientFactory()
    health = composition.build_health_readiness_handler(exact_environment(), client_factory=factory)
    event = api_event(composition.HEALTH_ROUTE_KEY, raw_path="/health")

    first = health(event)
    second = health(event)

    assert (
        first
        == second
        == {
            "statusCode": 200,
            "headers": {
                "Cache-Control": "no-store",
                "Content-Type": "application/json",
                "X-Content-Type-Options": "nosniff",
            },
            "body": '{"status":"ok"}',
            "isBase64Encoded": False,
        }
    )
    assert factory.calls == [("dynamodb", REGION), ("s3", REGION)]
    assert not factory.dynamodb.operations
    assert not factory.s3.operations
    assert ACCOUNT_ID not in json.dumps(first)
    assert PROFILE_FINGERPRINT not in json.dumps(first)


def test_health_rejects_request_content_before_constructing_dependencies() -> None:
    factory = RecordingClientFactory()
    health = composition.build_health_readiness_handler(exact_environment(), client_factory=factory)

    response = health(
        api_event(composition.HEALTH_ROUTE_KEY, raw_path="/health", body={"probe": True})
    )

    assert response["statusCode"] == 400
    assert not factory.calls


def test_health_dependency_failure_is_503_and_contains_no_exception_or_configuration() -> None:
    class ExplodingFactory:
        def __call__(self, _service_name: str, *, region_name: str) -> object:
            raise RuntimeError(f"secret-token:{region_name}:{ACCOUNT_ID}")

    health = composition.build_health_readiness_handler(
        exact_environment(), client_factory=ExplodingFactory()
    )

    response = health(api_event(composition.HEALTH_ROUTE_KEY, raw_path="/health"))

    assert response == {
        "statusCode": 503,
        "headers": {
            "Cache-Control": "no-store",
            "Content-Type": "application/json",
            "X-Content-Type-Options": "nosniff",
        },
        "body": '{"status":"unavailable"}',
        "isBase64Encoded": False,
    }
    assert "secret-token" not in json.dumps(response)
    assert REGION not in json.dumps(response)
    assert ACCOUNT_ID not in json.dumps(response)


def test_query_handler_cannot_serve_health_and_health_cannot_serve_seller_routes() -> None:
    query_factory = RecordingClientFactory()
    health_factory = RecordingClientFactory()
    query = composition.build_query_api_handler(exact_environment(), client_factory=query_factory)
    health = composition.build_health_readiness_handler(
        exact_environment(), client_factory=health_factory
    )

    query_response = query(api_event(composition.HEALTH_ROUTE_KEY, raw_path="/health"))
    health_response = health(api_event("GET /v1/jobs"))

    assert query_response["statusCode"] == health_response["statusCode"] == 404
    assert not query_factory.calls
    assert not health_factory.calls


def test_default_sdk_import_is_inside_the_factory_and_role_imports_are_allowlisted() -> None:
    source_path = ROOT / "src/mr_lister/cloud/phase6_composition.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    top_level_imports = {
        alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names
    }
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert "boto3" not in top_level_imports
    assert not any(module.startswith("mr_lister.production") for module in imported_modules)
    assert "mr_lister.control.agentcore" not in imported_modules
    assert "mr_lister.control.dispatch" not in imported_modules


def test_default_factory_reference_is_not_invoked_while_building_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def forbidden_until_invocation(service_name: str, *, region_name: str) -> object:
        calls.append((service_name, region_name))
        raise AssertionError("client construction must remain lazy")

    monkeypatch.setattr(composition, "default_aws_client_factory", forbidden_until_invocation)

    handler = composition.build_command_api_handler(exact_environment())

    assert not calls
    response = handler(api_event("GET /unrecognized"))
    assert response["statusCode"] == 404
    assert not calls


def test_default_s3_client_pins_regional_virtual_host_and_sigv4_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "phase66testaccess")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "phase66testsecret")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    client = composition.default_aws_client_factory("s3", region_name=REGION)

    post = client.generate_presigned_post(Bucket=BUCKET, Key="private/exact.png")
    get_url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": "private/exact.png", "VersionId": "version-one"},
        ExpiresIn=300,
        HttpMethod="GET",
    )
    query = parse_qs(urlsplit(get_url).query)

    assert post["url"] == f"{ARTIFACT_ORIGIN}/"
    assert urlsplit(get_url).netloc == urlsplit(ARTIFACT_ORIGIN).netloc
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Expires"] == ["300"]
    assert query["versionId"] == ["version-one"]
