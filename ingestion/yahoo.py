"""Yahoo Fantasy Sports API ingestion (SRS §3.1 Source 1, §7.1).

Handles the OAuth 2.0 token lifecycle (out-of-band authorization-code
grant, background refresh via persisted ``YAHOO_REFRESH_TOKEN``) and the
three payload extracts the pipeline needs: league scoring settings, the
user's active roster, and the free-agent pool.

All network access flows through injectable transports
(``token_transport`` / ``api_transport``) so unit tests run fully
offline. Secrets never leave ``.env`` — only ``persist_refresh_token``
writes to it, atomically.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union

import db
from ingestion.crosswalk import CrosswalkResolver, YahooIdentity

YAHOO_AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
YAHOO_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
YAHOO_API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"
REDIRECT_URI_OOB = "oob"
HTTP_TIMEOUT_SECONDS = 30.0

# (url, headers, form_data) -> parsed JSON
TokenTransport = Callable[[str, Dict[str, str], Dict[str, str]], Dict[str, Any]]
# (url, headers) -> parsed JSON
ApiTransport = Callable[[str, Dict[str, str]], Dict[str, Any]]

YAHOO_STAT_ID_MAP: Dict[int, str] = {
    1: "G",
    2: "A",
    4: "PLUS_MINUS",
    5: "PIM",
    8: "PPP",
    9: "SHP",
    11: "GWG",
    14: "SOG",
    16: "FW",
    19: "W",
    22: "GA",
    25: "SV",
    27: "SHO",
    31: "HIT",
    32: "BLK",
}

YAHOO_STAT_NAME_MAP: Dict[str, str] = {
    "G": "G",
    "A": "A",
    "+/-": "PLUS_MINUS",
    "PLUS_MINUS": "PLUS_MINUS",
    "PIM": "PIM",
    "PPP": "PPP",
    "SHP": "SHP",
    "GWG": "GWG",
    "SOG": "SOG",
    "FW": "FW",
    "HIT": "HIT",
    "BLK": "BLK",
    "W": "W",
    "GA": "GA",
    "SV": "SV",
    "SHO": "SHO",
}

# Yahoo slots that are lineup containers, not playing positions.
_SLOT_CODES = {"BN", "UTIL", "IR", "IR+", "IL", "IL+", "NA", "DL", "BE"}


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str
    refresh_token: str
    expires_at: float  # unix epoch seconds (time.time() + expires_in)
    token_type: str = "bearer"

    def is_expired(
        self, *, now: Optional[float] = None, skew_seconds: float = 60.0
    ) -> bool:
        current = time.time() if now is None else now
        return current + skew_seconds >= self.expires_at


@dataclass(frozen=True)
class LeagueSettings:
    league_key: str
    league_name: str
    stat_weights: Dict[str, float]  # canonical stat key -> fantasy point weight
    roster_positions: Dict[str, int]  # slot -> count
    max_weekly_adds: Optional[int] = None


@dataclass(frozen=True)
class YahooPlayer:
    yahoo_player_id: str  # e.g. "449.p.6743"
    display_name: str  # e.g. "Connor McDavid"
    primary_position: str  # first non-slot position, e.g. "C"
    eligible_positions: Tuple[str, ...]  # e.g. ("C", "UTIL")
    team_abbr: Optional[str] = None  # uppercase, e.g. "EDM"
    selected_slot: Optional[str] = None  # active roster slot, or None for FA
    status: Optional[str] = None  # e.g. "IR", "DTD"
    percent_owned: Optional[float] = None
    player_id: Optional[int] = None  # NHL id once crosswalked


# ---------------------------------------------------------------------------
# Default production transports (never used by tests — sockets are blocked).
# ---------------------------------------------------------------------------


def default_token_transport(
    url: str, headers: Dict[str, str], form_data: Dict[str, str]
) -> Dict[str, Any]:
    body = urllib.parse.urlencode(form_data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read())


def default_api_transport(url: str, headers: Dict[str, str]) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------


def build_authorization_url(
    client_id: str, redirect_uri: str = REDIRECT_URI_OOB
) -> str:
    """User-facing URL that starts the out-of-band auth-code grant."""
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
        }
    )
    return f"{YAHOO_AUTH_URL}?{query}"


def persist_refresh_token(env_path: Union[str, Path], refresh_token: str) -> None:
    """Atomically set ``YAHOO_REFRESH_TOKEN`` in ``env_path``.

    Preserves every other line (including comments) and rejects tokens
    containing newlines so a token can never smuggle extra config lines.
    """
    if "\n" in refresh_token or "\r" in refresh_token:
        raise ValueError("refresh_token must not contain newlines")
    path = Path(env_path)
    lines = path.read_text().splitlines() if path.exists() else []

    new_line = f"YAHOO_REFRESH_TOKEN={refresh_token}"
    out: List[str] = []
    written = False
    for line in lines:
        if line.startswith("YAHOO_REFRESH_TOKEN="):
            if not written:
                out.append(new_line)
                written = True
            continue  # drop any duplicate definitions
        out.append(line)
    if not written:
        out.append(new_line)

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Pure parsers — handle Yahoo's nested fantasy_content JSON AND flat dicts.
# ---------------------------------------------------------------------------


def _iter_player_nodes(node: Any) -> Iterator[Any]:
    """Yield the inner payload of every ``{"player": ...}`` wrapper found."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "player":
                yield value
            else:
                yield from _iter_player_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_player_nodes(item)


