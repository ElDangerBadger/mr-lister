"""Dedicated scheduled Lambda entry point; never used by seller/publication handlers."""

from __future__ import annotations

import os
from typing import Any

from mr_lister.judge_cleanup.aws import DynamoCleanupSource, DynamoCleanupStore
from mr_lister.judge_cleanup.models import CleanupConfig
from mr_lister.judge_cleanup.provider import PrintifyCleanupProvider
from mr_lister.judge_cleanup.service import CleanupRunError, JudgeCleanupService
from mr_lister.production.provider_secrets import SecretsManagerOwnerPrintifyConnectionResolver


def lambda_handler(event: Any, context: Any) -> dict[str, int | bool]:
    # Scheduler data is never accepted as product, owner, deadline or activation authority.
    if not isinstance(event, dict) or (
        event
        and not (
            event.get("source") == "aws.events"
            and event.get("detail-type") == "Scheduled Event"
            and event.get("detail") == {}
            and set(event)
            <= {
                "version",
                "id",
                "detail-type",
                "source",
                "account",
                "time",
                "region",
                "resources",
                "detail",
            }
        )
    ):
        raise CleanupRunError("Cleanup accepts only its scheduled event")
    try:
        import boto3
        from botocore.config import Config

        config = CleanupConfig.model_validate_json(os.environ["MR_LISTER_JUDGE_CLEANUP_CONFIG"])
        aws_config = Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1})
        client = boto3.client("dynamodb", config=aws_config)
        resolver = SecretsManagerOwnerPrintifyConnectionResolver(
            client=boto3.client("secretsmanager", config=aws_config),
            secret_arn=config.printify_secret_arn,
        )
        service = JudgeCleanupService(
            config=config,
            source=DynamoCleanupSource(client=client, config=config),
            store=DynamoCleanupStore(client=client, config=config),
            provider=PrintifyCleanupProvider(config=config, resolver=resolver),
            remaining_seconds=lambda: context.get_remaining_time_in_millis() / 1000,
        )
        return service.sweep()
    except Exception:
        pass
    raise CleanupRunError("Judge cleanup failed; inspect its durable audit and retry state")
