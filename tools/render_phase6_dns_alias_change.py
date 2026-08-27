"""Render one exact, private Phase 6 Route 53 apex-alias request offline.

The renderer consumes canonical post-deployment CloudFormation outputs, a canonical public hosted
zone observation, and an independently supplied CloudFront domain name.  It emits only the
``cli-input-json`` document for one Route 53 ``change-resource-record-sets`` call; it imports no
AWS SDK, starts no subprocess, and cannot execute the request.
"""

from __future__ import annotations

import argparse
import json
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from tools.bind_phase6_runtime_config import (
    APPLICATION_ORIGIN,
    EXPECTED_CLOUDFORMATION_OUTPUT_KEYS,
    STACK_ID,
    STACK_NAME,
    load_phase6_web_stack_outputs,
)

ROOT: Final = Path(__file__).resolve().parents[1]
DNS_ALIAS_CHANGE_OUTPUT: Final = Path(
    ".mr_lister_private/phase6-dns-alias/change-resource-record-sets.local.json"
)

ACCOUNT_ID: Final = "384627057108"
REGION: Final = "us-west-2"
APPLICATION_DOMAIN: Final = "massskutiny.com"
CLOUDFRONT_HOSTED_ZONE_ID: Final = "Z2FDTNDATAQYW2"
EXPECTED_NAME_SERVERS: Final = frozenset(
    {
        "ns-592.awsdns-10.net",
        "ns-1427.awsdns-50.org",
        "ns-366.awsdns-45.com",
        "ns-1832.awsdns-37.co.uk",
    }
)

_COMMENT: Final = (
    "Mr Lister Phase 6 massskutiny.com apex aliases to verified CloudFront distribution"
)
_GENERIC_ERROR: Final = "Phase 6 DNS alias change input is invalid"
_HOSTED_ZONE_ID = re.compile(r"^Z[A-Z0-9]{10,31}$")
_CLOUDFRONT_DOMAIN = re.compile(r"^d[a-z0-9]{8,31}\.cloudfront\.net$")


class Phase6DnsAliasChangeError(RuntimeError):
    """A value-free failure for broadened or mismatched DNS alias input."""


def render_phase6_dns_alias_change(
    *,
    hosted_zone_id: str,
    hosted_zone_observation_path: Path,
    cloudfront_domain_name: str,
    stack_output_capture_path: Path,
    repository_root: Path = ROOT,
) -> bytes:
    """Return canonical AWS CLI input bytes for the exact apex A/AAAA alias change."""

    try:
        repository = _repository(repository_root)
        _validate_hosted_zone_id(hosted_zone_id)
        _validate_hosted_zone_observation(
            hosted_zone_observation_path,
            hosted_zone_id=hosted_zone_id,
            repository=repository,
        )
        _validate_cloudfront_domain(cloudfront_domain_name)
        capture = (
            stack_output_capture_path
            if stack_output_capture_path.is_absolute()
            else repository / stack_output_capture_path
        )
        outputs = load_phase6_web_stack_outputs(capture, repository_root=repository)
        if set(outputs) != EXPECTED_CLOUDFORMATION_OUTPUT_KEYS:
            raise ValueError
        if outputs["SellerWebDistributionDomainName"] != cloudfront_domain_name:
            raise ValueError

        request = _build_change_request(hosted_zone_id, cloudfront_domain_name)
        _verify_change_request(
            request,
            hosted_zone_id=hosted_zone_id,
            cloudfront_domain_name=cloudfront_domain_name,
        )
        return _canonical_json(request)
    except Phase6DnsAliasChangeError:
        raise
    except Exception:
        raise Phase6DnsAliasChangeError(_GENERIC_ERROR) from None


def write_phase6_dns_alias_change(
    *,
    hosted_zone_id: str,
    hosted_zone_observation_path: Path,
    cloudfront_domain_name: str,
    stack_output_capture_path: Path,
    repository_root: Path = ROOT,
) -> Path:
    """Create the fixed private request file once and refuse replacement."""

    try:
        repository = _repository(repository_root)
        destination = _output_destination(repository)
        _prepare_private_parent(repository, destination.parent)
        if destination.exists() or destination.is_symlink():
            raise ValueError
        raw = render_phase6_dns_alias_change(
            hosted_zone_id=hosted_zone_id,
            hosted_zone_observation_path=hosted_zone_observation_path,
            cloudfront_domain_name=cloudfront_domain_name,
            stack_output_capture_path=stack_output_capture_path,
            repository_root=repository,
        )
        with destination.open("xb") as stream:
            stream.write(raw)
        destination.chmod(0o600)
        if destination.read_bytes() != raw or stat.S_IMODE(destination.stat().st_mode) != 0o600:
            raise ValueError
        return destination
    except Phase6DnsAliasChangeError:
        raise
    except Exception:
        raise Phase6DnsAliasChangeError(_GENERIC_ERROR) from None


