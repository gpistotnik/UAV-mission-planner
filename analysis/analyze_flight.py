#!/usr/bin/env python3
"""Poletna analiza enega leta --> metrike, tabela in grafi.

Vhod je mapa seje, kot jo ustvari zapisovalnik telemetrije
(:mod:`missions.services.telemetry_log`)::

    flightlogs/20260725-181203_misija-3/
        meta.json  plan.json  telemetry.jsonl  [captures.jsonl]

``captures.jsonl`` in mapa ``images/`` nastaneta neposredno v aktivni seji
na USB ključku prek ``scripts/camera_trigger.py``.

Izhod (v ``<seja>/analysis/``):

* ``metrics.json``   --- vse metrike strojno berljivo,
* ``metrics.md``     --- tabela za neposredno vstavitev v poglavje Rezultati,
* ``track.png``      --- nacrtovana in dejanska pot,
* ``crosstrack.png`` --- odstopanje od poti v odvisnosti od casa,
* ``altitude.png``   --- profil visine,
* ``capture.png``    --- histogram odstopanja lokacij posnetkov.

Uporaba::

    python3 analysis/analyze_flight.py flightlogs/20260725-181203_misija-3
    python3 analysis/analyze_flight.py flightlogs/*/ --no-plots
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import metrics as M  # noqa: E402

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _PLOTS = True
except Exception:  # pragma: no cover
    plt = None  # type: ignore
    _PLOTS = False


# ---------------------------------------------------------------------------
# Analiza ene seje
# ---------------------------------------------------------------------------
def analyze(session: Path, make_plots: bool = True,
            window_mode: str = "mission") -> dict[str, Any]:
    tel_path = session / "telemetry.jsonl"
    if not tel_path.is_file():
        raise FileNotFoundError(f"Ni {tel_path}")

    meta: dict[str, Any] = {}
    if (session / "meta.json").is_file():
        meta = json.loads((session / "meta.json").read_text(encoding="utf-8"))
    plan: dict[str, Any] = {}
    if (session / "plan.json").is_file():
        plan = json.loads((session / "plan.json").read_text(encoding="utf-8"))

    rows = M.load_jsonl(tel_path)
    captures = M.load_jsonl(session / "captures.jsonl")

    track_all = M.extract_track(rows)
    intervals = M.armed_intervals(rows)
    airborne = M.filter_airborne(track_all, intervals)

    # Metrike sledenja se racunajo na oknu izvajanja misije (med prvim in
    # zadnjim dosezenim itemom), ne na celotnem letu: vzlet in pristanek nista
    # sledenje progi in bi RMS sistematicno precenila. Ce misija ni bila
    # izvedena (manj kot dva dosezena itema), pademo nazaj na cel let v zraku.
    window = M.mission_window(rows) if window_mode == "mission" else None
    track = M.filter_window(airborne, window)
    if len(track) < 2:
        window = None
        track = airborne

    planned = M.planned_points_from_plan(plan)
    mission_hdr = (plan.get("mission") or {})
    planned_alt = float(mission_hdr.get("default_altitude_m") or 0) or None
    item_count = len(plan.get("items") or []) or None

    result: dict[str, Any] = {
        "session": session.name,
        "mission_id": meta.get("mission_id"),
        "mission_name": meta.get("mission_name") or mission_hdr.get("name"),
        "started_at": meta.get("started_at"),
        "duration_s": meta.get("duration_s"),
        "messages": meta.get("messages") or len(rows),
        "armed_intervals": [
            {"from": round(a, 1), "to": round(b, 1), "duration_s": round(b - a, 1)}
            for a, b in intervals
        ],
        "track_points_total": len(track_all),
        "track_points_airborne": len(airborne),
        "track_points_evaluated": len(track),
        "window": ("izvajanje misije" if window else "cel let v zraku"),
        "window_s": (None if window is None
                     else {"from": round(window[0], 1), "to": round(window[1], 1),
                           "duration_s": round(window[1] - window[0], 1)}),
        "planned_points": len(planned),
        "planned_length_m": round(M.planned_length_m(planned), 1),
        "flown_length_m": round(M.flown_length_m(track), 1),
    }

    if planned and track:
        result["trajectory_tracking"] = M.cross_track_stats(track, planned)
    else:
        result["trajectory_tracking"] = {
            "n": 0,
            "note": "Ni nacrta (plan.json) ali ni pozicij nad tlemi.",
        }

    if planned_alt:
        result["altitude"] = M.altitude_stats(track, planned_alt)

    result["telemetry"] = M.telemetry_rate_stats(rows)
    result["mission_execution"] = M.mission_completion(rows, item_count)
    result["capture"] = M.capture_accuracy(captures, planned)

    out_dir = session / "analysis"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "metrics.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "metrics.md").write_text(
        render_markdown(result), encoding="utf-8")

    if make_plots and _PLOTS:
        _plot_all(out_dir, track, planned, captures, planned_alt, result)
        result["plots"] = "ustvarjeni"
    elif make_plots:
        result["plots"] = "preskoceni (matplotlib ni namescen)"

    return result


# ---------------------------------------------------------------------------
# Tabela za nalogo
# ---------------------------------------------------------------------------
def _fmt(stats: dict[str, Any], key: str, unit: str = "") -> str:
    v = stats.get(key)
    if v is None:
        return "—"
    return f"{v}{unit}"


def render_markdown(r: dict[str, Any]) -> str:
    tt = r.get("trajectory_tracking", {})
    al = r.get("altitude", {})
    tl = r.get("telemetry", {})
    me = r.get("mission_execution", {})
    cp = r.get("capture", {})
    cd = cp.get("distance_to_planned", {}) if isinstance(cp, dict) else {}
    ct = cp.get("trigger_to_capture_ms", {}) if isinstance(cp, dict) else {}

    lines = [
        f"# Analiza leta --- {r.get('session')}",
        "",
        f"- Misija: **{r.get('mission_name') or '—'}** "
        f"(ID {r.get('mission_id') if r.get('mission_id') is not None else '—'})",
        f"- Zacetek zapisa: {r.get('started_at') or '—'}",
        f"- Trajanje zapisa: {r.get('duration_s') or '—'} s, "
        f"sporocil: {r.get('messages')}",
        f"- Nacrtovana pot: {r.get('planned_length_m')} m "
        f"({r.get('planned_points')} tock)",
        f"- Preletena pot: {r.get('flown_length_m')} m "
        f"({r.get('track_points_evaluated')} od "
        f"{r.get('track_points_airborne')} pozicij v zraku)",
        f"- Okno ovrednotenja: {r.get('window')}"
        + (f", {r['window_s']['duration_s']} s" if r.get("window_s") else ""),
        "",
        "## Metrike",
        "",
        "| Metrika | RMS | povprecje | p95 | maksimum | n |",
        "|---|---:|---:|---:|---:|---:|",
        f"| Odstopanje od trajektorije [m] | {_fmt(tt,'rms')} | {_fmt(tt,'mean')} "
        f"| {_fmt(tt,'p95')} | {_fmt(tt,'max')} | {_fmt(tt,'n')} |",
        f"| Odstopanje visine [m] | {_fmt(al,'rms')} | {_fmt(al,'mean')} "
        f"| {_fmt(al,'p95')} | {_fmt(al,'max')} | {_fmt(al,'n')} |",
        f"| Razmik telemetrije [ms] | {_fmt(tl,'rms')} | {_fmt(tl,'mean')} "
        f"| {_fmt(tl,'p95')} | {_fmt(tl,'max')} | {_fmt(tl,'n')} |",
        f"| Odstopanje lokacije posnetka [m] | {_fmt(cd,'rms')} | {_fmt(cd,'mean')} "
        f"| {_fmt(cd,'p95')} | {_fmt(cd,'max')} | {_fmt(cd,'n')} |",
        f"| Zakasnitev sprozitve [ms] | {_fmt(ct,'rms')} | {_fmt(ct,'mean')} "
        f"| {_fmt(ct,'p95')} | {_fmt(ct,'max')} | {_fmt(ct,'n')} |",
        "",
        "## Izvedba misije",
        "",
        f"- Frekvenca pozicije: {tl.get('hz') or '—'} Hz",
        f"- Dosezenih ukazov: {me.get('reached_count')} / "
        f"{me.get('item_count') or '—'}"
        + (f" ({me.get('completion_pct')} %)"
           if me.get("completion_pct") is not None else ""),
        f"- Posnetkov: {cp.get('captures', 0)} "
        f"(z datoteko: {cp.get('captured_files', 0)}, "
        f"brez pozicije: {cp.get('without_position', 0)})",
        f"- Sporocil o napakah (severity <= 3): {me.get('error_count', 0)}",
    ]
    for msg in (me.get("error_messages") or [])[:10]:
        lines.append(f"  - `{msg}`")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Grafi
# ---------------------------------------------------------------------------
def _plot_all(
    out_dir: Path,
    track: Sequence[M.TrackPoint],
    planned: Sequence[tuple[float, float]],
    captures: Sequence[dict[str, Any]],
    planned_alt: Optional[float],
    result: dict[str, Any],
) -> None:
    # 1. Pot: nacrtovana vs. dejanska, v metrih od prve nacrtovane tocke.
    if planned or track:
        origin = planned[0] if planned else (track[0].lat, track[0].lon)
        plane = M.LocalPlane(origin[0], origin[1])
        fig, ax = plt.subplots(figsize=(7, 7))
        if planned:
            pxy = [plane.to_xy(la, lo) for la, lo in planned]
            ax.plot([p[0] for p in pxy], [p[1] for p in pxy],
                    "--", color="#1f6feb", lw=1.2, label="nacrtovano")
            ax.plot([p[0] for p in pxy], [p[1] for p in pxy],
                    ".", color="#1f6feb", ms=3)
        if track:
            txy = [plane.to_xy(p.lat, p.lon) for p in track]
            ax.plot([p[0] for p in txy], [p[1] for p in txy],
                    "-", color="#dc2626", lw=1.0, label="dejansko")
        cxy = [plane.to_xy(c["lat"], c["lon"]) for c in captures
               if c.get("lat") is not None and c.get("lon") is not None]
        if cxy:
            ax.plot([p[0] for p in cxy], [p[1] for p in cxy],
                    "o", color="#15803d", ms=4, alpha=0.7, label="posnetki")
        ax.set_aspect("equal", "datalim")
        ax.set_xlabel("vzhod [m]")
        ax.set_ylabel("sever [m]")
        ax.set_title(f"Pot --- {result.get('session')}")
        ax.grid(alpha=0.3)
        ax.legend()
        fig.tight_layout()
        fig.savefig(out_dir / "track.png", dpi=140)
        plt.close(fig)

    # 2. Odstopanje od poti v casu.
    if planned and track:
        errs = M.cross_track_errors(track, planned)
        t0 = track[0].t
        ts = [p.t - t0 for p in track]
        fig, ax = plt.subplots(figsize=(9, 3.4))
        ax.plot(ts, errs, color="#dc2626", lw=0.9)
        tt = result.get("trajectory_tracking", {})
        if tt.get("rms") is not None:
            ax.axhline(tt["rms"], color="#1f6feb", ls="--", lw=1,
                       label=f"RMS = {tt['rms']} m")
            ax.legend()
        ax.set_xlabel("cas od zacetka [s]")
        ax.set_ylabel("odstopanje [m]")
        ax.set_title("Odstopanje od nacrtovane trajektorije")
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(out_dir / "crosstrack.png", dpi=140)
        plt.close(fig)

    # 3. Profil visine.
    if track:
        t0 = track[0].t
        alts = [(p.t - t0, p.alt_rel_m) for p in track if p.alt_rel_m is not None]
        if alts:
            fig, ax = plt.subplots(figsize=(9, 3.4))
            ax.plot([a[0] for a in alts], [a[1] for a in alts],
                    color="#0f766e", lw=1.0, label="dejansko")
            if planned_alt:
                ax.axhline(planned_alt, color="#1f6feb", ls="--", lw=1,
                           label=f"nacrtovano {planned_alt:g} m")
            ax.set_xlabel("cas od zacetka [s]")
            ax.set_ylabel("visina AGL [m]")
            ax.set_title("Profil visine")
            ax.grid(alpha=0.3)
            ax.legend()
            fig.tight_layout()
            fig.savefig(out_dir / "altitude.png", dpi=140)
            plt.close(fig)

    # 4. Histogram odstopanja lokacij posnetkov.
    if captures and planned:
        dists = [min(M.haversine_m(c["lat"], c["lon"], la, lo)
                     for la, lo in planned)
                 for c in captures
                 if c.get("lat") is not None and c.get("lon") is not None]
        if dists:
            fig, ax = plt.subplots(figsize=(6, 3.4))
            ax.hist(dists, bins=min(30, max(5, len(dists) // 3)),
                    color="#15803d", alpha=0.8)
            ax.set_xlabel("razdalja do najblizje nacrtovane tocke [m]")
            ax.set_ylabel("stevilo posnetkov")
            ax.set_title("Tocnost zajema")
            ax.grid(alpha=0.3)
            fig.tight_layout()
            fig.savefig(out_dir / "capture.png", dpi=140)
            plt.close(fig)


# ---------------------------------------------------------------------------
# Zbirna tabela vec letov
# ---------------------------------------------------------------------------
def render_campaign(results: list[dict[str, Any]]) -> str:
    lines = [
        "# Validacijska kampanja --- zbirna tabela",
        "",
        "| Let | Misija | Trajanje [s] | RMS odst. [m] | p95 [m] | "
        "Visina RMS [m] | Telem. [Hz] | Ukazi | Posnetki | Napake |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        tt = r.get("trajectory_tracking", {})
        al = r.get("altitude", {})
        tl = r.get("telemetry", {})
        me = r.get("mission_execution", {})
        cp = r.get("capture", {})
        lines.append(
            f"| {r.get('session')} | {r.get('mission_name') or '—'} "
            f"| {r.get('duration_s') or '—'} | {tt.get('rms', '—')} "
            f"| {tt.get('p95', '—')} | {al.get('rms', '—')} "
            f"| {tl.get('hz', '—')} "
            f"| {me.get('reached_count', '—')}/{me.get('item_count') or '—'} "
            f"| {cp.get('captures', 0)} | {me.get('error_count', 0)} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Analiza zapisov leta --> metrike, tabela, grafi.")
    ap.add_argument("sessions", nargs="+",
                    help="mape sej (flightlogs/<seja>)")
    ap.add_argument("--no-plots", dest="plots", action="store_false",
                    help="ne risi grafov")
    ap.add_argument("--campaign-out", default=None,
                    help="pot do zbirne tabele vec letov (markdown)")
    ap.add_argument("--window", choices=("mission", "airborne"),
                    default="mission",
                    help="okno ovrednotenja: 'mission' (med prvim in zadnjim "
                         "dosezenim itemom, privzeto) ali 'airborne' (cel let)")
    ap.set_defaults(plots=True)
    args = ap.parse_args(argv)

    results: list[dict[str, Any]] = []
    for raw in args.sessions:
        session = Path(raw)
        if not session.is_dir():
            print(f"preskakujem (ni mapa): {session}", file=sys.stderr)
            continue
        try:
            r = analyze(session, make_plots=args.plots,
                        window_mode=args.window)
        except FileNotFoundError as exc:
            print(f"preskakujem {session.name}: {exc}", file=sys.stderr)
            continue
        results.append(r)
        tt = r.get("trajectory_tracking", {})
        print(f"[{session.name}] RMS odstopanja: {tt.get('rms', '—')} m, "
              f"pozicij: {r['track_points_evaluated']}/{r['track_points_airborne']}, "
              f"posnetkov: {r.get('capture', {}).get('captures', 0)} "
              f"-> {session / 'analysis'}")

    if not results:
        print("Nobena seja ni bila analizirana.", file=sys.stderr)
        return 1

    if args.campaign_out:
        Path(args.campaign_out).write_text(
            render_campaign(results), encoding="utf-8")
        print(f"zbirna tabela: {args.campaign_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
