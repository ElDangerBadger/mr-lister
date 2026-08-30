"""Dependency-injected composition for one exact Phase 7 provider boundary.

This module joins the already-sealed publication coordinator interfaces without creating an AWS
client, resolving a secret during construction, registering a handler, or contacting Printify.
Allowed provider audits and classified provider evidence are persisted through the same execution
store. Rejected, identity-free audit records are sent only to an explicit sanitized writer.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from mr_lister.publication.evidence_provenance import (
    PublicationProviderEvidenceCommit,
    PublicationProviderEvidenceStage,
)
from mr_lister.publication.execution_models import (
    PublicationCallClaim,
    PublicationExecutionAuthority,
    PublicationProviderAuditBinding,
    PublicationProviderAuditDecision,
    PublicationProviderAuditRecord,
)
from mr_lister.publication.execution_store import (
    PublicationProviderAuditCommit,
    build_provider_audit_commit,
)
from mr_lister.publication.provider_boundary import (
    PublicationHttpTransport,
    PublicationProviderInputError,
    StagedPrintifyPublicationBoundary,
    StagedPublicationProviderBoundary,
)
from mr_lister.publication.provider_credentials import (
    BoundPublicationProviderCredential,
    PublicationProviderCredentialAuthority,
)

_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")
_SAFE_USER_AGENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ /-]{0,127}$")
_CONFIGURATION_ERROR = "Publication provider runtime configuration is invalid"
_AUTHORITY_ERROR = "Publication provider runtime authority is invalid"


class PublicationProviderRuntimeError(RuntimeError):
    """Value-free failure assembling one exact provider runtime boundary."""


class PublicationProviderRuntimeStore(Protocol):
    """Small store surface consumed by the provider runtime join."""

    def load_execution_authority(
        self,
        owner_id: str,
        aggregate_id: str,
    ) -> PublicationExecutionAuthority: ...

    def commit_provider_audit(
        self,
        commit: PublicationProviderAuditCommit,
    ) -> PublicationProviderAuditBinding: ...

    def stage_evidence(
        self,
        commit: PublicationProviderEvidenceCommit,
    ) -> PublicationProviderEvidenceStage: ...


RejectedPublicationAuditWriter = Callable[[PublicationProviderAuditRecord], None]


class _StoreBackedPublicationAuditSink:
    """Adapt the execution store's exact CAS to the boundary audit protocol."""

    __slots__ = (
        "_aggregate_id",
        "_owner_id",
        "_rejected_writer",
        "_release_manifest_fingerprint",
        "_store",
    )

    def __init__(
        self,
        *,
        store: PublicationProviderRuntimeStore,
        owner_id: str,
        aggregate_id: str,
        release_manifest_fingerprint: str,
        rejected_writer: RejectedPublicationAuditWriter,
    ) -> None:
        self._store = store
        self._owner_id = owner_id
        self._aggregate_id = aggregate_id
        self._release_manifest_fingerprint = release_manifest_fingerprint
        self._rejected_writer = rejected_writer

    def write_allowed(
        self,
        *,
        record: PublicationProviderAuditRecord,
        call_claim: PublicationCallClaim,
    ) -> PublicationProviderAuditBinding:
        authority = _exact_authority(
            self._store.load_execution_authority(
                self._owner_id,
                self._aggregate_id,
            ),
            release_manifest_fingerprint=self._release_manifest_fingerprint,
            owner_id=self._owner_id,
            aggregate_id=self._aggregate_id,
        )
        commit = build_provider_audit_commit(authority, call_claim, record)
        return self._store.commit_provider_audit(commit)

    def write_rejected(self, record: PublicationProviderAuditRecord) -> None:
        try:
            exact = PublicationProviderAuditRecord.model_validate(record.model_dump(mode="python"))
            if exact != record or exact.decision is not PublicationProviderAuditDecision.REJECTED:
                raise ValueError
            self._rejected_writer(exact)
        except Exception:
            raise PublicationProviderRuntimeError(
                "Publication provider rejected-audit writer is unavailable"
            ) from None


