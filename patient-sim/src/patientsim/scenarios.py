"""Scenario definitions.

A scenario is data, not code: a persona, a goal, the things the patient must
probe for, and what counts as success. Adding a new test case means adding a
YAML file, which is what makes it cheap to iterate after listening to a call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import REPO_ROOT

SCENARIO_DIR = REPO_ROOT / "scenarios"


@dataclass
class Persona:
    name: str
    dob: str
    phone: str
    style: str = "calm and cooperative"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Scenario:
    id: str
    title: str
    category: str
    goal: str
    persona: Persona
    probes: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    watch_for: list[str] = field(default_factory=list)
    tactics: str = ""
    # "semantic" (polite turn-taking) or "server" (fixed VAD, allows idle timeout)
    turn_detection: str = "semantic"
    eagerness: str = "medium"
    idle_timeout_ms: int | None = None
    max_seconds: int | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Scenario":
        persona_raw = dict(raw.get("persona") or {})
        known = {"name", "dob", "phone", "style"}
        persona = Persona(
            name=persona_raw.get("name", "Alex Rivera"),
            dob=persona_raw.get("dob", "March 4, 1985"),
            phone=persona_raw.get("phone", "555-0142"),
            style=persona_raw.get("style", "calm and cooperative"),
            extra={k: v for k, v in persona_raw.items() if k not in known},
        )
        return cls(
            id=raw["id"],
            title=raw["title"],
            category=raw.get("category", "general"),
            goal=raw["goal"],
            persona=persona,
            probes=list(raw.get("probes") or []),
            success_criteria=list(raw.get("success_criteria") or []),
            watch_for=list(raw.get("watch_for") or []),
            tactics=raw.get("tactics", ""),
            turn_detection=raw.get("turn_detection", "semantic"),
            eagerness=raw.get("eagerness", "medium"),
            idle_timeout_ms=raw.get("idle_timeout_ms"),
            max_seconds=raw.get("max_seconds"),
        )


def load_all(directory: Path | None = None) -> list[Scenario]:
    directory = directory or SCENARIO_DIR
    files = sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml"))
    scenarios = []
    for path in files:
        raw = yaml.safe_load(path.read_text())
        if raw:
            scenarios.append(Scenario.from_dict(raw))
    if not scenarios:
        raise RuntimeError(f"No scenarios found in {directory}")
    return scenarios


def load_one(scenario_id: str, directory: Path | None = None) -> Scenario:
    for scenario in load_all(directory):
        if scenario.id == scenario_id:
            return scenario
    raise KeyError(f"Unknown scenario id: {scenario_id}")
