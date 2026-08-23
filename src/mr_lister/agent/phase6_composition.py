"""Production composition for the dedicated Phase 6 AgentCore Strands runtime.

The controller is pinned to Nova 2 Lite while the image-review and listing intelligence
worker is pinned to the checked Gemma 3 configuration.  This module has no Printify,
publication, order, fulfillment, Step Functions, or seller-API capability.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol, cast

from bedrock_agentcore import BedrockAgentCoreApp
from strands.models.model import Model

from mr_lister.agent.observability import LoggingAgentAuditSink
from mr_lister.agent.phase6 import (
    Phase6PreparationBackend,
    WorkerControlPreparationAdapter,
    create_phase6_agentcore_runtime,
)
from mr_lister.agent.phase6_producer import PinnedSourcePreparedReviewProducer
from mr_lister.control.dynamodb import DynamoDBSellerControlStore
from mr_lister.control.worker_service import WorkerControlService
from mr_lister.intelligence.bedrock import build_bedrock_adapter
from mr_lister.intelligence.settings import BedrockSettings
from mr_lister.review_profile import (
    ExactReviewProductProfile,
    FilesystemReviewProductAuthority,
    ReviewProfileNotFoundError,
)

PHASE6_GEMMA_MODEL_ID = "google.gemma-3-27b-it"
PHASE6_STRANDS_CONTROLLER_MODEL_ID = "us.amazon.nova-2-lite-v1:0"

_REGION = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+){1,2}-[1-9][0-9]?$|^us-gov-[a-z]+-[1-9]$")
_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9-]{1,15}$")
_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_SOURCE_KEY = re.compile(
    r"^private/owners/[a-f0-9]{64}/jobs/[A-Za-z0-9][A-Za-z0-9_-]{0,127}/"
    r"source/source\.png$"
)
_VERSION_ID = re.compile(r"^[\x21-\x7e]{1,1024}$")
_GENERIC_CONFIGURATION_ERROR = "Phase 6 AgentCore configuration is invalid"


class Phase6AgentCoreConfigurationError(RuntimeError):
    """One value-free error for missing or drifting runtime configuration."""


class Phase6AgentCoreDependencyError(RuntimeError):
    """One value-free error for missing runtime dependencies."""


class AgentCoreDynamoClient(Protocol):
    def get_item(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def put_item(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def transact_write_items(self, **kwargs: Any) -> Mapping[str, Any]: ...


class AgentCoreS3Client(Protocol):
    def get_object(self, **kwargs: Any) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class Phase6AgentCoreConfiguration:
    region: str
    environment_name: str
    account_id: str
    state_table: str
    artifact_bucket: str
    release_fingerprint: str
    profile: AgentCorePinnedProfileConfiguration
    intelligence_path: Path
    intelligence_fingerprint: str
    intelligence: BedrockSettings
    controller_model_id: str


@dataclass(frozen=True, slots=True)
class AgentCorePinnedProfileConfiguration:
    profile_path: Path
    exact: ExactReviewProductProfile


class AgentCorePinnedProductAuthority:
    """Expose only the one exact product profile packaged into this runtime."""

    __slots__ = ("_exact",)

    def __init__(self, exact: ExactReviewProductProfile) -> None:
        self._exact = exact

    def get_exact(self, *, profile_id: str, profile_version: int) -> ExactReviewProductProfile:
        if (
            profile_id != self._exact.profile.profile_id
            or profile_version != self._exact.profile.profile_version
        ):
            raise ReviewProfileNotFoundError("The review product profile was not found")
        return self._exact


class ExactPinnedSourceS3:
    """Add account, checksum, encryption, and exact-key constraints to source reads."""

    __slots__ = ("_bucket", "_bucket_owner", "_client")

    def __init__(
        self,
        *,
        client: AgentCoreS3Client,
        bucket: str,
        bucket_owner_account_id: str,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._bucket_owner = bucket_owner_account_id

    def get_object(self, *, Bucket: str, Key: str, VersionId: str) -> Mapping[str, object]:
        if (
            Bucket != self._bucket
            or not isinstance(Key, str)
            or _SOURCE_KEY.fullmatch(Key) is None
            or not isinstance(VersionId, str)
            or _VERSION_ID.fullmatch(VersionId) is None
            or VersionId == "null"
        ):
            raise Phase6AgentCoreDependencyError("Pinned source object is unavailable")
        try:
            response = self._client.get_object(
                Bucket=self._bucket,
                Key=Key,
                VersionId=VersionId,
                ChecksumMode="ENABLED",
                ExpectedBucketOwner=self._bucket_owner,
            )
        except Exception:
            raise Phase6AgentCoreDependencyError("Pinned source object is unavailable") from None
        if (
            not isinstance(response, Mapping)
            or response.get("VersionId") != VersionId
            or response.get("ServerSideEncryption") != "AES256"
            or not isinstance(response.get("ChecksumSHA256"), str)
        ):
            raise Phase6AgentCoreDependencyError("Pinned source object is unavailable")
        return cast(Mapping[str, object], response)


def load_phase6_agentcore_configuration(
    environment: Mapping[str, object],
) -> Phase6AgentCoreConfiguration:
    try:
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
        artifact_bucket = _required(environment, "MR_LISTER_ARTIFACT_BUCKET")
        if artifact_bucket != (
            f"mr-lister-phase6-artifacts-{environment_name}-{account_id}-{region}"
        ):
            raise ValueError
        release_fingerprint = _fingerprint(environment, "MR_LISTER_RELEASE_FINGERPRINT")
        profile = _profile(environment)
        intelligence_path = _exact_path(
            environment,
            "MR_LISTER_GEMMA_CONFIG_PATH",
            expected_name="google_gemma_3_27b_it.json",
        )
        intelligence_fingerprint = _fingerprint(
            environment,
            "MR_LISTER_GEMMA_CONFIG_FINGERPRINT",
        )
        raw_intelligence = intelligence_path.read_bytes()
        if (
            not 1 <= len(raw_intelligence) <= 64 * 1024
            or sha256(raw_intelligence).hexdigest() != intelligence_fingerprint
        ):
            raise ValueError
        intelligence = BedrockSettings.model_validate_json(raw_intelligence)
        if intelligence != BedrockSettings(
            region=region,
            model_id=PHASE6_GEMMA_MODEL_ID,
            output_mode="native_json_schema",
            max_tokens=2048,
            temperature=0.0,
            max_repair_attempts=2,
        ):
            raise ValueError
        controller_model_id = _required(environment, "MR_LISTER_STRANDS_CONTROLLER_MODEL_ID")
        if controller_model_id != PHASE6_STRANDS_CONTROLLER_MODEL_ID:
            raise ValueError
        return Phase6AgentCoreConfiguration(
            region=region,
            environment_name=environment_name,
            account_id=account_id,
            state_table=state_table,
            artifact_bucket=artifact_bucket,
            release_fingerprint=release_fingerprint,
            profile=profile,
            intelligence_path=intelligence_path,
            intelligence_fingerprint=intelligence_fingerprint,
            intelligence=intelligence,
            controller_model_id=controller_model_id,
        )
    except Exception:
        pass
    raise Phase6AgentCoreConfigurationError(_GENERIC_CONFIGURATION_ERROR)


def compose_phase6_agentcore_runtime(
    configuration: Phase6AgentCoreConfiguration,
    *,
    dynamodb_client: AgentCoreDynamoClient,
    s3_client: AgentCoreS3Client,
    intelligence: object,
    controller_model: Model | str,
) -> BedrockAgentCoreApp:
    if not all(
        callable(getattr(dynamodb_client, method, None))
        for method in ("get_item", "put_item", "transact_write_items")
    ):
        raise Phase6AgentCoreDependencyError("Phase 6 AgentCore dependency is unavailable")
    if not callable(getattr(s3_client, "get_object", None)):
        raise Phase6AgentCoreDependencyError("Phase 6 AgentCore dependency is unavailable")
    if not callable(getattr(intelligence, "inspect_artwork", None)) or not callable(
        getattr(intelligence, "draft_listing", None)
    ):
        raise Phase6AgentCoreDependencyError("Phase 6 AgentCore dependency is unavailable")
    if isinstance(controller_model, str) and controller_model != configuration.controller_model_id:
        raise Phase6AgentCoreDependencyError("Phase 6 AgentCore dependency is unavailable")

    store = DynamoDBSellerControlStore(
        client=dynamodb_client,
        table_name=configuration.state_table,
    )
    profiles = AgentCorePinnedProductAuthority(configuration.profile.exact)
    source_client = ExactPinnedSourceS3(
        client=s3_client,
        bucket=configuration.artifact_bucket,
        bucket_owner_account_id=configuration.account_id,
    )
    producer = PinnedSourcePreparedReviewProducer(
        store=store,
        s3=source_client,
        profiles=profiles,
        intelligence=cast(Any, intelligence),
    )
    worker = WorkerControlService(store=store)
    service = WorkerControlPreparationAdapter(
        store=store,
        worker=worker,
        producer=producer,
    )
    backend = Phase6PreparationBackend(store=store, service=service)
    return create_phase6_agentcore_runtime(
        backend=backend,
        model=controller_model,
        audit_sink=LoggingAgentAuditSink(),
    )


def build_phase6_agentcore_runtime(
    environment: Mapping[str, object],
    *,
    session: Any | None = None,
) -> BedrockAgentCoreApp:
    """Build the real runtime using AWS's default credential chain and no mutable fallback."""

    configuration = load_phase6_agentcore_configuration(environment)
    try:
        import boto3
        from botocore.config import Config
        from strands.models import BedrockModel

        active_session = session or boto3.Session(region_name=configuration.region)
        dynamodb = active_session.client(
            "dynamodb",
            region_name=configuration.region,
            config=Config(
                connect_timeout=10,
                read_timeout=30,
                retries={"mode": "standard", "max_attempts": 3},
            ),
        )
        s3 = active_session.client(
            "s3",
            region_name=configuration.region,
            config=Config(
                connect_timeout=10,
                read_timeout=60,
                retries={"mode": "standard", "max_attempts": 3},
                signature_version="s3v4",
                s3={"addressing_style": "virtual"},
            ),
        )
        intelligence = build_bedrock_adapter(
            configuration.intelligence,
            session=active_session,
        )
        controller = BedrockModel(
            boto_session=active_session,
            boto_client_config=Config(
                connect_timeout=10,
                read_timeout=120,
                retries={"mode": "standard", "max_attempts": 0},
            ),
            model_id=configuration.controller_model_id,
            max_tokens=700,
            temperature=0.0,
            streaming=False,
            use_native_token_count=False,
        )
    except Exception:
        raise Phase6AgentCoreDependencyError(
            "Phase 6 AgentCore dependency is unavailable"
        ) from None
    return compose_phase6_agentcore_runtime(
        configuration,
        dynamodb_client=dynamodb,
        s3_client=s3,
        intelligence=intelligence,
        controller_model=controller,
    )


