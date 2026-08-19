from __future__ import annotations

from mr_lister.contracts import ArtworkAnalysis, ListingIntelligence
from mr_lister.intelligence.schema import bedrock_output_schema


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def test_provider_schema_removes_constraints_bedrock_rejects() -> None:
    schema = bedrock_output_schema(ListingIntelligence)
    serialized_nodes = list(walk(schema))

    for node in serialized_nodes:
        assert "minLength" not in node
        assert "maxLength" not in node
        assert "minimum" not in node
        assert "maximum" not in node
        assert "minItems" not in node
        assert "maxItems" not in node
        assert "default" not in node

    assert schema["additionalProperties"] is False
    assert schema["properties"]["tags"]["type"] == "array"


def test_application_contract_remains_stricter_than_provider_schema() -> None:
    provider_schema = bedrock_output_schema(ArtworkAnalysis)
    application_schema = ArtworkAnalysis.model_json_schema()

    assert "minimum" not in provider_schema["properties"]["confidence"]
    assert application_schema["properties"]["confidence"]["minimum"] == 0.0
    assert application_schema["properties"]["confidence"]["maximum"] == 1.0
