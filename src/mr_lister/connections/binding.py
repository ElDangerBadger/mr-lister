"""Credential-free authority pinned before an ordinary merchant upload is queued."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, Strict, model_validator

OwnerId = Annotated[str, Strict(), Field(pattern=r"^[a-f0-9]{64}$")]
ConnectionId = Annotated[str, Strict(), Field(pattern=r"^conn_[a-f0-9]{32}$")]
ShopBindingId = Annotated[str, Strict(), Field(pattern=r"^binding_[a-f0-9]{32}$")]
Fingerprint = Annotated[str, Strict(), Field(pattern=r"^[a-f0-9]{64}$")]


def _destination_fingerprint(
    *, owner_id: str, connection_id: str, shop_binding_id: str, shop_id: int
) -> str:
    # Epoch fences access to this destination; it does not rewrite its immutable identity.
    payload = {
        "contract_version": "account-store-binding-v1",
        "owner_id": owner_id,
        "connection_id": connection_id,
        "shop_binding_id": shop_binding_id,
        "shop_id": shop_id,
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


class StoreBindingAuthority(BaseModel):
    """An immutable destination plus the authorization epoch for its provider calls.

    This object comes from an owner-scoped server connection record, never from
    client-supplied authority. It contains no token or secret identifier. Consumers
    must reparse it, compare the authenticated owner and recheck the current epoch.
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, revalidate_instances="always",
        hide_input_in_errors=True,
    )

    contract_version: Literal["account-store-binding-v1"] = "account-store-binding-v1"
    owner_id: OwnerId
    connection_id: ConnectionId
    shop_binding_id: ShopBindingId
    shop_id: Annotated[int, Strict(), Field(gt=0)]
    authorization_epoch: Annotated[int, Strict(), Field(ge=1)]
    binding_fingerprint: Fingerprint

    @model_validator(mode="after")
    def fingerprint_matches_destination(self) -> StoreBindingAuthority:
        if self.binding_fingerprint != _destination_fingerprint(
            owner_id=self.owner_id, connection_id=self.connection_id,
            shop_binding_id=self.shop_binding_id, shop_id=self.shop_id,
        ):
            raise ValueError("Store binding identity is inconsistent")
        return self

    @classmethod
    def create(
        cls, *, owner_id: str, connection_id: str, shop_binding_id: str,
        shop_id: int, authorization_epoch: int,
    ) -> StoreBindingAuthority:
        return cls(
            owner_id=owner_id, connection_id=connection_id,
            shop_binding_id=shop_binding_id, shop_id=shop_id,
            authorization_epoch=authorization_epoch,
            binding_fingerprint=_destination_fingerprint(
                owner_id=owner_id, connection_id=connection_id,
                shop_binding_id=shop_binding_id, shop_id=shop_id,
            ),
        )

    def checked_for_owner(self, owner_id: str) -> StoreBindingAuthority:
        checked = type(self).model_validate(self.model_dump(mode="python"))
        if checked.owner_id != owner_id:
            raise ValueError("Store binding owner does not match")
        return checked
