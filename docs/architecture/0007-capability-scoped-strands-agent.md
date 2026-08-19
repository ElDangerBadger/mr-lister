# ADR 0007: Capability-scoped Strands preparation agent

- Status: Accepted
- Date: 2026-08-19

## Context

Phase 3 introduces model-directed orchestration. Prompt instructions alone cannot be the
authorization boundary because artwork text, listing content, and user text all reach the model.
The existing workflow already owns deterministic validation, versioned approval, and publication
guards; the agent must not duplicate or weaken them.

## Decision

Each Strands agent instance is scoped to one trusted application `job_id`. The model never chooses
or changes that identifier through a tool argument. In `review` mode, the application exposes only
inspection and deterministic-validation tools. In `revise` mode, it additionally exposes a tool
that stages a complete replacement listing and returns it to human review.

No agent mode exposes approval or publication tools. Structured agent output always states that
human approval is required and publication is not authorized. Tool errors use stable application
codes and sanitized messages. Traces use a one-way session/job correlation digest rather than raw
artwork, prompts, or seller content.

The image-aware Bedrock adapter remains responsible for artwork and listing intelligence. Strands
orchestrates the review path around that tested boundary; it does not replace the application
contracts or gain marketplace authority.

## Consequences

- Prompt injection cannot discover an unavailable approval or publication capability.
- Cross-job access is absent from the model-visible schemas.
- Revision remains reversible and invalidates any prior approval through the existing workflow.
- AgentCore deployment can wrap this local entry boundary without changing tool authority.
- The deployed Nova canary verified Bedrock tool-use support while preserving the human approval
  and publication boundaries.
- Token/context reduction remains a post-Phase 3 optimization; it must not weaken the fixed tool or
  structured-output contracts.
