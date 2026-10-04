/** Globalno stanje načrtovalnika in obvestilni mehanizem (preprost pub/sub). */
import type { MissionPayload, MissionElement, WaypointElement, MapElement } from "./types";

type Listener = () => void;
type ToolMode = "select" | "add_wp" | "add_map";

export class PlannerState {
  mission: MissionPayload;
  selectedId: number | null = null;
  toolMode: ToolMode = "select";
  dirty = false;

  private listeners: Set<Listener> = new Set();
  private tempId = -1;

  constructor(mission: MissionPayload) {
    this.mission = mission;
  }

  /** Začasen negativen ID za nove (še neshranjene) elemente. */
  newTempId(): number { return this.tempId--; }

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  notify(): void { this.listeners.forEach((f) => f()); }

  markDirty(): void {
    this.dirty = true;
    this.notify();
  }

  setToolMode(m: ToolMode): void {
    this.toolMode = m;
    this.notify();
  }

  select(id: number | null): void {
    this.selectedId = id;
    this.notify();
  }

  selectedElement(): MissionElement | undefined {
    return this.mission.elements.find((e) => e.id === this.selectedId);
  }

  addElement(el: MissionElement): void {
    el.id = el.id ?? this.newTempId();
    el.order = this.mission.elements.length + 1;
    this.mission.elements.push(el);
    this.selectedId = el.id;
    this.markDirty();
  }

  removeElement(id: number): void {
    const i = this.mission.elements.findIndex((e) => e.id === id);
    if (i < 0) return;
    this.mission.elements.splice(i, 1);
    this.mission.elements.forEach((e, idx) => (e.order = idx + 1));
    if (this.selectedId === id) this.selectedId = null;
    this.markDirty();
  }

  moveElement(id: number, dir: -1 | 1): void {
    const i = this.mission.elements.findIndex((e) => e.id === id);
    const j = i + dir;
    if (i < 0 || j < 0 || j >= this.mission.elements.length) return;
    [this.mission.elements[i], this.mission.elements[j]] =
      [this.mission.elements[j], this.mission.elements[i]];
    this.mission.elements.forEach((e, idx) => (e.order = idx + 1));
    this.markDirty();
  }
}

export type { ToolMode };