class PublicationProviderRuntimeFactory:
    """Compose one staged Printify boundary from current durable authority.

    Construction validates only injected capability shapes and immutable runtime settings. It does
    not call the store, credential authority, rejected-audit writer, clock, or provider transport.
    Credential resolution remains a separate pre-claim step, as required by the coordinator.
    """

    __slots__ = (
        "_clock",
        "_credentials",
        "_rejected_audit_writer",
        "_release_manifest_fingerprint",
        "_store",
        "_timeout_seconds",
        "_transport",
        "_user_agent",
    )

    def __init__(
        self,
        *,
        store: PublicationProviderRuntimeStore,
        credentials: PublicationProviderCredentialAuthority,
        transport: PublicationHttpTransport,
        release_manifest_fingerprint: str,
        rejected_audit_writer: RejectedPublicationAuditWriter,
        clock: Callable[[], datetime],
        timeout_seconds: float,
        user_agent: str,
    ) -> None:
        try:
            if any(
                not callable(getattr(store, method, None))
                for method in (
                    "load_execution_authority",
                    "commit_provider_audit",
                    "stage_evidence",
                )
            ):
                raise TypeError
            if not callable(getattr(credentials, "resolve_exact", None)):
                raise TypeError
            if not callable(getattr(transport, "request", None)):
                raise TypeError
            if not callable(rejected_audit_writer) or not callable(clock):
                raise TypeError
            if (
                not isinstance(release_manifest_fingerprint, str)
                or _FINGERPRINT.fullmatch(release_manifest_fingerprint) is None
                or release_manifest_fingerprint == "0" * 64
            ):
                raise ValueError
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(float(timeout_seconds))
                or timeout_seconds <= 0
                or timeout_seconds > 60
            ):
                raise ValueError
            if not isinstance(user_agent, str) or _SAFE_USER_AGENT.fullmatch(user_agent) is None:
                raise ValueError
        except Exception:
            raise PublicationProviderRuntimeError(_CONFIGURATION_ERROR) from None
        self._store = store
        self._credentials = credentials
        self._transport = transport
        self._release_manifest_fingerprint = release_manifest_fingerprint
        self._rejected_audit_writer = rejected_audit_writer
        self._clock = clock
        self._timeout_seconds = float(timeout_seconds)
        self._user_agent = user_agent

    def prepare_credential(
        self,
        *,
        execution_authority: PublicationExecutionAuthority,
    ) -> BoundPublicationProviderCredential:
        """Resolve one already-bound opaque credential before a durable call claim."""

        try:
            exact = _exact_authority(
                execution_authority,
                release_manifest_fingerprint=self._release_manifest_fingerprint,
            )
            provider_authority = exact.provider_authority
            if provider_authority is None:
                raise ValueError
            credential = self._credentials.resolve_exact(authority=provider_authority)
            if type(credential) is not BoundPublicationProviderCredential:
                raise TypeError
            return credential
        except Exception:
            raise PublicationProviderRuntimeError(_AUTHORITY_ERROR) from None

    def __call__(
        self,
        *,
        execution_authority: PublicationExecutionAuthority,
        credential: BoundPublicationProviderCredential,
    ) -> StagedPublicationProviderBoundary:
        """Bind exact post-claim authority without reading storage or calling the provider."""

        exact = _exact_authority(
            execution_authority,
            release_manifest_fingerprint=self._release_manifest_fingerprint,
        )
        if type(credential) is not BoundPublicationProviderCredential:
            raise PublicationProviderRuntimeError(_AUTHORITY_ERROR)
        try:
            return StagedPrintifyPublicationBoundary(
                execution_authority=exact,
                credential=credential,
                transport=self._transport,
                audit_sink=_StoreBackedPublicationAuditSink(
                    store=self._store,
                    owner_id=exact.snapshot.owner_id,
                    aggregate_id=exact.aggregate.aggregate_id,
                    release_manifest_fingerprint=self._release_manifest_fingerprint,
                    rejected_writer=self._rejected_audit_writer,
                ),
                evidence_store=self._store,
                authority_reader=self._store.load_execution_authority,
                clock=self._clock,
                timeout_seconds=self._timeout_seconds,
                user_agent=self._user_agent,
            )
        except PublicationProviderInputError:
            raise
        except Exception:
            raise PublicationProviderRuntimeError(_AUTHORITY_ERROR) from None


def _exact_authority(
    authority: PublicationExecutionAuthority,
    *,
    release_manifest_fingerprint: str,
    owner_id: str | None = None,
    aggregate_id: str | None = None,
) -> PublicationExecutionAuthority:
    try:
        exact = PublicationExecutionAuthority.model_validate(authority.model_dump(mode="python"))
        if (
            exact != authority
            or exact.snapshot.release_manifest_fingerprint != release_manifest_fingerprint
            or (owner_id is not None and exact.snapshot.owner_id != owner_id)
            or (aggregate_id is not None and exact.aggregate.aggregate_id != aggregate_id)
        ):
            raise ValueError
        return exact
    except Exception:
        raise PublicationProviderRuntimeError(_AUTHORITY_ERROR) from None


__all__ = [
    "PublicationProviderRuntimeError",
    "PublicationProviderRuntimeFactory",
    "PublicationProviderRuntimeStore",
    "RejectedPublicationAuditWriter",
]
