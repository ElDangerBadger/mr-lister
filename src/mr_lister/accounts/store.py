"""Separate owner-keyed DynamoDB readers and conditional, create-only writers."""

from __future__ import annotations

import json
from typing import Protocol

from botocore.exceptions import ClientError

from .models import (
    ACCOUNT_ENTITY,
    AccountConfig,
    AccountConflict,
    AccountUnavailable,
    MerchantAccount,
    exact_object,
    strict_json,
    valid_owner,
)


class AccountReader(Protocol):
    def get(self, owner_id: str) -> MerchantAccount | None: ...


class AccountWriter(AccountReader, Protocol):
    def create_if_absent(self, account: MerchantAccount) -> MerchantAccount: ...


def account_key(owner_id: str) -> str:
    if not valid_owner(owner_id):
        raise ValueError("Invalid account identity")
    return f"OWNER#{owner_id}"


class DynamoAccountReader:
    def __init__(self, client: object, config: AccountConfig) -> None:
        self._client = client
        self._config = config

    def get(self, owner_id: str) -> MerchantAccount | None:
        try:
            key = account_key(owner_id)
            if owner_id in self._config.reserved_owner_ids:
                raise ValueError
            result = self._client.get_item(
                TableName=self._config.table_name,
                Key={"PK": {"S": key}},
                ConsistentRead=True,
            )
            if not isinstance(result, dict):
                raise ValueError
            if "Item" not in result:
                return None
            item = exact_object(result["Item"], {"PK", "entity_type", "payload"})
            if item["PK"] != {"S": key} or item["entity_type"] != {"S": ACCOUNT_ENTITY}:
                raise ValueError
            payload = exact_object(item["payload"], {"S"})["S"]
            account = MerchantAccount.from_payload(strict_json(payload))
            account.identity.require_configuration(self._config)
            if account.owner_id != owner_id:
                raise ValueError
            return account
        except Exception:
            pass
        raise AccountUnavailable


class DynamoAccountWriter(DynamoAccountReader):
    def create_if_absent(self, account: MerchantAccount) -> MerchantAccount:
        collision = False
        try:
            # Reparse even internal values before writing durable authority.
            account = MerchantAccount.from_payload(account.payload())
            account.identity.require_configuration(self._config)
            self._client.put_item(
                TableName=self._config.table_name,
                Item={
                    "PK": {"S": account_key(account.owner_id)},
                    "entity_type": {"S": ACCOUNT_ENTITY},
                    "payload": {
                        "S": json.dumps(account.payload(), sort_keys=True, separators=(",", ":"))
                    },
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
            return account
        except ClientError as error:
            collision = (
                error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
            )
        except Exception:
            pass
        if collision:
            current = self.get(account.owner_id)
            if current is not None and current.identity == account.identity:
                return current
            raise AccountConflict
        raise AccountUnavailable
