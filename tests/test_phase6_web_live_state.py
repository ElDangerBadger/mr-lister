from __future__ import annotations

import ast
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

import tools.verify_phase6_web_live_state as live
from tools.verify_phase6_web_live_state import (
    Phase6WebLiveStateError,
    canonical_phase6_web_live_state,
    verify_phase6_web_live_state,
)

NOW = datetime(2026, 8, 26, 12, 5, tzinfo=UTC)
CAPTURE_TIME = "2026-08-26T12:00:00Z"
DISTRIBUTION_ID = "E1234567890ABC"
DISTRIBUTION_DOMAIN = "d1234567890abc.cloudfront.net"
OAC_ID = "EABCDEFGHIJKLM"
API_ID = "a1b2c3d4e5"
USER_POOL_ID = "us-west-2_AbCdEf123"
CLIENT_ID = "a" * 26
SIGN_IN_ORIGIN = "https://mr-lister-dev-384627057108.auth.us-west-2.amazoncognito.com"
API_ORIGIN = f"https://{API_ID}.execute-api.us-west-2.amazonaws.com"

ROUTE_KEYS = (
    "GET /health",
    "GET /v1/jobs",
    "GET /v1/jobs/{job_id}",
    "GET /v1/jobs/{job_id}/artwork-preview",
    "GET /v1/jobs/{job_id}/review",
    "GET /v1/uploads/{upload_id}",
    "POST /v1/jobs/{job_id}/approve",
    "POST /v1/jobs/{job_id}/cancel",
    "POST /v1/jobs/{job_id}/economics/refresh",
    "POST /v1/jobs/{job_id}/retry",
    "POST /v1/uploads",
    "POST /v1/uploads/{upload_id}/authorize",
    "POST /v1/uploads/{upload_id}/cancel",
    "POST /v1/uploads/{upload_id}/complete",
    "PUT /v1/jobs/{job_id}/review/listing",
)


def _runtime_config() -> str:
    return json.dumps(
        {
            "cognito_authorize_url": f"{SIGN_IN_ORIGIN}/oauth2/authorize",
            "cognito_token_url": f"{SIGN_IN_ORIGIN}/oauth2/token",
            "cognito_logout_url": f"{SIGN_IN_ORIGIN}/logout",
            "client_id": CLIENT_ID,
            "redirect_uri": "https://massskutiny.com/auth/callback",
            "scopes": ["openid", "mr-lister-api/seller"],
        },
        separators=(",", ":"),
    )


def _outputs() -> dict[str, str]:
    account = live.ACCOUNT_ID
    region = live.REGION
    state_machine = f"arn:aws:states:{region}:{account}:stateMachine:"
    return {
        "ArtifactBucketBrowserOrigin": (
            "https://mr-lister-phase6-artifacts-dev-384627057108-us-west-2."
            "s3.us-west-2.amazonaws.com"
        ),
        "ArtifactBucketName": live.ARTIFACT_BUCKET_NAME,
        "DeploymentReadiness": live.READINESS,
        "OperationalAlarmTopicArn": (
            f"arn:aws:sns:{region}:{account}:mr-lister-phase6-dev-operational-alarms"
        ),
        "PrepareStateMachineArn": f"{state_machine}mr-lister-phase6-dev-prepare",
        "ReconcileProductStateMachineArn": (
            f"{state_machine}mr-lister-phase6-dev-reconcile-product"
        ),
        "RefreshEconomicsStateMachineArn": (
            f"{state_machine}mr-lister-phase6-dev-refresh-economics"
        ),
        "SellerApiOrigin": API_ORIGIN,
        "SellerApplicationOrigin": live.APPLICATION_ORIGIN,
        "SellerRuntimeConfig": _runtime_config(),
        "SellerRuntimeConfigObjectKey": "runtime-config.json",
        "SellerSignInOrigin": SIGN_IN_ORIGIN,
        "SellerUserPoolClientId": CLIENT_ID,
        "SellerUserPoolId": USER_POOL_ID,
        "SellerWebAssetBucketName": live.WEB_BUCKET_NAME,
        "SellerWebDistributionDomainName": DISTRIBUTION_DOMAIN,
        "SellerWebDistributionId": DISTRIBUTION_ID,
        "StateTableName": live.STACK_NAME,
        "SynchronizeProductStateMachineArn": (
            f"{state_machine}mr-lister-phase6-dev-synchronize-product"
        ),
    }


