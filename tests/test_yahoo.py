"""Tests for Yahoo Fantasy ingestion (ingestion/yahoo.py).

All transports are stubbed callables; conftest.py socket blocking makes
any accidental live call fail loudly.
"""

from __future__ import annotations

import base64
import sqlite3
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

import pytest

import db
from ingestion import yahoo
from ingestion.yahoo import OAuthTokens, YahooClient

SETTINGS_PAYLOAD: Dict[str, Any] = {
    "fantasy_content": {
        "league": [
            {
                "league_key": "449.l.12345",
                "name": "PuckLuck Dynasty",
                "scoring_type": "point",
            },
            {
                "settings": [
                    {
                        "scoring_type": "point",
                        "max_weekly_adds": "4",
                        "stat_modifiers": {
                            "stats": [
                                {"stat": {"stat_id": "1", "value": "3"}},
                                {"stat": {"stat_id": "2", "value": "2"}},
                                {"stat": {"stat_id": "5", "value": "0.5"}},
                                {"stat": {"stat_id": "8", "value": "1"}},
                                {"stat": {"stat_id": "9", "value": "1"}},
                                {"stat": {"stat_id": "14", "value": "0.4"}},
                                {"stat": {"stat_id": "16", "value": "0.5"}},
                                {"stat": {"stat_id": "31", "value": "0.5"}},
                                {"stat": {"stat_id": "32", "value": "0.5"}},
                                {"stat": {"stat_id": "19", "value": "4"}},
                                {"stat": {"stat_id": "22", "value": "-1"}},
                                {"stat": {"stat_id": "25", "value": "0.2"}},
                                {"stat": {"stat_id": "27", "value": "3"}},
                            ]
                        },
                        "roster_positions": [
                            {
                                "roster_position": {
                                    "position": "C",
                                    "count": "3",
                                }
                            },
                            {
                                "roster_position": {
                                    "position": "LW",
                                    "count": "2",
                                }
                            },
                            {
                                "roster_position": {
                                    "position": "RW",
                                    "count": "2",
                                }
                            },
                            {
                                "roster_position": {
                                    "position": "D",
                                    "count": "4",
                                }
                            },
                            {
                                "roster_position": {
                                    "position": "G",
                                    "count": "2",
                                }
                            },
                            {
                                "roster_position": {
                                    "position": "BN",
                                    "count": "4",
                                }
                            },
                            {
                                "roster_position": {
                                    "position": "IR+",
                                    "count": "2",
                                }
                            },
                        ],
                    }
                ]
            },
        ]
    }
}

ROSTER_PAYLOAD: Dict[str, Any] = {
    "fantasy_content": {
        "team": [
            {"team_key": "449.l.12345.t.7", "name": "McDavid Fan Club"},
            {
                "roster": {
                    "0": {
                        "players": {
                            "0": {
                                "player": [
                                    [
                                        {
                                            "player_key": "449.p.6743",
                                            "player_id": "6743",
                                            "name": {
                                                "full": "Connor McDavid",
                                                "first": "Connor",
                                                "last": "McDavid",
                                            },
                                            "editorial_team_abbr": "Edm",
                                            "display_position": "C",
                                            "eligible_positions": [
                                                {"position": "C"},
                                                {"position": "UTIL"},
                                            ],
                                        },
                                        {
                                            "selected_position": [
                                                {
                                                    "coverage_type": "date",
                                                    "date": "2026-10-23",
                                                    "position": "C",
                                                }
                                            ]
                                        },
                                    ]
                                ]
                            },
                            "1": {
                                "player": [
                                    [
                                        {
                                            "player_key": "449.p.7526",
                                            "name": {"full": "Evan Bouchard"},
                                            "editorial_team_abbr": "Edm",
                                            "display_position": "D",
                                            "eligible_positions": [{"position": "D"}],
                                        },
                                        {
                                            "selected_position": [
                                                {
                                                    "coverage_type": "date",
                                                    "date": "2026-10-23",
                                                    "position": "BN",
                                                }
                                            ]
                                        },
                                    ]
                                ]
                            },
                            "2": {
                                "player": [
                                    [
                                        {
                                            "player_key": "449.p.9100",
                                            "name": {"full": "Injured Winger"},
                                            "editorial_team_abbr": "Fla",
                                            "display_position": "LW",
                                            "status": "IR",
                                            "eligible_positions": [
                                                {"position": "LW"},
                                                {"position": "IR+"},
                                            ],
                                        },
                                        {
                                            "selected_position": [
                                                {
                                                    "coverage_type": "date",
                                                    "date": "2026-10-23",
                                                    "position": "IR+",
                                                }
                                            ]
                                        },
                                    ]
                                ]
                            },
                            "count": 3,
                        }
                    }
                }
            },
        ]
    }
}

