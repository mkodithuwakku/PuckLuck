/* PuckLuck dashboard renderer — betting-hub edition.
 *
 * Pure ES module: every render function returns an HTML string so the
 * same code runs in the browser and under `node --test`. Browser glue
 * (fetch + tab/team/filter binding + auto-refresh) is guarded behind
 * `typeof document`.
 *
 * Tabs: Picks (landing +EV board), Players (point scorers),
 * Totals (game totals), Roster, Moves (add/drop solver).
 */

export const STALE_THRESHOLD_HOURS = 26;
export const AUTO_REFRESH_INTERVAL_MS = 300_000;

export const TEAM_COLORS = {
  EDM: ["#FF4C00", "#041E42"],
  COL: ["#6F263D", "#236192"],
  TOR: ["#00205B", "#4da6ff"],
  CAR: ["#CC0000", "#111111"],
  FLA: ["#C8102E", "#B9975B"],
  NYR: ["#0038A8", "#CE1126"],
  BOS: ["#FFB81C", "#111111"],
  TBL: ["#002868", "#4da6ff"],
  DAL: ["#006847", "#8F8F8C"],
  VGK: ["#B4975A", "#333F42"],
  WPG: ["#041E42", "#AC162C"],
  VAN: ["#00843D", "#00205B"],
  CGY: ["#D2001C", "#FAAF19"],
  LAK: ["#A2AAAD", "#111111"],
  SEA: ["#99D9D9", "#001628"],
  NJD: ["#CE1126", "#111111"],
  WSH: ["#C8102E", "#041E42"],
  PIT: ["#FCB514", "#000000"],
  PHI: ["#F74902", "#000000"],
  CBJ: ["#002654", "#CE1126"],
  DET: ["#CE1126", "#FFFFFF"],
  BUF: ["#003087", "#FFB81C"],
  OTT: ["#C52032", "#C2912C"],
  MTL: ["#AF1E2D", "#192168"],
  STL: ["#002F87", "#FCB514"],
  MIN: ["#154734", "#A6192E"],
  NSH: ["#FFB81C", "#041E42"],
  CHI: ["#CF0A2C", "#000000"],
  UTA: ["#69B3E7", "#010101"],
  ANA: ["#F47A38", "#B9975B"],
  SJS: ["#006D75", "#EA7200"],
  NYI: ["#00539B", "#F47D30"],
};

const DEFAULT_TEAM_COLORS = ["#4da6ff", "#1c2026"];

const SLOT_ORDER = ["C", "LW", "RW", "D", "UTIL", "G", "BN", "IR", "IR+"];

const TAB_DEFS = [
  ["picks", "Picks"],
  ["scorers", "Players"],
  ["totals", "Totals"],
  ["roster", "Roster"],
  ["adddrop", "Moves"],
];

const SCORER_FILTERS = ["ALL", "POINTS", "GOALS", "ASSISTS", "+EV"];

const ESCAPES = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

function fmtInt(value) {
  const n = Number(value);
  return Number.isFinite(n) ? String(Math.round(n)) : "0";
}

function fmt1(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(1) : "0.0";
}

function fmt2(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(2) : "0.00";
}

function fmtSigned1(value) {
  const n = Number(value);
  const text = Number.isFinite(n) ? n.toFixed(1) : "0.0";
  return text.startsWith("-") ? text : `+${text}`;
}

function fmtSignedPct(value) {
  const n = Number(value) * 100;
  const text = Number.isFinite(n) ? n.toFixed(1) : "0.0";
  return `${text.startsWith("-") ? text : `+${text}`}%`;
}

function fmtOdds(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return "-110";
  const rounded = Math.round(n);
  return rounded > 0 ? `+${rounded}` : String(rounded);
}

