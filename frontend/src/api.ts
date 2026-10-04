/** Klici Django REST endpointov. */
import type { MissionPayload, MapElement } from "./types";

async function jpost(url: string, csrf: string, body: unknown): Promise<unknown> {
  const res = await fetch(url, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${url} -> ${res.status}: ${await res.text()}`);
  return res.json();
}

export async function saveMission(
  apiUrl: string, csrf: string, mission: MissionPayload,
): Promise<MissionPayload> {
  return jpost(apiUrl, csrf, mission) as Promise<MissionPayload>;
}

export interface GridPlanResult {
  waypoints_lonlat: number[][];
  stats: {
    gsd_cm_per_px: number; footprint_across_m: number; footprint_along_m: number;
    line_spacing_m: number; trigger_distance_m: number;
    total_distance_m: number; num_lines: number; estimated_images: number;
  };
}

export async function planMapPath(
  csrf: string, droneId: number, mapElement: MapElement,
): Promise<GridPlanResult> {
  return jpost("/api/grid/plan/", csrf, {
    drone_id: droneId, map_element: mapElement,
  }) as Promise<GridPlanResult>;
}
