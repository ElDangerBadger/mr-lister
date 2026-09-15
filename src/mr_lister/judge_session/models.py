"""Closed, non-secret configuration and durable record contracts for judge sessions."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import urlsplit

PREFIX = "MR_LISTER_JUDGE_SESSION_"
COOKIE_NAME = "__Secure-mr-lister-judge"
COOKIE_PATH = "/v1/judge-session"
SCOPE = "mr-lister-api/seller"
OPAQUE = re.compile(r"[A-Za-z0-9_-]{43,256}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
ISSUER = re.compile(
    r"https://cognito-idp\.([a-z0-9-]+)\.amazonaws\.com/([a-z0-9-]+_[A-Za-z0-9]+)\Z"
)


class Denied(Exception):
    """A credential is absent, invalid, expired, revoked, or exhausted."""


class Unavailable(Exception):
    """A dependency or deployment cannot safely establish a session."""


def digest(value: str) -> str:
    return sha256(value.encode("ascii")).hexdigest()


def strict_json(value: str | bytes, *, limit: int = 65536) -> object:
    if len(value) > limit:
        raise ValueError("JSON exceeds limit")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = item
        return result

    def invalid_constant(_: str) -> object:
        raise ValueError("Invalid JSON constant")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid_constant)


def integer(value: object, low: int = 0, high: int = 253402300799) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError("Invalid integer")
    return value


def exact_object(value: object, fields: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Invalid object fields")
    return value


@dataclass(frozen=True)
class Config:
    table: str
    seed_secret_arn: str
    issuer: str
    client_id: str
    subject: str
    owner_id: str
    cognito_origin: str
    application_origin: str
    campaign_id: str
    campaign_expires_at: int
    duration_seconds: int = 14400

    def __post_init__(self) -> None:
        match = ISSUER.fullmatch(self.issuer)
        if match is None or not match[2].startswith(match[1] + "_"):
            raise ValueError("Invalid issuer")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", self.table):
            raise ValueError("Invalid table")
        if not re.fullmatch(
            rf"arn:aws:secretsmanager:{re.escape(match[1])}:\d{{12}}:secret:"
            r"[A-Za-z0-9/_+=.@-]{1,512}-[A-Za-z0-9]{6}",
            self.seed_secret_arn,
        ):
            raise ValueError("Invalid seed secret ARN")
        if not re.fullmatch(r"[A-Za-z0-9]{1,128}", self.client_id):
            raise ValueError("Invalid client")
        if SAFE_ID.fullmatch(self.subject) is None or SAFE_ID.fullmatch(self.campaign_id) is None:
            raise ValueError("Invalid identity")
        expected_owner = sha256((self.issuer + "\0" + self.subject).encode()).hexdigest()
        if self.owner_id != expected_owner:
            raise ValueError("Owner binding differs")
        if not re.fullmatch(
            rf"https://[a-z0-9](?:[a-z0-9-]{{0,61}}[a-z0-9])?"
            rf"\.auth\.{re.escape(match[1])}\.amazoncognito\.com",
            self.cognito_origin,
        ):
            raise ValueError("Invalid Cognito origin")
        origin = urlsplit(self.application_origin)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.port
            or self.application_origin != f"https://{origin.hostname}"
        ):
            raise ValueError("Invalid application origin")
        integer(self.campaign_expires_at, 1)
        integer(self.duration_seconds, 60, 14400)

    @property
    def judge_group(self) -> str:
        return self.issuer.rsplit("/", 1)[1] + "_MrListerJudge"

    @property
    def token_endpoint(self) -> str:
        return self.cognito_origin + "/oauth2/token"

    @property
    def jwks_uri(self) -> str:
        return self.issuer + "/.well-known/jwks.json"

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> Config:
        if env.get(PREFIX + "ENABLED") != "true":
            raise Unavailable
        fields = {
            name: env[PREFIX + name.upper()]
            for name in (
                "table",
                "seed_secret_arn",
                "issuer",
                "client_id",
                "subject",
                "owner_id",
                "cognito_origin",
                "application_origin",
                "campaign_id",
            )
        }
        expiry = env[PREFIX + "CAMPAIGN_EXPIRES_AT_EPOCH"]
        duration = env.get(PREFIX + "DURATION_SECONDS", "14400")
        if (
            not expiry.isascii()
            or not expiry.isdigit()
            or not duration.isascii()
            or not duration.isdigit()
        ):
            raise ValueError("Invalid campaign timing")
        return cls(**fields, campaign_expires_at=int(expiry), duration_seconds=int(duration))


@dataclass(frozen=True)
class Invitation:
    invitation_digest: str
    expires_at: int
    redemption_limit: int
    redemptions: int

    @classmethod
    def read(
        cls, value: object, config: Config, expected_digest: str, now: int, *, redeem: bool = True
    ) -> Invitation:
        try:
            row = exact_object(
                value,
                {
                    "PK",
                    "contract_version",
                    "campaign_id",
                    "owner_id",
                    "expires_at",
                    "redemption_limit",
                    "redemptions",
                    "enabled",
                },
            )
            limit = integer(row["redemption_limit"], 1, 10000)
            count = integer(row["redemptions"], 0, limit)
            expiry = integer(row["expires_at"], 1, config.campaign_expires_at)
            if (
                row["PK"] != "INVITE#" + expected_digest
                or row["contract_version"] != "judge-session-invitation-v1"
                or row["campaign_id"] != config.campaign_id
                or row["owner_id"] != config.owner_id
                or row["enabled"] is not True
                or expiry <= now
                or (redeem and count >= limit)
            ):
                raise ValueError
            return cls(expected_digest, expiry, limit, count)
        except (ValueError, KeyError, TypeError):
            pass
        raise Denied


@dataclass(frozen=True)
class Session:
    session_digest: str
    invitation_digest: str
    created_at: int
    expires_at: int

    def row(self, config: Config) -> dict:
        return {
            "PK": "SESSION#" + self.session_digest,
            "contract_version": "judge-session-v1",
            "campaign_id": config.campaign_id,
            "owner_id": config.owner_id,
            "invitation_digest": self.invitation_digest,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "revoked": False,
        }

    @classmethod
    def read(cls, value: object, config: Config, expected_digest: str, now: int) -> Session:
        try:
            row = exact_object(
                value,
                {
                    "PK",
                    "contract_version",
                    "campaign_id",
                    "owner_id",
                    "invitation_digest",
                    "created_at",
                    "expires_at",
                    "revoked",
                },
            )
            created = integer(row["created_at"], 1, now)
            expiry = integer(row["expires_at"], created + 1, config.campaign_expires_at)
            if (
                row["PK"] != "SESSION#" + expected_digest
                or row["contract_version"] != "judge-session-v1"
                or row["campaign_id"] != config.campaign_id
                or row["owner_id"] != config.owner_id
                or row["revoked"] is not False
                or expiry <= now
                or expiry > created + config.duration_seconds
                or not isinstance(row["invitation_digest"], str)
                or DIGEST.fullmatch(row["invitation_digest"]) is None
            ):
                raise ValueError
            return cls(expected_digest, row["invitation_digest"], created, expiry)
        except (ValueError, KeyError, TypeError):
            pass
        raise Denied