function fmtGameTime(iso) {
  const d = new Date(iso || "");
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function getTeamColors(abbr) {
  const upper = String(abbr || "").toUpperCase().trim();
  return TEAM_COLORS[upper] || DEFAULT_TEAM_COLORS;
}

/* NHL season slug ("20262027") from a slate/report ISO date so headshot
 * URLs resolve to the correct season directory on assets.nhle.com. */
export function seasonSlug(isoDate) {
  const d = new Date(isoDate || Date.now());
  if (Number.isNaN(d.getTime())) return "latest";
  const year = d.getUTCFullYear();
  const start = d.getUTCMonth() >= 6 ? year : year - 1;
  return `${start}${start + 1}`;
}

export function renderTeamLogo(abbr) {
  const upper = String(abbr || "").toUpperCase().trim();
  if (!upper) return "";
  const [primary, secondary] = getTeamColors(upper);
  return `<span class="team-crest" style="--team-color:${primary};--team-secondary:${secondary}"><img class="team-logo" src="https://assets.nhle.com/logos/nhl/svg/${esc(
    upper
  )}_dark.svg" alt="${esc(
    upper
  )}" loading="lazy" onload="this.nextElementSibling.style.display='none'" onerror="this.style.display='none'" /><span class="team-initials">${esc(
    upper
  )}</span></span>`;
}

function playerInitials(name) {
  const parts = String(name || "")
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  if (!parts.length) return "?";
  const first = parts[0][0] || "";
  const last = parts.length > 1 ? parts[parts.length - 1][0] : "";
  return (first + last).toUpperCase() || "?";
}

export function renderPlayerHeadshot(player, season = "") {
  const [primary] = getTeamColors(player?.team_abbr);
  const initials = esc(playerInitials(player?.display_name));
  const id = Number(player?.player_id);
  const team = String(player?.team_abbr || "")
    .toUpperCase()
    .trim();
  const slug = String(season || "").trim() || "latest";
  const img =
    Number.isFinite(id) && id > 0 && team
      ? `<img class="headshot-img" src="https://assets.nhle.com/mugs/nhl/${esc(
          slug
        )}/${esc(team)}/${id}.png" alt="" loading="lazy" onload="this.nextElementSibling.style.display='none'" onerror="this.style.display='none'" />`
      : "";
  return `<span class="player-headshot" style="--team-color:${primary}">${img}<span class="hs-initials">${initials}</span></span>`;
}

function badge(label) {
  const classes = {
    CONFIRMED: "badge-confirmed",
    PROJECTED: "badge-projected",
    RECOMMEND_SWAP: "badge-add",
    RECOMMEND_ADD: "badge-add",
    HOLD_ASSET: "badge-hold",
    HOLD: "badge-hold",
    "+EV": "badge-ev",
    POSTPONED: "badge-postponed",
    FINAL: "badge-final",
    LIVE: "badge-live",
    "STALE DATA": "badge-stale",
  };
  return `<span class="badge ${classes[label] || "badge-projected"}">${esc(
    label
  )}</span>`;
}

function goalieBadge(status) {
  return status ? badge(String(status).toUpperCase()) : "";
}

const ENDED_STATUSES = new Set(["POSTPONED", "FINAL", "LIVE"]);
function isEnded(g) {
  return ENDED_STATUSES.has(String(g.status || "").toUpperCase());
}
function isFinal(g) {
  return String(g.status || "").toUpperCase() === "FINAL";
}

function evTier(ev) {
  const e = Number(ev || 0);
  if (e >= 0.08) return "edge-hot";
  if (e >= 0.03) return "edge-warm";
  return "edge-cool";
}

function probTier(prob) {
  const p = Number(prob || 0);
  if (p >= 0.7) return "tier-hot";
  if (p >= 0.5) return "tier-warm";
  return "tier-cool";
}

export function isReportStale(
  generatedAtIso,
  nowMs = Date.now(),
  maxHours = STALE_THRESHOLD_HOURS
) {
  const parsed = Date.parse(generatedAtIso);
  if (!generatedAtIso || !Number.isFinite(parsed)) return true;
  return nowMs - parsed > maxHours * 3600 * 1000;
}

export function resolveActiveTeam(report, teamIndex = 0) {
  if (Array.isArray(report?.teams) && report.teams.length > 0) {
    const idx = Math.max(
      0,
      Math.min(Number(teamIndex) || 0, report.teams.length - 1)
    );
    const t = report.teams[idx] || {};
    return {
      team_key: t.team_key || `team_${idx + 1}`,
      team_summary: t.team_summary || report?.team_summary || {},
      roster: t.roster || [],
      recommendation:
        t.recommendation !== undefined
          ? t.recommendation
          : report?.recommendation || null,
      waiver_targets: t.waiver_targets || report?.free_agents || [],
    };
  }
  return {
    team_key: "team_1",
    team_summary: report?.team_summary || {},
    roster: report?.roster || [],
    recommendation: report?.recommendation || null,
    waiver_targets: report?.free_agents || [],
  };
}

export function renderHeader(
  report,
  { nowMs = Date.now(), teamIndex = 0 } = {}
) {
  const slateDate = esc(report?.active_slate_date || "");

  const staleHours = Number(report?.stale_after_hours) || STALE_THRESHOLD_HOURS;
  const stale = isReportStale(report?.generated_at, nowMs, staleHours);
  const banner = stale ? `<div class="stale-banner">STALE DATA</div>` : "";

  const teams = Array.isArray(report?.teams) ? report.teams : [];
  const activeIdx =
    teams.length > 0
      ? Math.max(0, Math.min(Number(teamIndex) || 0, teams.length - 1))
      : 0;
  const switcherHtml =
    teams.length > 1
      ? `<div class="team-switcher" role="tablist" aria-label="Fantasy Teams">${teams
          .map((t, i) => {
            const ts = t?.team_summary || {};
            const tName = esc(ts.team_name || `Team ${i + 1}`);
            const lName = esc(ts.league_name || "");
            return `<button type="button" class="team-switch-btn${
              i === activeIdx ? " active" : ""
            }" data-team-index="${i}">
        <span class="ts-name">${tName}</span>
        ${lName ? `<span class="ts-league">${lName}</span>` : ""}
      </button>`;
          })
          .join("")}</div>`
      : "";

  const generated = report?.generated_at
    ? fmtGameTime(report.generated_at)
    : "";
  const kpi = renderKpiStrip(report, generated);
  return `<header class="pl-header">
  <div class="pl-header-top">
    <div class="pl-brand"><span class="pl-brand-mark">PL</span><span class="pl-brand-name">PuckLuck</span></div>
    <div class="pl-header-pills">
      ${slateDate ? `<span class="pill pill-slate">Slate <strong>${slateDate}</strong></span>` : ""}
      <button type="button" class="pill pill-live" id="sync-now-btn" title="Refresh report now"><span class="live-dot"></span>LIVE · 5m Sync</button>
    </div>
  </div>
  ${kpi}
  ${switcherHtml}
  ${banner}
</header>`;
}

/* KPI cards row — the "dashboard" glance: what's on, what's bettable. */
function renderKpiStrip(report, updatedLabel = "") {
  const games = report?.games || [];
  const scorers = report?.point_scorers || [];
  const statusOf = (g) => String(g.status || "").toUpperCase();
  const scheduled = games.filter((g) => statusOf(g) === "SCHEDULED").length;
  const live = games.filter((g) => statusOf(g) === "LIVE").length;
  const finals = games.filter((g) => statusOf(g) === "FINAL").length;
  const evPlays =
    games.filter((g) => g.is_positive_ev === true).length +
    scorers.filter((p) => p.is_positive_ev === true).length;
  const priced = scorers.filter((p) => !isEstimatedPrice(p.odds_source)).length;
  const cells = [
    { label: "SCHEDULED", value: scheduled, cls: "" },
    { label: "+EV PLAYS", value: evPlays, cls: "kpi-good" },
    { label: "LIVE", value: live, cls: "kpi-live" },
    { label: "FINAL", value: finals, cls: "kpi-done" },
    { label: "BOOK-PRICED", value: priced, cls: "kpi-book" },
  ];
  return `<div class="kpi-strip">${cells
    .map(
      (k) =>
        `<div class="kpi ${k.cls}"><b>${k.value}</b><span>${k.label}</span></div>`
    )
    .join("")}${
    updatedLabel
      ? `<span class="kpi-updated" title="Report generated at">UPD ${esc(
          updatedLabel
        )}</span>`
      : ""
  }</div>`;
}

