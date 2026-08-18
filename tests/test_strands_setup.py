"""Credential-free verification of the required Strands SDK installation."""


def test_strands_and_bedrock_provider_are_importable() -> None:
    from strands import Agent
    from strands.models import BedrockModel

    assert Agent.__name__ == "Agent"
    assert BedrockModel.__name__ == "BedrockModel"
