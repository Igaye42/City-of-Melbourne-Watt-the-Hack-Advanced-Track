"""Simulation runner — canonical engine driver.

Three entry points, ordered by abstraction:

  :func:`run_strategy`
      Highest-level. Drives the full lifecycle (plan once, replan on
      alerts, step every tick) against a :class:`ResolvedStrategy`.
      All four harnesses — local CLI, reference-tier scripts, FastAPI
      playground, admin eval container — call this.

  :func:`run_simulation`
      Lower-level. Calls a bare ``controller(view) -> action`` callable
      every step. No plan/replan. Kept for tests and tiny tooling.

  :func:`engine.step`
      Raw physics. Use directly only if you need step-level control
      of state mutation between ticks.

Anything that calls ``engine.step`` in a loop on its own is a candidate
for reuse — that pattern is exactly what ``run_strategy`` exists to
prevent re-implementing.
"""

from __future__ import annotations

import traceback
from typing import Any, Callable

from watt_the_hack.constants import DEFAULT_STEPS, DT_HOURS
from watt_the_hack.controllers.rule_based import rule_based_controller

from watt_the_hack.engine.base_engine import SimulationEngine
from watt_the_hack.engine.engine import Engine
from watt_the_hack.metrics.metrics import Metrics
from watt_the_hack.simulation.strategy import (
    ResolvedStrategy,
    ZERO_ACTION,
    resolve_strategy,
)


# Callback signatures used by run_strategy.
#   on_step(step_idx, controller_view, action, outputs, state_after)
#   on_error(phase, step_idx, exception)   where phase ∈ {"plan","replan","step"}
StepCallback = Callable[[int, dict, dict, dict, dict], None]
ErrorCallback = Callable[[str, int, BaseException], None]


def run_simulation(
    engine: SimulationEngine | None = None,
    controller=rule_based_controller,
    initial_state: dict | None = None,
    steps: int = DEFAULT_STEPS,
) -> dict:
    """Run a headless simulation against a bare ``controller(view) -> action`` callable.

    Kept for backwards compatibility and quick tests. New code should
    prefer :func:`run_strategy`, which also handles plan/replan.
    """
    engine = engine or Engine()
    metrics = Metrics(dt_hours=getattr(engine.config, "dt_hours", DT_HOURS))
    if initial_state is None:
        raise ValueError("initial_state must be provided.")
    state = initial_state

    states = []
    outputs_history = []

    for _ in range(steps):
        # Controllers ONLY see the public state surface. Engine.step
        # continues to read from the full state dict for forecast bias,
        # cyber-attack windows, and profile lookups.
        action = controller(engine.controller_view(state))
        state, outputs = engine.step(state, action)
        metrics.update(state, outputs)
        states.append(dict(state))
        outputs_history.append(dict(outputs))

    return {
        "final_state": state,
        "states": states,
        "outputs": outputs_history,
        "metrics": metrics.summary(),
    }