def _document() -> dict[str, object]:
    bucket_arn = f"arn:aws:s3:::{live.WEB_BUCKET_NAME}"
    distribution_arn = f"arn:aws:cloudfront::{live.ACCOUNT_ID}:distribution/{DISTRIBUTION_ID}"
    return {
        "account_id": live.ACCOUNT_ID,
        "capture_time": CAPTURE_TIME,
        "certificate": {
            "arn": live.CERTIFICATE_ARN,
            "domain_name": live.APEX_DOMAIN,
            "in_use_by": [distribution_arn],
            "status": "ISSUED",
            "subject_names": [live.APEX_DOMAIN],
        },
        "cloudfront": {
            "aliases": [live.APEX_DOMAIN],
            "arn": distribution_arn,
            "domain_name": DISTRIBUTION_DOMAIN,
            "enabled": True,
            "id": DISTRIBUTION_ID,
            "origin_access_control": {
                "id": OAC_ID,
                "origin_type": "s3",
                "signing_behavior": "always",
                "signing_protocol": "sigv4",
            },
            "origins": [
                {
                    "domain_name": (f"{live.WEB_BUCKET_NAME}.s3.{live.REGION}.amazonaws.com"),
                    "id": "SellerWebAssets",
                    "origin_access_control_id": OAC_ID,
                    "origin_access_identity": "",
                },
                {
                    "domain_name": f"{API_ID}.execute-api.{live.REGION}.amazonaws.com",
                    "https_port": 443,
                    "id": "SellerApi",
                    "origin_protocol_policy": "https-only",
                    "origin_ssl_protocols": ["TLSv1.2"],
                },
            ],
            "status": "Deployed",
            "viewer_certificate": {
                "acm_certificate_arn": live.CERTIFICATE_ARN,
                "minimum_protocol_version": "TLSv1.2_2021",
                "ssl_support_method": "sni-only",
            },
        },
        "cognito": {
            "client": {
                "allowed_oauth_flows": ["code"],
                "allowed_oauth_flows_user_pool_client": True,
                "allowed_oauth_scopes": ["openid", "mr-lister-api/seller"],
                "callback_urls": ["https://massskutiny.com/auth/callback"],
                "client_id": CLIENT_ID,
                "client_name": "mr-lister-phase6-dev-browser",
                "client_secret_present": False,
                "logout_urls": ["https://massskutiny.com/"],
                "supported_identity_providers": ["COGNITO"],
                "user_pool_id": USER_POOL_ID,
            },
            "domain": {
                "prefix": "mr-lister-dev-384627057108",
                "user_pool_id": USER_POOL_ID,
            },
            "user_pool": {
                "arn": (
                    f"arn:aws:cognito-idp:{live.REGION}:{live.ACCOUNT_ID}:userpool/{USER_POOL_ID}"
                ),
                "id": USER_POOL_ID,
                "name": "mr-lister-phase6-dev-sellers",
                "status": "Enabled",
            },
        },
        "format": live.FORMAT,
        "http_api": {
            "api_endpoint": API_ORIGIN,
            "api_id": API_ID,
            "authorizer": {
                "audience": [CLIENT_ID],
                "authorization_type": "JWT",
                "identity_source": ["$request.header.Authorization"],
                "issuer": (f"https://cognito-idp.{live.REGION}.amazonaws.com/{USER_POOL_ID}"),
                "name": "SellerJwtAuthorizer",
            },
            "cors": {
                "allow_credentials": False,
                "allow_headers": [
                    "Authorization",
                    "Content-Type",
                    "Idempotency-Key",
                    "If-Match",
                ],
                "allow_methods": ["GET", "POST", "PUT", "OPTIONS"],
                "allow_origins": [live.APPLICATION_ORIGIN],
                "expose_headers": ["ETag", "Retry-After", "X-Request-Id"],
                "max_age": 300,
            },
            "default_stage": {"auto_deploy": True, "name": "$default"},
            "disable_execute_api_endpoint": False,
            "protocol_type": "HTTP",
            "routes": [
                {
                    "authorization_scopes": (
                        [] if route == "GET /health" else ["mr-lister-api/seller"]
                    ),
                    "authorization_type": ("NONE" if route == "GET /health" else "JWT"),
                    "route_key": route,
                }
                for route in ROUTE_KEYS
            ],
        },
        "region": live.REGION,
        "stack": {
            "id": live.STACK_ID,
            "live_resource_count": 125,
            "name": live.STACK_NAME,
            "non_complete_resource_count": 0,
            "outputs": _outputs(),
            "readiness": live.READINESS,
            "service_role_arn": live.SERVICE_ROLE_ARN,
            "status": "UPDATE_COMPLETE",
            "tags": {
                "DeploymentClass": "FOUNDATION_ONLY",
                "Environment": "dev",
                "Project": "MrLister",
            },
            "target_template_sha256": live.TARGET_TEMPLATE_SHA256,
            "termination_protection": True,
        },
        "web_bucket": {
            "encryption_algorithms": ["AES256"],
            "name": live.WEB_BUCKET_NAME,
            "ownership": "BucketOwnerEnforced",
            "policy": {
                "Statement": [
                    {
                        "Action": "s3:*",
                        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                        "Effect": "Deny",
                        "Principal": "*",
                        "Resource": [bucket_arn, f"{bucket_arn}/*"],
                        "Sid": "DenyInsecureTransport",
                    },
                    {
                        "Action": "s3:GetObject",
                        "Condition": {
                            "StringEquals": {
                                "AWS:SourceAccount": live.ACCOUNT_ID,
                                "AWS:SourceArn": distribution_arn,
                            }
                        },
                        "Effect": "Allow",
                        "Principal": {"Service": "cloudfront.amazonaws.com"},
                        "Resource": f"{bucket_arn}/*",
                        "Sid": "AllowExactCloudFrontDistributionRead",
                    },
                ],
                "Version": "2012-10-17",
            },
            "policy_is_public": False,
            "public_access_block": {
                "block_public_acls": True,
                "block_public_policy": True,
                "ignore_public_acls": True,
                "restrict_public_buckets": True,
            },
            "region": live.REGION,
            "versioning": "Enabled",
        },
    }


