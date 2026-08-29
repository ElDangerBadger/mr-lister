from __future__ import annotations

import base64
import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import tools.capture_phase66_deployment_authority as capture

SOURCE_COMMIT = "a" * 40
NOW = datetime(2026, 8, 29, 18, 0, tzinfo=UTC)


class _Sts:
    def get_caller_identity(self) -> dict[str, object]:
        return {
            "Account": capture.EXPECTED_ACCOUNT_ID,
            "Arn": "arn:aws:iam::384627057108:user/not-retained",
            "UserId": "not-retained",
        }


class _CloudFormation:
    def __init__(self) -> None:
        logical_ids = [
            *capture._FUNCTION_LOGICAL_IDS,
            "SellerHttpApi",
            "SellerUserPool",
            "SellerUserPoolClient",
            "SellerWebDistribution",
        ]
        self.resources = [
            {
                "LogicalResourceId": logical_id,
                "PhysicalResourceId": f"physical-{logical_id}",
                "ResourceStatus": "UPDATE_COMPLETE",
                "ResourceType": (
                    "AWS::Serverless::Function"
                    if logical_id in capture._FUNCTION_LOGICAL_IDS
                    else "Custom::Test"
                ),
            }
            for logical_id in logical_ids
        ]
        self.outputs = [
            {"OutputKey": "DeploymentReadiness", "OutputValue": capture.EXPECTED_READINESS},
            {
                "OutputKey": "SellerApplicationOrigin",
                "OutputValue": capture.EXPECTED_APPLICATION_ORIGIN,
            },
            {"OutputKey": "AnotherOutput", "OutputValue": "arn:aws:test:not-retained"},
        ]

    def describe_stacks(self, **_kwargs: object) -> dict[str, object]:
        return {
            "Stacks": [
                {
                    "EnableTerminationProtection": True,
                    "Outputs": self.outputs,
                    "StackName": capture.EXPECTED_STACK_NAME,
                    "StackStatus": "UPDATE_COMPLETE",
                    "Tags": [
                        {"Key": "Project", "Value": "MrLister"},
                        {"Key": "Environment", "Value": "dev"},
                    ],
                }
            ]
        }

    def list_stack_resources(self, **kwargs: object) -> dict[str, object]:
        if "NextToken" not in kwargs:
            return {"StackResourceSummaries": self.resources[:7], "NextToken": "page-2"}
        return {"StackResourceSummaries": self.resources[7:]}

    def get_template(self, **_kwargs: object) -> dict[str, object]:
        return {
            "TemplateBody": {
                "Resources": {
                    "ReviewQueryApiFunction": {
                        "Properties": {"CodeUri": {"Version": "not-retained"}}
                    }
                }
            }
        }


class _Lambda:
    def get_function_configuration(self, *, FunctionName: str) -> dict[str, object]:
        is_review = FunctionName.endswith("ReviewQueryApiFunction")
        code = b"r" * 32 if is_review else b"s" * 32
        release = "new-hotfix-release" if is_review else "shared-release"
        return {
            "Architectures": ["arm64"],
            "CodeSha256": base64.b64encode(code).decode(),
            "Environment": {
                "Variables": {
                    "MR_LISTER_RELEASE_FINGERPRINT": release,
                    "PRIVATE_RESOURCE_ARN": "arn:aws:test:not-retained",
                }
            },
            "Handler": "phase6_lambda.handler",
            "LastUpdateStatus": "Successful",
            "MemorySize": 512 if is_review else 256,
            "Role": "arn:aws:iam::384627057108:role/not-retained",
            "Runtime": "python3.12",
            "State": "Active",
            "Timeout": 30 if is_review else 15,
        }


