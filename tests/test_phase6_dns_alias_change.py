"""Credential-free tests for the bounded Phase 6 Route 53 alias renderer."""

from __future__ import annotations

import json
import stat
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

import tools.render_phase6_dns_alias_change as dns_alias
from tools.bind_phase6_runtime_config import COGNITO_ORIGIN
from tools.render_phase6_dns_alias_change import (
    APPLICATION_DOMAIN,
    APPLICATION_ORIGIN,
    CLOUDFRONT_HOSTED_ZONE_ID,
    DNS_ALIAS_CHANGE_OUTPUT,
    EXPECTED_NAME_SERVERS,
    STACK_ID,
    STACK_NAME,
    Phase6DnsAliasChangeError,
    render_phase6_dns_alias_change,
    verify_rendered_phase6_dns_alias_change,
    write_phase6_dns_alias_change,
)

HOSTED_ZONE_ID = "Z0123456789ABCDEFGHIJ"
CLOUDFRONT_DOMAIN = "d111111abcdef8.cloudfront.net"
CLIENT_ID = "4client1234567890abcdef"


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _output_values() -> dict[str, str]:
    runtime_config = {
        "cognito_authorize_url": f"{COGNITO_ORIGIN}/oauth2/authorize",
        "cognito_token_url": f"{COGNITO_ORIGIN}/oauth2/token",
        "cognito_logout_url": f"{COGNITO_ORIGIN}/logout",
        "client_id": CLIENT_ID,
        "redirect_uri": f"{APPLICATION_ORIGIN}/auth/callback",
        "scopes": ["openid", "mr-lister-api/seller"],
    }
    return {
        "ArtifactBucketBrowserOrigin": (
            "https://mr-lister-phase6-artifacts-dev-384627057108-us-west-2."
            "s3.us-west-2.amazonaws.com"
        ),
        "ArtifactBucketName": "mr-lister-phase6-artifacts-dev-384627057108-us-west-2",
        "DeploymentReadiness": "WEB_EDGE_ACTIVE_DRAFT_ONLY",
        "OperationalAlarmTopicArn": (
            "arn:aws:sns:us-west-2:384627057108:mr-lister-phase6-dev-operational-alarms"
        ),
        "PrepareStateMachineArn": (
            "arn:aws:states:us-west-2:384627057108:stateMachine:mr-lister-phase6-dev-prepare"
        ),
        "ReconcileProductStateMachineArn": (
            "arn:aws:states:us-west-2:384627057108:stateMachine:"
            "mr-lister-phase6-dev-reconcile-product"
        ),
        "RefreshEconomicsStateMachineArn": (
            "arn:aws:states:us-west-2:384627057108:stateMachine:"
            "mr-lister-phase6-dev-refresh-economics"
        ),
        "SellerApiOrigin": "https://abc123.execute-api.us-west-2.amazonaws.com",
        "SellerApplicationOrigin": APPLICATION_ORIGIN,
        "SellerRuntimeConfig": json.dumps(
            runtime_config,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
        ),
        "SellerRuntimeConfigObjectKey": "runtime-config.json",
        "SellerSignInOrigin": COGNITO_ORIGIN,
        "SellerUserPoolClientId": CLIENT_ID,
        "SellerUserPoolId": "us-west-2_fixture",
        "SellerWebAssetBucketName": ("mr-lister-phase6-web-dev-384627057108-us-west-2"),
        "SellerWebDistributionDomainName": CLOUDFRONT_DOMAIN,
        "SellerWebDistributionId": "E1234567890ABC",
        "StateTableName": STACK_NAME,
        "SynchronizeProductStateMachineArn": (
            "arn:aws:states:us-west-2:384627057108:stateMachine:"
            "mr-lister-phase6-dev-synchronize-product"
        ),
    }


def _capture() -> dict[str, object]:
    outputs = _output_values()
    return {
        "Outputs": [
            {"OutputKey": key, "OutputValue": value} for key, value in sorted(outputs.items())
        ],
        "StackId": STACK_ID,
        "StackName": STACK_NAME,
        "StackStatus": "UPDATE_COMPLETE",
    }


def _hosted_zone() -> dict[str, object]:
    return {
        "DelegationSet": {"NameServers": sorted(EXPECTED_NAME_SERVERS)},
        "HostedZone": {
            "CallerReference": "phase6-massskutiny-public-zone",
            "Config": {"PrivateZone": False},
            "Id": f"/hostedzone/{HOSTED_ZONE_ID}",
            "Name": f"{APPLICATION_DOMAIN}.",
            "ResourceRecordSetCount": 4,
        },
    }