def _merge_player_node(node: Any) -> Dict[str, Any]:
    """Flatten a Yahoo player node into a single attribute dict.

    Yahoo encodes each player as lists of single-key dicts
    (``{"player": [[{attrs...}, {"selected_position": [...]}]]}``); scalar
    fields merge upward while list-valued fields (``eligible_positions``,
    ``selected_position``, ``percent_owned``) stay intact.
    """
    out: Dict[str, Any] = {}
    _collect_attrs(node, out)
    return out


def _collect_attrs(node: Any, out: Dict[str, Any]) -> None:
    if isinstance(node, list):
        for item in node:
            _collect_attrs(item, out)
    elif isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, dict):
                _collect_attrs(value, out)
            elif isinstance(value, list):
                out[key] = value
            else:
                out[key] = value


def _positions_from(raw: Any) -> Tuple[str, ...]:
    positions: List[str] = []
    items = raw if isinstance(raw, list) else ([raw] if raw else [])
    for item in items:
        pos = item.get("position") if isinstance(item, dict) else item
        if pos:
            positions.append(str(pos).strip().upper())
    return tuple(dict.fromkeys(positions))


def _extract_percent_owned(raw: Any) -> Optional[float]:
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, dict):
        raw = raw.get("value")
    elif isinstance(raw, list):
        values = [
            item.get("value")
            for item in raw
            if isinstance(item, dict) and item.get("value") is not None
        ]
        raw = values[-1] if values else None
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _extract_selected_slot(raw: Any, attrs: Dict[str, Any]) -> Optional[str]:
    if isinstance(raw, list) and raw:
        last = raw[-1]
        pos = last.get("position") if isinstance(last, dict) else last
        return str(pos).upper() if pos else None
    if isinstance(raw, str) and raw:
        return raw.upper()
    slot = attrs.get("selected_slot")
    return str(slot).upper() if slot else None


def _attrs_to_player(attrs: Dict[str, Any]) -> YahooPlayer:
    yahoo_id = str(attrs.get("player_key") or attrs.get("yahoo_player_id") or "")
    name_raw = attrs.get("name")
    display_name = str(
        attrs.get("display_name")
        or (name_raw if isinstance(name_raw, str) else attrs.get("full"))
        or ""
    )
    eligible = _positions_from(attrs.get("eligible_positions"))
    display_pos = str(attrs.get("display_position") or "").upper()
    if not eligible and display_pos:
        eligible = tuple(p for p in display_pos.split(",") if p)
    primary = next((p for p in eligible if p not in _SLOT_CODES), None)
    if primary is None:
        fallback = display_pos.split(",")[0] if display_pos else None
        primary = fallback or "C"

    status = attrs.get("status")
    return YahooPlayer(
        yahoo_player_id=yahoo_id,
        display_name=display_name,
        primary_position=primary,
        eligible_positions=eligible,
        team_abbr=(
            str(attrs.get("editorial_team_abbr") or attrs.get("team_abbr")).upper()
            if (attrs.get("editorial_team_abbr") or attrs.get("team_abbr"))
            else None
        ),
        selected_slot=_extract_selected_slot(attrs.get("selected_position"), attrs),
        status=str(status) if isinstance(status, str) and status else None,
        percent_owned=_extract_percent_owned(attrs.get("percent_owned")),
    )


def _extract_player_attrs(payload: Any) -> Iterator[Dict[str, Any]]:
    """Yield flattened player-attr dicts from nested or flat payloads."""
    if (
        isinstance(payload, dict)
        and isinstance(payload.get("players"), list)
        and not any(isinstance(v, dict) and "player" in v for v in payload["players"])
    ):
        for record in payload["players"]:
            yield _merge_player_node(record)
        return
    yielded = False
    for node in _iter_player_nodes(payload):
        yielded = True
        yield _merge_player_node(node)
    if not yielded and isinstance(payload, list):
        for record in payload:
            yield _merge_player_node(record)