def _write(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "phase6-web-live-state.json"
    path.write_bytes(canonical_phase6_web_live_state(document))
    return path


def _mapping(document: dict[str, object], key: str) -> dict[str, object]:
    value = document[key]
    assert isinstance(value, dict)
    return value


def _reject(tmp_path: Path, document: object) -> None:
    with pytest.raises(Phase6WebLiveStateError) as captured:
        verify_phase6_web_live_state(_write(tmp_path, document), now=NOW, repository_root=tmp_path)
    assert str(captured.value) == "Phase 6 web live-state evidence is invalid"


def test_accepts_exact_cross_service_observation_and_returns_frozen_record(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, _document())
    verified = verify_phase6_web_live_state(path, now=NOW, repository_root=tmp_path)

    assert verified.format == live.FORMAT
    assert verified.stack_id == live.STACK_ID
    assert verified.target_template_sha256 == live.TARGET_TEMPLATE_SHA256
    assert verified.certificate_arn == live.CERTIFICATE_ARN
    assert verified.distribution_id == DISTRIBUTION_ID
    assert verified.web_bucket_name == live.WEB_BUCKET_NAME
    assert verified.user_pool_id == USER_POOL_ID
    assert verified.user_pool_client_id == CLIENT_ID
    assert verified.api_id == API_ID
    assert verified.route_count == 15
    assert verified.output_count == 19
    assert verified.canonical_sha256 == sha256(path.read_bytes()).hexdigest()
    with pytest.raises(FrozenInstanceError):
        verified.api_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("stack", "status"), "UPDATE_ROLLBACK_COMPLETE"),
        (("stack", "target_template_sha256"), "0" * 64),
        (("stack", "outputs", "DeploymentReadiness"), "SCAFFOLD_ONLY"),
        (("certificate", "status"), "PENDING_VALIDATION"),
        (("cloudfront", "status"), "InProgress"),
        (("cloudfront", "aliases"), ["www.massskutiny.com"]),
        (("cloudfront", "viewer_certificate", "acm_certificate_arn"), "sentinel"),
        (("cloudfront", "origins", 0, "origin_access_control_id"), "sentinel"),
        (("web_bucket", "versioning"), "Suspended"),
        (("web_bucket", "encryption_algorithms"), ["aws:kms"]),
        (("web_bucket", "policy", "Statement", 1, "Principal"), {"AWS": "*"}),
        (("cognito", "client", "client_secret_present"), True),
        (("cognito", "client", "allowed_oauth_flows"), ["implicit"]),
        (("http_api", "default_stage", "auto_deploy"), False),
        (("http_api", "authorizer", "audience"), ["sentinel"]),
        (("http_api", "cors", "allow_origins"), ["https://example.test"]),
        (("http_api", "routes", 0, "authorization_type"), "JWT"),
    ],
)
def test_rejects_security_or_cross_service_drift(
    tmp_path: Path, path: tuple[object, ...], replacement: object
) -> None:
    document = _document()
    cursor: object = document
    for key in path[:-1]:
        if isinstance(cursor, dict):
            cursor = cursor[key]  # type: ignore[index]
        elif isinstance(cursor, list):
            cursor = cursor[key]  # type: ignore[index]
        else:
            raise AssertionError
    final = path[-1]
    if isinstance(cursor, dict):
        cursor[final] = replacement  # type: ignore[index]
    elif isinstance(cursor, list):
        cursor[final] = replacement  # type: ignore[index]
    else:
        raise AssertionError
    _reject(tmp_path, document)


