"""Strict private account records and public, credential-free setup projection."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

PREFIX = "MR_LISTER_ACCOUNT_"
ACCOUNT_GROUP = "account"
ACCOUNT_SCOPE = "mr-lister-api/seller"
ACCOUNT_ENTITY = "MERCHANT_ACCOUNT"
RECORD_CONTRACT = "merchant-account-v1"
PUBLIC_CONTRACT = "account-setup-v1"
_POOL = re.compile(r"^([a-z]{2}(?:-[a-z]+)+-[0-9])_[A-Za-z0-9]{1,55}$")
_CLIENT = re.compile(r"^[a-z0-9]{1,128}$")
_OWNER = re.compile(r"^[a-f0-9]{64}$")
_SUBJECT = re.compile(r"^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$")
_TABLE = re.compile(r"^[A-Za-z0-9_.-]{3,255}$")


class AccountUnavailable(Exception):
    def __init__(self) -> None:
        super().__init__("Account setup is temporarily unavailable.")


class AccountConflict(AccountUnavailable):
    """An immutable record already exists with different authority."""


def valid_owner(value: object) -> bool:
    return isinstance(value, str) and _OWNER.fullmatch(value) is not None and value != "0" * 64


def owner_id_for(issuer: str, subject: str) -> str:
    return sha256(issuer.encode() + b"\0" + subject.encode()).hexdigest()


def exact_object(value: object, fields: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Invalid account data")
    return value


def strict_json(value: object, *, limit: int = 32768) -> object:
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
        raise ValueError("Invalid account data")

    def pairs(entries: list[tuple[str, object]]) -> dict:
        result = {}
        for key, item in entries:
            if key in result:
                raise ValueError("Invalid account data")
            result[key] = item
        return result

    def constant(_value: str) -> None:
        raise ValueError("Invalid account data")

    try:
        return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError, RecursionError):
        pass
    raise ValueError("Invalid account data")


@dataclass(frozen=True, slots=True)
class AccountConfig:
    user_pool_id: str
    client_id: str
    table_name: str
    reserved_owner_ids: frozenset[str]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.user_pool_id, str)
            or _POOL.fullmatch(self.user_pool_id) is None
            or not isinstance(self.client_id, str)
            or _CLIENT.fullmatch(self.client_id) is None
            or not isinstance(self.table_name, str)
            or _TABLE.fullmatch(self.table_name) is None
            or not isinstance(self.reserved_owner_ids, frozenset)
            or not 1 <= len(self.reserved_owner_ids) <= 256
            or any(not valid_owner(owner) for owner in self.reserved_owner_ids)
        ):
            raise ValueError("Invalid account configuration")

    @property
    def region(self) -> str:
        return self.user_pool_id.split("_", 1)[0]

    @property
    def issuer(self) -> str:
        return f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> AccountConfig:
        try:
            reserved = strict_json(environment[PREFIX + "RESERVED_OWNER_IDS"], limit=17408)
            if (
                not isinstance(reserved, list)
                or not 1 <= len(reserved) <= 256
                or any(not valid_owner(owner) for owner in reserved)
                or len(set(reserved)) != len(reserved)
            ):
                raise ValueError
            return cls(
                user_pool_id=environment[PREFIX + "USER_POOL_ID"],
                client_id=environment[PREFIX + "CLIENT_ID"],
                table_name=environment[PREFIX + "TABLE_NAME"],
                reserved_owner_ids=frozenset(reserved),
            )
        except (KeyError, TypeError, ValueError):
            pass
        raise ValueError("Invalid account configuration")


@dataclass(frozen=True, slots=True)
class AccountIdentity:
    owner_id: str
    issuer: str
    subject: str
    client_id: str
    username_digest: str

    def __post_init__(self) -> None:
        if (
            not valid_owner(self.owner_id)
            or not isinstance(self.subject, str)
            or _SUBJECT.fullmatch(self.subject) is None
            or not isinstance(self.issuer, str)
            or not isinstance(self.client_id, str)
            or _CLIENT.fullmatch(self.client_id) is None
            or not valid_owner(self.username_digest)
            or self.owner_id != owner_id_for(self.issuer, self.subject)
        ):
            raise ValueError("Invalid account identity")

    def require_configuration(self, config: AccountConfig) -> None:
        if (
            self.issuer != config.issuer
            or self.client_id != config.client_id
            or self.owner_id in config.reserved_owner_ids
        ):
            raise ValueError("Invalid account identity")

    def payload(self) -> dict:
        return {
            "owner_id": self.owner_id,
            "issuer": self.issuer,
            "subject": self.subject,
            "client_id": self.client_id,
            "username_digest": self.username_digest,
        }


@dataclass(frozen=True, slots=True)
class MerchantAccount:
    identity: AccountIdentity
    created_at: int
    record_version: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.identity, AccountIdentity)
            or type(self.created_at) is not int
            or self.created_at <= 0
            or type(self.record_version) is not int
            or self.record_version != 1
        ):
            raise ValueError("Invalid account record")

    @property
    def owner_id(self) -> str:
        return self.identity.owner_id

    def payload(self) -> dict:
        return {
            "contract_version": RECORD_CONTRACT,
            **self.identity.payload(),
            "created_at": self.created_at,
            "record_version": self.record_version,
            "entitlements": ["manage_connections"],
            "setup_state": "connection_unavailable",
        }

    @classmethod
    def from_payload(cls, value: object) -> MerchantAccount:
        data = exact_object(
            value,
            {
                "contract_version",
                "owner_id",
                "issuer",
                "subject",
                "client_id",
                "username_digest",
                "created_at",
                "record_version",
                "entitlements",
                "setup_state",
            },
        )
        if (
            data["contract_version"] != RECORD_CONTRACT
            or data["entitlements"] != ["manage_connections"]
            or data["setup_state"] != "connection_unavailable"
        ):
            raise ValueError("Invalid account record")
        return cls(
            identity=AccountIdentity(
                **{
                    key: data[key]
                    for key in (
                        "owner_id",
                        "issuer",
                        "subject",
                        "client_id",
                        "username_digest",
                    )
                }
            ),
            created_at=data["created_at"],
            record_version=data["record_version"],
        )

    def public_projection(self) -> dict:
        return {
            "contract_version": PUBLIC_CONTRACT,
            "record_version": self.record_version,
            "state": "connection_unavailable",
            "connection_method": "unavailable",
            "store": None,
        }
