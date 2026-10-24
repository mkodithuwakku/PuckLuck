"""Yahoo <-> NHL player identity crosswalk (SRS §3.1/§3.2).

Yahoo payloads identify players by opaque text keys
(``yahoo_player_id``, e.g. ``449.p.6743``) while NHL.com and MoneyPuck
share numeric ``player_id`` values that key ``dim_players``. This module
resolves Yahoo identities onto warehouse rows via normalized-name
matching with position disambiguation, then writes the link back into
``dim_players.yahoo_player_id``.

Match ladder (first hit wins):
    1. exact    — canonical_name equality, unique
    2. position — exact-name tie broken by position group (F/D/G)
    3. alias    — first-name alias expansion ("alex" -> "alexander")
    4. fuzzy    — difflib ratio >= FUZZY_CUTOFF within position group
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

FUZZY_CUTOFF = 0.88

_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

# Common NHL first-name short forms -> registry forms (both directions
# are tried at match time).
FIRST_NAME_ALIASES: Dict[str, Tuple[str, ...]] = {
    "alex": ("alexander", "alexandre"),
    "alexander": ("alex",),
    "andy": ("andrew",),
    "andrew": ("andy",),
    "cam": ("cameron",),
    "cameron": ("cam",),
    "chris": ("christopher",),
    "christopher": ("chris",),
    "jake": ("jacob",),
    "jacob": ("jake",),
    "joe": ("joseph",),
    "joseph": ("joe",),
    "josh": ("joshua",),
    "joshua": ("josh",),
    "matt": ("matthew", "mathew"),
    "matthew": ("matt", "mathew"),
    "mathew": ("matt", "matthew"),
    "mike": ("michael",),
    "michael": ("mike",),
    "nate": ("nathan", "nathaniel"),
    "nathan": ("nate",),
    "nick": ("nicholas",),
    "nicholas": ("nick",),
    "pat": ("patrick",),
    "patrick": ("pat",),
    "sam": ("samuel",),
    "samuel": ("sam",),
    "tom": ("thomas",),
    "thomas": ("tom", "tomas"),
    "tomas": ("tom", "thomas"),
    "tony": ("anthony",),
    "anthony": ("tony",),
    "will": ("william",),
    "william": ("will",),
    "zach": ("zachary", "zack"),
    "zachary": ("zach", "zack"),
}

_NON_ALPHA = re.compile(r"[^a-z ]")


def normalize_name(name: str) -> str:
    """Canonicalize a display name for matching.

    Lowercases, strips diacritics ("Tomáš" -> "tomas"), removes all
    punctuation (hyphens/apostrophes collapse into adjacent letters so
    both sides of the crosswalk normalize identically), and drops
    generational suffixes.
    """
    decomposed = unicodedata.normalize("NFKD", name.strip().lower())
    ascii_name = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = _NON_ALPHA.sub("", ascii_name)
    tokens = [t for t in cleaned.split() if t not in _NAME_SUFFIXES]
    return " ".join(tokens)


def position_group(position: Optional[str]) -> Optional[str]:
    """Collapse NHL/Yahoo positions to 'F', 'D', or 'G'."""
    if not position:
        return None
    pos = position.strip().upper()
    if pos == "G":
        return "G"
    if pos == "D":
        return "D"
    return "F"  # C, LW, RW, F, UTIL-ish labels


@dataclass(frozen=True)
class YahooIdentity:
    """One Yahoo-side player record needing a warehouse link."""

    yahoo_player_id: str
    display_name: str
    position: Optional[str] = None
    team_abbr: Optional[str] = None  # reserved for future disambiguation


@dataclass(frozen=True)
class MatchResult:
    yahoo_player_id: str
    display_name: str
    player_id: Optional[int]
    method: str  # exact | position | alias | fuzzy | unmatched
    score: float = 0.0
    candidates: Tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class _PlayerRow:
    player_id: int
    canonical_name: str
    primary_position: str


class CrosswalkResolver:
    """Resolves Yahoo identities onto ``dim_players`` (keyed by NHL ID)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        rows = conn.execute(
            "SELECT player_id, canonical_name, primary_position FROM dim_players"
        ).fetchall()
        # Stored canonical_name is only lowercase; normalize again so
        # accent/punctuation handling is identical on both sides.
        self._players: List[_PlayerRow] = [
            _PlayerRow(
                r["player_id"],
                normalize_name(r["canonical_name"]),
                r["primary_position"],
            )
            for r in rows
        ]
        self._by_name: Dict[str, List[_PlayerRow]] = {}
        for p in self._players:
            self._by_name.setdefault(p.canonical_name, []).append(p)

    # -- matching ---------------------------------------------------------

    def _filter_position(
        self, candidates: Sequence[_PlayerRow], pos_group: Optional[str]
    ) -> List[_PlayerRow]:
        if pos_group is None:
            return list(candidates)
        same = [
            p for p in candidates if position_group(p.primary_position) == pos_group
        ]
        return same or list(candidates)

    def resolve(
        self, display_name: str, position: Optional[str] = None
    ) -> Tuple[Optional[int], str, float, Tuple[int, ...]]:
        """Return (player_id, method, score, candidate_ids)."""
        canonical = normalize_name(display_name)
        pos_group = position_group(position)

        exact = self._by_name.get(canonical, [])
        if len(exact) == 1:
            p = exact[0]
            return p.player_id, "exact", 1.0, (p.player_id,)
        if len(exact) > 1:
            narrowed = self._filter_position(exact, pos_group)
            if len(narrowed) == 1:
                p = narrowed[0]
                ids = tuple(p.player_id for p in exact)
                return p.player_id, "position", 1.0, ids
            ids = tuple(p.player_id for p in exact)
            return None, "unmatched", 0.0, ids

        # Alias expansion on the first name.
        tokens = canonical.split()
        if len(tokens) >= 2:
            for alt in FIRST_NAME_ALIASES.get(tokens[0], ()):
                alias_name = " ".join([alt, *tokens[1:]])
                hits = self._by_name.get(alias_name, [])
                narrowed = self._filter_position(hits, pos_group)
                if len(narrowed) == 1:
                    p = narrowed[0]
                    return p.player_id, "alias", 1.0, (p.player_id,)

        # Fuzzy fallback within the position group.
        pool = self._filter_position(self._players, pos_group)
        best: Optional[_PlayerRow] = None
        best_score = 0.0
        for p in pool:
            score = SequenceMatcher(None, canonical, p.canonical_name).ratio()
            if score > best_score:
                best, best_score = p, score
        if best is not None and best_score >= FUZZY_CUTOFF:
            return best.player_id, "fuzzy", best_score, (best.player_id,)
        return None, "unmatched", best_score, ()

    # -- write-back -------------------------------------------------------

    def attach(self, yahoo_player_id: str, player_id: int) -> None:
        """Link a Yahoo key to a warehouse player (re-linking if moved)."""
        self._conn.execute(
            "UPDATE dim_players SET yahoo_player_id = NULL WHERE yahoo_player_id = ?",
            (yahoo_player_id,),
        )
        self._conn.execute(
            "UPDATE dim_players SET yahoo_player_id = ? WHERE player_id = ?",
            (yahoo_player_id, player_id),
        )

    def link(self, identity: YahooIdentity) -> MatchResult:
        """Resolve one identity and persist the link when matched."""
        player_id, method, score, candidates = self.resolve(
            identity.display_name, identity.position
        )
        if player_id is not None:
            self.attach(identity.yahoo_player_id, player_id)
        return MatchResult(
            yahoo_player_id=identity.yahoo_player_id,
            display_name=identity.display_name,
            player_id=player_id,
            method=method,
            score=score,
            candidates=candidates,
        )

    def link_all(
        self, identities: Iterable[YahooIdentity], *, commit: bool = True
    ) -> List[MatchResult]:
        results = [self.link(i) for i in identities]
        if commit:
            self._conn.commit()
        return results
