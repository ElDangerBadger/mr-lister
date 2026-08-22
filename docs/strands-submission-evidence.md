# Strands Agents submission evidence

Status date: 2026-08-22

This is the public traceability map for Mr Lister's required Strands Agents implementation. It is
written so a judge—or an automated first-pass reviewer—can verify in under a minute that Strands is
the real agent framework, understand the non-trivial work it performs, and distinguish completed
evidence from the remaining production integration gate.

## Event requirement

The [Agents for Humans official rules](https://agentsforhumans.devpost.com/rules) require a new AI
agent built with Strands Agents that performs real work end to end. Stage One asks whether the
submission reasonably applies the required SDK. Stage Two scores how thoroughly and skillfully it
uses Strands, whether the implementation is working and non-trivial, and whether the use is
creative and well understood.

The [official FAQ](https://agentsforhumans.devpost.com/details/faqs) says the architecture diagram
should explicitly show Strands as the core agent and its loop:
`model -> tools -> reasoning -> response`. AgentCore is optional under the rules, but deployment on
AgentCore strengthens the Technical Implementation score.

## What Strands owns in Mr Lister

Strands owns the judgment-heavy listing-preparation loop. For one application-bound job and mode,
the controller model chooses among only the tools the application exposes, observes their bounded
results, reasons about readiness or revision, and returns a strict structured decision. This is
real tool-using orchestration, not a direct model call hidden behind an agent-shaped wrapper.

Strands does **not** own approval, state-transition validity, idempotency, credentials, or
publication authority. DynamoDB transactions and application contracts remain authoritative for
those safety properties. The separation is intentional agent engineering: Strands decides how to
perform judgment-heavy preparation, while deterministic code decides what actions are legally and
commercially permitted.

## Evidence map

| Claim | Source | Automated evidence | Runtime evidence |
| --- | --- | --- | --- |
| The application constructs and invokes a real `strands.Agent` | [`agent/runtime.py`](../src/mr_lister/agent/runtime.py) | [`test_strands_real_loop.py`](../tests/test_strands_real_loop.py), [`test_agent_tools.py`](../tests/test_agent_tools.py) | Sanitized AgentCore canary summary below |
| The loop has bounded turns/tokens and strict structured output | [`agent/runtime.py`](../src/mr_lister/agent/runtime.py), [`agent/contracts.py`](../src/mr_lister/agent/contracts.py) | [`test_agent_tools.py`](../tests/test_agent_tools.py) | `cycles`, token counts, `next_action`, and framework identity in audit output |
| The agent acts through meaningful custom tools | [`agent/tools.py`](../src/mr_lister/agent/tools.py) | Real `@tool` schemas and capability-scope tests in [`test_agent_tools.py`](../tests/test_agent_tools.py) | Live canary selected `inspect_staged_review` and `validate_staged_listing` |
| Agent capability changes by trusted application mode | [`build_preparation_agent`](../src/mr_lister/agent/runtime.py) | Prepare/review/revise tool-set assertions in [`test_agent_tools.py`](../tests/test_agent_tools.py) | Review canary had no prepare, revise, approve, or publish capability |
| The Strands agent runs through the official AgentCore SDK boundary | [`agent/agentcore_sdk.py`](../src/mr_lister/agent/agentcore_sdk.py) | [`test_agentcore_sdk.py`](../tests/test_agentcore_sdk.py) | Runtime version 2 reached `READY` in `us-west-2` and completed a live invocation |
| Agent output cannot claim human approval or publication | [`PreparationDecision`](../src/mr_lister/agent/contracts.py), [`AGENT_SYSTEM_PROMPT`](../src/mr_lister/agent/runtime.py) | Authority and prompt-injection tests in [`test_agent_tools.py`](../tests/test_agent_tools.py) | Live result returned human review and `publication_authorized=false` |
| Runtime evidence names Strands without exposing seller data | [`agent/observability.py`](../src/mr_lister/agent/observability.py) | [`test_agent_observability.py`](../tests/test_agent_observability.py) | Current response/audit contract requires `strands-agents` and `mr-lister-preparation`; redeployed canary pending |
| Durable Phase 6 preparation uses the real SDK agent and exactly one job-scoped tool | [`agent/phase6.py`](../src/mr_lister/agent/phase6.py) | [`test_phase6_strands_runtime.py`](../tests/test_phase6_strands_runtime.py) | Offline complete; Phase 6 deployment pending |
| The AgentCore boundary binds exact owner/job/work input to immutable Strands evidence | [`control/agentcore.py`](../src/mr_lister/control/agentcore.py) | [`test_phase6_agentcore_bridge.py`](../tests/test_phase6_agentcore_bridge.py), [`test_phase6_preparation_settlement.py`](../tests/test_phase6_preparation_settlement.py) | Offline complete; same-job live canary pending |

The sanitized public summary combines historical live-canary metrics and tool selection with
explicitly labeled current-source identity metadata in
[`evidence/strands-agentcore-canary.json`](evidence/strands-agentcore-canary.json). The fuller
evaluation narrative is [`phase3-controller-evaluation.md`](phase3-controller-evaluation.md).
Private raw runtime diagnostics remain excluded from Git because proving Strands does not justify
publishing session IDs, account identifiers, prompts, artwork, or provider payloads.

## Current completion boundary

Completed now:

- Strands Agents SDK is a direct application dependency and the tested version is recorded;
- the application constructs and invokes the real SDK agent loop;
- four real `@tool` functions perform preparation, inspection, deterministic validation, and
  revision for one bound job;
- structured output permanently requires human review and denies publication authority;
- credential-free tests execute the actual SDK loop;
- a live AgentCore canary used the intended tools and emitted sanitized cycle/tool/token evidence;
- the current source response and audit contracts require fixed Strands framework/agent identity,
  with redeployment evidence still pending.

The Phase 6.2 source now contains a fail-closed, work-bound AgentCore bridge and a genuine
single-tool Strands preparation runtime with immutable same-job evidence. It is verified offline;
the SAM Lambda composition remains deliberately `SCAFFOLD_ONLY`.

Still mandatory before the product or submission may claim a deployed end-to-end Strands path:

- the fail-closed Phase 6 Lambda adapters must be replaced and deployed so durable `PREPARE` work
  invokes the exact AgentCore Strands runtime with no direct deterministic or non-Strands fallback;
- the resulting Strands audit correlation must belong to the same seller job later displayed in
  consolidated review;
- an end-to-end acceptance test and demo must visibly show upload, Strands tool use, structured
  response, staged listing, and the human decision gate.

These are blocking Phase 6/8 checklist items, not optional polish.

## Five-minute demo proof

The submission video should make Strands visible three times:

1. **First 30 seconds:** say that Mr Lister is a Strands agent for POD sellers and show the labeled
   architecture loop.
2. **Working run:** upload one artwork and show progress while the real preparation request routes
   through AgentCore/Strands to a complete staged listing.
3. **Technical proof:** briefly show the public `Agent(...)` construction, the four `@tool`
   functions, and a sanitized audit/response containing framework, agent ID, cycles, selected
   tools, structured next action, and the human-approval boundary.

The Devpost text should include a section titled **How Mr Lister Uses Strands Agents** and link to
this file. Judges are not required to run the repository, so code, architecture, runtime evidence,
description, and video must each independently identify Strands.

## Local verification

```bash
source .venv/bin/activate
python -m pytest -q \
  tests/test_strands_setup.py \
  tests/test_strands_real_loop.py \
  tests/test_agent_tools.py \
  tests/test_agent_observability.py \
  tests/test_agentcore_sdk.py \
  tests/test_strands_submission_evidence.py
```
