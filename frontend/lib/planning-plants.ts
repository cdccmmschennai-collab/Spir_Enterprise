// Controlled Planning Plant list. The user picks one before extraction; the
// description always comes from this table and is never typed by the user.

export interface PlanningPlant {
  code: string;
  description: string;
}

export const PLANNING_PLANTS: readonly PlanningPlant[] = [
  { code: "2600", description: "Ras Laffan Offshore Fields" },
  { code: "2300", description: "Dukhan Fields" },
  { code: "2500", description: "Refinery Mesaieed" },
  { code: "1200", description: "Qatar Petroleum - Doha" },
  { code: "2800", description: "Mesaieed Industrial City" },
  { code: "2900", description: "North Field Alpha" },
  { code: "2400", description: "NGL Mesaieed" },
  { code: "2700", description: "Ras Laffan Industrial City" },
  { code: "3000", description: "RL Cooling Water Systems" },
];

export function findPlanningPlant(code: string | null | undefined): PlanningPlant | null {
  return PLANNING_PLANTS.find((p) => p.code === code) ?? null;
}

export const planningPlantLabel = (p: PlanningPlant) => `${p.code} - ${p.description}`;

// Case-insensitive search over the code, the description and the full label
// ("24" → 2400, "ngl" → NGL Mesaieed, "ras laffan" → both Ras Laffan plants).
export function filterPlanningPlants(query: string): PlanningPlant[] {
  const q = query.trim().replace(/\s+/g, " ").toLowerCase();
  if (!q) return [...PLANNING_PLANTS];
  return PLANNING_PLANTS.filter(
    (p) =>
      p.code.includes(q) ||
      p.description.toLowerCase().includes(q) ||
      planningPlantLabel(p).toLowerCase().includes(q)
  );
}

// Request fields for the extraction endpoints (form fields or JSON keys).
export function planningPlantFields(plant: PlanningPlant): Record<string, string> {
  return { planning_plant: plant.code, planning_plant_description: plant.description };
}
