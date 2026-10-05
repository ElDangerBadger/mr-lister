"""Bounded token validation and immutable one-store selection with retry recovery."""

from __future__ import annotations

from collections.abc import Callable

from mr_lister.accounts.models import MerchantAccount

from .binding import StoreBindingAuthority
from .credentials import checked_token
from .models import (
    CANDIDATE_LIFETIME,
    Candidate,
    Connection,
    ConnectionConfig,
    ConnectionConflict,
    ConnectionError,
    ExpiredCandidate,
    InvalidConnectionRequest,
    Setup,
    digest,
    operation_id,
)


class SellerGroupWriter:
    def __init__(self, client: object, config: ConnectionConfig) -> None:
        self._client = client
        self._config = config

    def grant(self, account: MerchantAccount) -> None:
        try:
            account = MerchantAccount.from_payload(account.payload())
            account.identity.require_configuration(self._config.accounts)
            if not self._config.workflow_enabled:
                raise ValueError
            # Native email pools accept the immutable Cognito sub as Username.
            self._client.admin_add_user_to_group(
                UserPoolId=self._config.accounts.user_pool_id,
                Username=account.identity.subject,
                GroupName="seller",
            )
            return
        except Exception:
            pass
        raise ConnectionError


class StoreSetupReader:
    def __init__(
        self, *, config: ConnectionConfig, directory: object, clock: Callable[[], int]
    ) -> None:
        self.config = config
        self.directory = directory
        self.clock = clock

    def get(self, account: MerchantAccount) -> dict:
        account = MerchantAccount.from_payload(account.payload())
        account.identity.require_configuration(self.config.accounts)
        setup = self.directory.get_setup(account.owner_id)
        store = None
        if setup.binding is not None:
            connection = self.directory.get_connection(setup.binding)
            if setup.seller_granted != (connection.state == "active"):
                raise ConnectionError
            store = connection.store
        candidate = (
            self.directory.get_candidate(account.owner_id, setup.candidate_id)
            if setup.candidate_id
            else None
        )
        valid = (
            candidate is not None
            and candidate.state == "validated"
            and candidate.expires_at > self.clock()
            and candidate.record_version == setup.record_version
        )
        return setup.public(
            workflow_enabled=self.config.workflow_enabled, candidate_valid=valid, store=store
        )