def _profile(environment: Mapping[str, object]) -> AgentCorePinnedProfileConfiguration:
    profile_id = _required(environment, "MR_LISTER_PRODUCT_PROFILE_ID")
    if _PROFILE_ID.fullmatch(profile_id) is None:
        raise ValueError
    version_text = _required(environment, "MR_LISTER_PRODUCT_PROFILE_VERSION")
    if re.fullmatch(r"[1-9][0-9]{0,5}", version_text) is None:
        raise ValueError
    expected_fingerprint = _fingerprint(environment, "MR_LISTER_PRODUCT_PROFILE_FINGERPRINT")
    path = _exact_path(
        environment,
        "MR_LISTER_PRODUCT_PROFILE_PATH",
        expected_name=f"{profile_id}.json",
    )
    exact = FilesystemReviewProductAuthority(profile_directory=path.parent).get_exact(
        profile_id=profile_id,
        profile_version=int(version_text),
    )
    if exact.fingerprint != expected_fingerprint:
        raise ValueError
    return AgentCorePinnedProfileConfiguration(profile_path=path, exact=exact)


def _exact_path(
    environment: Mapping[str, object],
    name: str,
    *,
    expected_name: str,
) -> Path:
    text = _required(environment, name)
    if not text.isascii() or "\\" in text:
        raise ValueError
    path = Path(text)
    if (
        not path.is_absolute()
        or path.as_posix() != text
        or path.name != expected_name
        or path.resolve(strict=True) != path
        or not path.is_file()
    ):
        raise ValueError
    return path


def _fingerprint(environment: Mapping[str, object], name: str) -> str:
    value = _required(environment, name)
    if _FINGERPRINT.fullmatch(value) is None or value == "0" * 64:
        raise ValueError
    return value


def _required(environment: Mapping[str, object], name: str) -> str:
    value = environment.get(name)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or value != value.strip()
        or "\x00" in value
    ):
        raise ValueError
    return value


__all__ = [
    "AgentCorePinnedProductAuthority",
    "AgentCorePinnedProfileConfiguration",
    "AgentCoreDynamoClient",
    "AgentCoreS3Client",
    "ExactPinnedSourceS3",
    "PHASE6_GEMMA_MODEL_ID",
    "PHASE6_STRANDS_CONTROLLER_MODEL_ID",
    "Phase6AgentCoreConfiguration",
    "Phase6AgentCoreConfigurationError",
    "Phase6AgentCoreDependencyError",
    "build_phase6_agentcore_runtime",
    "compose_phase6_agentcore_runtime",
    "load_phase6_agentcore_configuration",
]
