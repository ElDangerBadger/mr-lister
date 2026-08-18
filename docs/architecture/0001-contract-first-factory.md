# ADR 0001: Contract-first deterministic factory

- Status: Accepted
- Date: 2026-08-18

## Context

POD listing production combines interpretive work with repeatable business operations. Model
inference may succeed while returning incomplete, malformed, or commercially unusable output.

## Decision

Mr Lister is a deterministic factory around a narrow, validated intelligence boundary.
Application-owned, versioned contracts define every model and service boundary. Bedrock may
analyze artwork and draft listing intelligence, but deterministic code validates the result
and owns all consequential operations.

## Consequences

- Model output is untrusted until it passes application validation.
- Product policy, prices, state, permissions, and external writes remain outside prompts.
- Adapters conform to domain contracts rather than redefining them.
- Tests can exercise the factory without live model or marketplace access.