/* Compact fantasy context strip shown inside the Roster / Moves panels so
 * the betting tabs stay clean while team state remains one glance away. */
function renderFantasyContext(activeTeam) {
  const s = activeTeam?.team_summary || {};
  if (!s.team_name) return "";
  const limit = Number(s.adds_limit ?? 0);
  const used = Number(s.adds_used ?? 0);
  const addsLeft = Number.isFinite(limit) ? Math.max(0, limit - used) : 0;
  return `<div class="fantasy-context">
  <span class="fc-avatar">${esc(s.team_initials || "PL")}</span>
  <div class="fc-meta"><b>${esc(s.team_name)}</b><span>${esc(
    s.league_name || ""
  )}</span></div>
  <div class="fc-pills">
    <span class="pill">Adds <strong>${addsLeft}/${fmtInt(limit)}</strong></span>
    ${
      s.matchup_score
        ? `<span class="pill pill-matchup">${esc(s.matchup_score)}</span>`
        : ""
    }
  </div>
</div>`;
}

/* ---------- Best Picks (landing +EV board) ---------- */

function totalPick(g) {
  const side = String(g.best_side || "OVER").toUpperCase();
  const isUnder = side === "UNDER";
  const odds = isUnder ? g.under_odds : g.over_odds;
  const prob = Number(isUnder ? g.prob_under : g.prob_over) || 0;
  const [primary] = getTeamColors(g.home_abbr);
  return {
    ev: Number(g.ev || 0),
    sortProb: prob,
    html: `<div class="pick-row pick-total" style="--team-color:${primary}">
  <span class="crest-pair">${renderTeamLogo(g.away_abbr)}${renderTeamLogo(
      g.home_abbr
    )}</span>
  <div class="pick-main">
    <div class="pick-title"><span class="pick-tag tag-total">TOTAL</span>${esc(
      side
    )} ${fmt1(g.line)} <span class="pick-odds">${fmtOdds(odds)}</span></div>
    <div class="pick-sub">${esc(g.away_abbr)} @ ${esc(
      g.home_abbr
    )} · Model ${fmt1(g.expected_total)} vs Line ${fmt1(g.line)} · ${Math.round(
      prob * 100
    )}%${priceSourceNote(g.line_source)}</div>
  </div>
  <div class="pick-edge ${evTier(g.ev)}"><b>${fmtSignedPct(g.ev)}</b><small>EV</small></div>
</div>`,
  };
}

const PROP_MARKET_LABELS = {
  GOALS: "Anytime Goal",
  ASSISTS: "1+ Assist",
  POINTS: "1+ Point",
};

function propMarketLabel(market) {
  const key = String(market || "").toUpperCase();
  return PROP_MARKET_LABELS[key] || String(market || "1+ Point");
}

/* True when a price is model-estimated rather than a real book line. */
const ESTIMATED_PRICE_SOURCES = new Set(["", "consensus", "baseline", "demo", "puckluck demo"]);
function isEstimatedPrice(source) {
  return ESTIMATED_PRICE_SOURCES.has(String(source || "").trim().toLowerCase());
}

function priceSourceNote(source) {
  return isEstimatedPrice(source)
    ? " · est. price"
    : ` · ${esc(source)}`;
}

function propPick(p, season) {
  const rawMarket = String(p.best_prop_market || "POINTS");
  const market = propMarketLabel(rawMarket);
  const prob =
    /goal/i.test(rawMarket)
      ? Number(p.prob_goal || 0)
      : /assist/i.test(rawMarket)
        ? Number(p.prob_assist || 0)
        : Number(p.prob_point || 0);
  const fair =
    (/goal/i.test(rawMarket) && p.fair_goal_odds) ||
    (/assist/i.test(rawMarket) && p.fair_assist_odds) ||
    p.fair_point_odds;
  const [primary] = getTeamColors(p.team_abbr);
  const opp = p.opponent_abbr ? ` vs ${esc(p.opponent_abbr)}` : "";
  return {
    ev: Number(p.best_prop_ev || 0),
    sortProb: prob,
    html: `<div class="pick-row pick-prop" style="--team-color:${primary}">
  ${renderPlayerHeadshot(p, season)}
  <div class="pick-main">
    <div class="pick-title"><span class="pick-tag tag-prop">PROP</span>${esc(
      market
    )} <span class="pick-odds">${fmtOdds(p.best_prop_odds)}</span></div>
    <div class="pick-sub">${esc(p.display_name)} · ${esc(
      p.team_abbr || ""
    )}${opp} · Prob ${Math.round(prob * 100)}%${
      fair !== undefined ? ` · Fair ${fmtOdds(fair)}` : ""
    }${priceSourceNote(p.odds_source)}</div>
  </div>
  <div class="pick-edge ${evTier(p.best_prop_ev)}"><b>${fmtSignedPct(
    p.best_prop_ev
  )}</b><small>EV</small></div>
</div>`,
  };
}

function gameLean(g) {
  const delta = Number(g.expected_total || 0) - Number(g.line || 0);
  const lean = delta >= 0 ? "OVER" : "UNDER";
  const [primary] = getTeamColors(g.home_abbr);
  return {
    ev: Math.abs(delta),
    html: `<div class="pick-row pick-lean" style="--team-color:${primary}">
  <span class="crest-pair">${renderTeamLogo(g.away_abbr)}${renderTeamLogo(
      g.home_abbr
    )}</span>
  <div class="pick-main">
    <div class="pick-title"><span class="pick-tag tag-lean">LEAN</span>${lean} ${fmt1(
      g.line
    )}</div>
    <div class="pick-sub">${esc(g.away_abbr)} @ ${esc(
      g.home_abbr
    )} · Model ${fmt1(g.expected_total)} (${fmtSigned1(delta)})${priceSourceNote(
      g.line_source
    )}</div>
  </div>
  <div class="pick-edge edge-cool"><b>${fmtSigned1(delta)}</b><small>edge</small></div>
</div>`,
  };
}

