"""Verify a canonical Phase 6 seller-web post-deploy observation offline.

The input is a closed, normalized record assembled from saved, read-only AWS CLI responses.
This module imports no AWS SDK, starts no subprocess, and makes no network request.  It binds the
live stack to the sealed web-edge target, then joins ACM, CloudFront, S3, Cognito, API Gateway,
and stack-output observations so dynamic identifiers must agree across every service boundary.

Write evidence with :func:`canonical_phase6_web_live_state`.  Any malformed, stale, incomplete,
or drifting input raises one value-free error; observed values are never included in failures.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Final

from tools.render_phase6_web_edge_transition import (
    WEB_EDGE_READINESS,
    WEB_EDGE_TEMPLATE_SHA256,
)

ROOT: Final = Path(__file__).resolve().parents[1]
FORMAT: Final = "mr-lister-phase6-web-live-state-v1"
ACCOUNT_ID: Final = "384627057108"
REGION: Final = "us-west-2"
STACK_NAME: Final = "mr-lister-phase6-dev"
STACK_ID: Final = (
    "arn:aws:cloudformation:us-west-2:384627057108:stack/mr-lister-phase6-dev/"
    "f3456970-9fdc-11f1-b448-06b81627db1d"
)
SERVICE_ROLE_ARN: Final = "arn:aws:iam::384627057108:role/mr-lister-phase6-runtime-cfn-dev"
TARGET_TEMPLATE_SHA256: Final = WEB_EDGE_TEMPLATE_SHA256
APPLICATION_ORIGIN: Final = "https://massskutiny.com"
APEX_DOMAIN: Final = "massskutiny.com"
CERTIFICATE_ARN: Final = (
    "arn:aws:acm:us-east-1:384627057108:certificate/28b8cddb-a0d7-4dc8-98de-26fd87cb5b79"
)
WEB_BUCKET_NAME: Final = "mr-lister-phase6-web-dev-384627057108-us-west-2"
ARTIFACT_BUCKET_NAME: Final = "mr-lister-phase6-artifacts-dev-384627057108-us-west-2"
READINESS: Final = WEB_EDGE_READINESS

_GENERIC_ERROR = "Phase 6 web live-state evidence is invalid"
_MAX_INPUT_BYTES = 1024 * 1024
_MAX_FRESHNESS = timedelta(minutes=15)
_PLACEHOLDER = re.compile(
    r"<[A-Z][A-Z0-9_]*>|\$\{[^}\r\n]+}|__[A-Z][A-Z0-9_]*__|"
    r"\b(?:PLACEHOLDER|REPLACE_ME|CHANGEME)\b",
    re.IGNORECASE,
)
_DISTRIBUTION_ID = re.compile(r"^[A-Z0-9]{8,32}$")
_DISTRIBUTION_DOMAIN = re.compile(r"^d[a-z0-9]{8,32}\.cloudfront\.net$")
_API_ID = re.compile(r"^[a-z0-9]{10}$")
_USER_POOL_ID = re.compile(r"^us-west-2_[A-Za-z0-9]+$")
_USER_POOL_CLIENT_ID = re.compile(r"^[a-z0-9]{16,64}$")

_ROUTE_KEYS: Final = (
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
_STACK_TAGS: Final = {
    "DeploymentClass": "FOUNDATION_ONLY",
    "Environment": "dev",
    "Project": "MrLister",
}


class Phase6WebLiveStateError(RuntimeError):
    """Value-free failure for invalid Phase 6 web live-state evidence."""


@dataclass(frozen=True, slots=True)
class VerifiedPhase6WebLiveState:
    """Cross-service identity returned only after every assertion succeeds."""

    format: str
    capture_time: datetime
    stack_id: str
    target_template_sha256: str
    certificate_arn: str
    distribution_id: str
    web_bucket_name: str
    user_pool_id: str
    user_pool_client_id: str
    api_id: str
    route_count: int
    output_count: int
    canonical_sha256: str


def canonical_phase6_web_live_state(value: object) -> bytes:
    """Return the sole accepted byte representation for evidence and success records."""

    try:
        return (
            json.dumps(value, allow_nan=False, indent=2, separators=(",", ": "), sort_keys=True)
            + "\n"
        ).encode("utf-8")
    except Exception:
        raise Phase6WebLiveStateError(_GENERIC_ERROR) from None


def verify_phase6_web_live_state(
    evidence_path: Path,
    *,
    now: datetime | None = None,
    repository_root: Path = ROOT,
) -> VerifiedPhase6WebLiveState:
    """Verify one canonical saved observation without contacting AWS."""

    try:
        current_time = datetime.now(UTC) if now is None else _utc_datetime(now)
        raw, document = _load_document(evidence_path, _repository(repository_root))
        capture_time = _timestamp(document.get("capture_time"))
        if capture_time > current_time or current_time - capture_time > _MAX_FRESHNESS:
            raise ValueError

        _exact_keys(
            document,
            {
                "account_id",
                "capture_time",
                "certificate",
                "cloudfront",
                "cognito",
                "format",
                "http_api",
                "region",
                "stack",
                "web_bucket",
            },
        )
        if (
            document.get("format") != FORMAT
            or document.get("account_id") != ACCOUNT_ID
            or document.get("region") != REGION
        ):
            raise ValueError

        stack = _mapping(document, "stack")
        outputs = _validate_stack(stack)
        certificate = _mapping(document, "certificate")
        cloudfront = _mapping(document, "cloudfront")
        distribution_id, distribution_domain, oac_id = _validate_cloudfront(cloudfront)
        _validate_certificate(certificate, distribution_id)
        _validate_web_bucket(_mapping(document, "web_bucket"), distribution_id)
        user_pool_id, client_id, sign_in_origin = _validate_cognito(_mapping(document, "cognito"))
        api_id, api_origin = _validate_http_api(
            _mapping(document, "http_api"), user_pool_id=user_pool_id, client_id=client_id
        )
        _validate_cloudfront_origins(
            cloudfront, oac_id=oac_id, api_id=api_id, api_origin=api_origin
        )
        _validate_outputs(
            outputs,
            distribution_id=distribution_id,
            distribution_domain=distribution_domain,
            user_pool_id=user_pool_id,
            client_id=client_id,
            sign_in_origin=sign_in_origin,
            api_origin=api_origin,
        )

        return VerifiedPhase6WebLiveState(
            format=FORMAT,
            capture_time=capture_time,
            stack_id=STACK_ID,
            target_template_sha256=TARGET_TEMPLATE_SHA256,
            certificate_arn=CERTIFICATE_ARN,
            distribution_id=distribution_id,
            web_bucket_name=WEB_BUCKET_NAME,
            user_pool_id=user_pool_id,
            user_pool_client_id=client_id,
            api_id=api_id,
            route_count=len(_ROUTE_KEYS),
            output_count=len(outputs),
            canonical_sha256=sha256(raw).hexdigest(),
        )
    except Phase6WebLiveStateError:
        raise
    except Exception:
        raise Phase6WebLiveStateError(_GENERIC_ERROR) from None


def _validate_stack(stack: Mapping[str, object]) -> Mapping[str, object]:
    _exact_keys(
        stack,
        {
            "id",
            "live_resource_count",
            "name",
            "non_complete_resource_count",
            "outputs",
            "readiness",
            "service_role_arn",
            "status",
            "tags",
            "target_template_sha256",
            "termination_protection",
        },
    )
    if (
        stack.get("id") != STACK_ID
        or stack.get("name") != STACK_NAME
        or stack.get("status") != "UPDATE_COMPLETE"
        or stack.get("readiness") != READINESS
        or stack.get("target_template_sha256") != TARGET_TEMPLATE_SHA256
        or stack.get("service_role_arn") != SERVICE_ROLE_ARN
        or stack.get("termination_protection") is not True
        or _exact_int(stack.get("live_resource_count")) != 125
        or _exact_int(stack.get("non_complete_resource_count")) != 0
        or _mapping(stack, "tags") != _STACK_TAGS
    ):
        raise ValueError
    return _mapping(stack, "outputs")


def _validate_certificate(certificate: Mapping[str, object], distribution_id: str) -> None:
    _exact_keys(certificate, {"arn", "domain_name", "in_use_by", "status", "subject_names"})
    distribution_arn = f"arn:aws:cloudfront::{ACCOUNT_ID}:distribution/{distribution_id}"
    if certificate != {
        "arn": CERTIFICATE_ARN,
        "domain_name": APEX_DOMAIN,
        "in_use_by": [distribution_arn],
        "status": "ISSUED",
        "subject_names": [APEX_DOMAIN],
    }:
        raise ValueError


def _validate_cloudfront(value: Mapping[str, object]) -> tuple[str, str, str]:
    _exact_keys(
        value,
        {
            "aliases",
            "arn",
            "domain_name",
            "enabled",
            "id",
            "origin_access_control",
            "origins",
            "status",
            "viewer_certificate",
        },
    )
    distribution_id = _string(value.get("id"))
    domain = _string(value.get("domain_name"))
    expected_arn = f"arn:aws:cloudfront::{ACCOUNT_ID}:distribution/{distribution_id}"
    if (
        _DISTRIBUTION_ID.fullmatch(distribution_id) is None
        or _DISTRIBUTION_DOMAIN.fullmatch(domain) is None
        or value.get("arn") != expected_arn
        or value.get("status") != "Deployed"
        or value.get("enabled") is not True
        or value.get("aliases") != [APEX_DOMAIN]
    ):
        raise ValueError
    certificate = _mapping(value, "viewer_certificate")
    if certificate != {
        "acm_certificate_arn": CERTIFICATE_ARN,
        "minimum_protocol_version": "TLSv1.2_2021",
        "ssl_support_method": "sni-only",
    }:
        raise ValueError
    oac = _mapping(value, "origin_access_control")
    _exact_keys(oac, {"id", "origin_type", "signing_behavior", "signing_protocol"})
    oac_id = _string(oac.get("id"))
    if (
        _DISTRIBUTION_ID.fullmatch(oac_id) is None
        or oac.get("origin_type") != "s3"
        or oac.get("signing_behavior") != "always"
        or oac.get("signing_protocol") != "sigv4"
    ):
        raise ValueError
    return distribution_id, domain, oac_id


def _validate_cloudfront_origins(
    value: Mapping[str, object], *, oac_id: str, api_id: str, api_origin: str
) -> None:
    origins = value.get("origins")
    if not isinstance(origins, list):
        raise ValueError
    expected = [
        {
            "domain_name": f"{WEB_BUCKET_NAME}.s3.{REGION}.amazonaws.com",
            "id": "SellerWebAssets",
            "origin_access_control_id": oac_id,
            "origin_access_identity": "",
        },
        {
            "domain_name": api_origin.removeprefix("https://"),
            "https_port": 443,
            "id": "SellerApi",
            "origin_protocol_policy": "https-only",
            "origin_ssl_protocols": ["TLSv1.2"],
        },
    ]
    expected_api_domain = f"{api_id}.execute-api.{REGION}.amazonaws.com"
    if origins != expected or expected[1]["domain_name"] != expected_api_domain:
        raise ValueError


def _validate_web_bucket(value: Mapping[str, object], distribution_id: str) -> None:
    _exact_keys(
        value,
        {
            "encryption_algorithms",
            "name",
            "ownership",
            "policy",
            "policy_is_public",
            "public_access_block",
            "region",
            "versioning",
        },
    )
    if (
        value.get("name") != WEB_BUCKET_NAME
        or value.get("region") != REGION
        or value.get("versioning") != "Enabled"
        or value.get("encryption_algorithms") != ["AES256"]
        or value.get("ownership") != "BucketOwnerEnforced"
        or value.get("policy_is_public") is not False
        or value.get("public_access_block")
        != {
            "block_public_acls": True,
            "block_public_policy": True,
            "ignore_public_acls": True,
            "restrict_public_buckets": True,
        }
    ):
        raise ValueError
    bucket_arn = f"arn:aws:s3:::{WEB_BUCKET_NAME}"
    distribution_arn = f"arn:aws:cloudfront::{ACCOUNT_ID}:distribution/{distribution_id}"
    expected_policy = {
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
                        "AWS:SourceAccount": ACCOUNT_ID,
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
    }
    if value.get("policy") != expected_policy:
        raise ValueError


def _validate_cognito(value: Mapping[str, object]) -> tuple[str, str, str]:
    _exact_keys(value, {"client", "domain", "user_pool"})
    pool = _mapping(value, "user_pool")
    _exact_keys(pool, {"arn", "id", "name", "status"})
    pool_id = _string(pool.get("id"))
    if (
        _USER_POOL_ID.fullmatch(pool_id) is None
        or pool.get("arn") != f"arn:aws:cognito-idp:{REGION}:{ACCOUNT_ID}:userpool/{pool_id}"
        or pool.get("name") != "mr-lister-phase6-dev-sellers"
        or pool.get("status") != "Enabled"
    ):
        raise ValueError
    client = _mapping(value, "client")
    _exact_keys(
        client,
        {
            "allowed_oauth_flows",
            "allowed_oauth_flows_user_pool_client",
            "allowed_oauth_scopes",
            "callback_urls",
            "client_id",
            "client_name",
            "client_secret_present",
            "logout_urls",
            "supported_identity_providers",
            "user_pool_id",
        },
    )
    client_id = _string(client.get("client_id"))
    if (
        _USER_POOL_CLIENT_ID.fullmatch(client_id) is None
        or client.get("user_pool_id") != pool_id
        or client.get("client_name") != "mr-lister-phase6-dev-browser"
        or client.get("client_secret_present") is not False
        or client.get("allowed_oauth_flows_user_pool_client") is not True
        or client.get("allowed_oauth_flows") != ["code"]
        or client.get("allowed_oauth_scopes") != ["openid", "mr-lister-api/seller"]
        or client.get("callback_urls") != [f"{APPLICATION_ORIGIN}/auth/callback"]
        or client.get("logout_urls") != [f"{APPLICATION_ORIGIN}/"]
        or client.get("supported_identity_providers") != ["COGNITO"]
    ):
        raise ValueError
    domain = _mapping(value, "domain")
    _exact_keys(domain, {"prefix", "user_pool_id"})
    expected_prefix = f"mr-lister-dev-{ACCOUNT_ID}"
    if domain != {"prefix": expected_prefix, "user_pool_id": pool_id}:
        raise ValueError
    return pool_id, client_id, f"https://{expected_prefix}.auth.{REGION}.amazoncognito.com"


def _validate_http_api(
    value: Mapping[str, object], *, user_pool_id: str, client_id: str
) -> tuple[str, str]:
    _exact_keys(
        value,
        {
            "api_endpoint",
            "api_id",
            "authorizer",
            "cors",
            "default_stage",
            "disable_execute_api_endpoint",
            "protocol_type",
            "routes",
        },
    )
    api_id = _string(value.get("api_id"))
    api_origin = f"https://{api_id}.execute-api.{REGION}.amazonaws.com"
    if (
        _API_ID.fullmatch(api_id) is None
        or value.get("api_endpoint") != api_origin
        or value.get("protocol_type") != "HTTP"
        or value.get("disable_execute_api_endpoint") is not False
        or value.get("default_stage") != {"auto_deploy": True, "name": "$default"}
    ):
        raise ValueError
    if value.get("cors") != {
        "allow_credentials": False,
        "allow_headers": ["Authorization", "Content-Type", "Idempotency-Key", "If-Match"],
        "allow_methods": ["GET", "POST", "PUT", "OPTIONS"],
        "allow_origins": [APPLICATION_ORIGIN],
        "expose_headers": ["ETag", "Retry-After", "X-Request-Id"],
        "max_age": 300,
    }:
        raise ValueError
    if value.get("authorizer") != {
        "audience": [client_id],
        "authorization_type": "JWT",
        "identity_source": ["$request.header.Authorization"],
        "issuer": f"https://cognito-idp.{REGION}.amazonaws.com/{user_pool_id}",
        "name": "SellerJwtAuthorizer",
    }:
        raise ValueError
    routes = value.get("routes")
    expected_routes = [
        {
            "authorization_scopes": ([] if route == "GET /health" else ["mr-lister-api/seller"]),
            "authorization_type": ("NONE" if route == "GET /health" else "JWT"),
            "route_key": route,
        }
        for route in _ROUTE_KEYS
    ]
    if routes != expected_routes:
        raise ValueError
    return api_id, api_origin


def _validate_outputs(
    outputs: Mapping[str, object],
    *,
    distribution_id: str,
    distribution_domain: str,
    user_pool_id: str,
    client_id: str,
    sign_in_origin: str,
    api_origin: str,
) -> None:
    runtime_config = json.dumps(
        {
            "cognito_authorize_url": f"{sign_in_origin}/oauth2/authorize",
            "cognito_token_url": f"{sign_in_origin}/oauth2/token",
            "cognito_logout_url": f"{sign_in_origin}/logout",
            "client_id": client_id,
            "redirect_uri": f"{APPLICATION_ORIGIN}/auth/callback",
            "scopes": ["openid", "mr-lister-api/seller"],
        },
        separators=(",", ":"),
    )
    expected = {
        "ArtifactBucketBrowserOrigin": (
            f"https://{ARTIFACT_BUCKET_NAME}.s3.{REGION}.amazonaws.com"
        ),
        "ArtifactBucketName": ARTIFACT_BUCKET_NAME,
        "DeploymentReadiness": READINESS,
        "OperationalAlarmTopicArn": (
            f"arn:aws:sns:{REGION}:{ACCOUNT_ID}:mr-lister-phase6-dev-operational-alarms"
        ),
        "PrepareStateMachineArn": (
            f"arn:aws:states:{REGION}:{ACCOUNT_ID}:stateMachine:mr-lister-phase6-dev-prepare"
        ),
        "ReconcileProductStateMachineArn": (
            f"arn:aws:states:{REGION}:{ACCOUNT_ID}:stateMachine:"
            "mr-lister-phase6-dev-reconcile-product"
        ),
        "RefreshEconomicsStateMachineArn": (
            f"arn:aws:states:{REGION}:{ACCOUNT_ID}:stateMachine:"
            "mr-lister-phase6-dev-refresh-economics"
        ),
        "SellerApiOrigin": api_origin,
        "SellerApplicationOrigin": APPLICATION_ORIGIN,
        "SellerRuntimeConfig": runtime_config,
        "SellerRuntimeConfigObjectKey": "runtime-config.json",
        "SellerSignInOrigin": sign_in_origin,
        "SellerUserPoolClientId": client_id,
        "SellerUserPoolId": user_pool_id,
        "SellerWebAssetBucketName": WEB_BUCKET_NAME,
        "SellerWebDistributionDomainName": distribution_domain,
        "SellerWebDistributionId": distribution_id,
        "StateTableName": "mr-lister-phase6-dev",
        "SynchronizeProductStateMachineArn": (
            f"arn:aws:states:{REGION}:{ACCOUNT_ID}:stateMachine:"
            "mr-lister-phase6-dev-synchronize-product"
        ),
    }
    if outputs != expected:
        raise ValueError


def _repository(path: Path) -> Path:
    if not isinstance(path, Path) or path.is_symlink():
        raise ValueError
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError
    return resolved


def _load_document(path: Path, repository: Path) -> tuple[bytes, Mapping[str, object]]:
    if not isinstance(path, Path):
        raise ValueError
    candidate = path if path.is_absolute() else repository / path
    if not candidate.is_relative_to(repository):
        raise ValueError
    current = repository
    for component in candidate.relative_to(repository).parts:
        if component in {"", ".", ".."}:
            raise ValueError
        current = current / component
        if current.is_symlink():
            raise ValueError
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(repository):
        raise ValueError
    if not resolved.is_file():
        raise ValueError
    raw = resolved.read_bytes()
    if not raw or len(raw) > _MAX_INPUT_BYTES or b"\x00" in raw:
        raise ValueError
    value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_bad_constant)
    if not isinstance(value, Mapping) or canonical_phase6_web_live_state(value) != raw:
        raise ValueError
    _reject_placeholders(value)
    return raw, value


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        raise ValueError
    return nested


def _exact_keys(value: Mapping[str, object], keys: set[str]) -> None:
    if set(value) != keys:
        raise ValueError


def _string(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError
    return value


def _exact_int(value: object) -> int:
    if type(value) is not int:
        raise ValueError
    return value


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ValueError
    return parsed.astimezone(UTC)


def _utc_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError
    return value.astimezone(UTC)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _bad_constant(_value: str) -> object:
    raise ValueError


def _reject_placeholders(value: object) -> None:
    if isinstance(value, str):
        if _PLACEHOLDER.search(value):
            raise ValueError
    elif isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str) or _PLACEHOLDER.search(key):
                raise ValueError
            _reject_placeholders(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_placeholders(nested)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the verifier and print only the canonical success record."""

    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        verified = verify_phase6_web_live_state(arguments.evidence)
    except Phase6WebLiveStateError:
        parser.error(_GENERIC_ERROR)
    payload = asdict(verified)
    payload["capture_time"] = verified.capture_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    print(canonical_phase6_web_live_state(payload).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
