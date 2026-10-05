"""Strict credential-free connection metadata and safe public projections."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, Strict, model_validator

from mr_lister.accounts.models import AccountConfig

from .binding import StoreBindingAuthority

PREFIX = "MR_LISTER_CONNECTION_"
CANDIDATE_LIFETIME = 900
VALIDATION_BUDGET_WINDOW = 900
VALIDATION_BUDGET_LIMIT = 5
MERCHANT_AUTHORIZATION_VERSION = "2026-10-05"
Hex = Annotated[str, Strict(), Field(pattern=r"^[a-f0-9]{64}$")]
CandidateId = Annotated[str, Strict(), Field(pattern=r"^candidate_[a-f0-9]{32}$")]
Positive = Annotated[int, Strict(), Field(gt=0, le=9007199254740991)]
Text = Annotated[str, Strict(), Field(min_length=1, max_length=256)]


class ConnectionError(Exception):
    status = 503
    code = "CONNECTION_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("Store connection is temporarily unavailable.")


class ConnectionConflict(ConnectionError):
    status = 409
    code = "CONNECTION_CONFLICT"


class InvalidConnectionRequest(ConnectionError):
    status = 400
    code = "INVALID_CONNECTION_REQUEST"


class ExpiredCandidate(ConnectionError):
    status = 409
    code = "CANDIDATE_EXPIRED"


class InvalidCredential(ConnectionError):
    status = 422
    code = "CREDENTIAL_NOT_ACCEPTED"


class ConnectionRateLimited(ConnectionError):
    status = 429
    code = "CONNECTION_RATE_LIMITED"

    def __init__(self) -> None:
        super().__init__()
        self.args = ("Too many connection attempts. Please wait fifteen minutes and try again.",)


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    accounts: AccountConfig
    table_name: str
    secret_prefix: str
    workflow_enabled: bool = False

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", self.table_name)
            or not re.fullmatch(r"mr-lister/[a-z0-9_-]{1,40}/connections/", self.secret_prefix)
            or type(self.workflow_enabled) is not bool
        ):
            raise ValueError("Invalid connection configuration")

    @classmethod
    def from_environment(cls, env: dict) -> ConnectionConfig:
        try:
            return cls(
                AccountConfig.from_environment(env),
                env[PREFIX + "TABLE_NAME"],
                env[PREFIX + "SECRET_PREFIX"],
                env.get(PREFIX + "WORKFLOW_ENABLED") == "true",
            )
        except Exception:
            pass
        raise ConnectionError


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class Shop(StrictModel):
    id: Positive
    name: Text
    sales_channel: Annotated[str, Strict(), Field(min_length=1, max_length=64)]

    def public(self) -> dict:
        eligible = self.sales_channel == "etsy"
        return {
            "id": str(self.id),
            "name": self.name,
            "sales_channel": self.sales_channel,
            "eligible": eligible,
            "disabled_reason": None if eligible else "unsupported_channel",
        }


class SecretReference(StrictModel):
    name: Annotated[str, Strict(), Field(min_length=1, max_length=512)]
    version: Hex


class ValidationBudget(StrictModel):
    """Only recent attempt times: no token, credential digest, or browser key."""

    contract_version: Literal["connection-validation-budget-v1"] = "connection-validation-budget-v1"
    owner_id: Hex
    attempted_at: Annotated[
        tuple[Positive, ...], Field(min_length=1, max_length=VALIDATION_BUDGET_LIMIT)
    ]

    @model_validator(mode="after")
    def chronological(self) -> ValidationBudget:
        if tuple(sorted(self.attempted_at)) != self.attempted_at:
            raise ValueError("Invalid validation budget")
        return self


class Candidate(StrictModel):
    contract_version: Literal["printify-candidate-v1"] = "printify-candidate-v1"
    owner_id: Hex
    candidate_id: CandidateId
    connection_id: Annotated[str, Strict(), Field(pattern=r"^conn_[a-f0-9]{32}$")]
    shop_binding_id: Annotated[str, Strict(), Field(pattern=r"^binding_[a-f0-9]{32}$")]
    request_digest: Hex
    created_at: Positive
    merchant_authorization_version: Literal["2026-10-05"] = "2026-10-05"
    expires_at: Positive
    state: Literal["pending", "validated", "consumed", "deleting", "deleted"] = "pending"
    record_version: Positive = 1
    secret: SecretReference | None = None
    stores: tuple[Shop, ...] = ()

    @model_validator(mode="after")
    def coherent(self) -> Candidate:
        if self.expires_at != self.created_at + CANDIDATE_LIFETIME:
            raise ValueError("Invalid candidate lifetime")
        if len(self.stores) > 100 or len({s.id for s in self.stores}) != len(self.stores):
            raise ValueError("Invalid candidate shops")
        if self.state == "pending" and (self.secret is not None or self.stores):
            raise ValueError("Invalid pending candidate")
        if self.state in ("validated", "consumed") and self.secret is None:
            raise ValueError("Missing candidate credential")
        return self

    def public(self) -> dict:
        if self.state != "validated":
            raise ConnectionConflict
        return {
            "candidate_id": self.candidate_id,
            "record_version": self.record_version,
            "expires_at": datetime.fromtimestamp(self.expires_at, UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "stores": [s.public() for s in self.stores],
        }


class Setup(StrictModel):
    contract_version: Literal["store-setup-private-v1"] = "store-setup-private-v1"
    owner_id: Hex
    record_version: Positive = 1
    candidate_id: CandidateId | None = None
    binding: StoreBindingAuthority | None = None
    seller_granted: bool = False

    @model_validator(mode="after")
    def coherent(self) -> Setup:
        if self.binding is not None:
            self.binding.checked_for_owner(self.owner_id)
            if self.candidate_id is not None:
                raise ValueError("Selected setup cannot retain an open candidate")
        elif self.seller_granted:
            raise ValueError("Unbound seller grant")
        return self

    def public(self, *, workflow_enabled: bool, candidate_valid: bool, store: Shop | None) -> dict:
        state = "connection_required"
        public_store = None
        if self.binding is not None:
            state = "connection_unavailable"
            if workflow_enabled and store is not None:
                state = "ready" if self.seller_granted else "reconnect_required"
                public_store = {
                    "connection_id": self.binding.connection_id,
                    "shop_binding_id": self.binding.shop_binding_id,
                    "shop_id": self.binding.shop_id,
                    "name": store.name,
                    "sales_channel": "etsy",
                }
        elif candidate_valid:
            state = "choose_store"
        return {
            "contract_version": "account-setup-v1",
            "record_version": self.record_version,
            "state": state,
            "connection_method": "personal_token",
            "store": public_store,
        }


class Connection(StrictModel):
    contract_version: Literal["printify-connection-v1"] = "printify-connection-v1"
    binding: StoreBindingAuthority
    candidate_id: CandidateId
    secret: SecretReference
    generation: Positive = 1
    merchant_authorization_version: Literal["2026-10-05"] = "2026-10-05"
    store: Shop
    state: Literal["pending_activation", "active"] = "pending_activation"

    @model_validator(mode="after")
    def coherent(self) -> Connection:
        if (
            self.generation != 1
            or self.store.id != self.binding.shop_id
            or self.store.sales_channel != "etsy"
        ):
            raise ValueError("Invalid selected store")
        return self


class SelectionReceipt(StrictModel):
    owner_id: Hex
    request_digest: Hex
    binding: StoreBindingAuthority

    @model_validator(mode="after")
    def coherent(self) -> SelectionReceipt:
        self.binding.checked_for_owner(self.owner_id)
        return self


def canonical(model: StrictModel | StoreBindingAuthority) -> str:
    return json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def digest(*parts: str) -> str:
    return sha256("\0".join(parts).encode()).hexdigest()


def operation_id(owner: str, key: str, kind: str) -> str:
    if not isinstance(key, str) or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key) is None:
        raise InvalidConnectionRequest
    return digest(kind, owner, key)