function scorerLean(p, season) {
  const prob = Number(p.prob_point || 0);
  const [primary] = getTeamColors(p.team_abbr);
  const opp = p.opponent_abbr ? ` vs ${esc(p.opponent_abbr)}` : "";
  return {
    ev: prob,
    html: `<div class="pick-row pick-lean" style="--team-color:${primary}">
  ${renderPlayerHeadshot(p, season)}
  <div class="pick-main">
    <div class="pick-title"><span class="pick-tag tag-lean">LEAN</span>${esc(
      p.display_name
    )} 1+ Pt</div>
    <div class="pick-sub">${esc(p.team_abbr || "")}${opp} · Prob ${Math.round(
      prob * 100
    )}%</div>
  </div>
  <div class="pick-edge edge-cool"><b>${Math.round(
    prob * 100
  )}%</b><small>prob</small></div>
</div>`,
  };
}

export function renderBestPicksTab(
  games = [],
  pointScorers = [],
  { season = "" } = {}
) {
  const activeGames = (games || []).filter((g) => !isEnded(g));
  const totalPlays = activeGames
    .filter((g) => g.is_positive_ev === true)
    .map(totalPick)
    .sort((a, b) => b.ev - a.ev || b.sortProb - a.sortProb);
  const propPlays = (pointScorers || [])
    .filter((p) => p.is_positive_ev === true)
    .map((p) => propPick(p, season))
    .sort((a, b) => b.ev - a.ev || b.sortProb - a.sortProb);
  const playCount = totalPlays.length + propPlays.length;

  const section = (title, count, picks, offset) =>
    picks.length
      ? `<div class="section-title picks-subhead picks-group"><span>${title}</span><span class="st-meta">${count}</span></div>${picks
          .map(
            (pick, i) =>
              `<div class="pick-wrap"><span class="pick-rank">${
                offset + i + 1
              }</span>${pick.html}</div>`
          )
          .join("")}`
      : "";

  let rows =
    section("Game Totals", totalPlays.length, totalPlays, 0) +
    section("Player Props", propPlays.length, propPlays, totalPlays.length);

  if (playCount < 5) {
    const leans = [
      ...activeGames
        .filter((g) => g.is_positive_ev !== true)
        .map(gameLean),
      ...(pointScorers || [])
        .filter((p) => p.is_positive_ev !== true)
        .map((p) => scorerLean(p, season)),
    ]
      .sort((a, b) => b.ev - a.ev)
      .slice(0, Math.max(0, 8 - playCount));
    if (leans.length) {
      rows += `<div class="section-title picks-subhead">Model Leans<span class="st-meta">no bet</span></div>${leans
        .map((l) => `<div class="pick-wrap">${l.html}</div>`)
        .join("")}`;
    }
  }

  return `<section class="tab-panel" data-panel="picks">
  <div class="section-title">Today's Top Plays<span class="st-meta">${playCount} +EV</span></div>
  ${
    rows ||
    `<div class="empty-state">No edges on tonight's slate — check back closer to puck drop.</div>`
  }
</section>`;
}

/* ---------- Fantasy tabs ---------- */

function slotRank(slot) {
  const idx = SLOT_ORDER.indexOf(String(slot || "").toUpperCase());
  return idx === -1 ? SLOT_ORDER.length : idx;
}

export function renderRosterTab(roster = [], { season = "", team = null } = {}) {
  const rows = [...roster]
    .sort((a, b) => slotRank(a.selected_slot) - slotRank(b.selected_slot))
    .map((p) => {
      const [primary] = getTeamColors(p.team_abbr);
      const subParts = [
        p.position,
        p.team_abbr,
        p.opponent_abbr && `vs ${p.opponent_abbr}`,
      ]
        .filter(Boolean)
        .map(esc);
      const defRtg = Number(p.opp_def_rtg_rel);
      const defPill =
        Number.isFinite(defRtg) && defRtg !== 0
          ? `<span class="def-pill ${defRtg > 0 ? "def-soft" : "def-tough"}">DefRtg ${fmtSigned1(defRtg)}</span>`
          : "";
      const hits = Number(p.hits);
      const blocks = Number(p.blocks);
      const extraStats = [
        Number.isFinite(hits) && hits > 0
          ? `<span>HIT <b>${fmtInt(hits)}</b></span>`
          : "",
        Number.isFinite(blocks) && blocks > 0
          ? `<span>BLK <b>${fmtInt(blocks)}</b></span>`
          : "",
      ]
        .filter(Boolean)
        .join("");

      return `<div class="roster-row" style="--team-color:${primary}">
  <div class="slot-chip">${esc(p.selected_slot || p.position || "–")}</div>
  <div class="roster-main">
    <div class="player-head">
      ${renderPlayerHeadshot(p, season)}
      <div class="player-title-wrap">
        <div class="player-name">${esc(p.display_name)}${goalieBadge(
          p.goalie_status
        )}</div>
        <div class="player-sub">${subParts.join(" · ")}${
          defPill ? " " + defPill : ""
        }</div>
      </div>
    </div>
    <div class="stat-line">
      <span>SOG <b>${fmtInt(p.shots_on_goal)}</b></span>
      <span>iXG <b>${fmt2(p.ixg)}</b></span>
      <span>G <b>${fmtInt(p.goals)}</b></span>
      <span>A <b>${fmtInt(p.assists)}</b></span>
      ${extraStats}
    </div>
  </div>
  <div class="fp-col">${fmt1(p.projected_fp)}<small>proj FP</small></div>
</div>`;
    })
    .join("");
  return `<section class="tab-panel" data-panel="roster">
  ${renderFantasyContext(team)}
  <div class="section-title">Tonight's Roster</div>
  ${rows || `<div class="player-sub">No roster data.</div>`}
