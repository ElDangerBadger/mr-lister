"""Atomic invitation accounting and opaque sessions in one dedicated DynamoDB table."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from .models import Config, Denied, Invitation, Session, Unavailable


class SessionStore(Protocol):
    def invitation(self, invitation_digest: str) -> object: ...
    def session(self, session_digest: str) -> object: ...
    def reserve(self, invitation: Invitation, session: Session, now: int) -> None: ...
    def revoke(self, session_digest: str) -> None: ...


def encode(row: Mapping[str, object]) -> dict:
    output = {}
    for key, value in row.items():
        if type(value) is bool:
            output[key] = {"BOOL": value}
        elif type(value) is int:
            output[key] = {"N": str(value)}
        elif isinstance(value, str):
            output[key] = {"S": value}
        else:
            raise ValueError("Invalid durable value")
    return output


def decode(row: object) -> dict:
    if not isinstance(row, dict):
        raise ValueError
    output = {}
    for key, item in row.items():
        if not isinstance(item, dict) or len(item) != 1:
            raise ValueError
        if "S" in item and isinstance(item["S"], str):
            output[key] = item["S"]
        elif "BOOL" in item and type(item["BOOL"]) is bool:
            output[key] = item["BOOL"]
        elif (
            "N" in item
            and isinstance(item["N"], str)
            and item["N"].isascii()
            and item["N"].isdigit()
            and len(item["N"]) <= 12
        ):
            output[key] = int(item["N"])
        else:
            raise ValueError
    return output


class DynamoSessionStore:
    def __init__(self, client: Any, config: Config) -> None:
        self.client = client
        self.config = config

    def invitation(self, invitation_digest: str) -> object:
        return self._get("INVITE#" + invitation_digest)

    def session(self, session_digest: str) -> object:
        return self._get("SESSION#" + session_digest)

    def _get(self, key: str) -> object:
        try:
            response = self.client.get_item(
                TableName=self.config.table, Key={"PK": {"S": key}}, ConsistentRead=True
            )
            return decode(response["Item"]) if "Item" in response else None
        except Exception:
            pass
        raise Unavailable

    def reserve(self, invitation: Invitation, session: Session, now: int) -> None:
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.config.table,
                            "Key": {"PK": {"S": "INVITE#" + invitation.invitation_digest}},
                            "UpdateExpression": "SET redemptions = redemptions + :one",
                            "ConditionExpression": (
                                "contract_version = :version AND campaign_id = :campaign "
                                "AND owner_id = :owner AND enabled = :yes AND expires_at = :expiry "
                                "AND expires_at > :now AND redemption_limit = :limit "
                                "AND redemptions >= :zero AND redemptions < redemption_limit"
                            ),
                            "ExpressionAttributeValues": encode(
                                {
                                    ":version": "judge-session-invitation-v1",
                                    ":campaign": self.config.campaign_id,
                                    ":owner": self.config.owner_id,
                                    ":yes": True,
                                    ":expiry": invitation.expires_at,
                                    ":now": now,
                                    ":limit": invitation.redemption_limit,
                                    ":zero": 0,
                                    ":one": 1,
                                }
                            ),
                        },
                    },
                    {
                        "Put": {
                            "TableName": self.config.table,
                            "Item": encode(session.row(self.config)),
                            "ConditionExpression": "attribute_not_exists(PK)",
                        },
                    },
                ]
            )
            return
        except Exception as error:
            response = getattr(error, "response", {})
            code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
            reasons = response.get("CancellationReasons", []) if isinstance(response, dict) else []
            denied = (
                code == "TransactionCanceledException"
                and isinstance(reasons, list)
                and any(
                    isinstance(reason, dict) and reason.get("Code") == "ConditionalCheckFailed"
                    for reason in reasons
                )
            )
        if denied:
            raise Denied
        raise Unavailable

    def revoke(self, session_digest: str) -> None:
        try:
            self.client.update_item(
                TableName=self.config.table,
                Key={"PK": {"S": "SESSION#" + session_digest}},
                UpdateExpression="SET revoked = :yes",
                ConditionExpression=(
                    "attribute_exists(PK) AND campaign_id = :campaign "
                    "AND owner_id = :owner AND contract_version = :version"
                ),
                ExpressionAttributeValues=encode(
                    {
                        ":yes": True,
                        ":campaign": self.config.campaign_id,
                        ":owner": self.config.owner_id,
                        ":version": "judge-session-v1",
                    }
                ),
            )
            return
        except Exception as error:
            response = getattr(error, "response", {})
            missing = (
                isinstance(response, dict)
                and response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
            )
        if not missing:
            raise Unavailable
