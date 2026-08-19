"""Configuration for the Phase 2 Bedrock boundary."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BedrockSettings(BaseModel):
    """Non-secret, configuration-owned Bedrock invocation settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    region: str = Field(default="us-west-2", pattern=r"^[a-z]{2}-[a-z]+-\d$")
    model_id: str = Field(default="us.anthropic.claude-sonnet-4-6", min_length=1)
    output_mode: Literal["native_json_schema", "prompted_json"] = "native_json_schema"
    max_tokens: int = Field(default=2_048, ge=256, le=16_384)
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    max_repair_attempts: int = Field(default=1, ge=0, le=2)
