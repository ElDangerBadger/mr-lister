"""Capture a sanitized, read-only Phase 6.6 deployment authority.

The capture binds the deployed stack, all ten shared-code Lambda functions, the seller web edge,
health/security/CORS responses, and aggregate Cognito posture without retaining physical resource
identifiers, ARNs, URLs, user identities, credentials, or response bodies.  AWS and HTTP clients
are injected into :func:`capture_phase66_deployment_authority`; only the CLI constructs live
clients, and it requires an explicit profile, Region, stack, source commit, and repo-private output.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import secrets
import stat
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Final, Protocol

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
PRIVATE_OUTPUT_ROOT: Final = REPOSITORY_ROOT / ".mr_lister_private/phase66-acceptance"
FORMAT: Final = "phase6.6-sanitized-deployment-authority-v1"
EXPECTED_ACCOUNT_ID: Final = "384627057108"
EXPECTED_REGION: Final = "us-west-2"
EXPECTED_STACK_NAME: Final = "mr-lister-phase6-dev"
EXPECTED_APPLICATION_ORIGIN: Final = "https://massskutiny.com"
EXPECTED_READINESS: Final = "WEB_EDGE_ACTIVE_DRAFT_ONLY"

_GENERIC_ERROR = "Phase 6.6 deployment-authority capture is invalid"
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_SOURCE_COMMIT = re.compile(r"^[a-f0-9]{40}$")
_FUNCTION_LOGICAL_IDS: Final = (
    "DispatcherFunction",
    "PreparationDispatchFunction",
    "ProviderDraftFunction",
    "ReviewQueryApiFunction",
    "SellerCommandApiFunction",
    "SettlementFunction",
    "SourceVersionRetentionFunction",
    "StuckExecutionRecoveryFunction",
    "TerminalOperationalCleanupFunction",
    "UploadApiFunction",
)
_PHYSICAL_RESOURCE_LOGICAL_IDS: Final = {
    *_FUNCTION_LOGICAL_IDS,
    "SellerHttpApi",
    "SellerUserPool",
    "SellerUserPoolClient",
    "SellerWebDistribution",
}
_LAMBDA_CONFIGURATION_FIELDS: Final = (
    "Architectures",
    "DeadLetterConfig",
    "Environment",
    "EphemeralStorage",
    "FileSystemConfigs",
    "Handler",
    "ImageConfigResponse",
    "KMSKeyArn",
    "Layers",
    "LoggingConfig",
    "MemorySize",
    "PackageType",
    "Role",
    "Runtime",
    "RuntimeVersionConfig",
    "SnapStart",
    "Timeout",
    "TracingConfig",
    "VpcConfig",
)
_SECURITY_HEADERS: Final = (
    "cache-control",
    "content-security-policy",
    "permissions-policy",
    "referrer-policy",
    "strict-transport-security",
    "x-content-type-options",
    "x-frame-options",
)
_CORS_HEADERS: Final = (
    "access-control-allow-credentials",
    "access-control-allow-headers",
    "access-control-allow-methods",
    "access-control-allow-origin",
    "access-control-expose-headers",
    "access-control-max-age",
    "vary",
)
_API_CONFIGURATION_FIELDS: Final = (
    "ApiKeySelectionExpression",
    "CorsConfiguration",
    "Description",
    "DisableExecuteApiEndpoint",
    "IpAddressType",
    "ProtocolType",
    "RouteSelectionExpression",
    "Tags",
    "Version",
)
_MAX_HTTP_BODY_BYTES = 1024 * 1024


class Phase66DeploymentAuthorityError(RuntimeError):
    """A value-free capture or confinement failure."""


class AwsClientProvider(Protocol):
    """Minimal injected AWS client factory."""

    def client(self, service_name: str) -> Any: ...


class HttpClient(Protocol):
    """Minimal injected HTTP request boundary."""

    def request(self, method: str, url: str, *, headers: Mapping[str, str]) -> HttpResponse: ...


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _pretty(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            separators=(",", ": "),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError
    return value


def _sequence(value: object) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError
    return value


def _string(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError
    return value


def _exact_int(value: object) -> int:
    if type(value) is not int:
        raise ValueError
    return value


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError
    normalized = value.astimezone(UTC).replace(microsecond=0)
    return normalized.strftime("%Y-%m-%dT%H:%M:%SZ")


def _client(provider: AwsClientProvider, name: str) -> Any:
    client = provider.client(name)
    if client is None:
        raise ValueError
    return client


def _pages(method: Any, result_key: str, **arguments: object) -> list[Any]:
    values: list[Any] = []
    token: str | None = None
    observed_tokens: set[str] = set()
    for _ in range(100):
        request = dict(arguments)
        if token is not None:
            request["NextToken"] = token
        response = _mapping(method(**request))
        values.extend(_sequence(response.get(result_key, [])))
        next_value = response.get("NextToken")
        if next_value is None:
            return values
        token = _string(next_value)
        if token in observed_tokens:
            raise ValueError
        observed_tokens.add(token)
    raise ValueError


def _user_pages(method: Any, **arguments: object) -> list[Any]:
    values: list[Any] = []
    token: str | None = None
    observed_tokens: set[str] = set()
    for _ in range(100):
        request = dict(arguments)
        if token is not None:
            request["PaginationToken"] = token
        response = _mapping(method(**request))
        values.extend(_sequence(response.get("Users", [])))
        next_value = response.get("PaginationToken")
        if next_value is None:
            return values
        token = _string(next_value)
        if token in observed_tokens:
            raise ValueError
        observed_tokens.add(token)
    raise ValueError


def _stack_capture(
    provider: AwsClientProvider, stack_name: str
) -> tuple[dict[str, object], dict[str, str]]:
    cloudformation = _client(provider, "cloudformation")
    stacks = _sequence(cloudformation.describe_stacks(StackName=stack_name).get("Stacks"))
    if len(stacks) != 1:
        raise ValueError
    stack = _mapping(stacks[0])
    if _string(stack.get("StackName")) != stack_name:
        raise ValueError

    resources = _pages(
        cloudformation.list_stack_resources,
        "StackResourceSummaries",
        StackName=stack_name,
    )
    inventory: list[dict[str, object]] = []
    physical: dict[str, str] = {}
    incomplete = 0
    for raw_resource in resources:
        resource = _mapping(raw_resource)
        logical_id = _string(resource.get("LogicalResourceId"))
        resource_type = _string(resource.get("ResourceType"))
        resource_status = _string(resource.get("ResourceStatus"))
        physical_id = resource.get("PhysicalResourceId")
        if physical_id is not None:
            physical_id = _string(physical_id)
        inventory.append(
            {
                "logical_id": logical_id,
                "physical_id": physical_id,
                "resource_status": resource_status,
                "resource_type": resource_type,
            }
        )
        if not resource_status.endswith("_COMPLETE"):
            incomplete += 1
        if logical_id in _PHYSICAL_RESOURCE_LOGICAL_IDS:
            if physical_id is None or logical_id in physical:
                raise ValueError
            physical[logical_id] = physical_id
    if set(physical) != _PHYSICAL_RESOURCE_LOGICAL_IDS:
        raise ValueError

    outputs: dict[str, str] = {}
    for raw_output in _sequence(stack.get("Outputs", [])):
        output = _mapping(raw_output)
        key = _string(output.get("OutputKey"))
        value = _string(output.get("OutputValue"))
        if key in outputs:
            raise ValueError
        outputs[key] = value
    if outputs.get("DeploymentReadiness") != EXPECTED_READINESS:
        raise ValueError
    if outputs.get("SellerApplicationOrigin") != EXPECTED_APPLICATION_ORIGIN:
        raise ValueError

    template = _mapping(
        cloudformation.get_template(StackName=stack_name, TemplateStage="Processed")
    ).get("TemplateBody")
    if isinstance(template, str):
        template = json.loads(template)
    template = _mapping(template)
    tags = sorted(
        (
            {
                "key": _string(_mapping(tag).get("Key")),
                "value": _string(_mapping(tag).get("Value")),
            }
            for tag in _sequence(stack.get("Tags", []))
        ),
        key=lambda item: item["key"],
    )
    inventory.sort(key=lambda item: (str(item["logical_id"]), str(item["resource_type"])))
    sanitized = {
        "incomplete_resource_count": incomplete,
        "output_count": len(outputs),
        "outputs_digest": _digest(outputs),
        "resource_count": len(inventory),
        "resource_inventory_digest": _digest(inventory),
        "stack_status": _string(stack.get("StackStatus")),
        "tags_digest": _digest(tags),
        "template_digest": _digest(template),
        "termination_protection": stack.get("EnableTerminationProtection") is True,
    }
    return sanitized, {**physical, **outputs}


def _lambda_capture(
    provider: AwsClientProvider, bindings: Mapping[str, str]
) -> list[dict[str, object]]:
    client = _client(provider, "lambda")
    captured: list[dict[str, object]] = []
    for logical_id in _FUNCTION_LOGICAL_IDS:
        response = _mapping(client.get_function_configuration(FunctionName=bindings[logical_id]))
        try:
            raw_code_digest = base64.b64decode(
                _string(response.get("CodeSha256")),
                validate=True,
            )
        except (binascii.Error, ValueError):
            raise ValueError from None
        if len(raw_code_digest) != 32:
            raise ValueError
        configuration = {
            field: response[field] for field in _LAMBDA_CONFIGURATION_FIELDS if field in response
        }
        environment = _mapping(response.get("Environment", {}))
        variables = _mapping(environment.get("Variables", {}))
        release_fingerprint = _string(variables.get("MR_LISTER_RELEASE_FINGERPRINT"))
        captured.append(
            {
                "code_sha256": raw_code_digest.hex(),
                "configuration_digest": _digest(configuration),
                "last_update_status": _string(response.get("LastUpdateStatus")),
                "logical_id": logical_id,
                "release_fingerprint_digest": sha256(
                    release_fingerprint.encode("utf-8")
                ).hexdigest(),
                "state": _string(response.get("State")),
            }
        )
    return captured


def _normalized_headers(headers: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_key, raw_value in headers.items():
        key = _string(raw_key).casefold()
        value = _string(raw_value)
        if key in normalized:
            normalized[key] = f"{normalized[key]}, {value}"
        else:
            normalized[key] = value
    return normalized


def _web_capture(
    provider: AwsClientProvider,
    http_client: HttpClient,
    bindings: Mapping[str, str],
) -> dict[str, object]:
    cloudfront = _client(provider, "cloudfront")
    distribution_id = bindings["SellerWebDistribution"]
    distribution = _mapping(cloudfront.get_distribution(Id=distribution_id)).get("Distribution")
    distribution = _mapping(distribution)
    distribution_config = _mapping(cloudfront.get_distribution_config(Id=distribution_id)).get(
        "DistributionConfig"
    )
    distribution_config = _mapping(distribution_config)

    api = _client(provider, "apigatewayv2")
    api_id = bindings["SellerHttpApi"]
    api_response = _mapping(api.get_api(ApiId=api_id))
    api_config = {
        field: api_response[field] for field in _API_CONFIGURATION_FIELDS if field in api_response
    }
    routes = _pages(api.get_routes, "Items", ApiId=api_id)
    authorizers = _pages(api.get_authorizers, "Items", ApiId=api_id)
    api_authority = {
        "api": api_config,
        "authorizers": sorted(authorizers, key=lambda item: _digest(item)),
        "routes": sorted(routes, key=lambda item: _digest(item)),
    }

    origin = bindings.get("SellerApplicationOrigin")
    if origin != EXPECTED_APPLICATION_ORIGIN:
        raise ValueError
    application = http_client.request("GET", f"{origin}/", headers={})
    health = http_client.request("GET", f"{origin}/health", headers={})
    cors = http_client.request(
        "OPTIONS",
        f"{origin}/v1/jobs",
        headers={
            "Access-Control-Request-Headers": "Authorization",
            "Access-Control-Request-Method": "GET",
            "Origin": origin,
        },
    )
    if any(
        not isinstance(response, HttpResponse)
        or not 0 <= response.status_code <= 999
        or not isinstance(response.body, bytes)
        or len(response.body) > _MAX_HTTP_BODY_BYTES
        for response in (application, health, cors)
    ):
        raise ValueError
    application_headers = _normalized_headers(application.headers)
    security_headers = {
        key: application_headers[key] for key in _SECURITY_HEADERS if key in application_headers
    }
    cors_headers_all = _normalized_headers(cors.headers)
    cors_headers = {key: cors_headers_all[key] for key in _CORS_HEADERS if key in cors_headers_all}
    health_body = json.loads(health.body)
    health_passed = health.status_code == 200 and health_body == {"status": "ok"}
    security_passed = (
        application.status_code == 200
        and security_headers.get("x-content-type-options", "").casefold() == "nosniff"
        and security_headers.get("x-frame-options", "").casefold() == "deny"
        and security_headers.get("referrer-policy", "").casefold() == "no-referrer"
        and "max-age=63072000" in security_headers.get("strict-transport-security", "")
        and "object-src 'none'" in security_headers.get("content-security-policy", "")
        and "frame-ancestors 'none'" in security_headers.get("content-security-policy", "")
        and "no-store" in security_headers.get("cache-control", "")
    )
    allowed_methods = {
        value.strip().upper()
        for value in cors_headers.get("access-control-allow-methods", "").split(",")
        if value.strip()
    }
    allowed_headers = {
        value.strip().casefold()
        for value in cors_headers.get("access-control-allow-headers", "").split(",")
        if value.strip()
    }
    cors_passed = (
        cors.status_code in {200, 204}
        and cors_headers.get("access-control-allow-origin") == origin
        and {"GET", "OPTIONS"}.issubset(allowed_methods)
        and "authorization" in allowed_headers
        and cors_headers.get("access-control-allow-credentials", "false").casefold() != "true"
    )
    if not health_passed or not security_passed or not cors_passed:
        raise ValueError
    aliases = _sequence(distribution_config.get("Aliases", {}).get("Items", []))
    origins = _sequence(distribution_config.get("Origins", {}).get("Items", []))
    return {
        "alias_count": len(aliases),
        "api_configuration_digest": _digest(api_authority),
        "api_protocol": _string(api_response.get("ProtocolType")),
        "application_body_digest": sha256(application.body).hexdigest(),
        "application_status_code": _exact_int(application.status_code),
        "cors_headers_digest": _digest(cors_headers),
        "cors_passed": True,
        "cors_status_code": _exact_int(cors.status_code),
        "distribution_configuration_digest": _digest(distribution_config),
        "distribution_enabled": distribution_config.get("Enabled") is True,
        "distribution_status": _string(distribution.get("Status")),
        "health_body_digest": sha256(health.body).hexdigest(),
        "health_passed": True,
        "health_status_code": _exact_int(health.status_code),
        "origin_count": len(origins),
        "route_count": len(routes),
        "security_header_count": len(security_headers),
        "security_headers_digest": _digest(security_headers),
        "security_headers_passed": True,
    }


def _cognito_capture(provider: AwsClientProvider, bindings: Mapping[str, str]) -> dict[str, object]:
    client = _client(provider, "cognito-idp")
    pool_id = bindings["SellerUserPool"]
    client_id = bindings["SellerUserPoolClient"]
    pool = _mapping(client.describe_user_pool(UserPoolId=pool_id)).get("UserPool")
    pool = _mapping(pool)
    browser_client = _mapping(
        client.describe_user_pool_client(UserPoolId=pool_id, ClientId=client_id)
    ).get("UserPoolClient")
    browser_client = _mapping(browser_client)
    mfa = _mapping(client.get_user_pool_mfa_config(UserPoolId=pool_id))
    users = _user_pages(client.list_users, UserPoolId=pool_id, Limit=60)
    group_users = _pages(
        client.list_users_in_group,
        "Users",
        UserPoolId=pool_id,
        GroupName="seller",
        Limit=60,
    )
    usernames: set[str] = set()
    confirmed = 0
    enabled = 0
    software_token = 0
    for raw_user in users:
        user = _mapping(raw_user)
        username = _string(user.get("Username"))
        if username in usernames:
            raise ValueError
        usernames.add(username)
        confirmed += user.get("UserStatus") == "CONFIRMED"
        enabled += user.get("Enabled") is True
        detail = _mapping(client.admin_get_user(UserPoolId=pool_id, Username=username))
        settings = set(_sequence(detail.get("UserMFASettingList", [])))
        software_token += "SOFTWARE_TOKEN_MFA" in settings
    group_names = {_string(_mapping(user).get("Username")) for user in group_users}
    if not group_names.issubset(usernames):
        raise ValueError
    pool_projection = {
        key: value
        for key, value in pool.items()
        if key
        in {
            "AccountRecoverySetting",
            "AdminCreateUserConfig",
            "AutoVerifiedAttributes",
            "DeletionProtection",
            "EmailConfiguration",
            "LambdaConfig",
            "MfaConfiguration",
            "Policies",
            "SchemaAttributes",
            "UserAttributeUpdateSettings",
            "UsernameAttributes",
            "UsernameConfiguration",
            "VerificationMessageTemplate",
        }
    }
    client_projection = {
        key: value
        for key, value in browser_client.items()
        if key
        in {
            "AccessTokenValidity",
            "AllowedOAuthFlows",
            "AllowedOAuthFlowsUserPoolClient",
            "AllowedOAuthScopes",
            "AuthSessionValidity",
            "CallbackURLs",
            "EnableTokenRevocation",
            "ExplicitAuthFlows",
            "GenerateSecret",
            "IdTokenValidity",
            "LogoutURLs",
            "PreventUserExistenceErrors",
            "ReadAttributes",
            "RefreshTokenValidity",
            "SupportedIdentityProviders",
            "TokenValidityUnits",
            "WriteAttributes",
        }
    }
    return {
        "browser_client_configuration_digest": _digest(client_projection),
        "browser_client_secret_present": bool(browser_client.get("ClientSecret")),
        "confirmed_user_count": confirmed,
        "enabled_user_count": enabled,
        "mfa_configuration": _string(mfa.get("MfaConfiguration")),
        "pool_configuration_digest": _digest(pool_projection),
        "seller_group_member_count": len(group_names),
        "software_token_mfa_user_count": software_token,
        "user_count": len(users),
    }


def _reject_raw_authority(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError
            _reject_raw_authority(nested)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value:
            _reject_raw_authority(nested)
    elif isinstance(value, str):
        lowered = value.casefold()
        if (
            "arn:" in lowered
            or "://" in lowered
            or "bearer " in lowered
            or "file:" in lowered
            or "/users/" in lowered
            or re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value)
        ):
            raise ValueError


def capture_phase66_deployment_authority(
    *,
    aws_clients: AwsClientProvider,
    http_client: HttpClient,
    region: str,
    stack_name: str,
    source_commit: str,
    captured_at: datetime | None = None,
) -> dict[str, object]:
    """Capture one deterministic sanitized authority through injected read-only clients."""

    try:
        if (
            region != EXPECTED_REGION
            or stack_name != EXPECTED_STACK_NAME
            or _SOURCE_COMMIT.fullmatch(source_commit) is None
        ):
            raise ValueError
        sts = _client(aws_clients, "sts")
        caller = _mapping(sts.get_caller_identity())
        if caller.get("Account") != EXPECTED_ACCOUNT_ID:
            raise ValueError
        stack, bindings = _stack_capture(aws_clients, stack_name)
        authority = {
            "account_binding_digest": sha256(EXPECTED_ACCOUNT_ID.encode("ascii")).hexdigest(),
            "cognito": _cognito_capture(aws_clients, bindings),
            "lambdas": _lambda_capture(aws_clients, bindings),
            "readiness": EXPECTED_READINESS,
            "region": region,
            "source_commit_digest": sha256(source_commit.encode("ascii")).hexdigest(),
            "stack": stack,
            "stack_name": stack_name,
            "web_edge": _web_capture(aws_clients, http_client, bindings),
        }
        _reject_raw_authority(authority)
        deployment_digest = _digest(authority)
        if _DIGEST.fullmatch(deployment_digest) is None:
            raise ValueError
        document = {
            "authority": authority,
            "captured_at": _timestamp(datetime.now(UTC) if captured_at is None else captured_at),
            "deployment_digest": deployment_digest,
            "format": FORMAT,
        }
        _reject_raw_authority(document)
        return document
    except Phase66DeploymentAuthorityError:
        raise
    except Exception:
        raise Phase66DeploymentAuthorityError(_GENERIC_ERROR) from None


def _private_output(path: Path) -> Path:
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(PRIVATE_OUTPUT_ROOT)
    except ValueError:
        raise ValueError from None
    if not relative.parts:
        raise ValueError
    current = REPOSITORY_ROOT
    repository_metadata = current.lstat()
    if not stat.S_ISDIR(repository_metadata.st_mode) or stat.S_ISLNK(repository_metadata.st_mode):
        raise ValueError
    for component in candidate.parent.relative_to(REPOSITORY_ROOT).parts:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError
        current.chmod(0o700)
    if candidate.exists() or candidate.is_symlink():
        raise ValueError
    return candidate


def write_phase66_deployment_authority(path: Path, document: Mapping[str, object]) -> Path:
    """Atomically create one owner-only file inside the repo-private acceptance root."""

    temporary: Path | None = None
    descriptor: int | None = None
    try:
        if set(document) != {"authority", "captured_at", "deployment_digest", "format"}:
            raise ValueError
        authority = _mapping(document.get("authority"))
        if (
            document.get("format") != FORMAT
            or document.get("deployment_digest") != _digest(authority)
            or _DIGEST.fullmatch(_string(document.get("deployment_digest"))) is None
            or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
                _string(document.get("captured_at")),
            )
        ):
            raise ValueError
        _reject_raw_authority(document)
        rendered = _pretty(document)
        output = _private_output(path)
        temporary = output.with_name(f".{output.name}.{secrets.token_hex(12)}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output, follow_symlinks=False)
        temporary.unlink()
        temporary = None
        output.chmod(0o600)
        return output
    except Phase66DeploymentAuthorityError:
        raise
    except Exception:
        raise Phase66DeploymentAuthorityError(_GENERIC_ERROR) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


class _Boto3Provider:
    def __init__(self, profile: str, region: str) -> None:
        import boto3

        self._session = boto3.Session(profile_name=profile, region_name=region)

    def client(self, service_name: str) -> Any:
        return self._session.client(service_name)


class _UrllibHttpClient:
    def request(self, method: str, url: str, *, headers: Mapping[str, str]) -> HttpResponse:
        request = urllib.request.Request(url, method=method, headers=dict(headers))
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return HttpResponse(
                    status_code=response.status,
                    headers=dict(response.headers.items()),
                    body=response.read(1024 * 1024 + 1),
                )
        except urllib.error.HTTPError as error:
            return HttpResponse(
                status_code=error.code,
                headers=dict(error.headers.items()),
                body=error.read(1024 * 1024 + 1),
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--stack", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        if not arguments.profile or arguments.profile != arguments.profile.strip():
            raise ValueError
        document = capture_phase66_deployment_authority(
            aws_clients=_Boto3Provider(arguments.profile, arguments.region),
            http_client=_UrllibHttpClient(),
            region=arguments.region,
            stack_name=arguments.stack,
            source_commit=arguments.source_commit,
        )
        output = write_phase66_deployment_authority(arguments.output, document)
    except Exception:
        print(_GENERIC_ERROR)
        return 2
    print(
        json.dumps(
            {
                "deployment_digest": document["deployment_digest"],
                "result": "passed",
                "target_sha256": sha256(output.read_bytes()).hexdigest(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
