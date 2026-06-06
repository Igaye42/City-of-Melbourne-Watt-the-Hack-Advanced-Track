"""Scenario boot helper.

Encapsulates the three-step scenario boot sequence that every harness
performs identically:

    1. find + parse the scenario JSON
    2. build the Engine, applying per-scenario config overrides
    3. inject the initial forecast into state["forecast"] / state["alerts"]

This is the canonical entry point — all four harnesses (local CLI,
reference-tier scripts, FastAPI playground, admin eval container) call
:func:`boot_scenario` rather than reimplementing the sequence.

Why each step matters:

* **Config overrides** — some scenarios tune penalty rates, battery
  capacity, or carbon intensity. Skipping the overrides silently runs
  the scenario under default physics, which mis-scores controllers.
* **Initial forecast injection** — :meth:`Engine.step` writes the
  *next* forecast at the end of each step, but step 0 has no prior
  step to seed it. Without an explicit call to
  :meth:`Engine.add_forecast_to_state`, ``state["forecast"]`` is
  missing on step 0 and ``state["alerts"]`` is missing for any event
  whose ``at_step == 0``. The cloud judge calls this; every local
  harness must too, or scores drift.
"""

from __future__ import annotations

from pathlib import Path

from watt_the_hack.data_loaders.scenarios import (
    config_overrides,
    find_scenario_by_id,
    load_scenario,
)
from watt_the_hack.engine.engine import Engine, SimulationConfig


class ScenarioNotFound(ValueError):
    """Raised when a scenario id can't be resolved."""


def boot_scenario(
    scenario_id_or_path: str | Path,
    *,
    engine: Engine | None = None,
) -> tuple[Engine, dict, dict]:
    """Resolve a scenario, build the engine, inject the initial forecast.

    Returns ``(engine, initial_state, spec)``.

    ``scenario_id_or_path`` accepts either a scenario id (e.g. ``"duck_curve"``)
    or a path to a JSON file. The id form is preferred — it's what the
    cloud judge and the frontend use.

    If ``engine`` is provided it is used as-is (handy for the FastAPI
    server's shared engine instance). Otherwise a fresh ``Engine`` is
    constructed, applying the scenario's ``config_overrides`` if any.
    """
    spec_path = _resolve(scenario_id_or_path)
    spec, state = load_scenario(spec_path)

    if engine is None:
        overrides = config_overrides(spec)
        engine = (
            Engine(config=SimulationConfig(**overrides)) if overrides else Engine()
        )

    engine.add_forecast_to_state(state)
    return engine, state, spec


def scenario_steps(state: dict) -> int:
    """Return the canonical step count for a booted scenario.

    Reads ``state["_profiles_full"]`` (the engine-internal full series),
    not the legacy public ``state["profiles"]`` — the latter is no longer
    populated by ``load_scenario`` and reading it returns 0 on every
    modern scenario.
    """
    profiles = state.get("_profiles_full") or state.get("profiles") or {}
    return len(profiles.get("demand") or [])


def _resolve(scenario_id_or_path: str | Path) -> Path:
    p = Path(scenario_id_or_path)
    if p.is_file():
        return p
    spec_path = find_scenario_by_id(str(scenario_id_or_path))
    if spec_path is None:
        raise ScenarioNotFound(
            f"Unknown scenario: {scenario_id_or_path!r}. "
            "Pass a known scenario id or an existing JSON path."
        )
    return spec_path
