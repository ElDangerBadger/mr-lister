"""Trusted native confirmation/sign-in repairs account authority, never seller access."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256

from .models import (
    ACCOUNT_GROUP,
    AccountConfig,
    AccountIdentity,
    AccountUnavailable,
    MerchantAccount,
    owner_id_for,
)
from .store import AccountWriter

CONFIRM_SIGNUP = "PostConfirmation_ConfirmSignUp"
CONFIRM_PASSWORD = "PostConfirmation_ConfirmForgotPassword"
PRE_AUTHENTICATION = "PreAuthentication_Authentication"
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@dataclass(frozen=True, slots=True)
class NativeSignup:
    identity: AccountIdentity
    username: str = field(repr=False)
    # Present only for pre-authentication; this proof must be checked with Cognito before writes.
    email_digest: str | None = field(default=None, repr=False)


def native_signup(event: object, config: AccountConfig) -> NativeSignup | None:
    """Only trusted Cognito fields supply authority; browser metadata is ignored."""
    if not isinstance(event, Mapping):
        raise ValueError("Invalid account confirmation")
    if event.get("triggerSource") == CONFIRM_PASSWORD:
        return None
    if (
        event.get("version") != "1"
        or event.get("triggerSource") not in (CONFIRM_SIGNUP, PRE_AUTHENTICATION)
        or event.get("region") != config.region
        or event.get("userPoolId") != config.user_pool_id
        or not isinstance(event.get("callerContext"), Mapping)
        or event["callerContext"].get("clientId") != config.client_id
        or not isinstance(event.get("request"), Mapping)
        or not isinstance(event.get("response"), dict)
        or event["response"] != {}
    ):
        raise ValueError("Invalid account confirmation")
    pre_authentication = event["triggerSource"] == PRE_AUTHENTICATION
    if pre_authentication and event["request"].get("userNotFound", False) is not False:
        # Cognito can send an unknown-user event when user-existence suppression is enabled.
        # Neither unknown users nor malformed boolean values can acquire account authority.
        raise ValueError("Invalid account confirmation")
    attributes = event["request"].get("userAttributes")
    if not isinstance(attributes, Mapping):
        raise ValueError("Invalid account confirmation")
    username = event.get("userName")
    subject = attributes.get("sub")
    if (
        not isinstance(username, str)
        or not 1 <= len(username) <= 128
        or any(character.isspace() or ord(character) < 32 for character in username)
        or not isinstance(subject, str)
    ):
        raise ValueError("Invalid account confirmation")
    identity = AccountIdentity(
        owner_id=owner_id_for(config.issuer, subject),
        issuer=config.issuer,
        subject=subject,
        client_id=config.client_id,
        username_digest=sha256(username.encode()).hexdigest(),
    )
    # Presence of linked identity metadata excludes both external and linked-native users.
    if "identities" in attributes or attributes.get("cognito:user_status") == "EXTERNAL_PROVIDER":
        return None
    if pre_authentication and identity.owner_id in config.reserved_owner_ids:
        # Existing owner/judge logins retain their prior authentication path. No native
        # email or account requirements are imposed on reserved identities.
        return None
    email = attributes.get("email")
    verified = attributes.get("email_verified")
    if (
        not isinstance(email, str)
        or not 3 <= len(email) <= 254
        or not email.isascii()
        or _EMAIL.fullmatch(email) is None
        or not (verified is True or (type(verified) is str and verified == "true"))
        or attributes.get("cognito:user_status", "CONFIRMED") != "CONFIRMED"
        or username not in (subject, email)
    ):
        raise ValueError("Invalid account confirmation")
    identity.require_configuration(config)
    return NativeSignup(
        identity, username, sha256(email.encode()).hexdigest() if pre_authentication else None
    )


class CognitoAccountGroupWriter:
    """Exact native-user status proof and the fixed account-membership operation."""

    def __init__(self, client: object, config: AccountConfig) -> None:
        self._client = client
        self._config = config

    def verify_native_signup(self, signup: NativeSignup) -> None:
        """Read only the trusted native subject; never use browser metadata or a search API."""
        try:
            signup.identity.require_configuration(self._config)
            if signup.email_digest is None:
                raise ValueError
            result = self._client.admin_get_user(
                UserPoolId=self._config.user_pool_id,
                Username=signup.identity.subject,
            )
            if (
                not isinstance(result, Mapping)
                or result.get("UserStatus") != "CONFIRMED"
                or result.get("Enabled") is not True
                or result.get("Username") not in (signup.identity.subject, signup.username)
                or not isinstance(result.get("UserAttributes"), list)
                or not 1 <= len(result["UserAttributes"]) <= 256
            ):
                raise ValueError
            attributes = {}
            for attribute in result["UserAttributes"]:
                if (
                    not isinstance(attribute, dict)
                    or set(attribute) != {"Name", "Value"}
                    or not isinstance(attribute["Name"], str)
                    or not isinstance(attribute["Value"], str)
                    or attribute["Name"] in attributes
                ):
                    raise ValueError
                attributes[attribute["Name"]] = attribute["Value"]
            if (
                "identities" in attributes
                or attributes.get("sub") != signup.identity.subject
                or attributes.get("email_verified") != "true"
                or "email" not in attributes
                or sha256(attributes["email"].encode()).hexdigest() != signup.email_digest
            ):
                raise ValueError
            return
        except Exception:
            pass
        raise AccountUnavailable

    def assign(self, signup: NativeSignup) -> None:
        try:
            signup.identity.require_configuration(self._config)
            if sha256(signup.username.encode()).hexdigest() != signup.identity.username_digest:
                raise ValueError
            self._client.admin_add_user_to_group(
                UserPoolId=self._config.user_pool_id,
                Username=signup.identity.subject,
                GroupName=ACCOUNT_GROUP,
            )
            return
        except Exception:
            pass
        raise AccountUnavailable


class AccountProvisioner:
    def __init__(
        self,
        config: AccountConfig,
        *,
        store: AccountWriter,
        groups: CognitoAccountGroupWriter,
        clock: Callable[[], int],
    ) -> None:
        self._config = config
        self._store = store
        self._groups = groups
        self._clock = clock

    def provision(self, signup: NativeSignup) -> MerchantAccount:
        try:
            signup.identity.require_configuration(self._config)
            if signup.email_digest is not None:
                self._groups.verify_native_signup(signup)
            requested = MerchantAccount(identity=signup.identity, created_at=self._clock())
            stored = self._store.create_if_absent(requested)
            # A dependency must not redirect a group grant or return an invalid legacy row.
            stored = MerchantAccount.from_payload(stored.payload())
            if stored.identity != signup.identity:
                raise ValueError
            # If this call fails (including an unknown result), retry reuses the immutable row
            # and the idempotent Cognito membership operation. No seller group is ever added.
            self._groups.assign(signup)
            return stored
        except Exception:
            pass
        raise AccountUnavailable
