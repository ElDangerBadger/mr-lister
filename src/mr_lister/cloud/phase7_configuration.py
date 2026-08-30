"""Capability-free exact-disabled configuration authority shared by Phase 7 compositions.

The loader validates the complete frozen Phase 7 environment and checked product profile without
constructing an AWS client, importing an SDK, or registering a runtime entrypoint. Query,
request, and worker composition modules may share this authority without inheriting one another's
runtime capabilities.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from mr_lister.cloud.auth import SellerClaimsPolicy
from mr_lister.publication.application import PublicationRuntimeActivation
from mr_lister.publication.profile_eligibility import (
    PinnedPublicationProfileEligibilityAuthority,
    PublicationProfileEligibility,
    build_publication_profile_eligibility,
    require_exact_publication_profile_eligibility,
)
from mr_lister.review_profile import (
    ExactReviewProductProfile,
    FilesystemReviewProductAuthority,
    ReviewProfileNotFoundError,
)

SELLER_SCOPE = "mr-lister-api/seller"
SELLER_GROUP = "seller"

_REGION = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+){1,2}-[1-9][0-9]?$|^us-gov-[a-z]+-[1-9]$")
_TABLE = re.compile(r"^mr-lister-phase6-(?P<environment>[a-z][a-z0-9-]{1,15})$")
_CLIENT_ID = re.compile(r"^[A-Za-z0-9]{1,128}$")
_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_USER_POOL_ID = re.compile(r"^[a-z0-9-]+_[A-Za-z0-9]{1,64}$")
_GENERIC_CONFIGURATION_ERROR = "Phase 7 read-only composition configuration is invalid"


class Phase7ReadConfigurationError(RuntimeError):
    """Value-free failure for missing, malformed, enabled, or drifting configuration."""


@dataclass(frozen=True, slots=True)
class PinnedPublicationProfileConfiguration:
    path: Path
    exact: ExactReviewProductProfile


@dataclass(frozen=True, slots=True)
class Phase7ReadConfiguration:
    region: str
    environment_name: str
    state_table: str
    release_manifest_fingerprint: str
    claims_policy: SellerClaimsPolicy
    profile: PinnedPublicationProfileConfiguration
    eligibility: PublicationProfileEligibility
    activation: PublicationRuntimeActivation


class PinnedPublicationProfileAuthority:
    """Expose one checked draft-safe profile and no directory-wide profile capability."""

    __slots__ = ("_exact",)

    def __init__(self, exact: ExactReviewProductProfile) -> None:
        self._exact = exact

    def get_exact(
        self,
        *,
        profile_id: str,
        profile_version: int,
    ) -> ExactReviewProductProfile:
        if (
            profile_id != self._exact.profile.profile_id
            or profile_version != self._exact.profile.profile_version
        ):
            raise ReviewProfileNotFoundError("The review product profile was not found")
        return self._exact


def load_phase7_read_configuration(
    environment: Mapping[str, object],
) -> Phase7ReadConfiguration:
    """Load the closed configuration while requiring the complete disabled tuple."""

    try:
        region = _required(environment, "AWS_REGION")
        if _REGION.fullmatch(region) is None:
            raise ValueError
        table = _required(environment, "MR_LISTER_STATE_TABLE")
        table_match = _TABLE.fullmatch(table)
        if table_match is None:
            raise ValueError
        release_fingerprint = _required(environment, "MR_LISTER_RELEASE_FINGERPRINT")
        if _FINGERPRINT.fullmatch(release_fingerprint) is None or release_fingerprint == "0" * 64:
            raise ValueError
        if (
            _required(environment, "MR_LISTER_PHASE7_SCAFFOLD_ONLY") != "true"
            or _required(environment, "MR_LISTER_PHASE7_QUERY_ENABLED") != "false"
            or _required(environment, "MR_LISTER_PHASE7_REQUEST_ENABLED") != "false"
            or _required(environment, "MR_LISTER_PHASE7_PUBLICATION_ENABLED") != "false"
        ):
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

        profile = _profile_configuration(environment)
        if profile.exact.profile.publish_enabled is not False:
            raise ValueError
        eligibility = build_publication_profile_eligibility(
            profile_id=profile.exact.profile.profile_id,
            profile_version=profile.exact.profile.profile_version,
            profile_fingerprint=profile.exact.fingerprint,
            release_manifest_fingerprint=release_fingerprint,
            phase6_profile_publish_enabled=profile.exact.profile.publish_enabled,
        )
        eligibility = PinnedPublicationProfileEligibilityAuthority(eligibility).get_exact(
            profile_id=eligibility.profile_id,
            profile_version=eligibility.profile_version,
            profile_fingerprint=eligibility.profile_fingerprint,
            expected_sales_channel=eligibility.expected_sales_channel,
            release_manifest_fingerprint=eligibility.release_manifest_fingerprint,
            phase6_profile_publish_enabled=eligibility.phase6_profile_publish_enabled,
        )
        configured = Phase7ReadConfiguration(
            region=region,
            environment_name=table_match.group("environment"),
            state_table=table,
            release_manifest_fingerprint=release_fingerprint,
            claims_policy=SellerClaimsPolicy(
                issuer=issuer,
                client_id=client_id,
                required_scope=scope,
                required_group=group,
            ),
            profile=profile,
            eligibility=eligibility,
            activation=PublicationRuntimeActivation(
                request_enabled=False,
                publication_enabled=False,
                query_enabled=False,
                scaffold_only=True,
            ),
        )
        return validate_phase7_read_configuration(configured)
    except Exception:
        raise Phase7ReadConfigurationError(_GENERIC_CONFIGURATION_ERROR) from None


def validate_phase7_read_configuration(
    configuration: object,
) -> Phase7ReadConfiguration:
    """Deep-reparse every constituent before any composition receives capability."""

    try:
        if not isinstance(configuration, Phase7ReadConfiguration):
            raise ValueError
        region = configuration.region
        if _REGION.fullmatch(region) is None:
            raise ValueError
        table_match = _TABLE.fullmatch(configuration.state_table)
        if (
            table_match is None
            or table_match.group("environment") != configuration.environment_name
            or _FINGERPRINT.fullmatch(configuration.release_manifest_fingerprint) is None
            or configuration.release_manifest_fingerprint == "0" * 64
        ):
            raise ValueError
        claims = SellerClaimsPolicy(
            issuer=configuration.claims_policy.issuer,
            client_id=configuration.claims_policy.client_id,
            required_scope=configuration.claims_policy.required_scope,
            required_group=configuration.claims_policy.required_group,
        )
        _validate_cognito_issuer(claims.issuer, region)
        if claims.required_scope != SELLER_SCOPE or claims.required_group != SELLER_GROUP:
            raise ValueError
        profile = configuration.profile
        if (
            not isinstance(profile, PinnedPublicationProfileConfiguration)
            or not isinstance(profile.path, Path)
            or not profile.path.is_absolute()
            or profile.path.resolve(strict=True) != profile.path
            or profile.path.name != f"{profile.exact.profile.profile_id}.json"
            or profile.exact.profile.publish_enabled is not False
        ):
            raise ValueError
        reloaded = FilesystemReviewProductAuthority(
            profile_directory=profile.path.parent
        ).get_exact(
            profile_id=profile.exact.profile.profile_id,
            profile_version=profile.exact.profile.profile_version,
        )
        if reloaded != profile.exact:
            raise ValueError
        eligibility = require_exact_publication_profile_eligibility(
            configuration.eligibility.model_dump(mode="python"),
            profile_id=reloaded.profile.profile_id,
            profile_version=reloaded.profile.profile_version,
            profile_fingerprint=reloaded.fingerprint,
            expected_sales_channel="etsy",
            release_manifest_fingerprint=configuration.release_manifest_fingerprint,
            phase6_profile_publish_enabled=reloaded.profile.publish_enabled,
        )
        activation = PublicationRuntimeActivation.model_validate(
            configuration.activation.model_dump(mode="python")
        )
        return Phase7ReadConfiguration(
            region=region,
            environment_name=configuration.environment_name,
            state_table=configuration.state_table,
            release_manifest_fingerprint=configuration.release_manifest_fingerprint,
            claims_policy=claims,
            profile=PinnedPublicationProfileConfiguration(
                path=profile.path,
                exact=reloaded,
            ),
            eligibility=eligibility,
            activation=activation,
        )
    except Exception:
        raise Phase7ReadConfigurationError(_GENERIC_CONFIGURATION_ERROR) from None


def _profile_configuration(
    environment: Mapping[str, object],
) -> PinnedPublicationProfileConfiguration:
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
    path = Path(path_text)
    if (
        not path.is_absolute()
        or path.as_posix() != path_text
        or path.name != f"{profile_id}.json"
        or path.resolve(strict=True) != path
        or not path.is_file()
        or not 1 <= path.stat().st_size <= 1024 * 1024
    ):
        raise ValueError
    exact = FilesystemReviewProductAuthority(profile_directory=path.parent).get_exact(
        profile_id=profile_id,
        profile_version=profile_version,
    )
    if exact.fingerprint != expected_fingerprint:
        raise ValueError
    return PinnedPublicationProfileConfiguration(path=path, exact=exact)


def _validate_cognito_issuer(issuer: str, region: str) -> None:
    parsed = urlsplit(issuer)
    suffix = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
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


__all__ = [
    "SELLER_GROUP",
    "SELLER_SCOPE",
    "Phase7ReadConfiguration",
    "Phase7ReadConfigurationError",
    "PinnedPublicationProfileAuthority",
    "load_phase7_read_configuration",
    "validate_phase7_read_configuration",
]
