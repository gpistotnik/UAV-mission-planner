"""Primerjava zapisanih vx/vy z odvodom lat/lon (GLOBAL_POSITION_INT).

Za vsako sejo poroča RMSE in korelacijo med:
  - vx (N, m/s) in dN/dt iz lat
  - vy (E, m/s) in dE/dt iz lon
ter razpone. Uporablja mirne korake (|delta pos| < 5 m).
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

R = 6371000.0


def load_gpi(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("type") == "GLOBAL_POSITION_INT":
                out.append(r)
    return out


def window(rows: list[dict], path: Path) -> list[dict]:
    """Analizno okno MIR, sicer cel GPI nad 1 m AGL."""
    all_rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                all_rows.append(json.loads(line))
    mir = [r for r in all_rows if r.get("type") == "MISSION_ITEM_REACHED"]
    if len(mir) >= 2:
        t0, t1 = mir[0]["t"], mir[-1]["t"]
        return [r for r in rows if t0 <= r["t"] <= t1]
    return [
        r
        for r in rows
        if r.get("relative_alt") is not None and r["relative_alt"] >= 1000
    ]


def stats(gpi: list[dict]) -> dict | None:
    if len(gpi) < 10:
        return None
    lat0 = gpi[0]["lat"] / 1e7 * math.pi / 180
    dn_err, de_err = [], []
    vx_vals, vy_vals = [], []
    dn_vals, de_vals = [], []
    n_jump = 0
    for a, b in zip(gpi, gpi[1:]):
        dt = b["t"] - a["t"]
        if dt <= 0:
            continue
        dlat = (b["lat"] - a["lat"]) / 1e7 * math.pi / 180
        dlon = (b["lon"] - a["lon"]) / 1e7 * math.pi / 180
        dN = dlat * R
        dE = dlon * R * math.cos(lat0)
        if abs(dN) > 5 or abs(dE) > 5:
            n_jump += 1
            continue
        dNdt, dEdt = dN / dt, dE / dt
        vx = 0.5 * (a["vx"] + b["vx"]) / 100.0
        vy = 0.5 * (a["vy"] + b["vy"]) / 100.0
        dn_err.append(dNdt - vx)
        de_err.append(dEdt - vy)
        vx_vals.append(vx)
        vy_vals.append(vy)
        dn_vals.append(dNdt)
        de_vals.append(dEdt)
    if len(dn_err) < 5:
        return None

    def rms(xs):
        return math.sqrt(sum(x * x for x in xs) / len(xs))

    def corr(xs, ys):
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        denx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        deny = math.sqrt(sum((y - my) ** 2 for y in ys))
        if denx < 1e-9 or deny < 1e-9:
            return float("nan")
        return num / (denx * deny)

    return {
        "n": len(dn_err),
        "jumps": n_jump,
        "rms_N": rms(dn_err),
        "rms_E": rms(de_err),
        "corr_N": corr(dn_vals, vx_vals),
        "corr_E": corr(de_vals, vy_vals),
        "vx": (min(vx_vals), max(vx_vals)),
        "vy": (min(vy_vals), max(vy_vals)),
        "dN": (min(dn_vals), max(dn_vals)),
        "dE": (min(de_vals), max(de_vals)),
        "mean_abs_dE": sum(abs(x) for x in de_vals) / len(de_vals),
        "mean_abs_vy": sum(abs(x) for x in vy_vals) / len(vy_vals),
    }


def analyze_dir(root: Path, label: str) -> None:
    sessions = sorted(
        p for p in root.iterdir() if p.is_dir() and (p / "telemetry.jsonl").exists()
    )
    print(f"\n=== {label}: {root} ({len(sessions)} sej) ===")
    rows_out = []
    for s in sessions:
        gpi = load_gpi(s / "telemetry.jsonl")
        w = window(gpi, s / "telemetry.jsonl")
        st = stats(w)
        if not st:
            print(f"  {s.name}: PREMALO")
            continue
        ok = st["rms_N"] < 0.5 and st["rms_E"] < 0.5 and st["corr_E"] > 0.5
        flag = "OK" if ok else "MISMATCH"
        rows_out.append((flag, s.name, st))
        print(
            f"  {flag:8} {s.name}: "
            f"rmsN={st['rms_N']:.2f} rmsE={st['rms_E']:.2f} "
            f"corrN={st['corr_N']:.2f} corrE={st['corr_E']:.2f} "
            f"|vy|={st['mean_abs_vy']:.2f} |dE|={st['mean_abs_dE']:.2f} "
            f"vx=[{st['vx'][0]:.2f},{st['vx'][1]:.2f}] "
            f"vy=[{st['vy'][0]:.2f},{st['vy'][1]:.2f}] "
            f"dE=[{st['dE'][0]:.2f},{st['dE'][1]:.2f}] "
            f"jumps={st['jumps']}"
        )
    n_ok = sum(1 for f, _, _ in rows_out if f == "OK")
    n_bad = sum(1 for f, _, _ in rows_out if f == "MISMATCH")
    print(f"  -> OK={n_ok} MISMATCH={n_bad}")


def main() -> int:
    roots = [
        (
            Path(
                r"C:\Users\PC\Documents\Claude\Projects\magistrska"
                r"\mission_planner\flightlogs"
            ),
            "kampanja 29.8. (mission_planner)",
        ),
        (
            Path(r"C:\Users\PC\Downloads\UAV-mission-planner\flightlogs"),
            "lokalni flightlogs (20.10.)",
        ),
    ]
    for root, label in roots:
        if root.is_dir():
            analyze_dir(root, label)
        else:
            print(f"MANJKA: {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