def run_strategy(
    engine: Engine,
    state: dict,
    strategy: ResolvedStrategy | Any,
    steps: int,
    *,
    on_step: StepCallback | None = None,
    on_error: ErrorCallback | None = None,
    metrics: Metrics | None = None,
) -> dict:
    """Drive a full controller lifecycle against the engine for ``steps`` ticks.

    Calls ``strategy.plan(view)`` once before the loop if present,
    ``strategy.replan(view, alerts)`` on any step where
    ``state["alerts"]`` is non-empty, and ``strategy.step(view)`` every
    tick. Controllers always see :meth:`Engine.controller_view`, never
    the raw state.

    Per-tick observability:

    * ``on_step`` — called after every successful ``engine.step``. Use
      it to collect per-step rows (CSV writers, SSE streamers, DB
      inserters). Receives the controller view that produced this
      step's action, the action itself, the engine outputs, and the
      post-physics state.
    * ``on_error`` — called when ``plan`` / ``replan`` / ``step`` raise.
      ``step`` errors fall back to :data:`ZERO_ACTION` so the loop
      still completes; ``plan`` / ``replan`` errors leave the existing
      agent_plan in place. Use this hook to log to your context's
      logger of choice. If omitted, errors are silently absorbed
      apart from being counted via the ``controller_errors`` field
      in the returned summary.

    Returns a dict with ``metrics`` (summary), ``cost_breakdown``
    (aggregated component costs over the whole run),
    ``controller_errors`` (count of step-level failures), and
    ``final_state``.
    """
    if not isinstance(strategy, ResolvedStrategy):
        strategy = resolve_strategy(strategy)

    if metrics is None:
        metrics = Metrics(dt_hours=engine.config.dt_hours)
    breakdown: dict[str, float] = {}
    controller_errors = 0

    agent_plan: dict[str, Any] = dict(state.get("agent_plan") or {})
    if strategy.plan is not None:
        try:
            update = strategy.plan(Engine.controller_view(state))
            agent_plan = _merge_plan(agent_plan, update)
        except Exception as exc:  # noqa: BLE001
            _handle_error(on_error, "plan", -1, exc)
    state["agent_plan"] = agent_plan

    for i in range(steps):
        view = Engine.controller_view(state)
        alerts = view.get("alerts") or []
        if alerts and strategy.replan is not None:
            try:
                update = strategy.replan(view, alerts)
                agent_plan = _merge_plan(agent_plan, update)
                state["agent_plan"] = agent_plan
            except Exception as exc:  # noqa: BLE001
                _handle_error(on_error, "replan", i, exc)

        view = Engine.controller_view(state)
        try:
            action = strategy.step(view)
            if not isinstance(action, dict):
                raise TypeError(
                    f"step() must return dict, got {type(action).__name__}"
                )
        except Exception as exc:  # noqa: BLE001
            controller_errors += 1
            _handle_error(on_error, "step", i, exc)
            action = dict(ZERO_ACTION)

        # Fold any agent_plan the step returned into the persistent plan, so
        # every agent_plan key the engine reads — whether from state
        # (emergency_exemption, phishing bait) or from the action
        # (containment_ack, anomaly_ack) — sees it regardless of where the
        # controller set it. This makes the channel forgiving: returning an
        # agent_plan from step() "just works", same as setting it in
        # plan()/replan(). Keys accumulate and persist across steps.
        step_plan = action.get("agent_plan")
        if isinstance(step_plan, dict):
            agent_plan = {**agent_plan, **step_plan}
            state["agent_plan"] = agent_plan

        state, outputs = engine.step(state, action)
        metrics.update(state, outputs)
        for k, v in outputs.get("cost_breakdown", {}).items():
            breakdown[k] = breakdown.get(k, 0.0) + float(v)

        if on_step is not None:
            try:
                on_step(i, view, action, outputs, state)
            except Exception as exc:  # noqa: BLE001
                # Observability callback failures must not break the run.
                _handle_error(on_error, "on_step", i, exc)

    summary = metrics.summary()
    summary["controller_errors"] = controller_errors
    return {
        "metrics": summary,
        "cost_breakdown": breakdown,
        "controller_errors": controller_errors,
        "final_state": state,
    }


def _merge_plan(existing: dict, update: Any) -> dict:
    """Apply a plan/replan return value to the rolling agent_plan dict.

    Accepts either ``{"agent_plan": {...}}`` (preferred — explicit
    namespace) or a flat dict (legacy — merged directly). Anything else
    is ignored.
    """
    if not isinstance(update, dict):
        return existing
    if "agent_plan" in update and isinstance(update["agent_plan"], dict):
        return {**existing, **update["agent_plan"]}
    return {**existing, **update}


def _handle_error(cb: ErrorCallback | None, phase: str, step: int, exc: BaseException) -> None:
    if cb is None:
        return
    try:
        cb(phase, step, exc)
    except Exception:  # noqa: BLE001
        # An error handler that itself raises must not take down the run.
        traceback.print_exc()


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    from watt_the_hack.data_loaders.scenarios import (
        find_scenario_by_id,
        list_scenarios,
        load_scenario,
    )

    parser = argparse.ArgumentParser(
        description="Run a headless Watt The Hack simulation."
    )
    parser.add_argument(
        "--scenario",
        type=str,
        default=None,
        help="Scenario id (e.g. 'datacenter_burst') OR path to a scenario JSON file.",
    )
    parser.add_argument(
        "--list-scenarios",
        action="store_true",
        help="List available scenarios and exit.",
    )
    args = parser.parse_args()

    if args.list_scenarios:
        for s in list_scenarios():
            print(f"  {s['id']:30s} {s['pool']:10s} {s['title']}")
        raise SystemExit(0)

    initial_state = None
    scenario_label = "default"
    if args.scenario:
        # Accept either an id ("datacenter_burst") or a path
        if Path(args.scenario).is_file():
            scenario_path = Path(args.scenario)
        else:
            scenario_path = find_scenario_by_id(args.scenario)
            if scenario_path is None:
                raise SystemExit(
                    f"Could not find scenario {args.scenario!r} as either a path or an id. "
                    f"Try --list-scenarios."
                )
        spec, initial_state = load_scenario(scenario_path)
        scenario_label = spec.get("title", spec.get("id", str(scenario_path)))

    if not args.scenario:
        parser.error(
            "--scenario is required. Use --list-scenarios to see available options."
        )

    print(f"Running scenario: {scenario_label}")
    result = run_simulation(initial_state=initial_state)
    print(result["metrics"])
