/** Skupni tipi med frontend in backend. */

export type AltitudeMode = "AGL" | "AMSL";
export type HeadingMode = "AUTO" | "FIXED" | "POI" | "NEXT_WP";
export type FinishAction = "HOVER" | "RTH" | "LAND";
export type ElementType = "WP" | "MAP";
export type PathStyle = "STRAIGHT" | "CURVED";
export type ActionType = "NONE" | "PHOTO" | "VIDEO_START" | "VIDEO_STOP" | "CAPTURE_BURST";
export type MapPattern = "GRID" | "CROSSHATCH";

export interface DronePayload {
  id: number;
  display_name: string;
  max_speed_ms: number;
  max_altitude_m: number;
  sensor_width_mm: number;
  sensor_height_mm: number;
  focal_length_mm: number;
  image_width_px: number;
  image_height_px: number;
}

export interface WaypointElement {
  id?: number | null;
  order: number;
  element_type: "WP";
  name: string;
  altitude_m: number;
  speed_ms: number | null;
  lat: number;
  lon: number;
  heading_mode: HeadingMode;
  heading_deg: number | null;
  gimbal_pitch_deg: number;
  path_style: PathStyle;
  action_type: ActionType;
  hover_time_s: number;
}

export interface MapElement {
  id?: number | null;
  order: number;
  element_type: "MAP";
  name: string;
  altitude_m: number;
  speed_ms: number | null;
  polygon_geojson: { type: "Polygon"; coordinates: number[][][] };
  pattern: MapPattern;
  front_overlap_pct: number;
  side_overlap_pct: number;
  track_angle_deg: number;
}

export type MissionElement = WaypointElement | MapElement;

export interface MissionPayload {
  id: number | null;
  name: string;
  description: string;
  drone_id: number;
  home_lat: number | null;
  home_lon: number | null;
  home_alt_amsl_m: number | null;
  altitude_mode: AltitudeMode;
  default_altitude_m: number;
  default_speed_ms: number;
  finish_action: FinishAction;
  elements: MissionElement[];
}

declare global {
  interface Window {
    PLANNER_BOOTSTRAP: {
      // Planner mode obvezno:
      mission?: MissionPayload;
      drones?: DronePayload[];
      apiUrl?: string;
      // Vrsta strani:
      mode?: "planner" | "dashboard" | "testflight" | "magcal" | "settings" | "gallery";
      cameraUrl?: string;
      autoStart?: boolean;
      serialDevice?: string;
      serialBaud?: number;
      canControl?: boolean;
      // Skupno:
      csrfToken: string;
    };
  }
}
