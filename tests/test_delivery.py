"""Tests for the delivery layer (scripts/refresh.py, api/server.py, ops files)."""

from __future__ import annotations

import json
import plistlib
from pathlib import Path

import pytest
from fastapi.middleware.cors import CORSMiddleware

import scripts.refresh as refresh
from api.server import (
    ALLOWED_CORS_ORIGINS,
    create_app,
    validate_bind_host,
)
from fantasy.solver import CandidatePlayer, evaluate_swap
from scripts.refresh import (
    build_public_report,
    sanitize_public_report,
    validate_public_report,
    write_report_json,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _valid_report() -> dict:
    return build_public_report(
        generated_at="2026-10-23T12:00:00+00:00",
        team_summary={
            "team_name": "McDavid Fan Club",
            "team_initials": "MF",
            "league_name": "Dynasty",
            "adds_used": 1,
            "adds_limit": 4,
            "matchup_score": "84.2 - 71.0",
        },
        roster=[
            {
                "player_id": 8478402,
                "display_name": "Connor McDavid",
                "position": "C",
                "selected_slot": "C",
                "team_abbr": "EDM",
                "projected_fp": 8.4,
                "shots_on_goal": 4,
            }
        ],
        games=[
            {
                "game_id": 1,
                "start_time_utc": "2026-10-24T01:00:00Z",
                "home_abbr": "EDM",
                "away_abbr": "COL",
                "expected_total": 6.7,
                "line": 6.5,
                "prob_over": 0.56,
                "is_positive_ev": True,
            }
        ],
    )


def test_sanitize_and_validate_public_report_strips_unknown_and_blocks_secrets() -> (
    None
):
    raw = {
        "generated_at": "2026-10-23T12:00:00Z",
        "internal_db_path": "/Users/mkodi/data/puckluck.sqlite3",
        "refresh_token": "abc123",
        "team_summary": {
            "team_name": "Club",
            "client_secret": "leak",
            "debug_flag": True,
        },
        "roster": [
            {
                "player_id": 1,
                "display_name": "P",
                "oauth_access_token": "tok",
                "drop_reason": "internal",
            }
        ],
        "extra_section": {"anything": 1},
    }
    clean = sanitize_public_report(raw)
    assert "internal_db_path" not in clean
    assert "refresh_token" not in clean
    assert "extra_section" not in clean
    assert "client_secret" not in clean["team_summary"]
    assert "debug_flag" not in clean["team_summary"]
    assert "oauth_access_token" not in clean["roster"][0]
    assert "drop_reason" not in clean["roster"][0]

    valid = _valid_report()
    validate_public_report(valid)  # baseline: must not raise

    for bad_key in ("refresh_token", "client_secret", "api_key"):
        poisoned = dict(valid)
        poisoned["team_summary"] = {**valid["team_summary"], bad_key: "x"}
        with pytest.raises(ValueError):
            validate_public_report(poisoned)

    for bad_value in (
        "/Users/mkodi/secret.sqlite3",
        "/home/user/.env",
        "Bearer abcdef",
        "Basic Y2lkOnNlY3JldA==",
    ):
        poisoned = dict(valid)
        poisoned["team_summary"] = {
            **valid["team_summary"],
            "team_name": bad_value,
        }
        with pytest.raises(ValueError):
            validate_public_report(poisoned)

    with pytest.raises(ValueError):
        validate_public_report({"team_summary": {}})  # missing generated_at
    with pytest.raises(ValueError):
        validate_public_report({**valid, "generated_at": ""})
    with pytest.raises(ValueError):
        validate_public_report({**valid, "surprise": 1})


def test_build_public_report_from_swap_evaluation_and_write_json(
    tmp_path: Path,
) -> None:
    streamer = CandidatePlayer(
        player_id=9001,
        display_name="Streamer Winger",
        fp_tonight=4.2,
        fp_remaining_week=(2.0,),
        fp_ros_weekly_avg=10.0,
    )
    drop = CandidatePlayer(
        player_id=8480803,
        display_name="Evan Bouchard",
        fp_tonight=0.0,
        fp_remaining_week=(3.0,),
        fp_ros_weekly_avg=14.0,
    )
    evaluation = evaluate_swap(streamer, drop, horizon_weeks=2.5)

    report = build_public_report(
        generated_at="2026-10-23T12:00:00+00:00",
        recommendation=evaluation,
    )
    rec = report["recommendation"]
    assert rec["action"] == "HOLD_ASSET"
    assert rec["streamer_name"] == "Streamer Winger"
    assert rec["drop_name"] == "Evan Bouchard"
    assert rec["delta_value"] == pytest.approx(-6.8)
    assert rec["horizon_weeks"] == pytest.approx(2.5)
    # Nested dataclass internals never leak into the public payload.
    assert "streamer" not in rec and "drop_candidate" not in rec

    path = write_report_json(report, tmp_path / "report.json")
    loaded = json.loads(path.read_text())
    assert loaded == report
    assert not (tmp_path / "report.json.tmp").exists()


def test_write_report_json_refuses_invalid(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_report_json({"bad": True}, tmp_path / "report.json")
    assert not (tmp_path / "report.json").exists()


def test_validate_bind_host_enforces_loopback() -> None:
    assert validate_bind_host("127.0.0.1") == "127.0.0.1"
    assert validate_bind_host("localhost") == "localhost"
    assert validate_bind_host("::1") == "::1"
    for bad in ("0.0.0.0", "192.168.1.10", "8.8.8.8", ""):
        with pytest.raises(ValueError):
            validate_bind_host(bad)


def test_fastapi_health_and_report_endpoints(tmp_path: Path) -> None:
    app = create_app(report_provider=lambda: _valid_report())

    cors = [m for m in app.user_middleware if m.cls is CORSMiddleware]
    assert len(cors) == 1
    assert cors[0].kwargs["allow_origins"] == ALLOWED_CORS_ORIGINS
    assert all(
        "127.0.0.1" in o or "localhost" in o for o in cors[0].kwargs["allow_origins"]
    )
    assert cors[0].kwargs["allow_methods"] == ["GET"]

    routes = {r.path: r.endpoint for r in app.routes if hasattr(r, "endpoint")}
    health = routes["/api/health"]()
    assert health == {"status": "ok", "host": "127.0.0.1", "port": 8877}

    payload = routes["/api/report"]()
    validate_public_report(payload)  # served payload is allowlisted
    assert payload["generated_at"] == "2026-10-23T12:00:00+00:00"

    # Missing file -> synthesized populated demo report.
    app2 = create_app(report_path=tmp_path / "absent.json")
    routes2 = {r.path: r.endpoint for r in app2.routes if hasattr(r, "endpoint")}
    payload2 = routes2["/api/report"]()
    validate_public_report(payload2)


def test_launchd_plist_and_publish_script_contracts() -> None:
    plist_path = REPO_ROOT / "scripts" / "com.puckluck.refresh.plist"
    parsed = plistlib.loads(plist_path.read_bytes())
    assert parsed["Label"] == "com.puckluck.refresh"
    assert parsed["StartCalendarInterval"] == {"Hour": 6, "Minute": 0}
    args = parsed["ProgramArguments"]
    assert args[-2:] == ["-m", "scripts.refresh"]
    assert parsed["StandardOutPath"] == parsed["StandardErrorPath"]

    script = (REPO_ROOT / "scripts" / "publish_gh_pages.sh").read_text()
    assert "set -euo pipefail" in script
    assert "dist/report.json" in script
    # Security boundary present in the script itself.
    assert ".env" in script and "sqlite3" in script


def test_end_to_end_demo_pipeline_populates_all_sections(tmp_path: Path) -> None:
    out = refresh.main(
        [
            "--db-path",
            str(tmp_path / "wh.sqlite3"),
            "--output",
            str(tmp_path / "report.json"),
            "--demo",
        ]
    )
    report = json.loads(Path(out).read_text())
    validate_public_report(report)

    assert len(report["roster"]) >= 4
    assert any(
        r.get("position") == "G" and r.get("goalie_status") == "CONFIRMED"
        for r in report["roster"]
    )
    assert len(report["free_agents"]) >= 2
    assert all(fa["ixg_per60_ewma"] > 0 for fa in report["free_agents"])
    assert report["recommendation"]["action"] in {
        "RECOMMEND_SWAP",
        "HOLD_ASSET",
    }
    assert len(report["games"]) >= 2
    assert all(g["expected_total"] > 0 and g["line"] > 0 for g in report["games"])


def test_fastapi_serves_report_json_and_mounts_web_static(
    tmp_path: Path,
) -> None:
    app = create_app(report_provider=lambda: _valid_report())
    routes = {r.path: r.endpoint for r in app.routes if hasattr(r, "endpoint")}

    api_payload = routes["/api/report"]()
    json_payload = routes["/report.json"]()
    assert api_payload == json_payload
    validate_public_report(json_payload)

    # StaticFiles mount for the PWA at "/" when web/ exists.
    assert (REPO_ROOT / "web").is_dir()
    mounts = [r for r in app.routes if hasattr(r, "app")]
    assert mounts, "expected a StaticFiles mount for web/"

    # Missing file -> populated demo report (not an empty shell).
    app2 = create_app(report_path=tmp_path / "absent.json")
    routes2 = {r.path: r.endpoint for r in app2.routes if hasattr(r, "endpoint")}
    demo = routes2["/report.json"]()
    validate_public_report(demo)
    assert len(demo["roster"]) >= 4
    assert len(demo["games"]) >= 2
