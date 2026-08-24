"""Exact, non-serializable credential authority for one publication graph.

The durable binding in this module contains no bearer material.  The companion capability keeps
the token private and releases a lower-level credential only after re-parsing one exact provider
authority and matching its owner, shop, aggregate, snapshot, and reconstructed-authority identity.
Nothing here reads a secret, creates a transport, or enables a provider call.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictInt,
    model_validator,
)

from mr_lister.publication.execution_fingerprints import execution_record_fingerprint
from mr_lister.publication.execution_models import PublicationProviderAuthority
from mr_lister.publication.models import Fingerprint, OwnerId, PublicationModel, SafeId

_UNAVAILABLE = "Publication provider credential is unavailable"
_MAX_BEARER_TOKEN_CHARS = 4_096


class PublicationProviderCredentialError(RuntimeError):
    """Value-free failure resolving or binding one publication credential."""


class _CredentialModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class OwnerBoundPrintifyCredential(_CredentialModel):
    """Lower-level redacted credential bound to one exact owner and Printify shop."""

    owner_id: OwnerId
    printify_shop_id: StrictInt = Field(gt=0)
    bearer_token: SecretStr = Field(exclude=True, repr=False)

    def __init__(self, **values: object) -> None:
        try:
            super().__init__(**values)
            return
        except Exception:
            pass
        raise PublicationProviderCredentialError(_UNAVAILABLE) from None

    @model_validator(mode="after")
    def token_is_bounded_without_disclosure(self) -> OwnerBoundPrintifyCredential:
        _require_safe_token(self.bearer_token)
        return self

    def __reduce__(self) -> object:
        raise TypeError("Publication credentials cannot be serialized")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("Publication credentials cannot be serialized")


class PublicationProviderCredentialBinding(PublicationModel):
    """Credential-free, content-bound scope for one reconstructed provider authority."""

    owner_id: OwnerId
    printify_shop_id: StrictInt = Field(gt=0)
    aggregate_id: SafeId
    snapshot_id: SafeId
    snapshot_fingerprint: Fingerprint
    provider_authority_id: SafeId
    provider_authority_fingerprint: Fingerprint
    fingerprint: Fingerprint

    @model_validator(mode="after")
    def binding_is_content_bound(self) -> PublicationProviderCredentialBinding:
        if self.fingerprint != execution_record_fingerprint(
            "provider_credential_binding",
            self,
        ):
            raise ValueError("Publication provider credential binding is invalid")
        return self


class PublicationProviderCredentialAuthority(Protocol):
    """Resolve a fresh secret capability for one exact provider authority."""

    def resolve_exact(
        self,
        *,
        authority: PublicationProviderAuthority,
    ) -> BoundPublicationProviderCredential: ...


class BoundPublicationProviderCredential:
    """Opaque token capability that cannot cross its exact publication binding."""

    __slots__ = ("_binding", "_token")

    def __init__(
        self,
        *,
        binding: PublicationProviderCredentialBinding,
        bearer_token: SecretStr,
    ) -> None:
        try:
            object.__setattr__(
                self,
                "_binding",
                PublicationProviderCredentialBinding.model_validate(
                    binding.model_dump(mode="python")
                ),
            )
            token = _require_safe_token(bearer_token)
            object.__setattr__(self, "_token", SecretStr(token))
            return
        except Exception:
            pass
        raise PublicationProviderCredentialError(_UNAVAILABLE) from None

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("Publication credential capabilities are immutable")

    @property
    def binding(self) -> PublicationProviderCredentialBinding:
        """Return only the credential-free immutable scope."""

        try:
            binding = PublicationProviderCredentialBinding.model_validate(
                self._binding.model_dump(mode="python")
            )
        except Exception:
            pass
        else:
            return binding
        raise PublicationProviderCredentialError(_UNAVAILABLE) from None

    def for_authority(
        self,
        authority: PublicationProviderAuthority,
    ) -> OwnerBoundPrintifyCredential:
        """Release a redacted lower-level credential only for the exact bound graph."""

        try:
            exact = PublicationProviderAuthority.model_validate(authority.model_dump(mode="python"))
            expected = build_publication_provider_credential_binding(exact)
            binding = self.binding
            if binding != expected:
                raise ValueError
            credential = OwnerBoundPrintifyCredential(
                owner_id=binding.owner_id,
                printify_shop_id=binding.printify_shop_id,
                bearer_token=self._token.get_secret_value(),
            )
        except Exception:
            pass
        else:
            return credential
        raise PublicationProviderCredentialError(_UNAVAILABLE) from None

    def __repr__(self) -> str:
        return "BoundPublicationProviderCredential(<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> object:
        raise TypeError("Publication credentials cannot be serialized")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("Publication credentials cannot be serialized")


def build_publication_provider_credential_binding(
    authority: PublicationProviderAuthority,
) -> PublicationProviderCredentialBinding:
    """Derive the sole credential-free binding accepted for an exact authority."""

    try:
        exact = PublicationProviderAuthority.model_validate(authority.model_dump(mode="python"))
        values = {
            "owner_id": exact.owner_id,
            "printify_shop_id": exact.printify_shop_id,
            "aggregate_id": exact.aggregate_id,
            "snapshot_id": exact.snapshot_id,
            "snapshot_fingerprint": exact.snapshot_fingerprint,
            "provider_authority_id": exact.provider_authority_id,
            "provider_authority_fingerprint": exact.fingerprint,
        }
        binding = PublicationProviderCredentialBinding(
            **values,
            fingerprint=execution_record_fingerprint(
                "provider_credential_binding",
                values,
            ),
        )
    except Exception:
        pass
    else:
        return binding
    raise PublicationProviderCredentialError(_UNAVAILABLE) from None


def issue_bound_publication_provider_credential(
    *,
    authority: PublicationProviderAuthority,
    bearer_token: SecretStr,
) -> BoundPublicationProviderCredential:
    """Issue one opaque capability after validating the complete exact authority."""

    try:
        credential = BoundPublicationProviderCredential(
            binding=build_publication_provider_credential_binding(authority),
            bearer_token=bearer_token,
        )
    except Exception:
        pass
    else:
        return credential
    raise PublicationProviderCredentialError(_UNAVAILABLE) from None


def _require_safe_token(value: object) -> str:
    if not isinstance(value, SecretStr):
        raise ValueError("invalid credential")
    token = value.get_secret_value()
    if (
        not token
        or len(token) > _MAX_BEARER_TOKEN_CHARS
        or token != token.strip()
        or not token.isascii()
        or any(character.isspace() or ord(character) < 33 for character in token)
    ):
        raise ValueError("invalid credential")
    return token


__all__ = [
    "BoundPublicationProviderCredential",
    "OwnerBoundPrintifyCredential",
    "PublicationProviderCredentialAuthority",
    "PublicationProviderCredentialBinding",
    "PublicationProviderCredentialError",
    "build_publication_provider_credential_binding",
    "issue_bound_publication_provider_credential",
]