</section>`;
}

function renderWaiverRow(p, season = "") {
  const [primary] = getTeamColors(p.team_abbr);
  const ixg60 = Number(p.ixg_per60_ewma || 0);
  const icorsi60 = Number(p.icorsi_per60_ewma || 0);
  const pp1 = Number(p.pp_toi_share_ewma || 0);
  const ixgPct = Math.min(100, Math.max(6, Math.round((ixg60 / 1.2) * 100)));
  const corsiPct = Math.min(100, Math.max(6, Math.round((icorsi60 / 18) * 100)));
  const ppPct = Math.min(100, Math.max(4, Math.round(pp1 * 100)));
  const oppPart = p.opponent_abbr ? ` · vs ${esc(p.opponent_abbr)}` : "";

  return `<div class="fa-row" style="--team-color:${primary}">
  <div class="fa-main">
    <div class="player-head">
      ${renderPlayerHeadshot(p, season)}
      <div class="player-title-wrap">
        <div class="player-name">${esc(p.display_name)}</div>
        <div class="player-sub">${esc(p.position || "")} · ${esc(
          p.team_abbr || ""
        )}${oppPart} · ${fmt1(p.percent_owned)}% owned</div>
      </div>
    </div>
    <div class="fa-metrics">
      <span class="metric-chip">iXG/60 <b>${fmt2(ixg60)}</b><i class="mini-bar"><i style="width:${ixgPct}%"></i></i></span>
      <span class="metric-chip">iCorsi/60 <b>${fmt2(icorsi60)}</b><i class="mini-bar"><i style="width:${corsiPct}%"></i></i></span>
      <span class="metric-chip">PP1 <b>${fmt2(pp1)}</b><i class="mini-bar"><i style="width:${ppPct}%"></i></i></span>
      <span class="metric-chip">proj FP <b>${fmt1(p.projected_fp)}</b></span>
    </div>
  </div>
  <div class="fp-col">${fmt1(p.projected_fp)}<small>proj FP</small></div>
</div>`;
}

function filterAndRankWaivers(list = [], query = "") {
  const q = String(query || "").toLowerCase().trim();
  return [...list]
    .filter(
      (p) =>
        !q ||
        String(p.display_name || "")
          .toLowerCase()
          .includes(q) ||
        String(p.team_abbr || "")
          .toLowerCase()
          .includes(q) ||
        String(p.position || "")
          .toLowerCase()
          .includes(q)
    )
    .sort(
      (a, b) =>
        Number(b.ixg_per60_ewma || 0) - Number(a.ixg_per60_ewma || 0)
    );
}

export function renderAddDropTab(
  recommendation,
  {
    stale = false,
    waiverTargets = [],
    query = "",
    season = "",
    team = null,
  } = {}
) {
  const rankedWaivers = filterAndRankWaivers(waiverTargets, query);
  const waiverRows = rankedWaivers
    .map((p) => renderWaiverRow(p, season))
    .join("");
  const waiverSection = `<div class="waiver-connected-block">
    <div class="section-title">Available Waiver Streamers</div>
    <input class="fa-search" id="waiver-search" type="search" placeholder="Search waiver targets by player, team, or position..." value="${esc(
      query
    )}" />
    ${waiverRows || `<div class="player-sub">No waiver streamers match.</div>`}
  </div>`;

  const context = renderFantasyContext(team);

  if (!recommendation) {
    return `<section class="tab-panel" data-panel="adddrop">
  ${context}
  <div class="section-title">Streaming Solver</div>
  <div class="adddrop-card"><div class="player-sub">No recommendation today.</div></div>
  ${waiverSection}
</section>`;
  }

  const action = String(recommendation.action || "");
  const actionBadge =
    action === "RECOMMEND_SWAP" || action === "RECOMMEND_ADD"
      ? badge("RECOMMEND_ADD")
      : action === "HOLD_ASSET" || action === "HOLD"
        ? badge("HOLD")
        : badge(action || "HOLD");
  const staleAttrs = stale ? ` stale-disabled" data-stale="true"` : "";
  const staleNotice = stale
    ? `<div class="stale-banner">STALE DATA — recommendations frozen</div>`
    : "";
  const pair = [recommendation.streamer_name, recommendation.drop_name]
    .filter(Boolean)
    .map(esc)
    .join(" ↔ ");

  const shortGain = Math.max(0, Number(recommendation.short_term_gain || 0));
  const depMag = Math.abs(Number(recommendation.asset_depreciation_penalty || 0));
  const totalSpan = Math.max(1, shortGain + depMag);
  const gainPct = Math.round((shortGain / totalSpan) * 100);
  const depPct = 100 - gainPct;

  return `<section class="tab-panel" data-panel="adddrop">
  ${context}
  <div class="section-title">Streaming Solver</div>
  <article class="adddrop-card${staleAttrs}>
    ${staleNotice}
    <div class="adddrop-head">
      <div class="adddrop-pair">
        ${recommendation.streamer_team ? renderTeamLogo(recommendation.streamer_team) : ""}
        <span class="player-name">${pair || "Streaming decision"}</span>
        ${recommendation.drop_team ? renderTeamLogo(recommendation.drop_team) : ""}
      </div>
      ${actionBadge}
    </div>
    <div class="adddrop-explanation">${esc(recommendation.explanation)}</div>
    <div class="solver-comp-bar" title="Short-term streaming gain vs multi-week asset depreciation">
      <div class="comp-gain" style="width:${gainPct}%"></div>
      <div class="comp-loss" style="width:${depPct}%"></div>
    </div>
    <details class="math-drawer">
      <summary>Multi-week net math</summary>
      <div class="math-row"><span>Tonight gain</span><span>${fmtSigned1(
        recommendation.tonight_gain
      )} FP</span></div>
      <div class="math-row"><span>Weekly short-term gain</span><span>${fmtSigned1(
        recommendation.short_term_gain
      )} FP</span></div>
      <div class="math-row"><span>Asset depreciation (${fmt1(
        recommendation.horizon_weeks
      )} wk)</span><span>${fmtSigned1(
        recommendation.asset_depreciation_penalty
      )} FP</span></div>
      <div class="math-row"><span><b>Net delta</b></span><span><b>${fmtSigned1(
        recommendation.delta_value
      )} FP</b></span></div>
    </details>
  </article>
  ${waiverSection}
</section>`;
}