FREE_AGENT_PAYLOAD: Dict[str, Any] = {
    "fantasy_content": {
        "league": [
            {"league_key": "449.l.12345"},
            {
                "players": {
                    "0": {
                        "player": [
                            [
                                {
                                    "player_key": "449.p.4141",
                                    "name": {"full": "Rookie Streamer"},
                                    "editorial_team_abbr": "Tor",
                                    "display_position": "RW",
                                    "eligible_positions": [
                                        {"position": "RW"},
                                        {"position": "UTIL"},
                                    ],
                                    "ownership": [{"ownership_type": "freeagents"}],
                                    "percent_owned": [
                                        {
                                            "coverage_type": "week",
                                            "week": "3",
                                            "value": "12.5",
                                        }
                                    ],
                                }
                            ]
                        ]
                    },
                    "1": {
                        "player": [
                            [
                                {
                                    "player_key": "449.p.5555",
                                    "name": {"full": "DTD Callup"},
                                    "editorial_team_abbr": "Nyr",
                                    "display_position": "D",
                                    "status": "DTD",
                                    "eligible_positions": [{"position": "D"}],
                                    "percent_owned": [
                                        {
                                            "coverage_type": "week",
                                            "value": "4",
                                        }
                                    ],
                                }
                            ]
                        ]
                    },
                    "count": 2,
                }
            },
        ]
    }
}


def _client(
    *,
    token_transport=None,
    api_transport=None,
    env_path=None,
    clock=lambda: 1_700_000_000.0,
) -> YahooClient:
    kwargs: Dict[str, Any] = {"clock": clock}
    if token_transport is not None:
        kwargs["token_transport"] = token_transport
    if api_transport is not None:
        kwargs["api_transport"] = api_transport
    if env_path is not None:
        kwargs["env_path"] = env_path
    return YahooClient("cid", "secret", "rt0", **kwargs)


def _fresh_client(api_transport) -> YahooClient:
    """Client whose access token is already valid (no refresh needed)."""
    client = _client(api_transport=api_transport)
    client._tokens = OAuthTokens(
        access_token="valid-token",
        refresh_token="rt0",
        expires_at=client._clock() + 3600,
    )
    return client


def test_build_authorization_url() -> None:
    url = yahoo.build_authorization_url("my-client-id")
    parsed = urlparse(url)
    assert parsed.netloc == "api.login.yahoo.com"
    params = parse_qs(parsed.query)
    assert params["client_id"] == ["my-client-id"]
    assert params["redirect_uri"] == ["oob"]
    assert params["response_type"] == ["code"]


def test_token_refresh_and_env_persistence(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# comment line\n" "YAHOO_CLIENT_ID=cid\n" "YAHOO_REFRESH_TOKEN=rt0\n"
    )
    calls = []

    def token_transport(url, headers, form_data):
        calls.append((url, headers, form_data))
        assert url == yahoo.YAHOO_TOKEN_URL
        expected = base64.b64encode(b"cid:secret").decode("ascii")
        assert headers["Authorization"] == f"Basic {expected}"
        assert form_data["grant_type"] == "refresh_token"
        assert form_data["refresh_token"] == "rt0"
        return {
            "access_token": "fresh-access",
            "refresh_token": "rt1",
            "expires_in": 3600,
            "token_type": "bearer",
        }

    client = _client(token_transport=token_transport, env_path=env_path)
    # No token yet -> ensure_token must refresh exactly once.
    token = client.ensure_token()
    assert token == "fresh-access"
    assert len(calls) == 1

    env_text = env_path.read_text()
    assert "# comment line" in env_text
    assert "YAHOO_CLIENT_ID=cid" in env_text
    assert "YAHOO_REFRESH_TOKEN=rt1" in env_text
    assert "rt0" not in env_text
    assert not (tmp_path / ".env.tmp").exists()  # atomic swap cleaned up


