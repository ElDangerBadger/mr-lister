"""Validated Printify connection settings loaded from a secret boundary."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from mr_lister.workflow.secrets import SecretReader


class PrintifyConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    api_token: SecretStr = Field(min_length=1)
    shop_id: int = Field(gt=0)


def load_printify_connection(*, reader: SecretReader, secret_arn: str) -> PrintifyConnection:
    """Read and validate one seller connection without retaining its response envelope."""

    return PrintifyConnection.model_validate_json(reader.get_secret(secret_arn))
