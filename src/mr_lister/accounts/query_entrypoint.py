"""AccountQuery Lambda; inert without its explicit enable flag and exact configuration."""

from __future__ import annotations

import os
from functools import lru_cache

from .models import PREFIX, AccountConfig
from .query import AccountQueryHandler, error_response
from .store import DynamoAccountReader


@lru_cache(maxsize=1)
def build_reader(config: AccountConfig) -> DynamoAccountReader:
    import boto3
    from botocore.config import Config

    session = boto3.Session(region_name=config.region)
    options = Config(connect_timeout=3, read_timeout=3, retries={"total_max_attempts": 1})
    return DynamoAccountReader(session.client("dynamodb", config=options), config)


def lambda_handler(event: dict, context: object | None = None) -> dict:
    if os.environ.get(PREFIX + "QUERY_ENABLED") != "true":
        return error_response(503)
    try:
        config = AccountConfig.from_environment(os.environ)
        # Authentication and reserved-owner checks precede SDK client construction.
        return AccountQueryHandler(config, reader_factory=lambda: build_reader(config))(
            event, context
        )
    except Exception:
        return error_response(503)