def _repository(tmp_path: Path, capture: object | None = None) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    evidence = repository / ".mr_lister_private" / "phase6-web-edge"
    evidence.mkdir(parents=True)
    capture_path = evidence / "stack-outputs.json"
    capture_path.write_bytes(_canonical(_capture() if capture is None else capture))
    dns_evidence = repository / ".mr_lister_private" / "phase6-dns-alias"
    dns_evidence.mkdir()
    (dns_evidence / "hosted-zone.json").write_bytes(_canonical(_hosted_zone()))
    return repository, capture_path


def _render(repository: Path, capture_path: Path, **overrides: object) -> bytes:
    options: dict[str, object] = {
        "hosted_zone_id": HOSTED_ZONE_ID,
        "hosted_zone_observation_path": (
            repository / ".mr_lister_private" / "phase6-dns-alias" / "hosted-zone.json"
        ),
        "cloudfront_domain_name": CLOUDFRONT_DOMAIN,
        "stack_output_capture_path": capture_path,
        "repository_root": repository,
    }
    options.update(overrides)
    return render_phase6_dns_alias_change(**options)  # type: ignore[arg-type]


def test_render_is_canonical_cli_input_with_only_apex_a_and_aaaa_aliases(
    tmp_path: Path,
) -> None:
    repository, capture_path = _repository(tmp_path)

    raw = _render(repository, capture_path)
    request = json.loads(raw)

    assert raw == _canonical(request)
    assert set(request) == {"ChangeBatch", "HostedZoneId"}
    assert request["HostedZoneId"] == HOSTED_ZONE_ID
    assert request["ChangeBatch"]["Comment"] == (
        "Mr Lister Phase 6 massskutiny.com apex aliases to verified CloudFront distribution"
    )
    changes = request["ChangeBatch"]["Changes"]
    assert [change["ResourceRecordSet"]["Type"] for change in changes] == ["A", "AAAA"]
    assert [change["Action"] for change in changes] == ["CREATE", "CREATE"]
    assert b"UPSERT" not in raw
    for change in changes:
        assert change == {
            "Action": "CREATE",
            "ResourceRecordSet": {
                "AliasTarget": {
                    "DNSName": f"{CLOUDFRONT_DOMAIN}.",
                    "EvaluateTargetHealth": False,
                    "HostedZoneId": CLOUDFRONT_HOSTED_ZONE_ID,
                },
                "Name": f"{APPLICATION_DOMAIN}.",
                "Type": change["ResourceRecordSet"]["Type"],
            },
        }


def test_render_requires_independent_target_to_equal_verified_stack_output(tmp_path: Path) -> None:
    repository, capture_path = _repository(tmp_path)

    with pytest.raises(Phase6DnsAliasChangeError):
        _render(
            repository,
            capture_path,
            cloudfront_domain_name="d222222abcdef8.cloudfront.net",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("StackId", STACK_ID.replace("384627057108", "999999999999")),
        ("StackId", STACK_ID.replace("us-west-2", "us-east-1")),
        ("StackId", STACK_ID.replace(STACK_NAME, "other-stack")),
        ("StackName", "other-stack"),
        ("StackStatus", "UPDATE_IN_PROGRESS"),
        ("StackStatus", "CREATE_COMPLETE"),
    ),
)
def test_wrong_account_region_stack_and_status_are_rejected(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    capture = _capture()
    capture[field] = value
    repository, capture_path = _repository(tmp_path, capture)

    with pytest.raises(Phase6DnsAliasChangeError):
        _render(repository, capture_path)


@pytest.mark.parametrize(
    ("output_key", "value"),
    (
        ("SellerApplicationOrigin", "https://www.massskutiny.com"),
        ("SellerApplicationOrigin", "http://massskutiny.com"),
        ("DeploymentReadiness", "SCAFFOLD_ONLY"),
        ("SellerRuntimeConfigObjectKey", "other.json"),
        ("StateTableName", "other-stack"),
        ("SellerWebDistributionDomainName", "massskutiny.com"),
        ("SellerWebDistributionDomainName", "abc123.execute-api.us-west-2.amazonaws.com"),
    ),
)
def test_wrong_application_and_cloudfront_outputs_are_rejected(
    tmp_path: Path,
    output_key: str,
    value: str,
) -> None:
    capture = _capture()
    record = next(
        item
        for item in capture["Outputs"]
        if item["OutputKey"] == output_key  # type: ignore[index]
    )
    record["OutputValue"] = value  # type: ignore[index]
    repository, capture_path = _repository(tmp_path, capture)

    with pytest.raises(Phase6DnsAliasChangeError):
        _render(
            repository,
            capture_path,
            cloudfront_domain_name=value if "cloudfront" in value else CLOUDFRONT_DOMAIN,
        )


@pytest.mark.parametrize(
    "hosted_zone_id",
    (
        "",
        "0123456789ABC",
        "Z123",
        "z0123456789abcdefghij",
        "/hostedzone/Z0123456789ABCDEFGHIJ",
        "Z0123456789ABCDEFGHIJ ",
        "Z0123456789ABC_DEF",
        CLOUDFRONT_HOSTED_ZONE_ID,
    ),
)
def test_invalid_or_swapped_hosted_zone_id_is_rejected(
    tmp_path: Path,
    hosted_zone_id: str,
) -> None:
    repository, capture_path = _repository(tmp_path)

    with pytest.raises(Phase6DnsAliasChangeError):
        _render(repository, capture_path, hosted_zone_id=hosted_zone_id)


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("HostedZone", "Name"), "other.example."),
        (("HostedZone", "Id"), "/hostedzone/Z9999999999WRONG"),
        (("HostedZone", "Config", "PrivateZone"), True),
        (("DelegationSet", "NameServers", 0), "ns-1.example.net"),
    ),
)
def test_hosted_zone_observation_must_prove_exact_public_authority(
    tmp_path: Path,
    path: tuple[object, ...],
    value: object,
) -> None:
    repository, capture_path = _repository(tmp_path)
    observation_path = repository / ".mr_lister_private" / "phase6-dns-alias" / "hosted-zone.json"
    observation = _hosted_zone()
    target: object = observation
    for component in path[:-1]:
        target = target[component]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]
    observation_path.write_bytes(_canonical(observation))

    with pytest.raises(Phase6DnsAliasChangeError):
        _render(repository, capture_path)