class ConnectionService(StoreSetupReader):
    def __init__(
        self,
        *,
        config: ConnectionConfig,
        store: object,
        credentials: object,
        provider: object,
        groups: object,
        clock: Callable[[], int],
    ) -> None:
        super().__init__(config=config, directory=store, clock=clock)
        self.store = store
        self.credentials = credentials
        self.provider = provider
        self.groups = groups

    def _account(self, account: MerchantAccount) -> MerchantAccount:
        account = MerchantAccount.from_payload(account.payload())
        account.identity.require_configuration(self.config.accounts)
        return account

    def validate(self, account: MerchantAccount, *, token: object, idempotency_key: str) -> dict:
        account = self._account(account)
        if self.store.get_setup(account.owner_id).binding is not None:
            # This milestone has no credential rotation or store-retargeting flow.
            raise ConnectionConflict
        token = checked_token(token)
        operation = operation_id(account.owner_id, idempotency_key, "validate")
        request_digest = digest("validate-request", token.get_secret_value())
        candidate_id = "candidate_" + operation[:32]
        existing = self.store.get_candidate(account.owner_id, candidate_id)
        if existing is not None:
            if existing.request_digest != request_digest:
                raise ConnectionConflict
            candidate = existing
        else:
            now = self.clock()
            candidate = self.store.reserve_candidate(
                Candidate(
                    owner_id=account.owner_id,
                    candidate_id=candidate_id,
                    connection_id="conn_" + digest("connection", operation)[:32],
                    shop_binding_id="binding_" + digest("binding", operation)[:32],
                    request_digest=request_digest,
                    created_at=now,
                    expires_at=now + CANDIDATE_LIFETIME,
                )
            )
        if candidate.expires_at <= self.clock():
            raise ExpiredCandidate
        if candidate.state != "pending":
            return candidate.public()
        if self.store.get_setup(account.owner_id).binding is not None:
            raise ConnectionConflict
        if candidate.expires_at <= self.clock():
            raise ExpiredCandidate
        ref = self.credentials.save(candidate, token)
        probe = Candidate.model_validate(
            {**candidate.model_dump(), "state": "validated", "secret": ref.model_dump()}
        )
        # Resolve the exact immutable secret version used by selection, not the submitted object.
        stores = self.provider.list_shops(self.credentials.load(probe))
        if candidate.expires_at <= self.clock():
            raise ExpiredCandidate
        previous = self.store.get_setup(account.owner_id)
        version = previous.record_version if previous.binding else previous.record_version + 1
        validated = Candidate.model_validate(
            {**probe.model_dump(), "stores": stores, "record_version": version}
        )
        return self.store.publish_candidate(candidate, validated, previous).public()

    def select(
        self,
        account: MerchantAccount,
        *,
        candidate_id: str,
        shop_id: int,
        expected_setup_version: int,
        idempotency_key: str,
    ) -> dict:
        account = self._account(account)
        operation = operation_id(account.owner_id, idempotency_key, "select")
        if (
            type(shop_id) is not int
            or shop_id <= 0
            or type(expected_setup_version) is not int
            or expected_setup_version <= 0
        ):
            raise InvalidConnectionRequest
        import re

        if (
            not isinstance(candidate_id, str)
            or re.fullmatch(r"candidate_[a-f0-9]{32}", candidate_id) is None
        ):
            raise InvalidConnectionRequest
        request_digest = digest(
            "select-request", candidate_id, str(shop_id), str(expected_setup_version)
        )
        receipt = self.store.get_receipt(account.owner_id, operation)
        if receipt is not None:
            if receipt.request_digest != request_digest:
                raise ConnectionConflict
            setup = self.store.get_setup(account.owner_id)
            if setup.binding != receipt.binding:
                raise ConnectionConflict
            return self._finish(account, setup)
        previous = self.store.get_setup(account.owner_id)
        if (
            previous.binding is not None
            or previous.record_version != expected_setup_version
            or previous.candidate_id != candidate_id
        ):
            raise ConnectionConflict
        candidate = self.store.get_candidate(account.owner_id, candidate_id)
        if (
            candidate is None
            or candidate.state != "validated"
            or candidate.record_version != expected_setup_version
        ):
            raise ConnectionConflict
        if candidate.expires_at <= self.clock():
            raise ExpiredCandidate
        original = next((s for s in candidate.stores if s.id == shop_id), None)
        if original is None or original.sales_channel != "etsy":
            raise InvalidConnectionRequest
        token = self.credentials.load(candidate)
        stores = self.provider.list_shops(token)
        selected = next((s for s in stores if s.id == shop_id and s.sales_channel == "etsy"), None)
        if selected is None:
            raise ConnectionConflict
        # Recheck time after network I/O. TTL deletion never substitutes for this authorization.
        if candidate.expires_at <= self.clock():
            raise ExpiredCandidate
        binding = StoreBindingAuthority.create(
            owner_id=account.owner_id,
            connection_id=candidate.connection_id,
            shop_binding_id=candidate.shop_binding_id,
            shop_id=shop_id,
            authorization_epoch=1,
        )
        connection = Connection(
            binding=binding,
            candidate_id=candidate.candidate_id,
            secret=candidate.secret,
            store=selected,
        )
        try:
            setup = self.store.select(candidate, previous, connection, operation, request_digest)
        except ConnectionError:
            # Unknown transaction outcome: recover only this exact owned committed receipt.
            receipt = self.store.get_receipt(account.owner_id, operation)
            if (
                receipt is None
                or receipt.request_digest != request_digest
                or receipt.binding != binding
            ):
                raise ConnectionConflict from None
            setup = self.store.get_setup(account.owner_id)
            if setup.binding != binding:
                raise ConnectionConflict from None
        return self._finish(account, setup)

    def _finish(self, account: MerchantAccount, setup: Setup) -> dict:
        connection = self.store.get_connection(setup.binding)
        if setup.binding is None or connection.binding.owner_id != account.owner_id:
            raise ConnectionConflict
        if self.config.workflow_enabled and not setup.seller_granted:
            self.groups.grant(account)
            setup = self.store.mark_seller_granted(setup)
        return self.get(account)

    def activate(self, account: MerchantAccount) -> dict:
        account = self._account(account)
        if not self.config.workflow_enabled:
            raise ConnectionConflict
        setup = self.store.get_setup(account.owner_id)
        if setup.binding is None:
            raise ConnectionConflict
        # No candidate, secret, or provider dependency: recover an already committed destination.
        return self._finish(account, setup)
