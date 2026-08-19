# ADR 0005: Bedrock model and Region strategy

- Status: Accepted (amended)
- Date: 2026-08-18

## Context

Phase 2 requires multimodal artwork interpretation and schema-constrained listing intelligence.
The Agents for Humans rules require Strands Agents but do not mandate a particular model. Phase 3
targets AgentCore Runtime, whose regional support favors US West (Oregon) over US West
(N. California).

## Decision

Use a three-tier testing strategy:

1. The deterministic fake adapter is the default for local development and the offline suite.
2. Amazon Nova 2 Lite (`us.amazon.nova-2-lite-v1:0`) is the live-development and Phase 2
   evaluation model. It avoids a third-party Marketplace subscription and can consume AWS account
   credits, though its invocations remain usage-priced.
3. Anthropic Claude Sonnet 4.6 (`us.anthropic.claude-sonnet-4-6`) is an optional final quality
   benchmark only after its account terms and cost are deliberately accepted.

Invoke live models through the Bedrock Converse API in `us-west-2` and keep model identifiers and
output capabilities in configuration rather than domain code. Nova uses schema-in-prompt JSON
with strict application validation because it does not support Bedrock native structured output;
Claude uses Bedrock's native JSON-schema output.

The application-owned intelligence port remains provider-neutral. Deterministic validation,
repair limits, approval, product policy, and publication authorization remain outside the model.

## Consequences

- Bedrock and later AgentCore resources share `us-west-2` as their home Region.
- US cross-Region inference may route within the supported US geography for capacity.
- Models are replaceable without changing workflow or marketplace contracts.
- Provider capability differences are explicit rather than hidden behind model-ID heuristics.
- Live model tests are explicit and cost-bearing; the default suite remains offline.
- Model quality is evaluated on representative owned or synthetic artwork before Phase 2 closes.
- Comparable runs record model ID, prompt version, split, and trial; frozen holdout cases are not
  used to tune the prompt version they evaluate.