@pytest.mark.parametrize(
    "cloudfront_domain_name",
    (
        "",
        "D111111ABCDEF8.cloudfront.net",
        "d111111abcdef8.cloudfront.net.",
        "https://d111111abcdef8.cloudfront.net",
        "dualstack.d111111abcdef8.cloudfront.net",
        "d111111abcdef8.cloudfront.net/path",
        "massskutiny.com",
    ),
)
def test_noncanonical_cloudfront_target_is_rejected(
    tmp_path: Path,
    cloudfront_domain_name: str,
) -> None:
    capture = _capture()
    record = next(
        item
        for item in capture["Outputs"]  # type: ignore[union-attr]
        if item["OutputKey"] == "SellerWebDistributionDomainName"
    )
    record["OutputValue"] = cloudfront_domain_name
    repository, capture_path = _repository(tmp_path, capture)

    with pytest.raises(Phase6DnsAliasChangeError):
        _render(repository, capture_path, cloudfront_domain_name=cloudfront_domain_name)


@pytest.mark.parametrize("record_type", ("CNAME", "NS", "SOA", "MX", "TXT"))
def test_broadened_or_validation_record_types_are_rejected_before_render(
    tmp_path: Path,
    record_type: str,
) -> None:
    repository, capture_path = _repository(tmp_path)
    broadened = dns_alias._build_change_request(HOSTED_ZONE_ID, CLOUDFRONT_DOMAIN)
    changes = broadened["ChangeBatch"]["Changes"]  # type: ignore[index]
    changes.append(  # type: ignore[union-attr]
        {
            "Action": "CREATE",
            "ResourceRecordSet": {
                "Name": f"_validation.{APPLICATION_DOMAIN}.",
                "ResourceRecords": [{"Value": "unreviewed"}],
                "TTL": 300,
                "Type": record_type,
            },
        }
    )

    with (
        patch.object(dns_alias, "_build_change_request", return_value=broadened),
        pytest.raises(Phase6DnsAliasChangeError),
    ):
        _render(repository, capture_path)


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("ChangeBatch", "Changes", 0, "Action"), "UPSERT"),
        (("ChangeBatch", "Changes", 1, "Action"), "DELETE"),
        (("ChangeBatch", "Changes", 0, "ResourceRecordSet", "Name"), "www.massskutiny.com."),
        (
            (
                "ChangeBatch",
                "Changes",
                0,
                "ResourceRecordSet",
                "AliasTarget",
                "HostedZoneId",
            ),
            "ZWRONGTARGET",
        ),
        (
            (
                "ChangeBatch",
                "Changes",
                1,
                "ResourceRecordSet",
                "AliasTarget",
                "EvaluateTargetHealth",
            ),
            True,
        ),
        (
            (
                "ChangeBatch",
                "Changes",
                1,
                "ResourceRecordSet",
                "AliasTarget",
                "DNSName",
            ),
            "d222222abcdef8.cloudfront.net.",
        ),
    ),
)
def test_wrong_action_name_alias_zone_health_and_target_are_rejected(
    tmp_path: Path,
    path: tuple[object, ...],
    value: object,
) -> None:
    repository, capture_path = _repository(tmp_path)
    request = dns_alias._build_change_request(HOSTED_ZONE_ID, CLOUDFRONT_DOMAIN)
    target: object = request
    for component in path[:-1]:
        target = target[component]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]

    with (
        patch.object(dns_alias, "_build_change_request", return_value=request),
        pytest.raises(Phase6DnsAliasChangeError),
    ):
        _render(repository, capture_path)


