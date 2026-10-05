"""Separate route entrypoints support read-only query and scoped command IAM roles."""

from __future__ import annotations

import os
import time
from functools import lru_cache

from mr_lister.accounts.store import DynamoAccountReader

from .credentials import SecretsManagerCredentialReader, SecretsManagerCredentialStore
from .http import ConnectionHttpHandler, unavailable
from .models import PREFIX, ConnectionConfig
from .service import ConnectionService, SellerGroupWriter, StoreSetupReader
from .store import DynamoConnectionDirectory, DynamoConnectionStore
from .transport import PrintifyShopsTransport


class LazyCapability:
    def __init__(self, factory) -> None:
        self._factory = factory
        self._value = None

    def __getattr__(self, name: str):
        if self._value is None:
            self._value = self._factory()
        return getattr(self._value, name)


@lru_cache(maxsize=4)
def sdk_client(service: str, region: str):
    import boto3
    from botocore.config import Config

    return boto3.Session(region_name=region).client(
        service, config=Config(connect_timeout=3, read_timeout=3, retries={"total_max_attempts": 1})
    )


def build_handler(config: ConnectionConfig, operation: str) -> ConnectionHttpHandler:
    def dynamo():
        return sdk_client("dynamodb", config.accounts.region)

    def service():
        if operation == "get":
            return StoreSetupReader(
                config=config,
                directory=DynamoConnectionDirectory(dynamo(), config),
                clock=lambda: int(time.time()),
            )
        credential_class = (
            SecretsManagerCredentialStore
            if operation == "validate"
            else SecretsManagerCredentialReader
        )
        return ConnectionService(
            config=config,
            store=DynamoConnectionStore(dynamo(), config),
            credentials=LazyCapability(
                lambda: credential_class(
                    sdk_client("secretsmanager", config.accounts.region), config
                )
            ),
            provider=LazyCapability(PrintifyShopsTransport),
            groups=LazyCapability(
                lambda: SellerGroupWriter(sdk_client("cognito-idp", config.accounts.region), config)
            ),
            clock=lambda: int(time.time()),
        )

    return ConnectionHttpHandler(
        config=config,
        account_reader_factory=lambda: DynamoAccountReader(dynamo(), config.accounts),
        service_factory=service,
        allowed_operation=operation,
    )


def _handle(event: dict, context: object, operation: str) -> dict:
    if os.environ.get(PREFIX + "ENABLED") != "true":
        return unavailable()
    try:
        return build_handler(ConnectionConfig.from_environment(os.environ), operation)(
            event, context
        )
    except Exception:
        return unavailable()


def query_handler(event: dict, context: object = None) -> dict:
    return _handle(event, context, "get")


def validate_handler(event: dict, context: object = None) -> dict:
    return _handle(event, context, "validate")


def select_handler(event: dict, context: object = None) -> dict:
    return _handle(event, context, "select")


def activate_handler(event: dict, context: object = None) -> dict:
    return _handle(event, context, "activate")


def cleanup_handler(event: dict, context: object = None) -> dict:
    if os.environ.get(PREFIX + "ENABLED") != "true":
        return {"processed": 0, "completed": 0, "failed": 0}
    try:
        from .cleanup import CandidateCleanupService, CandidateSecretDisposer
        from .models import ConnectionError

        index = os.environ.get(PREFIX + "CLEANUP_INDEX")
        if index != "CandidateCleanupDue":
            raise ConnectionError
        config = ConnectionConfig.from_environment(os.environ)
        store = DynamoConnectionStore(sdk_client("dynamodb", config.accounts.region), config)
        disposer = CandidateSecretDisposer(
            sdk_client("secretsmanager", config.accounts.region), config
        )
        result = CandidateCleanupService(
            store=store, disposer=disposer, clock=lambda: int(time.time())
        ).run_due(index_name=index)
        if result["failed"]:
            raise ConnectionError
        return result
    except Exception:
        pass
    from .models import ConnectionError

    raise ConnectionError
