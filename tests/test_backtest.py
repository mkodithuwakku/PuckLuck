"""Phase 8 unit tests: walk-forward backtester, self-tuner, and cloud Pages workflows."""

from __future__ import annotations

import json
import plistlib
from pathlib import Path

import db
from models.backtest import (
    CANDIDATE_BASE_RATES,
    CANDIDATE_EWMA_ALPHAS,
    CANDIDATE_RIDGE_LAMBDAS,
    TunedModelParams,
    load_model_params,
    run_walk_forward_backtest,
    save_model_params,
)
from scripts.refresh import (
    MODEL_HEALTH_ALLOWLIST,
    main as refresh_main,
    seed_demo_warehouse,
    validate_public_report,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_walk_forward_backtest_evaluates_strictly_out_of_sample_and_suggests_tuning(
    tmp_path: Path,
) -> None:
    conn = db.init_db(tmp_path / "backtest.sqlite3")
    try:
        seed_demo_warehouse(conn, as_of_date="2026-10-23")
        report1 = run_walk_forward_backtest(
            conn, as_of_date="2026-10-23", window_days=14
        )

        assert report1.window_start == "2026-10-09"
        assert report1.window_end == "2026-10-23"
        assert report1.samples_evaluated > 0
        assert report1.games_evaluated > 0
        assert set(report1.component_mae.keys()) == {
            "SOG",
            "iXG",
            "G",
            "A",
            "BLK",
            "HIT",
        }
        assert report1.fantasy_point_mae > 0.0
        assert 0.0 <= report1.totals_brier_score <= 1.0
        assert report1.recommended_params.ewma_alpha in CANDIDATE_EWMA_ALPHAS
        assert report1.recommended_params.ridge_l2_penalty in CANDIDATE_RIDGE_LAMBDAS
        assert report1.recommended_params.poisson_base_rate in CANDIDATE_BASE_RATES
        assert len(report1.suggestions) >= 1
        assert all(isinstance(s, str) and s.strip() for s in report1.suggestions)

        # Temporal integrity (SRS §8): poisoning rows on or after as_of_date
        # must have zero effect on walk-forward evaluation.
        conn.execute(
            "INSERT OR REPLACE INTO fact_skater_games "
            "(player_id, game_id, game_date, season, team_id, opponent_id, is_home, "
            "toi_seconds, pp_toi_seconds, goals, assists, shots_on_goal, "
            "blocks, hits, ixg, icorsi, ihdcf, rest_days) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                8478402,
                2026102399,
                "2026-10-23",
                "20262027",
                25,
                21,
                1,
                1500,
                300,
                99,
                99,
                99,
                99,
                99,
                50.0,
                99.0,
                50.0,
                2,
            ),
        )
        conn.commit()

        report2 = run_walk_forward_backtest(
            conn, as_of_date="2026-10-23", window_days=14
        )
        assert report2.samples_evaluated == report1.samples_evaluated
        assert report2.games_evaluated == report1.games_evaluated
        assert report2.fantasy_point_mae == report1.fantasy_point_mae
        assert report2.totals_brier_score == report1.totals_brier_score
        assert report2.component_mae == report1.component_mae
    finally:
        conn.close()


def test_save_and_load_model_params_and_refresh_backtest_flag(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "missing_params.json"
    default_params = load_model_params(missing_path)
    assert default_params == TunedModelParams(
        ewma_alpha=0.08, ridge_l2_penalty=1.0, poisson_base_rate=3.05
    )

    custom = TunedModelParams(
        ewma_alpha=0.12,
        ridge_l2_penalty=2.5,
        poisson_base_rate=3.10,
        updated_at="2026-10-23T12:00:00+00:00",
    )
    params_path = tmp_path / "model_params.json"
    save_model_params(custom, params_path)
    loaded = load_model_params(params_path)
    assert loaded == custom

    db_path = tmp_path / "refresh_bt.sqlite3"
    out_path = tmp_path / "report.json"
    tuned_path = tmp_path / "tuned_params.json"
    refresh_main(
        [
            "--db-path",
            str(db_path),
            "--output",
            str(out_path),
            "--params-path",
            str(tuned_path),
            "--date",
            "2026-10-23",
            "--demo",
            "--backtest",
        ]
    )

    assert tuned_path.is_file()
    assert out_path.is_file()
    payload = json.loads(out_path.read_text())
    validate_public_report(payload)
    assert "model_health" in payload
    mh = payload["model_health"]
    assert set(mh.keys()) <= set(MODEL_HEALTH_ALLOWLIST)
    assert mh["samples_evaluated"] > 0
    assert isinstance(mh["suggestions"], list)
    assert len(mh["suggestions"]) >= 1


def test_github_actions_pages_workflows_and_wake_catchup_plist() -> None:
    refresh_wf = (REPO_ROOT / ".github/workflows/refresh.yml").read_text()
    backtest_wf = (REPO_ROOT / ".github/workflows/backtest.yml").read_text()

    assert "branches: [main]" in refresh_wf
    assert "cron: '30 12,16,21,23 * * *'" in refresh_wf
    assert "workflow_dispatch:" in refresh_wf
    assert "pages: write" in refresh_wf
    assert "id-token: write" in refresh_wf
    assert "actions/cache@v4" in refresh_wf
    assert (
        "python -m scripts.refresh --live || python -m scripts.refresh --demo"
        in refresh_wf
    )
    assert "cp dist/report.json _site/report.json" in refresh_wf
    assert "! find _site -name '.env*' -o -name '*.sqlite3' | grep -q ." in refresh_wf
    assert "actions/configure-pages@v5" in refresh_wf
    assert "actions/upload-pages-artifact@v3" in refresh_wf
    assert "actions/deploy-pages@v4" in refresh_wf

    assert "cron: '0 11 1,15 * *'" in backtest_wf
    assert "workflow_dispatch:" in backtest_wf
    assert (
        "python -m scripts.refresh --live --backtest || python -m scripts.refresh --demo --backtest"
        in backtest_wf
    )
    assert "actions/upload-pages-artifact@v3" in backtest_wf
    assert "actions/deploy-pages@v4" in backtest_wf

    plist_bytes = (REPO_ROOT / "scripts/com.puckluck.refresh.plist").read_bytes()
    plist_data = plistlib.loads(plist_bytes)
    assert plist_data["RunAtLoad"] is True
    assert plist_data["StartInterval"] == 3600