def parse_roster(payload: Dict[str, Any]) -> List[YahooPlayer]:
    """Parse ``team/{team_key}/roster`` into normalized YahooPlayers."""
    return [_attrs_to_player(a) for a in _extract_player_attrs(payload)]


def parse_free_agents(payload: Dict[str, Any]) -> List[YahooPlayer]:
    """Parse ``league/{key}/players;status=FA;out=ownership``."""
    return [_attrs_to_player(a) for a in _extract_player_attrs(payload)]


def _canonical_stat_key(
    stat_id: Any = None, name: Optional[str] = None
) -> Optional[str]:
    """Map a Yahoo stat_id or stat name to the PuckLuck canonical key."""
    if stat_id not in (None, ""):
        try:
            mapped = YAHOO_STAT_ID_MAP.get(int(stat_id))
        except (TypeError, ValueError):
            mapped = None
        if mapped:
            return mapped
    if name:
        raw = str(name).strip()
        if raw == "+/-":
            return "PLUS_MINUS"
        upper = raw.upper()
        return YAHOO_STAT_NAME_MAP.get(upper, upper)
    return None


def _dig_settings(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (settings_dict, league_meta_dict) from any wrapping depth."""
    node: Any = payload.get("fantasy_content", payload)
    meta: Dict[str, Any] = {}
    settings: Dict[str, Any] = {}
    league = node.get("league") if isinstance(node, dict) else None
    if isinstance(league, list):
        for item in league:
            if isinstance(item, dict) and "settings" in item:
                s = item["settings"]
                if isinstance(s, list):
                    settings = s[0] if s else {}
                elif isinstance(s, dict):
                    settings = s
            elif isinstance(item, dict):
                meta.update(item)
    elif isinstance(node, dict):
        s = node.get("settings", node)
        if isinstance(s, list):
            settings = s[0] if s else {}
        elif isinstance(s, dict):
            settings = s
    return settings, meta


def _settings_from_dict(
    settings: Dict[str, Any], meta: Dict[str, Any], league_key: Optional[str]
) -> LeagueSettings:
    stat_weights: Dict[str, float] = {}
    mods = settings.get("stat_modifiers") or {}
    stats = mods.get("stats") or []
    for entry in stats:
        s = entry.get("stat", entry) if isinstance(entry, dict) else {}
        if not isinstance(s, dict):
            continue
        key = _canonical_stat_key(
            s.get("stat_id"), s.get("display_name") or s.get("name")
        )
        if key is None:
            continue
        try:
            stat_weights[key] = float(s.get("value", 0) or 0)
        except (TypeError, ValueError):
            continue

    roster_positions: Dict[str, int] = {}
    for entry in settings.get("roster_positions") or []:
        rp = entry.get("roster_position", entry) if isinstance(entry, dict) else {}
        if not isinstance(rp, dict):
            continue
        pos = str(rp.get("position") or "").upper()
        if not pos:
            continue
        try:
            roster_positions[pos] = roster_positions.get(pos, 0) + int(
                rp.get("count") or 0
            )
        except (TypeError, ValueError):
            continue

    max_adds: Optional[int] = None
    raw_adds = settings.get("max_weekly_adds")
    if raw_adds is not None:
        try:
            parsed = int(raw_adds)
            max_adds = parsed if parsed >= 0 else None
        except (TypeError, ValueError):
            max_adds = None

    return LeagueSettings(
        league_key=str(league_key or meta.get("league_key") or ""),
        league_name=str(meta.get("name") or settings.get("name") or ""),
        stat_weights=stat_weights,
        roster_positions=roster_positions,
        max_weekly_adds=max_adds,
    )


def parse_league_settings(
    payload: Dict[str, Any], *, league_key: Optional[str] = None
) -> LeagueSettings:
    """Parse ``league/{league_key}/settings`` (nested or flat)."""
    if isinstance(payload.get("stat_weights"), dict):
        # Already-normalized flat dict.
        weights = {
            _canonical_stat_key(name=k) or k: float(v)
            for k, v in payload["stat_weights"].items()
        }
        raw_positions = payload.get("roster_positions") or {}
        positions = {str(k).upper(): int(v) for k, v in raw_positions.items()}
        raw_adds = payload.get("max_weekly_adds")
        return LeagueSettings(
            league_key=str(league_key or payload.get("league_key") or ""),
            league_name=str(payload.get("league_name") or ""),
            stat_weights=weights,
            roster_positions=positions,
            max_weekly_adds=int(raw_adds) if raw_adds is not None else None,
        )
    settings, meta = _dig_settings(payload)
    return _settings_from_dict(settings, meta, league_key)


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


class YahooClient:
    """OAuth-aware Yahoo Fantasy client with injectable transports."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        *,
        env_path: Optional[Union[str, Path]] = None,
        token_transport: TokenTransport = default_token_transport,
        api_transport: ApiTransport = default_api_transport,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._env_path = Path(env_path) if env_path else None
        self._token_transport = token_transport
        self._api_transport = api_transport
        self._clock = clock
        self._tokens: Optional[OAuthTokens] = None

    # -- OAuth -------------------------------------------------------------

    def _basic_auth_header(self) -> str:
        creds = f"{self._client_id}:{self._client_secret}".encode("utf-8")
        return "Basic " + base64.b64encode(creds).decode("ascii")

    def _tokens_from_payload(self, payload: Dict[str, Any]) -> OAuthTokens:
        return OAuthTokens(
            access_token=str(payload["access_token"]),
            refresh_token=str(payload.get("refresh_token") or self._refresh_token),
            expires_at=self._clock() + float(payload.get("expires_in", 3600)),
            token_type=str(payload.get("token_type") or "bearer"),
        )

    def exchange_code(
        self, code: str, redirect_uri: str = REDIRECT_URI_OOB
    ) -> OAuthTokens:
        """Exchange an out-of-band authorization code for tokens."""
        payload = self._token_transport(
            YAHOO_TOKEN_URL,
            {
                "Authorization": self._basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        self._tokens = self._tokens_from_payload(payload)
        if payload.get("refresh_token"):
            self._refresh_token = str(payload["refresh_token"])
            if self._env_path is not None:
                persist_refresh_token(self._env_path, self._refresh_token)
        return self._tokens

    def refresh_access_token(self) -> OAuthTokens:
        """Renew the access token via ``grant_type=refresh_token``."""
        payload = self._token_transport(
            YAHOO_TOKEN_URL,
            {
                "Authorization": self._basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            },
        )
        self._tokens = self._tokens_from_payload(payload)
        if payload.get("refresh_token"):
            self._refresh_token = str(payload["refresh_token"])
            if self._env_path is not None:
                persist_refresh_token(self._env_path, self._refresh_token)
        return self._tokens

    def ensure_token(self) -> str:
        """Return a valid access token, refreshing only when expired."""
        if self._tokens is None or self._tokens.is_expired(now=self._clock()):
            self.refresh_access_token()
        assert self._tokens is not None
        return self._tokens.access_token

    # -- API calls -----------------------------------------------------------

    def get_json(self, resource_path: str) -> Dict[str, Any]:
        """GET ``{YAHOO_API_BASE}/{resource_path}?format=json``."""
        path = resource_path.lstrip("/")
        sep = "&" if "?" in path else "?"
        url = f"{YAHOO_API_BASE}/{path}{sep}format=json"
        headers = {"Authorization": f"Bearer {self.ensure_token()}"}
        return self._api_transport(url, headers)

    def fetch_league_settings(self, league_key: str) -> LeagueSettings:
        payload = self.get_json(f"league/{league_key}/settings")
        return parse_league_settings(payload, league_key=league_key)

    def fetch_roster(self, team_key: str) -> List[YahooPlayer]:
        payload = self.get_json(f"team/{team_key}/roster")
        return parse_roster(payload)

    def fetch_free_agents(
        self, league_key: str, *, start: int = 0, count: int = 25
    ) -> List[YahooPlayer]:
        payload = self.get_json(
            f"league/{league_key}/players;status=FA;"
            f"start={start};count={count};out=ownership"
        )
        return parse_free_agents(payload)

    # -- warehouse sync ------------------------------------------------------

    def sync_players_to_db(
        self, conn: sqlite3.Connection, players: Sequence[YahooPlayer]
    ) -> List[YahooPlayer]:
        """Crosswalk Yahoo identities onto dim_players; return enriched list."""
        resolver = CrosswalkResolver(conn)
        identities = [
            YahooIdentity(
                yahoo_player_id=p.yahoo_player_id,
                display_name=p.display_name,
                position=p.primary_position,
                team_abbr=p.team_abbr,
            )
            for p in players
        ]
        results = resolver.link_all(identities)
        return [
            replace(player, player_id=result.player_id)
            for player, result in zip(players, results)
        ]
