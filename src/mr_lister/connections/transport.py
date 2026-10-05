"""The token-validation capability can only read Printify's fixed shops endpoint."""

from __future__ import annotations

import http.client
import ssl

from pydantic import SecretStr

from mr_lister.accounts.models import strict_json

from .credentials import checked_token
from .models import ConnectionError, InvalidCredential, Shop

HOST = "api.printify.com"
PATH = "/v1/shops.json"
MAX_RESPONSE_BYTES = 65536


class PrintifyShopsTransport:
    def __init__(self, *, connection_factory=None) -> None:
        self._factory = connection_factory or http.client.HTTPSConnection

    def list_shops(self, token: SecretStr) -> tuple[Shop, ...]:
        denied = False
        connection = None
        try:
            token = checked_token(token)
            connection = self._factory(HOST, timeout=5, context=ssl.create_default_context())
            connection.request(
                "GET",
                PATH,
                headers={
                    "Authorization": "Bearer " + token.get_secret_value(),
                    "Accept": "application/json",
                    "User-Agent": "MrLister-StoreSetup/1",
                },
            )
            result = connection.getresponse()
            if result.status in (401, 403):
                denied = True
                raise ValueError
            if result.status != 200:
                raise ValueError
            content_type = result.getheader("Content-Type", "").split(";", 1)[0].lower()
            if content_type != "application/json":
                raise ValueError
            raw = result.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError
            data = strict_json(raw.decode("utf-8"), limit=MAX_RESPONSE_BYTES)
            if not isinstance(data, list) or len(data) > 100:
                raise ValueError
            shops = tuple(
                Shop.model_validate({key: entry[key] for key in ("id", "name", "sales_channel")})
                for entry in data
                if isinstance(entry, dict)
            )
            if len(shops) != len(data) or len({s.id for s in shops}) != len(shops):
                raise ValueError
            return shops
        except Exception:
            pass
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        if denied:
            raise InvalidCredential
        raise ConnectionError