class _CloudFront:
    def get_distribution(self, **_kwargs: object) -> dict[str, object]:
        return {
            "Distribution": {
                "ARN": "arn:aws:cloudfront::384627057108:distribution/not-retained",
                "DomainName": "not-retained.cloudfront.net",
                "Status": "Deployed",
            }
        }

    def get_distribution_config(self, **_kwargs: object) -> dict[str, object]:
        return {
            "DistributionConfig": {
                "Aliases": {"Items": ["massskutiny.com"], "Quantity": 1},
                "Enabled": True,
                "Origins": {
                    "Items": [
                        {"DomainName": "private-bucket.s3.amazonaws.com"},
                        {"DomainName": "private-api.execute-api.amazonaws.com"},
                    ],
                    "Quantity": 2,
                },
                "ViewerCertificate": {
                    "ACMCertificateArn": "arn:aws:acm:us-east-1:384627057108:not-retained"
                },
            }
        }


class _Api:
    def get_api(self, **_kwargs: object) -> dict[str, object]:
        return {
            "ApiEndpoint": "https://private-api.execute-api.us-west-2.amazonaws.com",
            "CreatedDate": NOW,
            "CorsConfiguration": {
                "AllowHeaders": ["Authorization"],
                "AllowMethods": ["GET", "OPTIONS"],
                "AllowOrigins": [capture.EXPECTED_APPLICATION_ORIGIN],
            },
            "ProtocolType": "HTTP",
        }

    def get_routes(self, **_kwargs: object) -> dict[str, object]:
        return {"Items": [{"RouteKey": f"GET /route-{index}"} for index in range(15)]}

    def get_authorizers(self, **_kwargs: object) -> dict[str, object]:
        return {
            "Items": [
                {
                    "AuthorizerType": "JWT",
                    "JwtConfiguration": {
                        "Issuer": "https://private-idp.example.invalid",
                    },
                }
            ]
        }


class _Cognito:
    users = (
        {"Username": "seller-a-private", "UserStatus": "CONFIRMED", "Enabled": True},
        {"Username": "seller-b-private", "UserStatus": "CONFIRMED", "Enabled": True},
        {"Username": "seller-c-private", "UserStatus": "FORCE_CHANGE_PASSWORD", "Enabled": True},
    )

    def describe_user_pool(self, **_kwargs: object) -> dict[str, object]:
        return {
            "UserPool": {
                "Arn": "arn:aws:cognito-idp:not-retained",
                "MfaConfiguration": "ON",
                "Policies": {"PasswordPolicy": {"MinimumLength": 16}},
                "UsernameConfiguration": {"CaseSensitive": False},
            }
        }

    def describe_user_pool_client(self, **_kwargs: object) -> dict[str, object]:
        return {
            "UserPoolClient": {
                "AllowedOAuthFlows": ["code"],
                "AllowedOAuthFlowsUserPoolClient": True,
                "AllowedOAuthScopes": ["openid", "mr-lister-api/seller"],
                "CallbackURLs": [f"{capture.EXPECTED_APPLICATION_ORIGIN}/auth/callback"],
                "GenerateSecret": False,
                "SupportedIdentityProviders": ["COGNITO"],
            }
        }

    def get_user_pool_mfa_config(self, **_kwargs: object) -> dict[str, object]:
        return {"MfaConfiguration": "ON", "SoftwareTokenMfaConfiguration": {"Enabled": True}}

    def list_users(self, **kwargs: object) -> dict[str, object]:
        if "PaginationToken" not in kwargs:
            return {"Users": list(self.users[:2]), "PaginationToken": "users-2"}
        return {"Users": list(self.users[2:])}

    def list_users_in_group(self, **_kwargs: object) -> dict[str, object]:
        return {"Users": list(self.users)}

    def admin_get_user(self, *, Username: str, **_kwargs: object) -> dict[str, object]:
        return {
            "Username": Username,
            "UserMFASettingList": (
                ["SOFTWARE_TOKEN_MFA"] if Username != "seller-c-private" else []
            ),
        }


class _Provider:
    def __init__(self) -> None:
        self.cloudformation = _CloudFormation()
        self.clients = {
            "apigatewayv2": _Api(),
            "cloudformation": self.cloudformation,
            "cloudfront": _CloudFront(),
            "cognito-idp": _Cognito(),
            "lambda": _Lambda(),
            "sts": _Sts(),
        }

    def client(self, service_name: str) -> Any:
        return self.clients[service_name]


