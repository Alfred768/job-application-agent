"""Persistent logical JobApplicationAgent session helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hello_agents.core.contracts import Observation


class DeterministicSessionLLM:
    """No-network LLM adapter used when rehydrating operational stages."""

    provider = "deterministic"

    def invoke(self, _messages: list[dict[str, str]], **_kwargs: Any) -> str:
        return ""

    def stream_invoke(
        self,
        _messages: list[dict[str, str]],
        **_kwargs: Any,
    ):
        if False:
            yield ""


def observation_from_mapping(
    raw: Mapping[str, Any] | None,
    *,
    default_kind: str,
    default_source: str,
    payload: Mapping[str, Any] | None = None,
) -> Observation:
    """Restore a privacy-safe serialized Observation without inventing an ID."""
    source = raw if isinstance(raw, Mapping) else {}
    observation_id = str(source.get("observation_id") or "").strip()
    observed_at = str(source.get("observed_at") or "").strip()
    source_payload = source.get("payload")
    restored_payload = (
        dict(payload)
        if payload is not None
        else (
            dict(source_payload)
            if isinstance(source_payload, Mapping)
            else {}
        )
    )
    values = {
        "kind": str(source.get("kind") or default_kind),
        "source": str(source.get("source") or default_source),
        "payload": restored_payload,
    }
    if observation_id:
        values["observation_id"] = observation_id
    if observed_at:
        values["observed_at"] = observed_at
    return Observation(**values)


def latest_trajectory_observation(
    package_dir: str | Path | None,
    *,
    exclude_stages: set[str] | frozenset[str] = frozenset(),
) -> Observation | None:
    """Return the last serialized Observation for one application trajectory."""
    if not package_dir:
        return None
    path = Path(package_dir) / "agent-trajectory.json"
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    stages = payload.get("stages")
    if not isinstance(stages, Mapping):
        return None
    candidates: list[Mapping[str, Any]] = []
    for stage_name in (
        "evaluation",
        "repair",
        "recovery",
        "execution",
        "preparation",
    ):
        if stage_name in exclude_stages:
            continue
        _collect_loop_observations(stages.get(stage_name), candidates)
        if candidates:
            break
    if not candidates:
        return None
    latest = candidates[-1]
    return observation_from_mapping(
        latest,
        default_kind="application_handoff",
        default_source="agent_trajectory",
    )


def _collect_loop_observations(
    value: Any,
    target: list[Mapping[str, Any]],
) -> None:
    if isinstance(value, Mapping):
        for key in ("agent_loop", "agent_loops", "runtime_steps", "loop"):
            nested = value.get(key)
            if nested is not None:
                _collect_loop_observations(nested, target)
        observations = value.get("observations")
        if isinstance(observations, list):
            target.extend(
                item for item in observations if isinstance(item, Mapping)
            )
        return
    if isinstance(value, list):
        for item in value:
            _collect_loop_observations(item, target)