export function renderFreeAgentsTab(
  freeAgents = [],
  { query = "", season = "" } = {}
) {
  const rows = filterAndRankWaivers(freeAgents, query)
    .map((p) => renderWaiverRow(p, season))
    .join("");
  return `<section class="tab-panel" data-panel="agents">
  <div class="section-title">Waiver Pool</div>
  <input class="fa-search" id="fa-search" type="search" placeholder="Search free agents" value="${esc(
    query
  )}" />
  ${rows || `<div class="player-sub">No free agents match.</div>`}
</section>`;
}

/* ---------- Game Totals ---------- */

export function renderGameTotalsTab(games = []) {
  const rank = (g) => {
    if (isEnded(g)) return { tier: 2, edge: 0 };
    if (g.is_positive_ev === true) return { tier: 0, edge: Number(g.ev || 0) };
    return {
      tier: 1,
      edge: Math.abs(Number(g.expected_total || 0) - Number(g.line || 0)),
    };
  };
  const cards = [...games]
    .sort((a, b) => {
      const ra = rank(a);
      const rb = rank(b);
      return (
        ra.tier - rb.tier ||
        rb.edge - ra.edge ||
        String(a.start_time_utc || "").localeCompare(
          String(b.start_time_utc || "")
        )
      );
    })
    .map((g) => {
      const ended = isEnded(g);
      const final = isFinal(g);
      const [awayColor] = getTeamColors(g.away_abbr);
      const [homeColor] = getTeamColors(g.home_abbr);
      const evBadge =
        g.is_positive_ev === true && !ended ? badge("+EV") : "";
      const statusBadge = ended
        ? badge(String(g.status).toUpperCase())
        : g.status && g.status !== "Scheduled"
          ? badge(String(g.status).toUpperCase())
          : "";
      const time = fmtGameTime(g.start_time_utc);
      const goalies = [
        `${esc(g.away_abbr)} ${goalieBadge(g.away_goalie_status)}`,
        `${esc(g.home_abbr)} ${goalieBadge(g.home_goalie_status)}`,
      ].join(" · ");

      const expTotal = Number(g.expected_total || 0);
      const line = Number(g.line || 0);
      const delta = expTotal - line;
      const dirClass =
        delta > 0.05 ? "edge-over" : delta < -0.05 ? "edge-under" : "edge-even";
      const dirLabel =
        delta > 0.05
          ? "MODEL LEANS OVER"
          : delta < -0.05
            ? "MODEL LEANS UNDER"
            : "MODEL ON LINE";
      const probOver = Number(g.prob_over || 0);
      const probOverPct = Math.round(probOver * 100);
      const probUnderPct = 100 - probOverPct;
      const meterClass =
        probOver >= 0.55
          ? "meter-over"
          : probOver <= 0.45
            ? "meter-under"
            : "meter-even";
      const lamAway = Number.isFinite(Number(g.lambda_away))
        ? Number(g.lambda_away)
        : expTotal * 0.48;
      const lamHome = Number.isFinite(Number(g.lambda_home))
        ? Number(g.lambda_home)
        : Math.max(0, expTotal - lamAway);

      const bestSide = String(g.best_side || "OVER").toUpperCase();
      const leanOutcome =
        g.lean_hit === true
          ? `<span class="lean-outcome lean-hit">LEAN ${esc(bestSide)} ${fmt1(line)} ✓</span>`
          : g.lean_hit === false
            ? `<span class="lean-outcome lean-miss">LEAN ${esc(bestSide)} ${fmt1(line)} ✗</span>`
            : final &&
                Number(g.final_total) === line
              ? `<span class="lean-outcome">PUSH</span>`
              : "";
      const finalRow = final
        ? `<div class="final-row"><b>${esc(g.away_abbr)} ${esc(
            String(g.final_away_score ?? "–")
          )} – ${esc(g.home_abbr)} ${esc(
            String(g.final_home_score ?? "–")
          )}</b><span class="final-total">total ${fmt1(
            g.final_total
          )}</span>${leanOutcome}</div>`
        : "";
      const bestPickBanner =
        g.is_positive_ev === true && !ended
          ? `<div class="ev-pick-banner">BEST PICK: <strong>${esc(
              bestSide
            )} ${fmt1(g.line)} ${fmtOdds(
              bestSide === "UNDER" ? g.under_odds : g.over_odds
            )}</strong> (${fmtSignedPct(g.ev)} EV)</div>`
          : "";

      const lineSrc = String(g.line_source || "consensus");
      const lineSrcTag = isEstimatedPrice(lineSrc)
        ? `<span class="mls-src mls-src-est">EST. LINE</span>`
        : `<span class="mls-src">${esc(lineSrc)}</span>`;

      const metrics = ended && !final
        ? ""
        : `<div class="model-line-strip ${dirClass}">
    <div class="mls-box"><span class="mls-label">MODEL</span><b>${fmt1(
      expTotal
    )}</b></div>
    <div class="mls-edge"><b>${fmtSigned1(delta)}</b><span>${dirLabel}</span></div>
    <div class="mls-box"><span class="mls-label">LINE</span><b>${fmt1(
      line
    )}</b>${lineSrcTag}</div>
  </div>
  <div class="over-meter ${meterClass}">
    <div class="over-meter-labels"><span>U ${probUnderPct}%</span><span>O ${probOverPct}%</span></div>
    <div class="over-bar"><i class="over-mid"></i><div class="over-fill" style="width:${Math.min(
      100,
      Math.max(0, probOverPct)
    )}%"></div></div>
  </div>${
    final
      ? finalRow
      : `<div class="game-metrics">
    <span class="odds-chip${
      bestSide === "OVER" ? " odds-chip-best" : ""
    }">O ${fmtOdds(g.over_odds)}</span>
    <span class="odds-chip${
      bestSide === "UNDER" ? " odds-chip-best" : ""
    }">U ${fmtOdds(g.under_odds)}</span>
    <span class="lambda-chip">${esc(g.away_abbr)} λ <b>${fmt2(
      lamAway
    )}</b> · ${esc(g.home_abbr)} λ <b>${fmt2(lamHome)}</b></span>
    ${evBadge}
  </div>
  ${bestPickBanner}`
  }`;

      return `<article class="game-card${
        g.is_positive_ev === true && !ended ? " game-card-ev" : ""
      }${ended ? " game-card-ended" : ""}">
  <div class="game-card-accent" style="--away-color:${awayColor};--home-color:${homeColor}"></div>
  <div class="game-top">
    <span class="game-teams">${renderTeamLogo(g.away_abbr)} ${esc(
      g.away_abbr
    )} <span class="at-sep">@</span> ${renderTeamLogo(g.home_abbr)} ${esc(
        g.home_abbr
      )}</span>
    <span class="game-time">${esc(time)} ${statusBadge}</span>
  </div>
  ${metrics}
  <div class="game-goalies">${goalies}</div>
</article>`;
    })
    .join("");
  const statusOf = (g) => String(g.status || "").toUpperCase();
  const counts = ["SCHEDULED", "LIVE", "FINAL", "POSTPONED"].map((s) => [
    s,
    games.filter((g) => statusOf(g) === s).length,
  ]);
  const pulse = counts.some(([, n]) => n > 0)
    ? `<div class="pulse-strip">${counts
        .filter(([, n]) => n > 0)
        .map(
          ([s, n]) =>
            `<span class="pulse-chip pulse-${s.toLowerCase()}">${n} ${s}</span>`
        )
        .join("")}</div>`
    : "";
  return `<section class="tab-panel" data-panel="totals">
  <div class="section-title">Tonight's Slate<span class="st-meta">${games.length} games</span></div>
  ${pulse}
  ${cards || `<div class="player-sub">No games scheduled.</div>`}
</section>`;
}