def test_ensure_token_reuses_valid_token() -> None:
    def boom(url, headers, form_data):
        raise AssertionError("token_transport must not be called")

    client = _client(token_transport=boom)
    client._tokens = OAuthTokens(
        access_token="still-valid",
        refresh_token="rt0",
        expires_at=client._clock() + 3600,
    )
    assert client.ensure_token() == "still-valid"


def test_parse_league_settings() -> None:
    settings = yahoo.parse_league_settings(SETTINGS_PAYLOAD)
    assert settings.league_key == "449.l.12345"
    assert settings.league_name == "PuckLuck Dynasty"
    assert settings.stat_weights == {
        "G": 3.0,
        "A": 2.0,
        "PIM": 0.5,
        "PPP": 1.0,
        "SHP": 1.0,
        "SOG": 0.4,
        "FW": 0.5,
        "HIT": 0.5,
        "BLK": 0.5,
        "W": 4.0,
        "GA": -1.0,
        "SV": 0.2,
        "SHO": 3.0,
    }
    assert settings.roster_positions == {
        "C": 3,
        "LW": 2,
        "RW": 2,
        "D": 4,
        "G": 2,
        "BN": 4,
        "IR+": 2,
    }
    assert settings.max_weekly_adds == 4


def test_fetch_roster_and_sync_to_db() -> None:
    def api_transport(url, headers):
        assert headers["Authorization"] == "Bearer valid-token"
        assert url.startswith(f"{yahoo.YAHOO_API_BASE}/team/449.l.12345.t.7/roster")
        assert "format=json" in url
        return ROSTER_PAYLOAD

    client = _fresh_client(api_transport)
    players = client.fetch_roster("449.l.12345.t.7")

    assert len(players) == 3
    mcdavid = players[0]
    assert mcdavid.yahoo_player_id == "449.p.6743"
    assert mcdavid.display_name == "Connor McDavid"
    assert mcdavid.primary_position == "C"
    assert mcdavid.selected_slot == "C"
    assert mcdavid.team_abbr == "EDM"
    assert players[1].selected_slot == "BN"
    assert players[2].selected_slot == "IR+"
    assert players[2].status == "IR"

    conn = db.init_db(":memory:")
    db.upsert_player(conn, 8478402, "Connor McDavid", "C")
    db.upsert_player(conn, 8478017, "Evan Bouchard", "D")
    conn.commit()

    synced = client.sync_players_to_db(conn, players)
    assert synced[0].player_id == 8478402
    assert synced[1].player_id == 8478017
    assert synced[2].player_id is None  # "Injured Winger" not in warehouse
    row = conn.execute(
        "SELECT yahoo_player_id FROM dim_players WHERE player_id = 8478402"
    ).fetchone()
    assert row["yahoo_player_id"] == "449.p.6743"
    conn.close()


def test_fetch_free_agents() -> None:
    def api_transport(url, headers):
        assert url.startswith(
            f"{yahoo.YAHOO_API_BASE}/league/449.l.12345/players;status=FA"
        )
        assert "start=0" in url and "count=25" in url and "out=ownership" in url
        return FREE_AGENT_PAYLOAD

    client = _fresh_client(api_transport)
    players = client.fetch_free_agents("449.l.12345")

    assert len(players) == 2
    assert players[0].display_name == "Rookie Streamer"
    assert players[0].percent_owned == pytest.approx(12.5)
    assert players[0].selected_slot is None
    assert players[0].team_abbr == "TOR"
    assert players[1].status == "DTD"
    assert players[1].percent_owned == pytest.approx(4.0)


def test_persist_refresh_token_rejects_newline(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("YAHOO_CLIENT_ID=cid\n")
    with pytest.raises(ValueError):
        yahoo.persist_refresh_token(env_path, "tok\nMALICIOUS=1")
    # File untouched after rejection.
    assert "YAHOO_REFRESH_TOKEN" not in env_path.read_text()
