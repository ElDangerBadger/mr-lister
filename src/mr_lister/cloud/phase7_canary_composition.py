"""Private composition for one release-bound, direct-invoke Phase 7 canary.

The configuration has no seller authentication or public route authority.  It requires every
broad Phase 7 capability flag to remain false, accepts the canary mode only from a verified
packaged binding, and constructs clients only when an entrypoint explicitly builds the graph.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import SecretStr

from mr_lister.cloud.phase7_worker_composition import compose_publication_worker_graph
from mr_lister.publication.canary_runtime import (
    PublicationCanaryBinding,
    PublicationCanaryRuntime,
)
from mr_lister.publication.execution_models import (
    PublicationProviderAuditDecision,
    PublicationProviderAuditRecord,
    PublicationProviderAuthority,
)
from mr_lister.publication.profile_eligibility import (
    PublicationProfileEligibility,
    build_publication_profile_eligibility,
    require_exact_publication_profile_eligibility,
)
from mr_lister.publication.provider_boundary import (
    PublicationHttpTransport,
    RedirectSafePublicationTransport,
)
from mr_lister.publication.provider_credentials import (
    BoundPublicationProviderCredential,
    PublicationProviderCredentialAuthority,
    PublicationProviderCredentialError,
    issue_bound_publication_provider_credential,
)
from mr_lister.review_profile import ExactReviewProductProfile, FilesystemReviewProductAuthority

Phase7CanaryAwsService = Literal["dynamodb", "secretsmanager"]

PHASE7_CANARY_TIMEOUT_SECONDS = 15.0
PHASE7_CANARY_USER_AGENT = "MrLister-Phase7/canary"

_REGION = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+){1,2}-[1-9][0-9]?$|^us-gov-[a-z]+-[1-9]$")
_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9-]{1,15}$")
_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_SECRET_ARN = re.compile(
    r"^arn:(aws|aws-us-gov|aws-cn):secretsmanager:([a-z0-9-]+):"
    r"([0-9]{12}):secret:mr-lister/[A-Za-z0-9/_-]+-[A-Za-z0-9]{6}$"
)
_GENERIC_CONFIGURATION_ERROR = "Phase 7 canary configuration is invalid"
_GENERIC_CREDENTIAL_ERROR = "Publication provider credential is unavailable"
_OWNER_SECRET_SCHEMA_VERSION = "phase6-printify-owner-v1"
_OWNER_SECRET_FIELDS = frozenset({"schema_version", "owner_id", "shop_id", "api_token"})
_MAX_SECRET_STRING_CHARS = 16_384
_API_TOKEN = re.compile(r"^[\x21-\x7e]{1,4096}$")
_REJECTED_AUDIT_LOGGER = logging.getLogger("mr_lister.phase7.canary.rejected_audit")


class Phase7CanaryConfigurationError(RuntimeError):
    """Value-free refusal for malformed, drifting, or over-capable canary settings."""


class Phase7CanaryAwsClientFactory(Protocol):
    def __call__(
        self,
        service_name: Phase7CanaryAwsService,
        *,
        region_name: str,
    ) -> object: ...


class Phase7CanaryHandler(Protocol):
    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, str]: ...


class FreshCanaryPublicationProviderCredentialAuthority:
    """Resolve and bind the one owner secret afresh for every provider step."""

    __slots__ = ("_get_secret_value", "_secret_arn")

    def __init__(self, *, client: object, secret_arn: str) -> None:
        get_secret_value = getattr(client, "get_secret_value", None)
        if not callable(get_secret_value) or _SECRET_ARN.fullmatch(secret_arn) is None:
            raise PublicationProviderCredentialError(
                "Publication provider credential configuration is invalid"
            ) from None
        self._get_secret_value = get_secret_value
        self._secret_arn = secret_arn

    def resolve_exact(
        self,
        *,
        authority: PublicationProviderAuthority,
    ) -> BoundPublicationProviderCredential:
        try:
            exact = PublicationProviderAuthority.model_validate(authority.model_dump(mode="python"))
            response = self._get_secret_value(SecretId=self._secret_arn)
            if not isinstance(response, Mapping) or "SecretBinary" in response:
                raise ValueError
            if response.get("ARN", self._secret_arn) != self._secret_arn:
                raise ValueError
            if response.get("VersionStages", ["AWSCURRENT"]) != ["AWSCURRENT"]:
                raise ValueError
            secret_string = response.get("SecretString")
            if (
                not isinstance(secret_string, str)
                or not secret_string
                or len(secret_string) > _MAX_SECRET_STRING_CHARS
            ):
                raise ValueError
            payload = json.loads(
                secret_string,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
            if not isinstance(payload, dict) or set(payload) != _OWNER_SECRET_FIELDS:
                raise ValueError
            owner_id = payload["owner_id"]
            shop_id = payload["shop_id"]
            token = payload["api_token"]
            if (
                payload["schema_version"] != _OWNER_SECRET_SCHEMA_VERSION
                or owner_id != exact.owner_id
                or type(shop_id) is not int
                or shop_id != exact.printify_shop_id
                or not isinstance(token, str)
                or _API_TOKEN.fullmatch(token) is None
            ):
                raise ValueError
            return issue_bound_publication_provider_credential(
                authority=exact,
                bearer_token=SecretStr(token),
            )
        except Exception:
            pass
        raise PublicationProviderCredentialError(_GENERIC_CREDENTIAL_ERROR) from None


@dataclass(frozen=True, slots=True)
class PinnedCanaryProfileConfiguration:
    path: Path
    exact: ExactReviewProductProfile


@dataclass(frozen=True, slots=True)
class Phase7CanaryConfiguration:
    region: str
    environment_name: str
    account_id: str
    state_table: str
    application_release_fingerprint: str
    canary_release_fingerprint: str
    profile: PinnedCanaryProfileConfiguration
    eligibility: PublicationProfileEligibility
    secret_arn: str
    binding: PublicationCanaryBinding


def load_phase7_canary_configuration(
    environment: Mapping[str, object],
    *,
    binding: PublicationCanaryBinding,
) -> Phase7CanaryConfiguration:
    """Load one private canary configuration without constructing any capability."""

    try:
        if not isinstance(environment, Mapping):
            raise ValueError
        exact_binding = _exact_binding(binding)
        region = _required(environment, "AWS_REGION")
        if _REGION.fullmatch(region) is None:
            raise ValueError
        environment_name = _required(environment, "MR_LISTER_ENVIRONMENT")
        if _ENVIRONMENT.fullmatch(environment_name) is None:
            raise ValueError
        account_id = _required(environment, "MR_LISTER_AWS_ACCOUNT_ID")
        if _ACCOUNT_ID.fullmatch(account_id) is None or account_id == "0" * 12:
            raise ValueError
        state_table = _required(environment, "MR_LISTER_STATE_TABLE")
        if state_table != f"mr-lister-phase6-{environment_name}":
            raise ValueError
        application_release = _fingerprint(
            environment,
            "MR_LISTER_RELEASE_FINGERPRINT",
        )
        canary_release = _fingerprint(
            environment,
            "MR_LISTER_PHASE7_CANARY_RELEASE_FINGERPRINT",
        )
        binding_fingerprint = _fingerprint(
            environment,
            "MR_LISTER_PHASE7_CANARY_BINDING_FINGERPRINT",
        )
        if (
            exact_binding.release_manifest_fingerprint != application_release
            or exact_binding.fingerprint != binding_fingerprint
            or _required(environment, "MR_LISTER_PHASE7_CANARY_MODE") != exact_binding.mode.value
            or _required(environment, "MR_LISTER_PHASE7_SCAFFOLD_ONLY") != "false"
            or _required(environment, "MR_LISTER_PHASE7_QUERY_ENABLED") != "false"
            or _required(environment, "MR_LISTER_PHASE7_REQUEST_ENABLED") != "false"
            or _required(environment, "MR_LISTER_PHASE7_PUBLICATION_ENABLED") != "false"
            or _required(environment, "MR_LISTER_PHASE7_CANARY_ENABLED") != "true"
        ):
            raise ValueError
        profile = _profile_configuration(environment)
        if profile.exact.profile.publish_enabled is not False:
            raise ValueError
        eligibility = build_publication_profile_eligibility(
            profile_id=profile.exact.profile.profile_id,
            profile_version=profile.exact.profile.profile_version,
            profile_fingerprint=profile.exact.fingerprint,
            release_manifest_fingerprint=application_release,
            phase6_profile_publish_enabled=profile.exact.profile.publish_enabled,
        )
        secret_arn = _required(environment, "MR_LISTER_PRINTIFY_SECRET_ARN")
        _require_exact_secret_arn(
            secret_arn,
            region=region,
            account_id=account_id,
        )
        return validate_phase7_canary_configuration(
            Phase7CanaryConfiguration(
                region=region,
                environment_name=environment_name,
                account_id=account_id,
                state_table=state_table,
                application_release_fingerprint=application_release,
                canary_release_fingerprint=canary_release,
                profile=profile,
                eligibility=eligibility,
                secret_arn=secret_arn,
                binding=exact_binding,
            )
        )
    except Exception:
        raise Phase7CanaryConfigurationError(_GENERIC_CONFIGURATION_ERROR) from None


def validate_phase7_canary_configuration(
    configuration: object,
) -> Phase7CanaryConfiguration:
    """Deep-reparse a canary configuration before it receives clients or a transport."""

    try:
        if not isinstance(configuration, Phase7CanaryConfiguration):
            raise ValueError
        if (
            _REGION.fullmatch(configuration.region) is None
            or _ENVIRONMENT.fullmatch(configuration.environment_name) is None
            or _ACCOUNT_ID.fullmatch(configuration.account_id) is None
            or configuration.account_id == "0" * 12
            or configuration.state_table != f"mr-lister-phase6-{configuration.environment_name}"
        ):
            raise ValueError
        _require_nonzero_fingerprint(configuration.application_release_fingerprint)
        _require_nonzero_fingerprint(configuration.canary_release_fingerprint)
        binding = _exact_binding(configuration.binding)
        if binding.release_manifest_fingerprint != configuration.application_release_fingerprint:
            raise ValueError
        _require_exact_secret_arn(
            configuration.secret_arn,
            region=configuration.region,
            account_id=configuration.account_id,
        )
        profile = configuration.profile
        if (
            not isinstance(profile, PinnedCanaryProfileConfiguration)
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
            release_manifest_fingerprint=configuration.application_release_fingerprint,
            phase6_profile_publish_enabled=reloaded.profile.publish_enabled,
        )
        return Phase7CanaryConfiguration(
            region=configuration.region,
            environment_name=configuration.environment_name,
            account_id=configuration.account_id,
            state_table=configuration.state_table,
            application_release_fingerprint=configuration.application_release_fingerprint,
            canary_release_fingerprint=configuration.canary_release_fingerprint,
            profile=PinnedCanaryProfileConfiguration(path=profile.path, exact=reloaded),
            eligibility=eligibility,
            secret_arn=configuration.secret_arn,
            binding=binding,
        )
    except Exception:
        raise Phase7CanaryConfigurationError(_GENERIC_CONFIGURATION_ERROR) from None


def compose_publication_canary_runtime(
    configuration: Phase7CanaryConfiguration,
    *,
    dynamodb: object,
    credentials: PublicationProviderCredentialAuthority,
    transport: PublicationHttpTransport,
    rejected_audit_writer: Callable[[PublicationProviderAuditRecord], None],
    clock: Callable[[], datetime] | None = None,
) -> PublicationCanaryRuntime:
    """Join the exact canary envelope to the shared worker graph without performing I/O."""

    exact = validate_phase7_canary_configuration(configuration)
    coordinator = compose_publication_worker_graph(
        state_table=exact.state_table,
        release_manifest_fingerprint=exact.application_release_fingerprint,
        exact_profile=exact.profile.exact,
        eligibility=exact.eligibility,
        dynamodb=dynamodb,
        credentials=credentials,
        transport=transport,
        rejected_audit_writer=rejected_audit_writer,
        clock=clock,
        timeout_seconds=PHASE7_CANARY_TIMEOUT_SECONDS,
        user_agent=PHASE7_CANARY_USER_AGENT,
    )
    return PublicationCanaryRuntime(binding=exact.binding, coordinator=coordinator)


class _PublicationCanaryHandler:
    __slots__ = ("_runtime",)

    def __init__(self, runtime: PublicationCanaryRuntime) -> None:
        self._runtime = runtime

    def __call__(
        self,
        event: Mapping[str, Any],
        context: object | None = None,
    ) -> dict[str, str]:
        del context
        return self._runtime.invoke(event)


def build_publication_canary_handler(
    environment: Mapping[str, object],
    *,
    binding: PublicationCanaryBinding,
    client_factory: Phase7CanaryAwsClientFactory | None = None,
    transport: PublicationHttpTransport | None = None,
    rejected_audit_writer: Callable[[PublicationProviderAuditRecord], None] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Phase7CanaryHandler:
    """Build one private handler after its caller has verified the packaged release."""

    configuration = load_phase7_canary_configuration(environment, binding=binding)
    factory = client_factory or default_aws_client_factory
    dynamodb = _client(
        factory,
        "dynamodb",
        configuration.region,
        required_methods=("get_item", "query", "transact_write_items"),
    )
    secrets = _client(
        factory,
        "secretsmanager",
        configuration.region,
        required_methods=("get_secret_value",),
    )
    credentials = FreshCanaryPublicationProviderCredentialAuthority(
        client=secrets,
        secret_arn=configuration.secret_arn,
    )
    runtime = compose_publication_canary_runtime(
        configuration,
        dynamodb=dynamodb,
        credentials=credentials,
        transport=transport or RedirectSafePublicationTransport(),
        rejected_audit_writer=rejected_audit_writer or write_sanitized_rejected_audit,
        clock=clock,
    )
    return _PublicationCanaryHandler(runtime)


def default_aws_client_factory(
    service_name: Phase7CanaryAwsService,
    *,
    region_name: str,
) -> object:
    """Create only one regional canary dependency, never at module import time."""

    if service_name not in {"dynamodb", "secretsmanager"}:
        raise ValueError("Unsupported Phase 7 canary AWS client")
    import boto3

    return boto3.client(service_name, region_name=region_name)


def write_sanitized_rejected_audit(record: PublicationProviderAuditRecord) -> None:
    """Log only the closed, identity-free rejected provider audit contract."""

    try:
        exact = PublicationProviderAuditRecord.model_validate(record.model_dump(mode="python"))
        if exact != record or exact.decision is not PublicationProviderAuditDecision.REJECTED:
            raise ValueError
        payload = {
            "category": exact.category.value,
            "decision": exact.decision.value,
            "event": "phase7_canary_provider_rejected",
            "fingerprint": exact.fingerprint,
            "method_category": exact.method_category,
            "route_template": exact.route_template,
        }
        _REJECTED_AUDIT_LOGGER.error(
            "%s",
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
        )
        return
    except Exception:
        raise RuntimeError("Phase 7 canary rejected-audit logger is unavailable") from None


def _client(
    factory: Phase7CanaryAwsClientFactory,
    service_name: Phase7CanaryAwsService,
    region_name: str,
    *,
    required_methods: tuple[str, ...],
) -> object:
    client = factory(service_name, region_name=region_name)
    if any(not callable(getattr(client, method, None)) for method in required_methods):
        raise RuntimeError("Phase 7 canary dependency is unavailable")
    return client


def _profile_configuration(
    environment: Mapping[str, object],
) -> PinnedCanaryProfileConfiguration:
    profile_id = _required(environment, "MR_LISTER_PRODUCT_PROFILE_ID")
    if _PROFILE_ID.fullmatch(profile_id) is None:
        raise ValueError
    version_text = _required(environment, "MR_LISTER_PRODUCT_PROFILE_VERSION")
    if re.fullmatch(r"[1-9][0-9]{0,5}", version_text) is None:
        raise ValueError
    fingerprint = _fingerprint(environment, "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT")
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
        profile_version=int(version_text),
    )
    if exact.fingerprint != fingerprint:
        raise ValueError
    return PinnedCanaryProfileConfiguration(path=path, exact=exact)


def _exact_binding(binding: object) -> PublicationCanaryBinding:
    if not isinstance(binding, PublicationCanaryBinding):
        raise ValueError
    exact = PublicationCanaryBinding.model_validate(binding.model_dump(mode="python"))
    if exact != binding:
        raise ValueError
    return exact


def _fingerprint(environment: Mapping[str, object], name: str) -> str:
    value = _required(environment, name)
    _require_nonzero_fingerprint(value)
    return value


def _require_nonzero_fingerprint(value: object) -> None:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None or value == "0" * 64:
        raise ValueError


def _require_exact_secret_arn(value: str, *, region: str, account_id: str) -> None:
    matched = _SECRET_ARN.fullmatch(value)
    if (
        matched is None
        or matched.group(1) != _partition(region)
        or matched.group(2) != region
        or matched.group(3) != account_id
    ):
        raise ValueError


def _partition(region: str) -> str:
    if region.startswith("cn-"):
        return "aws-cn"
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    return "aws"


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


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError


__all__ = [
    "PHASE7_CANARY_TIMEOUT_SECONDS",
    "PHASE7_CANARY_USER_AGENT",
    "FreshCanaryPublicationProviderCredentialAuthority",
    "Phase7CanaryConfiguration",
    "Phase7CanaryConfigurationError",
    "Phase7CanaryHandler",
    "PinnedCanaryProfileConfiguration",
    "build_publication_canary_handler",
    "compose_publication_canary_runtime",
    "load_phase7_canary_configuration",
    "validate_phase7_canary_configuration",
    "write_sanitized_rejected_audit",
]
