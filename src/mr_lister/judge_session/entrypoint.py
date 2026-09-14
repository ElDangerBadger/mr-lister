"""Dedicated Lambda entrypoint, inert until its explicit judge-session flag is enabled."""

from __future__ import annotations

import os
import time
from functools import lru_cache

from .http import JudgeSessionHandler, error_response
from .models import PREFIX, Config
from .service import JudgeSessionService
from .store import DynamoSessionStore
from .tokens import BoundedTransport, VerifiedOAuthIssuer


@lru_cache(maxsize=1)
def build_handler(config: Config) -> JudgeSessionHandler:
    import boto3
    from botocore.config import Config as BotoConfig

    options = BotoConfig(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 1})
    region = config.issuer.split(".")[1]
    session = boto3.Session(region_name=region)
    clock = lambda: int(time.time())  # noqa: E731
    store = DynamoSessionStore(session.client("dynamodb", config=options), config)
    issuer = VerifiedOAuthIssuer(
        config,
        secrets=session.client("secretsmanager", config=options),
        transport=BoundedTransport(),
        clock=clock,
    )
    service = JudgeSessionService(config, store=store, issuer=issuer, clock=clock)
    return JudgeSessionHandler(config, service)


def lambda_handler(event: dict, context: object | None = None) -> dict:
    if os.environ.get(PREFIX + "ENABLED") != "true":
        return error_response(503)
    try:
        return build_handler(Config.from_environment(os.environ))(event, context)
    except Exception:
        return error_response(503)
