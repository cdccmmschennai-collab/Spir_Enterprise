"""
services/planning_plant.py
--------------------------
The user-selected Planning Plant — the single source of truth for the
PLANNING PLANT / PLANNING PLANT DESCRIPTION output columns.

The user picks one of the controlled plants before extraction (frontend
dropdown). Every extraction entry point validates the submitted pair here
and passes the result down to the pipeline, which stamps it onto every row.
Workbook content never decides or overrides the plant.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Controlled Planning Plant master — the only values that may be submitted or output.
PLANT_MASTER: dict[str, str] = {
    "2600": "Ras Laffan Offshore Fields",
    "2300": "Dukhan Fields",
    "2500": "Refinery Mesaieed",
    "1200": "Qatar Petroleum - Doha",
    "2800": "Mesaieed Industrial City",
    "2900": "North Field Alpha",
    "2400": "NGL Mesaieed",
    "2700": "Ras Laffan Industrial City",
    "3000": "RL Cooling Water Systems",
}


class InvalidPlanningPlant(ValueError):
    """The submitted Planning Plant is missing or not in the controlled master."""


@dataclass(frozen=True)
class PlanningPlant:
    code: str
    description: str


def resolve_planning_plant(
    code: Optional[str],
    description: Optional[str],
) -> PlanningPlant:
    """
    Validate a submitted (planning_plant, planning_plant_description) pair.

    Both are required. The code must be one of the controlled codes and the
    description must be that code's controlled description exactly (surrounding
    whitespace ignored). Raises InvalidPlanningPlant otherwise — there is no
    default and no fallback.
    """
    code = (code or "").strip()
    description = (description or "").strip()
    if not code:
        raise InvalidPlanningPlant("planning_plant is required — select a Planning Plant")
    if code not in PLANT_MASTER:
        raise InvalidPlanningPlant(f"planning_plant '{code}' is not a valid Planning Plant")
    if not description:
        raise InvalidPlanningPlant("planning_plant_description is required")
    if description != PLANT_MASTER[code]:
        raise InvalidPlanningPlant(
            f"planning_plant_description '{description}' does not match Planning Plant {code}"
        )
    return PlanningPlant(code=code, description=PLANT_MASTER[code])