/* ---------- Point Scorers ---------- */

export function renderPointScorersTab(
  pointScorers = [],
  { query = "", filter = "ALL", season = "" } = {}
) {
  const q = String(query || "").toLowerCase().trim();
  const activeFilter = SCORER_FILTERS.includes(String(filter).toUpperCase())
    ? String(filter).toUpperCase()
    : "ALL";

  const filtered = [...pointScorers]
    .filter((p) => {
      if (activeFilter === "+EV" && p.is_positive_ev !== true) return false;
      if (!q) return true;
      return (
        String(p.display_name || "")
          .toLowerCase()
          .includes(q) ||
        String(p.team_abbr || "")
          .toLowerCase()
          .includes(q) ||
        String(p.opponent_abbr || "")
          .toLowerCase()
          .includes(q) ||
        String(p.position || "")
          .toLowerCase()
          .includes(q)
      );
    })
    .sort((a, b) => {
      if (activeFilter === "GOALS") {
        return (
          Number(b.prob_goal || 0) - Number(a.prob_goal || 0) ||
          Number(b.best_prop_ev || 0) - Number(a.best_prop_ev || 0)
        );
      }
      if (activeFilter === "ASSISTS") {
        return (
          Number(b.prob_assist || 0) - Number(a.prob_assist || 0) ||
          Number(b.best_prop_ev || 0) - Number(a.best_prop_ev || 0)
        );
      }
      if (activeFilter === "+EV") {
        return (
          Number(b.best_prop_ev || 0) - Number(a.best_prop_ev || 0) ||
          Number(b.prob_point || 0) - Number(a.prob_point || 0)
        );
      }
      return (
        Number(b.prob_point || 0) - Number(a.prob_point || 0) ||
        Number(b.best_prop_ev || 0) - Number(a.best_prop_ev || 0)
      );
    });

  const filterBar = `<div class="scorer-controls">
    <input class="fa-search" id="scorer-search" type="search" placeholder="Search skaters by name, team, or matchup..." value="${esc(
      query
    )}" />
    <div class="scorer-filter-bar" role="group" aria-label="Scorer Filters">
      ${SCORER_FILTERS.map(
        (f) =>
          `<button type="button" class="scorer-filter-btn${
            f === activeFilter ? " active" : ""
          }" data-scorer-filter="${f}">${esc(f)}</button>`
      ).join("")}
    </div>
  </div>`;

  const rows = filtered
    .map((p) => {
      const [primary] = getTeamColors(p.team_abbr);
      const probPt = Number(p.prob_point || 0);
      const probGoal = Number(p.prob_goal || 0);
      const probAst = Number(p.prob_assist || 0);
      const projPts =
        p.projected_points !== undefined
          ? p.projected_points
          : Number(p.projected_goals || 0) + Number(p.projected_assists || 0);

      const realBook = !isEstimatedPrice(p.odds_source);
      const evBadge = p.is_positive_ev === true ? badge("+EV") : "";
      const profitBanner =
        p.is_positive_ev === true
          ? `<div class="scorer-ev-banner">PROFIT EDGE: <strong>${esc(
              propMarketLabel(p.best_prop_market)
            )} ${fmtOdds(p.best_prop_odds)}</strong> (${fmtSignedPct(
              p.best_prop_ev
            )} EV · Fair ${fmtOdds(
              p.fair_point_odds || p.fair_goal_odds
            )})<span class="src-note">${esc(
              String(p.odds_source || "")
            )}</span></div>`
          : "";

      const pill = (label, prob, odds, src, extraClass = "") => `<div class="prob-pill ${probTier(
        prob
      )}">
      <div class="prob-pill-top"><span>${label} <b>${Math.round(
        prob * 100
        )}%</b></span><span class="odds-tag">${fmtOdds(odds)}${
        isEstimatedPrice(src) ? " est" : ""
      }</span></div>
      <div class="prob-bar"><div class="prob-fill ${extraClass}" style="width:${Math.min(
        100,
        Math.round(prob * 100)
      )}%"></div></div>
    </div>`;

      return `<div class="scorer-row${
        p.is_positive_ev === true ? " scorer-row-ev" : ""
      }" style="--team-color:${primary}">
  <div class="scorer-top">
    <div class="player-head">
      ${renderPlayerHeadshot(p, season)}
      <div class="player-title-wrap">
        <div class="player-name">${esc(p.display_name)}${evBadge}</div>
        <div class="player-sub">${esc(p.position || "F")} · ${esc(
          p.team_abbr || ""
        )}${p.opponent_abbr ? ` vs ${esc(p.opponent_abbr)}` : ""} · SOG <b>${fmt1(
          p.projected_sog ?? p.shots_on_goal
        )}</b> · iXG/60 <b>${fmt2(p.ixg_per60_ewma)}</b>${
          realBook
            ? ` · <span class="src-note">${esc(String(p.odds_source))}</span>`
            : ""
        }</div>
      </div>
    </div>
    <div class="fp-col">${fmt2(projPts)}<small>exp Pts</small></div>
  </div>
  <div class="scorer-prob-grid">
    ${pill("1+ Pt:", probPt, p.market_point_odds, p.market_point_src)}
    ${pill("Goal:", probGoal, p.market_goal_odds, p.market_goal_src, "prob-fill-goal")}
    ${pill("Assist:", probAst, p.market_assist_odds, p.market_assist_src, "prob-fill-assist")}
  </div>
  ${profitBanner}
