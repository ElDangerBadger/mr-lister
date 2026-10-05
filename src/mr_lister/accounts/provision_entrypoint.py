"""Cognito confirmation/pre-auth repair; disabled means no provisioning or group grant."""

from __future__ import annotations

import os
import time
from functools import lru_cache

from .models import PREFIX, AccountConfig, AccountUnavailable
from .provision import AccountProvisioner, CognitoAccountGroupWriter, native_signup
from .store import DynamoAccountWriter


@lru_cache(maxsize=1)
def build_provisioner(config: AccountConfig) -> AccountProvisioner:
    import boto3
    from botocore.config import Config

    session = boto3.Session(region_name=config.region)
    options = Config(connect_timeout=3, read_timeout=3, retries={"total_max_attempts": 1})
    return AccountProvisioner(
        config,
        store=DynamoAccountWriter(session.client("dynamodb", config=options), config),
        groups=CognitoAccountGroupWriter(session.client("cognito-idp", config=options), config),
        clock=lambda: int(time.time()),
    )


def lambda_handler(event: dict, context: object | None = None) -> dict:
    if os.environ.get(PREFIX + "PROVISION_ENABLED") != "true":
        return event
    try:
        config = AccountConfig.from_environment(os.environ)
        signup = native_signup(event, config)
        if signup is not None:
            build_provisioner(config).provision(signup)
        return event
    except Exception:
        pass
    # Raised outside the caught exception: SDK/validation messages never reach Cognito or logs.
    raise AccountUnavailable
