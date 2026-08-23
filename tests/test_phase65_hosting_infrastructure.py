from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "infra" / "phase6" / "template.json"


def load_template() -> dict[str, Any]:
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))


def test_sam_json_has_no_duplicate_keys_hidden_by_the_standard_decoder() -> None:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _value in pairs]
        assert len(keys) == len(set(keys)), f"duplicate JSON object key in {keys!r}"
        return dict(pairs)

    parsed = json.loads(
        TEMPLATE.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    )
    assert parsed == load_template()


def _resource_dependencies(value: object, resource_names: set[str]) -> set[str]:
    dependencies: set[str] = set()
    if isinstance(value, list):
        for item in value:
            dependencies.update(_resource_dependencies(item, resource_names))
    elif isinstance(value, dict):
        ref = value.get("Ref")
        if isinstance(ref, str) and ref in resource_names:
            dependencies.add(ref)
        get_att = value.get("Fn::GetAtt")
        if isinstance(get_att, list) and get_att and get_att[0] in resource_names:
            dependencies.add(get_att[0])
        sub = value.get("Fn::Sub")
        if isinstance(sub, str):
            for variable in re.findall(r"\$\{([A-Za-z0-9:.]+)", sub):
                logical_id = variable.split(".", maxsplit=1)[0]
                if logical_id in resource_names:
                    dependencies.add(logical_id)
        for item in value.values():
            dependencies.update(_resource_dependencies(item, resource_names))
    return dependencies


