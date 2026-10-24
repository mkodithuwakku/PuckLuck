"""MoneyPuck bulk ingestion (SRS §3.1, Source 3).

Downloads the daily static season-summary CSVs (skaters + goalies) from
MoneyPuck and caches them as Parquet under
``data/cache/moneypuck/<season>/<dataset>.parquet``. The cached parquet is
a faithful mirror of the source CSV (all columns preserved); the
``*_METRIC_COLUMNS`` maps give downstream modules a stable contract for
the SRS target metrics without depending on MoneyPuck's naming.

The daemon refreshes at 05:30 ET, so a cache younger than
``CACHE_MAX_AGE_HOURS`` is considered fresh for the day. All network
access flows through an injectable ``Fetcher`` so unit tests never open
sockets (SRS §8).
"""

from __future__ import annotations

import io
import logging
import os
import time
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple, Union

import pandas as pd

log = logging.getLogger(__name__)

MONEYPUCK_BASE_URL = "https://moneypuck.com/moneypuck/playerData/seasonSummary"
DATASETS: Tuple[str, ...] = ("skaters", "goalies")
USER_AGENT = "PuckLuck/0.1 (local analytics pipeline)"
DEFAULT_CACHE_DIR = Path("data/cache/moneypuck")
CACHE_MAX_AGE_HOURS = 20.0
FETCH_TIMEOUT_SECONDS = 60.0

# Maps PuckLuck canonical metric names -> MoneyPuck CSV column names.
SKATER_METRIC_COLUMNS: Dict[str, str] = {
    "ixg": "I_F_xGoals",
    "icorsi": "I_F_shotAttempts",
    "ihdcf": "I_F_highDangerShots",
    "on_ice_xgf": "OnIce_F_xGoals",
    "on_ice_xga": "OnIce_A_xGoals",
    "icetime": "icetime",
}
GOALIE_METRIC_COLUMNS: Dict[str, str] = {
    "gsax": "goalsSavedAboveExpected",
    "save_pct": "savePercentage",
    "hd_shots_against": "highDangerShots",
    "xgoals_against": "xGoals",
}

Fetcher = Callable[[str], bytes]
PathLike = Union[str, Path]


@dataclass(frozen=True)
class CacheResult:
    """Outcome of refreshing one dataset into the parquet cache."""

    dataset: str
    path: Path
    downloaded: bool
    rows: int


def season_for_date(d: Optional[date] = None) -> int:
    """Return the MoneyPuck season key (the season's starting year).

    NHL seasons span Oct–Jun; the July–Sept offseason maps to the
    upcoming season so a July 2026 run requests ``2026`` (2026-27).
    """
    d = d or date.today()
    return d.year if d.month >= 7 else d.year - 1


def dataset_url(season: int, dataset: str) -> str:
    """Build the MoneyPuck CSV URL, e.g. .../2024/regular/skaters.csv."""
    if dataset not in DATASETS:
        raise ValueError(f"unknown dataset {dataset!r}; expected one of {DATASETS}")
    return f"{MONEYPUCK_BASE_URL}/{season}/regular/{dataset}.csv"


def fetch_csv(url: str) -> bytes:
    """HTTP GET ``url`` with the custom User-Agent required by SRS §3.1."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
        return resp.read()


def cache_path(cache_dir: PathLike, season: int, dataset: str) -> Path:
    if dataset not in DATASETS:
        raise ValueError(f"unknown dataset {dataset!r}; expected one of {DATASETS}")
    return Path(cache_dir) / str(season) / f"{dataset}.parquet"


def is_cache_fresh(path: PathLike, max_age_hours: float = CACHE_MAX_AGE_HOURS) -> bool:
    """True when the parquet file exists and is younger than the daily TTL."""
    p = Path(path)
    if not p.is_file():
        return False
    age_seconds = time.time() - p.stat().st_mtime
    return age_seconds < max_age_hours * 3600.0


def parse_csv(content: bytes) -> pd.DataFrame:
    """Parse a MoneyPuck CSV payload; raises on empty/malformed content."""
    df = pd.read_csv(io.BytesIO(content), low_memory=False)
    if df.empty:
        raise ValueError("MoneyPuck CSV parsed to zero rows")
    if "playerId" not in df.columns:
        raise ValueError(
            "MoneyPuck CSV missing required 'playerId' column "
            f"(got columns: {list(df.columns)[:10]}...)"
        )
    return df


def update_cache(
    cache_dir: PathLike = DEFAULT_CACHE_DIR,
    season: Optional[int] = None,
    fetcher: Fetcher = fetch_csv,
    datasets: Sequence[str] = DATASETS,
    max_age_hours: float = CACHE_MAX_AGE_HOURS,
) -> Dict[str, CacheResult]:
    """Ensure fresh parquet caches for each dataset; download only if stale.

    ``fetcher`` is the single seam where bytes enter the system — tests
    inject a stub, production uses :func:`fetch_csv`.
    """
    season = season if season is not None else season_for_date()
    results: Dict[str, CacheResult] = {}
    for dataset in datasets:
        path = cache_path(cache_dir, season, dataset)
        if is_cache_fresh(path, max_age_hours):
            log.info("%s cache fresh: %s", dataset, path)
            results[dataset] = CacheResult(dataset, path, False, -1)
            continue

        url = dataset_url(season, dataset)
        log.info("downloading %s", url)
        df = parse_csv(fetcher(url))

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)
        log.info("cached %d rows -> %s", len(df), path)
        results[dataset] = CacheResult(dataset, path, True, len(df))
    return results


def load_dataset(
    dataset: str,
    cache_dir: PathLike = DEFAULT_CACHE_DIR,
    season: Optional[int] = None,
) -> pd.DataFrame:
    """Load a cached dataset; raises FileNotFoundError if never fetched."""
    season = season if season is not None else season_for_date()
    path = cache_path(cache_dir, season, dataset)
    if not path.is_file():
        raise FileNotFoundError(
            f"no cached MoneyPuck {dataset} parquet at {path}; "
            "run update_cache() first"
        )
    return pd.read_parquet(path)


def load_skaters(
    cache_dir: PathLike = DEFAULT_CACHE_DIR, season: Optional[int] = None
) -> pd.DataFrame:
    return load_dataset("skaters", cache_dir, season)


def load_goalies(
    cache_dir: PathLike = DEFAULT_CACHE_DIR, season: Optional[int] = None
) -> pd.DataFrame:
    return load_dataset("goalies", cache_dir, season)
