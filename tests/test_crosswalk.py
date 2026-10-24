"""Tests for the Yahoo <-> NHL identity crosswalk (ingestion/crosswalk.py)."""

from __future__ import annotations

import sqlite3

import pytest

import db
from ingestion.crosswalk import (
    CrosswalkResolver,
    YahooIdentity,
    normalize_name,
    position_group,
)


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = db.init_db(":memory:")
    db.upsert_player(c, 8478402, "Connor McDavid", "C")
    db.upsert_player(c, 8480069, "Sebastian Aho", "D")  # NYI defenseman
    db.upsert_player(c, 8478427, "Sebastian Aho", "LW")  # CAR forward
    db.upsert_player(c, 8481214, "Alexander Ovechkin", "LW")
    db.upsert_player(c, 8480796, "André Burakovsky", "LW")
    db.upsert_player(c, 8479979, "Stuart Skinner", "G")
    c.commit()
    yield c
    c.close()


def test_normalize_name() -> None:
    assert normalize_name("Connor McDavid") == "connor mcdavid"
    assert normalize_name("Tomáš Hertl") == "tomas hertl"
    assert normalize_name("Martin Jones Jr.") == "martin jones"
    assert normalize_name("Pierre-Luc Dubois") == "pierreluc dubois"
    assert normalize_name("  Elias   Lindholm ") == "elias lindholm"


def test_position_group() -> None:
    assert position_group("G") == "G"
    assert position_group("D") == "D"
    assert position_group("C") == "F"
    assert position_group("LW") == "F"
    assert position_group(None) is None


def test_exact_match(conn: sqlite3.Connection) -> None:
    r = CrosswalkResolver(conn)
    pid, method, score, _ = r.resolve("Connor McDavid", "C")
    assert (pid, method) == (8478402, "exact")
    assert score == 1.0


def test_name_collision_disambiguated_by_position(
    conn: sqlite3.Connection,
) -> None:
    r = CrosswalkResolver(conn)
    pid, method, _, candidates = r.resolve("Sebastian Aho", "D")
    assert pid == 8480069
    assert method == "position"
    assert set(candidates) == {8480069, 8478427}

    pid, method, _, _ = r.resolve("Sebastian Aho", "LW")
    assert (pid, method) == (8478427, "position")


def test_accented_yahoo_name_matches(conn: sqlite3.Connection) -> None:
    r = CrosswalkResolver(conn)
    pid, method, _, _ = r.resolve("Andre Burakovsky", "LW")
    assert pid == 8480796
    assert method in {"exact", "fuzzy"}


def test_first_name_alias(conn: sqlite3.Connection) -> None:
    r = CrosswalkResolver(conn)
    pid, method, _, _ = r.resolve("Alex Ovechkin", "LW")
    assert (pid, method) == (8481214, "alias")


def test_fuzzy_match(conn: sqlite3.Connection) -> None:
    r = CrosswalkResolver(conn)
    pid, method, score, _ = r.resolve("Conor McDavid", "C")  # typo
    assert pid == 8478402
    assert method in {"alias", "fuzzy"}
    assert score >= 0.88


def test_unmatched(conn: sqlite3.Connection) -> None:
    r = CrosswalkResolver(conn)
    pid, method, _, _ = r.resolve("Wayne Gretzky", "C")
    assert pid is None and method == "unmatched"


def test_link_writes_yahoo_id(conn: sqlite3.Connection) -> None:
    r = CrosswalkResolver(conn)
    results = r.link_all(
        [
            YahooIdentity("449.p.6743", "Connor McDavid", "C"),
            YahooIdentity("449.p.9999", "Nobody Real", "RW"),
        ]
    )
    assert results[0].player_id == 8478402
    assert results[1].player_id is None

    row = conn.execute(
        "SELECT yahoo_player_id FROM dim_players WHERE player_id = 8478402"
    ).fetchone()
    assert row["yahoo_player_id"] == "449.p.6743"


def test_relink_moves_yahoo_key(conn: sqlite3.Connection) -> None:
    r = CrosswalkResolver(conn)
    r.link(YahooIdentity("449.p.1", "Connor McDavid", "C"))
    db.upsert_player(conn, 9999999, "Connor McDavid", "C")
    # Force re-key to a different warehouse row; old link must be cleared.
    r2 = CrosswalkResolver(conn)
    r2.attach("449.p.1", 9999999)
    old = conn.execute(
        "SELECT yahoo_player_id FROM dim_players WHERE player_id = 8478402"
    ).fetchone()
    new = conn.execute(
        "SELECT yahoo_player_id FROM dim_players WHERE player_id = 9999999"
    ).fetchone()
    assert old["yahoo_player_id"] is None
    assert new["yahoo_player_id"] == "449.p.1"
