"""Configuration for the Phase 2 Bedrock boundary."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BedrockSettings(BaseModel):
    """Non-secret, configuration-owned Bedrock invocation settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transport: Literal["converse", "mantle"] = "converse"
    region: str = Field(default="us-west-2", pattern=r"^[a-z]{2}-[a-z]+-\d$")
    model_id: str = Field(default="us.anthropic.claude-sonnet-4-6", min_length=1)
    output_mode: Literal["native_json_schema", "prompted_json"] = "native_json_schema"
    max_tokens: int = Field(default=2_048, ge=256, le=16_384)
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    max_repair_attempts: int = Field(default=1, ge=0, le=2)

    @model_validator(mode="after")
    def candidate_transport_is_explicit(self) -> "BedrockSettings":
        """Keep the experimental Mantle boundary separate from existing Converse models."""
        candidate_models = {"google.gemma-4-31b", "google.gemma-4-26b-a4b"}
        if self.transport == "mantle":
            if self.model_id not in candidate_models:
                raise ValueError("Mantle evaluation requires a supported Gemma 4 candidate")
            if self.region not in {"us-east-1", "us-east-2", "us-west-2", "eu-central-1"}:
                raise ValueError("Mantle evaluation requires a supported candidate region")
            if self.output_mode != "native_json_schema":
                raise ValueError("Mantle evaluation requires the explicit JSON-schema candidate")
        elif self.model_id in candidate_models:
            raise ValueError("Gemma 4 candidates require explicit Mantle transport")
        return self