class _Http:
    def __init__(self, *, security: bool = True, cors: bool = True) -> None:
        self.security = security
        self.cors = cors
        self.requests: list[tuple[str, str]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
    ) -> capture.HttpResponse:
        self.requests.append((method, url))
        if url.endswith("/health"):
            return capture.HttpResponse(
                200, {"Content-Type": "application/json"}, b'{"status":"ok"}'
            )
        if method == "OPTIONS":
            return capture.HttpResponse(
                204,
                {
                    "Access-Control-Allow-Headers": "Authorization, Content-Type",
                    "Access-Control-Allow-Methods": "GET, OPTIONS",
                    "Access-Control-Allow-Origin": (
                        capture.EXPECTED_APPLICATION_ORIGIN
                        if self.cors
                        else "https://wrong.invalid"
                    ),
                },
                b"",
            )
        security_headers = {
            "Cache-Control": "private, no-store, max-age=0",
            "Content-Security-Policy": (
                "default-src 'self'; object-src 'none'; frame-ancestors 'none'"
            ),
            "Permissions-Policy": "camera=(), geolocation=(), microphone=()",
            "Referrer-Policy": "no-referrer",
            "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
            "X-Content-Type-Options": "nosniff" if self.security else "wrong",
            "X-Frame-Options": "DENY",
        }
        return capture.HttpResponse(200, security_headers, b"web-application-bytes")


def _capture(
    *,
    provider: _Provider | None = None,
    http: _Http | None = None,
    captured_at: datetime = NOW,
) -> dict[str, object]:
    return capture.capture_phase66_deployment_authority(
        aws_clients=_Provider() if provider is None else provider,
        http_client=_Http() if http is None else http,
        region=capture.EXPECTED_REGION,
        stack_name=capture.EXPECTED_STACK_NAME,
        source_commit=SOURCE_COMMIT,
        captured_at=captured_at,
    )


def test_capture_is_sanitized_complete_and_binds_review_hotfix() -> None:
    http = _Http()

    document = _capture(http=http)

    authority = document["authority"]
    assert document["format"] == capture.FORMAT
    assert document["captured_at"] == "2026-08-29T18:00:00Z"
    assert document["deployment_digest"] == capture._digest(authority)
    assert authority["source_commit_digest"] == capture.sha256(SOURCE_COMMIT.encode()).hexdigest()
    assert authority["stack"] == {
        "incomplete_resource_count": 0,
        "output_count": 3,
        "outputs_digest": authority["stack"]["outputs_digest"],
        "resource_count": 14,
        "resource_inventory_digest": authority["stack"]["resource_inventory_digest"],
        "stack_status": "UPDATE_COMPLETE",
        "tags_digest": authority["stack"]["tags_digest"],
        "template_digest": authority["stack"]["template_digest"],
        "termination_protection": True,
    }
    functions = {item["logical_id"]: item for item in authority["lambdas"]}
    assert set(functions) == set(capture._FUNCTION_LOGICAL_IDS)
    assert functions["ReviewQueryApiFunction"]["code_sha256"] == (b"r" * 32).hex()
    assert (
        functions["ReviewQueryApiFunction"]["code_sha256"]
        != functions["UploadApiFunction"]["code_sha256"]
    )
    assert (
        functions["ReviewQueryApiFunction"]["release_fingerprint_digest"]
        != functions["UploadApiFunction"]["release_fingerprint_digest"]
    )
    assert authority["web_edge"]["health_passed"] is True
    assert authority["web_edge"]["security_headers_passed"] is True
    assert authority["web_edge"]["cors_passed"] is True
    assert authority["cognito"]["user_count"] == 3
    assert authority["cognito"]["confirmed_user_count"] == 2
    assert authority["cognito"]["software_token_mfa_user_count"] == 2
    assert authority["cognito"]["seller_group_member_count"] == 3
    assert http.requests == [
        ("GET", "https://massskutiny.com/"),
        ("GET", "https://massskutiny.com/health"),
        ("OPTIONS", "https://massskutiny.com/v1/jobs"),
    ]

    rendered = json.dumps(document, sort_keys=True)
    assert "arn:" not in rendered.casefold()
    assert "://" not in rendered
    assert "seller-a-private" not in rendered
    assert "/Users/" not in rendered


