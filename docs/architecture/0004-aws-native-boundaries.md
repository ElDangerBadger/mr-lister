# ADR 0004: AWS-native service boundaries

- Status: Accepted
- Date: 2026-08-18

## Context

Mr Lister is being built for an AWS agent hackathon and as the foundation of a commercial
product. Managed services are valuable only when they support the customer workflow and its
safety properties.

## Decision

Amazon Bedrock provides multimodal intelligence, and Strands provides bounded orchestration
targeted for AgentCore Runtime. Configuration and deterministic application code retain
authority over product policy, validation, approval, idempotency, and marketplace writes.
Later phases add private S3 storage, DynamoDB state, Step Functions durability, Secrets Manager,
and sanitized CloudWatch telemetry when the vertical slice requires them.

## Consequences

- Phase 0 contains no live service integration.
- Phase 1 proves the complete workflow with fake adapters first.
- Managed infrastructure is introduced behind application-owned interfaces.
- Agent tool access never implies authorization to publish.