def verify_rendered_phase6_dns_alias_change(
    *,
    hosted_zone_id: str,
    hosted_zone_observation_path: Path,
    cloudfront_domain_name: str,
    stack_output_capture_path: Path,
    repository_root: Path = ROOT,
) -> None:
    """Require the existing private request to equal a fresh render byte-for-byte."""

    try:
        repository = _repository(repository_root)
        destination = _output_destination(repository)
        if (
            destination.is_symlink()
            or not destination.is_file()
            or stat.S_IMODE(destination.stat().st_mode) != 0o600
        ):
            raise ValueError
        expected = render_phase6_dns_alias_change(
            hosted_zone_id=hosted_zone_id,
            hosted_zone_observation_path=hosted_zone_observation_path,
            cloudfront_domain_name=cloudfront_domain_name,
            stack_output_capture_path=stack_output_capture_path,
            repository_root=repository,
        )
        if destination.read_bytes() != expected:
            raise ValueError
    except Phase6DnsAliasChangeError:
        raise
    except Exception:
        raise Phase6DnsAliasChangeError(_GENERIC_ERROR) from None


def _repository(repository_root: Path) -> Path:
    repository = repository_root.resolve(strict=True)
    if repository_root.is_symlink() or not repository.is_dir():
        raise ValueError
    return repository


def _validate_hosted_zone_id(hosted_zone_id: object) -> None:
    if (
        not isinstance(hosted_zone_id, str)
        or _HOSTED_ZONE_ID.fullmatch(hosted_zone_id) is None
        or hosted_zone_id == CLOUDFRONT_HOSTED_ZONE_ID
    ):
        raise ValueError


def _validate_hosted_zone_observation(
    path: Path,
    *,
    hosted_zone_id: str,
    repository: Path,
) -> None:
    document = _load_canonical_mapping(path, repository)
    if set(document) != {"DelegationSet", "HostedZone"}:
        raise ValueError
    delegation_set = document.get("DelegationSet")
    hosted_zone = document.get("HostedZone")
    if (
        not isinstance(delegation_set, Mapping)
        or set(delegation_set) != {"NameServers"}
        or not isinstance(hosted_zone, Mapping)
        or set(hosted_zone) != {"CallerReference", "Config", "Id", "Name", "ResourceRecordSetCount"}
    ):
        raise ValueError
    name_servers = delegation_set.get("NameServers")
    if (
        not isinstance(name_servers, list)
        or len(name_servers) != len(EXPECTED_NAME_SERVERS)
        or any(not isinstance(value, str) for value in name_servers)
        or set(name_servers) != EXPECTED_NAME_SERVERS
    ):
        raise ValueError
    caller_reference = hosted_zone.get("CallerReference")
    record_count = hosted_zone.get("ResourceRecordSetCount")
    config = hosted_zone.get("Config")
    if (
        not isinstance(caller_reference, str)
        or not caller_reference
        or not caller_reference.isascii()
        or len(caller_reference) > 128
        or hosted_zone.get("Id") != f"/hostedzone/{hosted_zone_id}"
        or hosted_zone.get("Name") != f"{APPLICATION_DOMAIN}."
        or isinstance(record_count, bool)
        or not isinstance(record_count, int)
        or record_count < 2
        or not isinstance(config, Mapping)
        or not {"PrivateZone"} <= set(config) <= {"Comment", "PrivateZone"}
        or config.get("PrivateZone") is not False
    ):
        raise ValueError
    if "Comment" in config:
        comment = config["Comment"]
        if (
            not isinstance(comment, str)
            or not comment.isascii()
            or len(comment) > 256
            or comment != comment.strip()
        ):
            raise ValueError


def _validate_cloudfront_domain(cloudfront_domain_name: object) -> None:
    if (
        not isinstance(cloudfront_domain_name, str)
        or _CLOUDFRONT_DOMAIN.fullmatch(cloudfront_domain_name) is None
    ):
        raise ValueError


