"""Separate Secrets Manager credential capability; no tokens enter metadata or repr."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, SecretStr, model_validator

from .binding import StoreBindingAuthority
from .models import (
    Candidate,
    CandidateId,
    Connection,
    ConnectionConfig,
    ConnectionError,
    Hex,
    InvalidConnectionRequest,
    SecretReference,
    StrictModel,
    digest,
)


def checked_token(value: object) -> SecretStr:
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    if (
        not isinstance(value, str)
        or not 16 <= len(value) <= 4096
        or not value.isascii()
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
        raise InvalidConnectionRequest
    return SecretStr(value)


class CredentialEnvelope(StrictModel):
    contract_version: Literal["printify-personal-token-v1"] = "printify-personal-token-v1"
    owner_id: Hex
    candidate_id: CandidateId
    connection_id: str
    shop_binding_id: str
    generation: int = 1
    token: SecretStr = Field(exclude=True, repr=False)

    @model_validator(mode="after")
    def coherent(self) -> CredentialEnvelope:
        import re

        if (
            re.fullmatch(r"conn_[a-f0-9]{32}", self.connection_id) is None
            or re.fullmatch(r"binding_[a-f0-9]{32}", self.shop_binding_id) is None
            or type(self.generation) is not int
            or self.generation != 1
        ):
            raise ValueError("Invalid credential envelope")
        checked_token(self.token)
        return self

    def __reduce__(self) -> object:
        raise TypeError("Credentials cannot be serialized")

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("Credentials cannot be serialized")


class SecretsManagerCredentialReader:
    def __init__(self, client: object, config: ConnectionConfig) -> None:
        self._client = client
        self.config = config

    def _name(self, owner: str, candidate_id: str) -> str:
        if owner in self.config.accounts.reserved_owner_ids:
            raise ConnectionError
        return self.config.secret_prefix + owner + "/" + candidate_id

    def load(self, record: Candidate | Connection) -> SecretStr:
        try:
            if isinstance(record, Candidate):
                owner, candidate_id = record.owner_id, record.candidate_id
                connection_id, binding_id = record.connection_id, record.shop_binding_id
            else:
                owner, candidate_id = record.binding.owner_id, record.candidate_id
                connection_id, binding_id = (
                    record.binding.connection_id,
                    record.binding.shop_binding_id,
                )
            ref = record.secret
            if ref is None or ref.name != self._name(owner, candidate_id):
                raise ValueError
            result = self._client.get_secret_value(SecretId=ref.name, VersionId=ref.version)
            if (
                not isinstance(result, dict)
                or result.get("Name") != ref.name
                or result.get("VersionId") != ref.version
                or "SecretBinary" in result
            ):
                raise ValueError
            from mr_lister.accounts.models import strict_json

            raw = result.get("SecretString")
            strict_json(raw, limit=16384)
            envelope = CredentialEnvelope.model_validate_json(raw)
            if (
                envelope.owner_id != owner
                or envelope.candidate_id != candidate_id
                or envelope.connection_id != connection_id
                or envelope.shop_binding_id != binding_id
                or envelope.generation != 1
            ):
                raise ValueError
            return checked_token(envelope.token)
        except Exception:
            pass
        raise ConnectionError


class SecretsManagerCredentialStore(SecretsManagerCredentialReader):
    def save(self, candidate: Candidate, token: SecretStr) -> SecretReference:
        try:
            token = checked_token(token)
            name = self._name(candidate.owner_id, candidate.candidate_id)
            version = digest(
                "credential-version",
                candidate.owner_id,
                candidate.candidate_id,
                candidate.request_digest,
            )
            ref = SecretReference(name=name, version=version)
            envelope = CredentialEnvelope(
                owner_id=candidate.owner_id,
                candidate_id=candidate.candidate_id,
                connection_id=candidate.connection_id,
                shop_binding_id=candidate.shop_binding_id,
                token=token,
            )
            # Serialization is confined to the explicit Secrets Manager write boundary.
            payload = {**envelope.model_dump(), "token": token.get_secret_value()}
            try:
                result = self._client.create_secret(
                    Name=name,
                    ClientRequestToken=version,
                    SecretString=json.dumps(payload, sort_keys=True, separators=(",", ":")),
                )
                if result.get("Name") != name or result.get("VersionId") != version:
                    raise ValueError
            except Exception:
                # A lost create response or exact retry must resolve the named immutable version.
                pass
            probe = Candidate.model_validate(
                {**candidate.model_dump(), "state": "validated", "secret": ref.model_dump()}
            )
            recovered = self.load(probe)
            from hmac import compare_digest

            if not compare_digest(recovered.get_secret_value(), token.get_secret_value()):
                raise ValueError
            return ref
        except Exception:
            pass
        raise ConnectionError


class ExactConnectionResolver:
    def __init__(self, *, directory: object, credentials: SecretsManagerCredentialReader) -> None:
        self._directory = directory
        self._credentials = credentials

    def resolve_exact(self, *, binding: StoreBindingAuthority):
        try:
            binding = StoreBindingAuthority.model_validate(binding.model_dump())
            connection = self._directory.require_current(binding)
            if connection.binding != binding:
                raise ValueError
            token = self._credentials.load(connection)
            # Recheck after secret retrieval; same-transaction epoch condition remains required
            # at the downstream durable provider claim boundary.
            if self._directory.require_current(binding) != connection:
                raise ValueError
            from mr_lister.production.provider_resources import OwnerPrintifyConnection

            return OwnerPrintifyConnection(
                owner_id=binding.owner_id,
                shop_id=binding.shop_id,
                api_token=token.get_secret_value(),
                store_binding=binding,
            )
        except Exception:
            pass
        raise ConnectionError