</div>`;
    })
    .join("");

  return `<section class="tab-panel" data-panel="scorers">
  <div class="section-title">Point Scorers &amp; Prop Edges</div>
  ${filterBar}
  ${rows || `<div class="player-sub">No point scorers match current filter.</div>`}
</section>`;
}

/* ---------- Dashboard shell ---------- */

export function renderDashboard(
  report,
  {
    nowMs = Date.now(),
    activeTab = "picks",
    teamIndex = 0,
    query = "",
    scorerQuery = "",
    scorerFilter = "ALL",
  } = {}
) {
  const stale = isReportStale(
    report?.generated_at,
    nowMs,
    Number(report?.stale_after_hours) || STALE_THRESHOLD_HOURS
  );
  const activeTeam = resolveActiveTeam(report, teamIndex);
  const season = seasonSlug(
    report?.active_slate_date || report?.generated_at || ""
  );
  const panels = [
    renderBestPicksTab(report?.games || [], report?.point_scorers || [], {
      season,
    }),
    renderPointScorersTab(report?.point_scorers || [], {
      query: scorerQuery,
      filter: scorerFilter,
      season,
    }),
    renderGameTotalsTab(report?.games || []),
    renderRosterTab(activeTeam.roster, { season, team: activeTeam }),
    renderAddDropTab(activeTeam.recommendation, {
      stale,
      waiverTargets: activeTeam.waiver_targets,
      query,
      season,
      team: activeTeam,
    }),
  ];
  const panelHtml = panels
    .map((html, i) =>
      html.replace(
        'class="tab-panel"',
        `class="tab-panel${TAB_DEFS[i][0] === activeTab ? " active" : ""}"`
      )
    )
    .join("");
  const pickCount =
    (report?.games || []).filter((g) => g.is_positive_ev === true).length +
    (report?.point_scorers || []).filter((p) => p.is_positive_ev === true)
      .length;
  const nav = TAB_DEFS.map(
    ([key, label]) =>
      `<button class="nav-btn${key === activeTab ? " active" : ""}" data-tab="${key}">${label}${
        key === "picks" && pickCount
          ? `<span class="nav-count">${pickCount}</span>`
          : ""
      }</button>`
  ).join("");

  return `<div class="pl-dashboard">
${renderHeader(report, { nowMs, teamIndex })}
<main class="pl-main">${panelHtml}</main>
<footer class="pl-foot">Bivariate-Poisson model · Lines via The Odds Gap + Bovada · Data: NHL Stats</footer>
<nav class="bottom-nav">${nav}</nav>
</div>`;
}

/* ---------- Browser glue (inert under node --test) ---------- */

if (typeof document !== "undefined") {
  let report = null;
  let state = {
    activeTab: "picks",
    teamIndex: 0,
    query: "",
    scorerQuery: "",
    scorerFilter: "ALL",
  };

  const fetchLatestReport = async () => {
    try {
      const resp = await fetch("./report.json", { cache: "no-store" });
      if (resp.ok) {
        report = await resp.json();
      }
    } catch {
      if (!report) report = {};
    }
  };

  const mount = (focusId = null) => {
    const root = document.getElementById("app");
    if (!root) return;
    root.innerHTML = renderDashboard(report || {}, state);

    document.querySelectorAll(".nav-btn").forEach((btn) =>
      btn.addEventListener("click", () => {
        state.activeTab = btn.dataset.tab || "picks";
        mount();
      })
    );

    document.querySelectorAll(".team-switch-btn").forEach((btn) =>
      btn.addEventListener("click", () => {
        state.teamIndex = Number(btn.dataset.teamIndex) || 0;
        mount();
      })
    );

    document.querySelectorAll(".scorer-filter-btn").forEach((btn) =>
      btn.addEventListener("click", () => {
        state.scorerFilter = btn.dataset.scorerFilter || "ALL";
        mount();
      })
    );

    document
      .getElementById("waiver-search")
      ?.addEventListener("input", (e) => {
        state.query = e.target.value;
        mount("waiver-search");
      });

    document
      .getElementById("scorer-search")
      ?.addEventListener("input", (e) => {
        state.scorerQuery = e.target.value;
        mount("scorer-search");
      });

    document
      .getElementById("sync-now-btn")
      ?.addEventListener("click", async () => {
        await fetchLatestReport();
        mount();
      });

    if (focusId) {
      const input = document.getElementById(focusId);
      input?.focus();
      const len = input?.value?.length || 0;
      input?.setSelectionRange(len, len);
    }
  };

  document.addEventListener("DOMContentLoaded", async () => {
    await fetchLatestReport();
    mount();
    setInterval(async () => {
      await fetchLatestReport();
      mount();
    }, AUTO_REFRESH_INTERVAL_MS);
  });

  if (typeof navigator !== "undefined" && "serviceWorker" in navigator) {
    navigator.serviceWorker.register("./sw.js").catch(() => {});
  }
}
