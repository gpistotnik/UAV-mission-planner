"""Integracijski test izhodov analize leta."""
from __future__ import annotations

import json
from pathlib import Path

from analysis.analyze_flight import analyze


def test_analysis_outputs_stay_inside_session(tmp_path: Path) -> None:
    session = tmp_path / "20260726-120000_test"
    session.mkdir()
    rows = [
        {"t": 1000.0, "type": "HEARTBEAT", "base_mode": 128},
        {
            "t": 1001.0,
            "type": "GLOBAL_POSITION_INT",
            "lat": 460500000,
            "lon": 145000000,
            "relative_alt": 2000,
            "alt": 302000,
        },
        {"t": 1002.0, "type": "HEARTBEAT", "base_mode": 0},
    ]
    (session / "telemetry.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    result = analyze(session, make_plots=False)

    assert result["session"] == session.name
    assert (session / "analysis" / "metrics.json").is_file()
    assert (session / "analysis" / "metrics.md").is_file()
    assert not (tmp_path / "analysis").exists()