def test_web_bucket_is_private_separate_and_readable_only_by_exact_distribution() -> None:
    resources = load_template()["Resources"]
    asset_bucket = resources["SellerWebAssetBucket"]
    artifact_bucket = resources["PrivateArtifactBucket"]

    assert asset_bucket["DeletionPolicy"] == "Retain"
    assert asset_bucket["UpdateReplacePolicy"] == "Retain"
    assert asset_bucket["Properties"]["BucketName"] != artifact_bucket["Properties"]["BucketName"]
    assert asset_bucket["Properties"]["BucketEncryption"] == {
        "ServerSideEncryptionConfiguration": [
            {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
        ]
    }
    assert all(asset_bucket["Properties"]["PublicAccessBlockConfiguration"].values())
    assert asset_bucket["Properties"]["OwnershipControls"] == {
        "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]
    }
    assert "CorsConfiguration" not in asset_bucket["Properties"]

    statements = resources["SellerWebAssetBucketPolicy"]["Properties"]["PolicyDocument"][
        "Statement"
    ]
    assert statements[0]["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
    assert statements[1] == {
        "Sid": "AllowExactCloudFrontDistributionRead",
        "Effect": "Allow",
        "Principal": {"Service": "cloudfront.amazonaws.com"},
        "Action": "s3:GetObject",
        "Resource": {"Fn::Sub": "${SellerWebAssetBucket.Arn}/*"},
        "Condition": {
            "StringEquals": {
                "AWS:SourceAccount": {"Ref": "AWS::AccountId"},
                "AWS:SourceArn": {
                    "Fn::Sub": (
                        "arn:${AWS::Partition}:cloudfront::${AWS::AccountId}:distribution/"
                        "${SellerWebDistribution}"
                    )
                },
            }
        },
    }
    serialized_policy = json.dumps(statements, sort_keys=True)
    assert "PrivateArtifactBucket" not in serialized_policy
    assert "s3:PutObject" not in serialized_policy


def test_distribution_is_https_only_oac_and_has_no_blanket_error_rewrite() -> None:
    template = load_template()
    resources = template["Resources"]
    certificate = template["Parameters"]["ApplicationCertificateArn"]
    assert "Default" not in certificate
    assert re.fullmatch(
        certificate["AllowedPattern"],
        "arn:aws:acm:us-east-1:123456789012:certificate/01234567-89ab-cdef-0123-456789abcdef",
    )

    oac = resources["SellerWebOriginAccessControl"]["Properties"]["OriginAccessControlConfig"]
    assert oac["OriginAccessControlOriginType"] == "s3"
    assert oac["SigningBehavior"] == "always"
    assert oac["SigningProtocol"] == "sigv4"

    config = resources["SellerWebDistribution"]["Properties"]["DistributionConfig"]
    assert config["Enabled"] is True
    assert config["DefaultRootObject"] == "index.html"
    assert "CustomErrorResponses" not in config
    assert config["Aliases"] == [
        {
            "Fn::Select": [
                1,
                {"Fn::Split": ["https://", {"Ref": "ApplicationOrigin"}]},
            ]
        }
    ]
    assert config["ViewerCertificate"] == {
        "AcmCertificateArn": {"Ref": "ApplicationCertificateArn"},
        "MinimumProtocolVersion": "TLSv1.2_2021",
        "SslSupportMethod": "sni-only",
    }
    assert "Logging" not in config

    origins = {origin["Id"]: origin for origin in config["Origins"]}
    assert origins["SellerWebAssets"] == {
        "DomainName": {"Fn::GetAtt": ["SellerWebAssetBucket", "RegionalDomainName"]},
        "Id": "SellerWebAssets",
        "OriginAccessControlId": {"Ref": "SellerWebOriginAccessControl"},
        "S3OriginConfig": {"OriginAccessIdentity": ""},
    }
    api_origin = origins["SellerApi"]
    assert api_origin["DomainName"] == {
        "Fn::Sub": "${SellerHttpApi}.execute-api.${AWS::Region}.${AWS::URLSuffix}"
    }
    assert api_origin["CustomOriginConfig"]["OriginProtocolPolicy"] == "https-only"
    assert api_origin["CustomOriginConfig"]["OriginSSLProtocols"] == ["TLSv1.2"]


def test_spa_routes_are_exactly_allowlisted_and_api_methods_fail_closed() -> None:
    resources = load_template()["Resources"]
    spa = resources["SellerSpaRouteFunction"]["Properties"]
    assert spa["AutoPublish"] is True
    assert spa["FunctionConfig"]["Runtime"] == "cloudfront-js-2.0"
    code = spa["FunctionCode"]
    for route in ("/", "/auth/callback", "/jobs", "/jobs/", "/uploads/"):
        assert f"'{route}'" in code
    assert "request.uri = '/index.html'" in code
    assert "lastIndexOf" not in code
    assert "CustomErrorResponses" not in json.dumps(resources["SellerWebDistribution"])

    guard = resources["SellerApiMethodGuardFunction"]["Properties"]
    assert guard["AutoPublish"] is True
    guard_code = guard["FunctionCode"]
    for method in ("GET", "HEAD", "OPTIONS", "POST", "PUT"):
        assert f"method !== '{method}'" in guard_code
    assert "statusCode: 405" in guard_code
    assert "private, no-store, max-age=0" in guard_code
    assert "body: {" in guard_code
    assert "encoding: 'text'" in guard_code
    assert 'data: \'{"error"' in guard_code


def test_cache_behaviors_separate_immutable_assets_runtime_config_and_api() -> None:
    resources = load_template()["Resources"]
    config = resources["SellerWebDistribution"]["Properties"]["DistributionConfig"]
    behaviors = {behavior["PathPattern"]: behavior for behavior in config["CacheBehaviors"]}
    assert set(behaviors) == {"/assets/*", "/health", "/runtime-config.json", "/v1/*"}

    assets = behaviors["/assets/*"]
    assert assets["TargetOriginId"] == "SellerWebAssets"
    assert assets["CachePolicyId"] == {"Ref": "SellerWebImmutableAssetCachePolicy"}
    assert assets["ResponseHeadersPolicyId"] == {"Ref": "SellerWebImmutableResponseHeadersPolicy"}

    runtime = behaviors["/runtime-config.json"]
    assert runtime["TargetOriginId"] == "SellerWebAssets"
    assert runtime["CachePolicyId"] == {"Ref": "SellerWebNoStoreCachePolicy"}
    assert runtime["ResponseHeadersPolicyId"] == {"Ref": "SellerWebNoStoreResponseHeadersPolicy"}

    default = config["DefaultCacheBehavior"]
    assert default["CachePolicyId"] == {"Ref": "SellerWebNoStoreCachePolicy"}
    assert default["ResponseHeadersPolicyId"] == {"Ref": "SellerWebNoStoreResponseHeadersPolicy"}

    immutable = resources["SellerWebImmutableAssetCachePolicy"]["Properties"]["CachePolicyConfig"]
    assert (immutable["MinTTL"], immutable["DefaultTTL"], immutable["MaxTTL"]) == (
        31536000,
        31536000,
        31536000,
    )
    no_store = resources["SellerWebNoStoreCachePolicy"]["Properties"]["CachePolicyConfig"]
    assert (no_store["MinTTL"], no_store["DefaultTTL"], no_store["MaxTTL"]) == (0, 0, 0)

    health = behaviors["/health"]
    assert health == {
        "AllowedMethods": ["GET", "HEAD"],
        "CachePolicyId": {"Ref": "SellerWebNoStoreCachePolicy"},
        "CachedMethods": ["GET", "HEAD"],
        "Compress": False,
        "PathPattern": "/health",
        "ResponseHeadersPolicyId": {"Ref": "SellerWebNoStoreResponseHeadersPolicy"},
        "TargetOriginId": "SellerApi",
        "ViewerProtocolPolicy": "https-only",
    }

    api = behaviors["/v1/*"]
    assert api["TargetOriginId"] == "SellerApi"
    assert api["CachePolicyId"] == {"Ref": "SellerApiNoStoreCachePolicy"}
    assert api["ResponseHeadersPolicyId"] == {"Ref": "SellerWebNoStoreResponseHeadersPolicy"}
    assert api["ViewerProtocolPolicy"] == "https-only"
    # CloudFront weakens strong ETags when it auto-compresses a response. The review ETag is
    # command authority and must survive the same-origin edge byte-for-byte for If-Match.
    assert api["Compress"] is False
    assert api["FunctionAssociations"] == [
        {
            "EventType": "viewer-request",
            "FunctionARN": {"Fn::GetAtt": ["SellerApiMethodGuardFunction", "FunctionARN"]},
        }
    ]

    api_cache = resources["SellerApiNoStoreCachePolicy"]["Properties"]["CachePolicyConfig"]
    assert (api_cache["MinTTL"], api_cache["DefaultTTL"], api_cache["MaxTTL"]) == (0, 0, 0)
    forwarded = api_cache["ParametersInCacheKeyAndForwardedToOrigin"]
    assert forwarded["CookiesConfig"] == {"CookieBehavior": "none"}
    assert forwarded["EnableAcceptEncodingBrotli"] is False
    assert forwarded["EnableAcceptEncodingGzip"] is False
    assert forwarded["QueryStringsConfig"] == {"QueryStringBehavior": "all"}
    assert forwarded["HeadersConfig"] == {
        "HeaderBehavior": "whitelist",
        "Headers": [
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "If-Match",
            "Origin",
            "Access-Control-Request-Headers",
            "Access-Control-Request-Method",
        ],
    }


def test_security_headers_are_exact_and_forbid_inline_execution() -> None:
    resources = load_template()["Resources"]
    no_store = resources["SellerWebNoStoreResponseHeadersPolicy"]["Properties"][
        "ResponseHeadersPolicyConfig"
    ]
    immutable = resources["SellerWebImmutableResponseHeadersPolicy"]["Properties"][
        "ResponseHeadersPolicyConfig"
    ]
    assert no_store["SecurityHeadersConfig"] == immutable["SecurityHeadersConfig"]

    security = no_store["SecurityHeadersConfig"]
    assert security["ContentTypeOptions"] == {"Override": True}
    assert security["FrameOptions"] == {"FrameOption": "DENY", "Override": True}
    assert security["ReferrerPolicy"] == {
        "Override": True,
        "ReferrerPolicy": "no-referrer",
    }
    assert security["StrictTransportSecurity"] == {
        "AccessControlMaxAgeSec": 63072000,
        "IncludeSubdomains": True,
        "Override": True,
        "Preload": True,
    }
    csp = security["ContentSecurityPolicy"]["ContentSecurityPolicy"]["Fn::Sub"]
    for directive in (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data: blob: https://images.printify.com",
        "connect-src 'self' https://${SellerUserPoolDomain}.auth.${AWS::Region}.amazoncognito.com",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "worker-src 'none'",
    ):
        assert directive in csp
    assert "https://${PrivateArtifactBucket}.s3.${AWS::Region}.${AWS::URLSuffix}" in csp
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert "*" not in csp

    no_store_headers = {item["Header"]: item for item in no_store["CustomHeadersConfig"]["Items"]}
    assert no_store_headers["Cache-Control"]["Value"] == "private, no-store, max-age=0"
    assert no_store_headers["Permissions-Policy"]["Value"] == (
        "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
    )
    immutable_headers = {item["Header"]: item for item in immutable["CustomHeadersConfig"]["Items"]}
    assert immutable_headers["Cache-Control"]["Value"] == ("public, max-age=31536000, immutable")


def test_owner_scoped_upload_recovery_route_stays_on_upload_boundary() -> None:
    resources = load_template()["Resources"]
    upload = resources["UploadApiFunction"]
    recovery = upload["Properties"]["Events"]["GetUpload"]
    assert recovery == {
        "Type": "HttpApi",
        "Properties": {
            "ApiId": {"Ref": "SellerHttpApi"},
            "Path": "/v1/uploads/{upload_id}",
            "Method": "GET",
            "PayloadFormatVersion": "2.0",
            "Auth": {
                "Authorizer": "SellerJwtAuthorizer",
                "AuthorizationScopes": ["mr-lister-api/seller"],
            },
        },
    }
    role = resources["UploadApiFunctionRole"]
    serialized_role = json.dumps(role, sort_keys=True)
    assert "dynamodb:GetItem" in serialized_role
    for forbidden in (
        "OwnerJobsIndex",
        "states:",
        "bedrock",
        "secretsmanager",
        "lambda:InvokeFunction",
    ):
        assert forbidden not in serialized_role


def test_cognito_redirects_cannot_drift_from_the_distribution_alias() -> None:
    resources = load_template()["Resources"]
    client = resources["SellerUserPoolClient"]["Properties"]
    assert client["CallbackURLs"] == [{"Fn::Sub": "${ApplicationOrigin}/auth/callback"}]
    assert client["LogoutURLs"] == [{"Fn::Sub": "${ApplicationOrigin}/"}]
    assert client["GenerateSecret"] is False
    parameters = load_template()["Parameters"]
    assert "ApplicationCallbackUrl" not in parameters
    assert "ApplicationLogoutUrl" not in parameters


def test_runtime_config_outputs_are_public_nonsecret_and_stack_stays_scaffold_only() -> None:
    outputs = load_template()["Outputs"]
    assert outputs["DeploymentReadiness"]["Value"] == "SCAFFOLD_ONLY"
    assert outputs["SellerRuntimeConfigObjectKey"]["Value"] == "runtime-config.json"
    runtime_config = outputs["SellerRuntimeConfig"]["Value"]["Fn::Sub"]
    assert set(json.loads(runtime_config)) == {
        "cognito_authorize_url",
        "cognito_token_url",
        "cognito_logout_url",
        "client_id",
        "redirect_uri",
        "scopes",
    }
    for public_value in (
        '"cognito_authorize_url":"https://${SellerUserPoolDomain}.auth.${AWS::Region}.amazoncognito.com/oauth2/authorize"',
        '"cognito_token_url":"https://${SellerUserPoolDomain}.auth.${AWS::Region}.amazoncognito.com/oauth2/token"',
        '"cognito_logout_url":"https://${SellerUserPoolDomain}.auth.${AWS::Region}.amazoncognito.com/logout"',
        '"client_id":"${SellerUserPoolClient}"',
        '"redirect_uri":"${ApplicationOrigin}/auth/callback"',
        '"scopes":["openid","mr-lister-api/seller"]',
    ):
        assert public_value in runtime_config
    for forbidden in (
        "client_secret",
        "access_token",
        "refresh_token",
        "PrintifySecretArn",
        "AgentCoreRuntimeArn",
    ):
        assert forbidden.casefold() not in runtime_config.casefold()
    assert outputs["SellerWebAssetBucketName"]["Value"] == {"Ref": "SellerWebAssetBucket"}
    assert outputs["SellerApplicationOrigin"]["Value"] == {"Ref": "ApplicationOrigin"}


def test_cloudformation_resource_graph_has_no_dependency_cycle() -> None:
    resources = load_template()["Resources"]
    resource_names = set(resources)
    dependencies: dict[str, set[str]] = {}
    for name, resource in resources.items():
        discovered = _resource_dependencies(resource.get("Properties", {}), resource_names)
        explicit = resource.get("DependsOn", [])
        if isinstance(explicit, str):
            explicit = [explicit]
        dependencies[name] = discovered.union(explicit)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str, path: tuple[str, ...]) -> None:
        if name in visiting:
            cycle_start = path.index(name)
            cycle = " -> ".join((*path[cycle_start:], name))
            raise AssertionError(f"CloudFormation resource dependency cycle: {cycle}")
        if name in visited:
            return
        visiting.add(name)
        for dependency in dependencies[name]:
            visit(dependency, (*path, name))
        visiting.remove(name)
        visited.add(name)

    for resource_name in resources:
        visit(resource_name, ())
