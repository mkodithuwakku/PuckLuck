"""Tests for the MoneyPuck ingestion pipeline (ingestion/moneypuck.py).

All tests run offline: the fetcher seam is stubbed and conftest.py blocks
sockets, so any accidental live call raises NetworkAccessBlocked.
"""

from __future__ import annotations

import os
import socket
import time
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from ingestion import moneypuck

SKATERS_CSV = b"""season,playerId,name,team,position,situation,games_played,icetime,I_F_xGoals,I_F_shotAttempts,I_F_highDangerShots,OnIce_F_xGoals
2025,8478402,Connor McDavid,EDM,C,all,10,12000,8.2,150,40,95.5
2025,8479318,Auston Matthews,TOR,C,all,10,11900,7.9,140,35,88.1
"""

GOALIES_CSV = b"""season,playerId,name,team,situation,games_played,goalsSavedAboveExpected,savePercentage,highDangerShots,xGoals
2025,8479979,Stuart Skinner,EDM,all,10,4.5,0.912,80,28.4
"""


def fake_fetcher(url: str) -> bytes:
    if url.endswith("/skaters.csv"):
        return SKATERS_CSV
    if url.endswith("/goalies.csv"):
        return GOALIES_CSV
    raise AssertionError(f"unexpected url {url}")


def test_season_for_date() -> None:
    assert moneypuck.season_for_date(date(2026, 10, 23)) == 2026
    assert moneypuck.season_for_date(date(2027, 3, 1)) == 2026
    assert moneypuck.season_for_date(date(2026, 7, 1)) == 2026
    assert moneypuck.season_for_date(date(2026, 6, 30)) == 2025


def test_dataset_url() -> None:
    assert (
        moneypuck.dataset_url(2025, "skaters")
        == "https://moneypuck.com/moneypuck/playerData/seasonSummary/"
        "2025/regular/skaters.csv"
    )
    with pytest.raises(ValueError):
        moneypuck.dataset_url(2025, "coaches")


def test_update_cache_downloads_and_writes_parquet(tmp_path: Path) -> None:
    results = moneypuck.update_cache(tmp_path, season=2025, fetcher=fake_fetcher)
    assert set(results) == {"skaters", "goalies"}
    assert all(r.downloaded for r in results.values())

    skaters = moneypuck.load_skaters(tmp_path, season=2025)
    assert len(skaters) == 2
    assert moneypuck.SKATER_METRIC_COLUMNS["ixg"] in skaters.columns
    assert skaters.loc[skaters["playerId"] == 8478402, "I_F_xGoals"].iloc[
        0
    ] == pytest.approx(8.2)

    goalies = moneypuck.load_goalies(tmp_path, season=2025)
    assert moneypuck.GOALIE_METRIC_COLUMNS["gsax"] in goalies.columns


def test_update_cache_skips_fresh_files(tmp_path: Path) -> None:
    moneypuck.update_cache(tmp_path, season=2025, fetcher=fake_fetcher)

    def boom(url: str) -> bytes:
        raise AssertionError("fetcher must not run for fresh cache")

    results = moneypuck.update_cache(tmp_path, season=2025, fetcher=boom)
    assert not any(r.downloaded for r in results.values())


def test_update_cache_refreshes_stale_files(tmp_path: Path) -> None:
    moneypuck.update_cache(tmp_path, season=2025, fetcher=fake_fetcher)
    path = moneypuck.cache_path(tmp_path, 2025, "skaters")
    old = time.time() - 48 * 3600
    os.utime(path, (old, old))

    calls = []

    def counting_fetcher(url: str) -> bytes:
        calls.append(url)
        return fake_fetcher(url)

    results = moneypuck.update_cache(tmp_path, season=2025, fetcher=counting_fetcher)
    assert results["skaters"].downloaded is True
    assert results["goalies"].downloaded is False  # still fresh
    assert calls == [moneypuck.dataset_url(2025, "skaters")]


def test_parse_csv_rejects_bad_payloads() -> None:
    with pytest.raises(ValueError):
        moneypuck.parse_csv(b"")
    with pytest.raises(ValueError):
        moneypuck.parse_csv(b"a,b\n1,2\n")  # no playerId column


def test_load_dataset_missing_cache(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        moneypuck.load_dataset("skaters", tmp_path, season=2025)


def test_network_is_blocked() -> None:
    with pytest.raises(RuntimeError):
        socket.socket()
    with pytest.raises(RuntimeError):
        socket.create_connection(("example.com", 443))