def _build_change_request(
    hosted_zone_id: str,
    cloudfront_domain_name: str,
) -> dict[str, object]:
    alias_target = {
        "DNSName": f"{cloudfront_domain_name}.",
        "EvaluateTargetHealth": False,
        "HostedZoneId": CLOUDFRONT_HOSTED_ZONE_ID,
    }
    return {
        "ChangeBatch": {
            "Changes": [
                {
                    "Action": "CREATE",
                    "ResourceRecordSet": {
                        "AliasTarget": dict(alias_target),
                        "Name": f"{APPLICATION_DOMAIN}.",
                        "Type": record_type,
                    },
                }
                for record_type in ("A", "AAAA")
            ],
            "Comment": _COMMENT,
        },
        "HostedZoneId": hosted_zone_id,
    }


def _verify_change_request(
    request: Mapping[str, object],
    *,
    hosted_zone_id: str,
    cloudfront_domain_name: str,
) -> None:
    if (
        set(request) != {"ChangeBatch", "HostedZoneId"}
        or request.get("HostedZoneId") != hosted_zone_id
    ):
        raise ValueError
    batch = request.get("ChangeBatch")
    if not isinstance(batch, Mapping) or set(batch) != {"Changes", "Comment"}:
        raise ValueError
    changes = batch.get("Changes")
    if batch.get("Comment") != _COMMENT or not isinstance(changes, list) or len(changes) != 2:
        raise ValueError
    for change, record_type in zip(changes, ("A", "AAAA"), strict=True):
        if (
            not isinstance(change, Mapping)
            or set(change) != {"Action", "ResourceRecordSet"}
            or change.get("Action") != "CREATE"
        ):
            raise ValueError
        record = change.get("ResourceRecordSet")
        if (
            not isinstance(record, Mapping)
            or set(record) != {"AliasTarget", "Name", "Type"}
            or record.get("Name") != f"{APPLICATION_DOMAIN}."
            or record.get("Type") != record_type
        ):
            raise ValueError
        alias = record.get("AliasTarget")
        if (
            not isinstance(alias, Mapping)
            or set(alias) != {"DNSName", "EvaluateTargetHealth", "HostedZoneId"}
            or alias.get("DNSName") != f"{cloudfront_domain_name}."
            or alias.get("EvaluateTargetHealth") is not False
            or alias.get("HostedZoneId") != CLOUDFRONT_HOSTED_ZONE_ID
        ):
            raise ValueError


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _load_canonical_mapping(path: Path, repository: Path) -> Mapping[str, object]:
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
    if not resolved.is_relative_to(repository) or not resolved.is_file():
        raise ValueError
    raw = resolved.read_bytes()
    if not raw or len(raw) > 65_536 or b"\x00" in raw:
        raise ValueError
    document = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(document, Mapping) or _canonical_json(document) != raw:
        raise ValueError
    return document


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _output_destination(repository: Path) -> Path:
    destination = repository / DNS_ALIAS_CHANGE_OUTPUT
    if destination.relative_to(repository).parts[0] != ".mr_lister_private":
        raise ValueError
    return destination


def _prepare_private_parent(repository: Path, parent: Path) -> None:
    if not parent.is_relative_to(repository):
        raise ValueError
    current = repository
    for component in parent.relative_to(repository).parts:
        current = current / component
        if current.exists():
            if current.is_symlink() or not current.is_dir():
                raise ValueError
        else:
            current.mkdir(mode=0o700)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hosted-zone-id", required=True)
    parser.add_argument("--hosted-zone-observation", required=True, type=Path)
    parser.add_argument("--cloudfront-domain-name", required=True)
    parser.add_argument("--stack-output-capture", required=True, type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true")
    action.add_argument("--verify", action="store_true")
    arguments = parser.parse_args()
    options = {
        "hosted_zone_id": arguments.hosted_zone_id,
        "hosted_zone_observation_path": arguments.hosted_zone_observation,
        "cloudfront_domain_name": arguments.cloudfront_domain_name,
        "stack_output_capture_path": arguments.stack_output_capture,
    }
    try:
        if arguments.write:
            print(write_phase6_dns_alias_change(**options))
        else:
            verify_rendered_phase6_dns_alias_change(**options)
            print(_output_destination(ROOT))
    except Phase6DnsAliasChangeError as error:
        parser.exit(2, f"{error}\n")


__all__ = [
    "ACCOUNT_ID",
    "APPLICATION_DOMAIN",
    "APPLICATION_ORIGIN",
    "CLOUDFRONT_HOSTED_ZONE_ID",
    "DNS_ALIAS_CHANGE_OUTPUT",
    "EXPECTED_NAME_SERVERS",
    "REGION",
    "STACK_ID",
    "STACK_NAME",
    "Phase6DnsAliasChangeError",
    "render_phase6_dns_alias_change",
    "verify_rendered_phase6_dns_alias_change",
    "write_phase6_dns_alias_change",
]


if __name__ == "__main__":
    main()