def test_capture_must_have_exact_sorted_complete_two_field_output_inventory(
    tmp_path: Path,
) -> None:
    missing = _capture()
    missing["Outputs"] = missing["Outputs"][:-1]  # type: ignore[index]
    repository, capture_path = _repository(tmp_path / "missing", missing)
    with pytest.raises(Phase6DnsAliasChangeError):
        _render(repository, capture_path)

    extra = _capture()
    extra["Outputs"].append(  # type: ignore[union-attr]
        {"OutputKey": "Unexpected", "OutputValue": "unexpected"}
    )
    repository, capture_path = _repository(tmp_path / "extra", extra)
    with pytest.raises(Phase6DnsAliasChangeError):
        _render(repository, capture_path)

    unsorted = _capture()
    unsorted["Outputs"][0], unsorted["Outputs"][1] = (  # type: ignore[index]
        unsorted["Outputs"][1],
        unsorted["Outputs"][0],
    )
    repository, capture_path = _repository(tmp_path / "unsorted", unsorted)
    with pytest.raises(Phase6DnsAliasChangeError):
        _render(repository, capture_path)

    broadened_record = _capture()
    broadened_record["Outputs"][0]["Description"] = "not in the projection"  # type: ignore[index]
    repository, capture_path = _repository(tmp_path / "broad", broadened_record)
    with pytest.raises(Phase6DnsAliasChangeError):
        _render(repository, capture_path)


def test_capture_must_be_canonical_unique_repo_local_and_not_symlinked(tmp_path: Path) -> None:
    repository, capture_path = _repository(tmp_path)
    capture_path.write_text(json.dumps(_capture(), indent=2), encoding="utf-8")
    with pytest.raises(Phase6DnsAliasChangeError):
        _render(repository, capture_path)

    capture_path.write_text(
        '{"Outputs":[],"Outputs":[],"StackId":"x","StackName":"x","StackStatus":"x"}\n',
        encoding="utf-8",
    )
    with pytest.raises(Phase6DnsAliasChangeError):
        _render(repository, capture_path)

    capture_path.write_bytes(_canonical(_capture()))
    link = capture_path.with_name("capture-link.json")
    link.symlink_to(capture_path)
    with pytest.raises(Phase6DnsAliasChangeError):
        _render(repository, link)

    outside = tmp_path / "outside.json"
    outside.write_bytes(_canonical(_capture()))
    with pytest.raises(Phase6DnsAliasChangeError):
        _render(repository, outside)


def test_private_output_is_fixed_create_only_mode_0600_and_exactly_verifiable(
    tmp_path: Path,
) -> None:
    repository, capture_path = _repository(tmp_path)
    options = {
        "hosted_zone_id": HOSTED_ZONE_ID,
        "hosted_zone_observation_path": (
            repository / ".mr_lister_private" / "phase6-dns-alias" / "hosted-zone.json"
        ),
        "cloudfront_domain_name": CLOUDFRONT_DOMAIN,
        "stack_output_capture_path": capture_path,
        "repository_root": repository,
    }

    destination = write_phase6_dns_alias_change(**options)
    original = destination.read_bytes()

    assert destination == repository / DNS_ALIAS_CHANGE_OUTPUT
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    verify_rendered_phase6_dns_alias_change(**options)
    with pytest.raises(Phase6DnsAliasChangeError):
        write_phase6_dns_alias_change(**options)
    assert destination.read_bytes() == original

    destination.chmod(0o644)
    with pytest.raises(Phase6DnsAliasChangeError):
        verify_rendered_phase6_dns_alias_change(**options)
    destination.chmod(0o600)

    changed = deepcopy(json.loads(original))
    changed["ChangeBatch"]["Changes"][0]["ResourceRecordSet"]["Type"] = "CNAME"
    destination.write_bytes(_canonical(changed))
    with pytest.raises(Phase6DnsAliasChangeError):
        verify_rendered_phase6_dns_alias_change(**options)


def test_capture_errors_do_not_disclose_sensitive_values(tmp_path: Path) -> None:
    capture = _capture()
    capture["StackId"] = "secret-invalid-stack-value"
    repository, capture_path = _repository(tmp_path, capture)

    with pytest.raises(Phase6DnsAliasChangeError) as raised:
        _render(repository, capture_path)

    assert str(raised.value) == "Phase 6 DNS alias change input is invalid"
    assert "secret-invalid-stack-value" not in str(raised.value)