def test_rejects_missing_extra_or_reordered_closed_sets(tmp_path: Path) -> None:
    missing = _document()
    _mapping(missing, "stack").pop("readiness")
    _reject(tmp_path, missing)

    extra = _document()
    _mapping(extra, "certificate")["validation_records"] = []
    _reject(tmp_path, extra)

    route_order = _document()
    routes = _mapping(route_order, "http_api")["routes"]
    assert isinstance(routes, list)
    routes.reverse()
    _reject(tmp_path, route_order)

    output_set = _document()
    outputs = _mapping(_mapping(output_set, "stack"), "outputs")
    outputs["UnexpectedOutput"] = "sentinel"
    _reject(tmp_path, output_set)


@pytest.mark.parametrize(
    "captured_at",
    [
        (NOW - timedelta(minutes=15, seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        (NOW + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "2026-08-26T12:00:00+00:00",
    ],
)
def test_rejects_stale_future_or_noncanonical_capture_time(
    tmp_path: Path, captured_at: str
) -> None:
    document = _document()
    document["capture_time"] = captured_at
    _reject(tmp_path, document)


def test_rejects_noncanonical_duplicate_placeholder_and_symlink_inputs(tmp_path: Path) -> None:
    document = _document()
    pretty = canonical_phase6_web_live_state(document)
    noncanonical = tmp_path / "noncanonical.json"
    noncanonical.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(Phase6WebLiveStateError):
        verify_phase6_web_live_state(noncanonical, now=NOW, repository_root=tmp_path)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_bytes(
        pretty.replace(
            b'{\n  "account_id":',
            b'{\n  "account_id":"x",\n  "account_id":',
            1,
        )
    )
    with pytest.raises(Phase6WebLiveStateError):
        verify_phase6_web_live_state(duplicate, now=NOW, repository_root=tmp_path)

    placeholder = _document()
    _mapping(placeholder, "cloudfront")["domain_name"] = "<DISTRIBUTION_DOMAIN>"
    _reject(tmp_path, placeholder)

    source = _write(tmp_path, _document())
    symlink = tmp_path / "evidence-link.json"
    symlink.symlink_to(source)
    with pytest.raises(Phase6WebLiveStateError):
        verify_phase6_web_live_state(symlink, now=NOW, repository_root=tmp_path)

    real = tmp_path / "real-evidence"
    real.mkdir()
    nested = _write(real, _document())
    linked_parent = tmp_path / "linked-evidence"
    linked_parent.symlink_to(real, target_is_directory=True)
    with pytest.raises(Phase6WebLiveStateError):
        verify_phase6_web_live_state(
            linked_parent / nested.name,
            now=NOW,
            repository_root=tmp_path,
        )


def test_error_is_value_free_and_module_is_offline(tmp_path: Path) -> None:
    document = _document()
    sentinel = "sensitive-observed-value-90817"
    _mapping(document, "certificate")["status"] = sentinel
    path = _write(tmp_path, document)
    with pytest.raises(Phase6WebLiveStateError) as captured:
        verify_phase6_web_live_state(path, now=NOW, repository_root=tmp_path)
    assert str(captured.value) == "Phase 6 web live-state evidence is invalid"
    assert sentinel not in str(captured.value)

    source = Path(live.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (
            node.names if isinstance(node, ast.Import) else [ast.alias(node.module or "")]
        )
    }
    assert not imports.intersection({"boto3", "botocore", "requests", "socket", "subprocess"})
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not calls.intersection({"popen", "run", "system", "urlopen"})
