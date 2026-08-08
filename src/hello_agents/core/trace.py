"""Privacy-safe serialization for auditable Agent Core trajectories."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import AgentLoopResult, Observation, ToolResult


_SAFE_RESULT_KEYS = (
    "status",
    "error",
    "exit_code",
    "submit_gate",
    "field_count",
    "filled_count",
    "review_count",
    "blocking_review_count",
    "application_id",
    "retry_ready",
    "retry_scope",
)


def _result_summary(result: ToolResult) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "call_id": result.call_id,
        "tool_name": result.tool_name,
        "effect": result.effect.value,
        "ok": result.ok,
        "error": result.error,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
    }
    if isinstance(result.output, Mapping):
        output = {
            key: result.output.get(key)
            for key in _SAFE_RESULT_KEYS
            if key in result.output
        }
        if output:
            summary["output"] = output
    return summary


def _observation_summary(observation: Observation) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in (
        "call_id",
        "ok",
        "effect",
        "error",
        "policy_code",
        "status",
        "application_id",
        "phase",
    ):
        if key in observation.payload:
            payload[key] = observation.payload.get(key)
    output = observation.payload.get("output")
    if isinstance(output, Mapping):
        output_summary = {
            key: output.get(key)
            for key in _SAFE_RESULT_KEYS
            if key in output
        }
        if output_summary:
            payload["output"] = output_summary
    return {
        "observation_id": observation.observation_id,
        "kind": observation.kind,
        "source": observation.source,
        "observed_at": observation.observed_at,
        "payload": payload,
    }


def agent_loop_result_to_dict(result: AgentLoopResult) -> dict[str, Any]:
    """Serialize a loop without Tool parameters, facts, credentials, or page text."""
    return {
        "schema_version": 1,
        "plan": {
            "plan_id": result.plan.plan_id,
            "objective": result.plan.objective,
            "created_at": result.plan.created_at,
            "steps": [
                {
                    "call_id": step.call_id,
                    "tool_name": step.tool_name,
                    "effect": step.effect.value,
                    "purpose": step.purpose,
                }
                for step in result.plan.steps
            ],
        },
        "status": result.status,
        "observations": [
            _observation_summary(observation)
            for observation in result.observations
        ],
        "rounds": [
            {
                "round_id": round_.round_id,
                "index": round_.index,
                "status": round_.status,
                "parallel_group_id": round_.parallel_group_id,
                "input_observation": _observation_summary(
                    round_.input_observation
                ),
                "thought": {
                    "thought_id": round_.thought.thought_id,
                    "strategy": round_.thought.strategy,
                    "summary": round_.thought.summary,
                    "plan": list(round_.thought.plan),
                    "selected_call_id": (
                        round_.thought.selected_action.call_id
                    ),
                    "selected_tool": (
                        round_.thought.selected_action.tool_name
                    ),
                    "reflection": round_.thought.reflection,
                    "self_criticism": round_.thought.self_criticism,
                    "memory_observation_ids": list(
                        round_.thought.memory_observation_ids
                    ),
                    "long_term_memory_hit_count": (
                        round_.thought.long_term_memory_hit_count
                    ),
                    "created_at": round_.thought.created_at,
                },
                "action": {
                    "call_id": round_.action.call_id,
                    "tool_name": round_.action.tool_name,
                    "effect": round_.action.effect.value,
                    "purpose": round_.action.purpose,
                },
                "policy_decision": (
                    {
                        "allowed": round_.policy_decision.allowed,
                        "code": round_.policy_decision.code,
                        "reason": round_.policy_decision.reason,
                        "policy": round_.policy_decision.policy,
                        "decided_at": round_.policy_decision.decided_at,
                    }
                    if round_.policy_decision is not None
                    else None
                ),
                "tool_result": _result_summary(round_.tool_result),
                "new_observation": _observation_summary(
                    round_.new_observation
                ),
                "memory_update": {
                    "tool_call_id": round_.memory_update.tool_call_id,
                    "observation_id": round_.memory_update.observation_id,
                    "short_term_updated": (
                        round_.memory_update.short_term_updated
                    ),
                    "long_term_updated": (
                        round_.memory_update.long_term_updated
                    ),
                    "long_term_namespace": (
                        round_.memory_update.long_term_namespace
                    ),
                    "summary": round_.memory_update.summary,
                    "updated_at": round_.memory_update.updated_at,
                },
            }
            for round_ in result.rounds
        ],
    }