def test_deployment_digest_is_stable_across_capture_times() -> None:
    first = _capture(captured_at=NOW)
    second = _capture(captured_at=NOW + timedelta(minutes=5))

    assert first["captured_at"] != second["captured_at"]
    assert first["authority"] == second["authority"]
    assert first["deployment_digest"] == second["deployment_digest"]


@pytest.mark.parametrize("failure", ("stack", "lambda", "security", "cors", "account"))
def test_capture_fails_closed_on_incomplete_or_drifted_authority(failure: str) -> None:
    provider = _Provider()
    http = _Http()
    if failure == "stack":
        provider.cloudformation.resources.pop()
    elif failure == "lambda":
        provider.cloudformation.resources = [
            resource
            for resource in provider.cloudformation.resources
            if resource["LogicalResourceId"] != "ReviewQueryApiFunction"
        ]
    elif failure == "security":
        http.security = False
    elif failure == "cors":
        http.cors = False
    else:
        provider.clients["sts"] = type(
            "WrongAccount",
            (),
            {"get_caller_identity": lambda _self: {"Account": "000000000000"}},
        )()

    with pytest.raises(capture.Phase66DeploymentAuthorityError):
        _capture(provider=provider, http=http)


@pytest.mark.parametrize(
    ("region", "stack_name", "source_commit"),
    (
        ("us-east-1", capture.EXPECTED_STACK_NAME, SOURCE_COMMIT),
        (capture.EXPECTED_REGION, "another-stack", SOURCE_COMMIT),
        (capture.EXPECTED_REGION, capture.EXPECTED_STACK_NAME, "A" * 40),
    ),
)
def test_capture_requires_exact_live_binding(
    region: str,
    stack_name: str,
    source_commit: str,
) -> None:
    with pytest.raises(capture.Phase66DeploymentAuthorityError):
        capture.capture_phase66_deployment_authority(
            aws_clients=_Provider(),
            http_client=_Http(),
            region=region,
            stack_name=stack_name,
            source_commit=source_commit,
            captured_at=NOW,
        )


def test_private_write_is_atomic_create_only_and_owner_confined(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    private_root = repository / ".mr_lister_private/phase66-acceptance"
    output = private_root / "run/deployment-authority.json"
    monkeypatch.setattr(capture, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(capture, "PRIVATE_OUTPUT_ROOT", private_root)
    document = _capture()

    assert capture.write_phase66_deployment_authority(output, document) == output
    assert json.loads(output.read_bytes()) == document
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
    with pytest.raises(capture.Phase66DeploymentAuthorityError):
        capture.write_phase66_deployment_authority(output, document)
    with pytest.raises(capture.Phase66DeploymentAuthorityError):
        capture.write_phase66_deployment_authority(repository / "public.json", document)


def test_private_write_rejects_symlinked_private_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (repository / ".mr_lister_private").symlink_to(outside, target_is_directory=True)
    private_root = repository / ".mr_lister_private/phase66-acceptance"
    monkeypatch.setattr(capture, "REPOSITORY_ROOT", repository)
    monkeypatch.setattr(capture, "PRIVATE_OUTPUT_ROOT", private_root)

    with pytest.raises(capture.Phase66DeploymentAuthorityError):
        capture.write_phase66_deployment_authority(
            private_root / "deployment-authority.json",
            _capture(),
        )
    assert not (outside / "phase66-acceptance/deployment-authority.json").exists()


def test_writer_rejects_raw_authority_even_inside_an_untrusted_document(
    tmp_path: Path,
) -> None:
    with pytest.raises(capture.Phase66DeploymentAuthorityError):
        capture.write_phase66_deployment_authority(
            tmp_path / "outside.json",
            {"raw": "https://private.example.invalid"},
        )
