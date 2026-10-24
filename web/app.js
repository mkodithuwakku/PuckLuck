/* PuckLuck dashboard renderer (SRS §6.1/§6.2).
 *
 * Pure ES module: every render function returns an HTML string so the
 * same code runs in the browser and under `node --test`. Browser glue
 * (fetch + tab binding) is guarded behind `typeof document`.
 */

export const STALE_THRESHOLD_HOURS = 26;

const SLOT_ORDER = ["C", "LW", "RW", "D", "UTIL", "G", "BN", "IR", "IR+"];

const TAB_DEFS = [
  ["roster", "01 Roster"],
  ["adddrop", "02 Add / Drop"],
  ["totals", "03 Game Totals"],
  ["agents", "04 Free Agents"],
];

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
    "STALE DATA": "badge-stale",
  };
  return `<span class="badge ${classes[label] || "badge-projected"}">${esc(
    label
  )}</span>`;
}

function goalieBadge(status) {
  return status ? badge(String(status).toUpperCase()) : "";
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

export function renderHeader(report, { nowMs = Date.now() } = {}) {
  const summary = report?.team_summary ?? {};
  const initials = esc(summary.team_initials || "PL");
  const teamName = esc(summary.team_name || "PuckLuck");
  const leagueName = esc(summary.league_name || "");
  const limit = Number(summary.adds_limit ?? 0);
  const used = Number(summary.adds_used ?? 0);
  const addsLeft = Number.isFinite(limit) ? Math.max(0, limit - used) : 0;
  const matchup = esc(summary.matchup_score || "");

  const staleHours = Number(report?.stale_after_hours) || STALE_THRESHOLD_HOURS;
  const stale = isReportStale(report?.generated_at, nowMs, staleHours);
  const banner = stale
    ? `<div class="stale-banner">STALE DATA</div>`
    : "";

  return `<header class="pl-header">
  <div class="pl-avatar">${initials}</div>
  <div class="pl-team-meta">
    <span class="pl-team-name">${teamName}</span>
    <span class="pl-league-name">${leagueName}</span>
  </div>
  <div class="pl-header-pills">
    <span class="pill">Adds Left: <strong>${addsLeft}/${fmtInt(limit)}</strong></span>
    ${matchup ? `<span class="pill">${matchup}</span>` : ""}
  </div>
  ${banner}
</header>`;
}

function slotRank(slot) {
  const idx = SLOT_ORDER.indexOf(String(slot || "").toUpperCase());
  return idx === -1 ? SLOT_ORDER.length : idx;
}

export function renderRosterTab(roster = []) {
  const rows = [...roster]
    .sort((a, b) => slotRank(a.selected_slot) - slotRank(b.selected_slot))
    .map((p) => {
      const sub = [p.team_abbr, p.opponent_abbr && `vs ${p.opponent_abbr}`]
        .filter(Boolean)
        .map(esc)
        .join(" · ");
      return `<div class="roster-row">
  <div class="slot-chip">${esc(p.selected_slot || p.position || "–")}</div>
  <div>
    <div class="player-name">${esc(p.display_name)}${goalieBadge(
        p.goalie_status
      )}</div>
    <div class="player-sub">${esc(p.position || "")}${
        sub ? " · " + sub : ""
      }</div>
    <div class="stat-line">
      <span>SOG <b>${fmtInt(p.shots_on_goal)}</b></span>
      <span>iXG <b>${fmt2(p.ixg)}</b></span>
      <span>G <b>${fmtInt(p.goals)}</b></span>
      <span>A <b>${fmtInt(p.assists)}</b></span>
    </div>
  </div>
  <div class="fp-col">${fmt1(p.projected_fp)}<small>proj FP</small></div>
</div>`;
    })
    .join("");
  return `<section class="tab-panel" data-panel="roster">
  <div class="section-title">Tonight's Roster</div>
  ${rows || `<div class="player-sub">No roster data.</div>`}
</section>`;
}

export function renderAddDropTab(recommendation, { stale = false } = {}) {
  if (!recommendation) {
    return `<section class="tab-panel" data-panel="adddrop">
  <div class="section-title">Streaming Solver</div>
  <div class="adddrop-card"><div class="player-sub">No recommendation today.</div></div>
</section>`;
  }
  const action = String(recommendation.action || "");
  const actionBadge =
    action === "RECOMMEND_SWAP"
      ? badge("RECOMMEND_ADD")
      : action === "HOLD_ASSET"
        ? badge("HOLD")
        : badge(action || "HOLD");
  const staleAttrs = stale ? ` stale-disabled" data-stale="true` : "";
  const staleNotice = stale
    ? `<div class="stale-banner">STALE DATA — recommendations frozen</div>`
    : "";
  const pair = [recommendation.streamer_name, recommendation.drop_name]
    .filter(Boolean)
    .map(esc)
    .join(" ↔ ");

  return `<section class="tab-panel" data-panel="adddrop">
  <div class="section-title">Streaming Solver</div>
  <article class="adddrop-card${staleAttrs}">
    ${staleNotice}
    <div class="adddrop-head">
      <span class="player-name">${pair || "Streaming decision"}</span>
      ${actionBadge}
    </div>
    <div class="adddrop-explanation">${esc(recommendation.explanation)}</div>
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
</section>`;
}

export function renderGameTotalsTab(games = []) {
  const cards = [...games]
    .sort((a, b) =>
      String(a.start_time_utc || "").localeCompare(
        String(b.start_time_utc || "")
      )
    )
    .map((g) => {
      const postponed =
        String(g.status || "").toUpperCase() === "POSTPONED";
      const evBadge =
        g.is_positive_ev === true && !postponed ? badge("+EV") : "";
      const statusBadge = postponed
        ? badge("POSTPONED")
        : g.status && g.status !== "Scheduled"
          ? badge(String(g.status).toUpperCase())
          : "";
      const time = String(g.start_time_utc || "").slice(11, 16);
      const goalies = [
        `${esc(g.home_abbr)} ${goalieBadge(g.home_goalie_status)}`,
        `${esc(g.away_abbr)} ${goalieBadge(g.away_goalie_status)}`,
      ].join(" · ");
      const metrics = postponed
        ? ""
        : `<div class="game-metrics">
    <span>Model <b>${fmt1(g.expected_total)}</b></span>
    <span>Line <b>${fmt1(g.line)}</b></span>
    <span>P(Over) <b>${fmt2(g.prob_over)}</b></span>
    ${evBadge}
  </div>`;
      return `<article class="game-card">
  <div class="game-top">
    <span class="game-teams">${esc(g.away_abbr)} @ ${esc(
        g.home_abbr
      )}</span>
    <span class="game-time">${esc(time)} UTC ${statusBadge}</span>
  </div>
  ${metrics}
  <div class="game-goalies">${goalies}</div>
</article>`;
    })
    .join("");
  return `<section class="tab-panel" data-panel="totals">
  <div class="section-title">Tonight's Slate</div>
  ${cards || `<div class="player-sub">No games scheduled.</div>`}
</section>`;
}

export function renderFreeAgentsTab(freeAgents = [], { query = "" } = {}) {
  const q = String(query || "").toLowerCase();
  const rows = [...freeAgents]
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
    )
    .map((p) => `<div class="fa-row">
  <div>
    <div class="player-name">${esc(p.display_name)}</div>
    <div class="player-sub">${esc(p.position || "")} · ${esc(
      p.team_abbr || ""
    )} · ${fmt1(p.percent_owned)}% owned</div>
    <div class="fa-metrics">
      <span>iXG/60 <b>${fmt2(p.ixg_per60_ewma)}</b></span>
      <span>iCorsi/60 <b>${fmt2(p.icorsi_per60_ewma)}</b></span>
      <span>PP1 <b>${fmt2(p.pp_toi_share_ewma)}</b></span>
      <span>proj FP <b>${fmt1(p.projected_fp)}</b></span>
    </div>
  </div>
</div>`)
    .join("");
  return `<section class="tab-panel" data-panel="agents">
  <div class="section-title">Waiver Pool</div>
  <input class="fa-search" id="fa-search" type="search" placeholder="Search free agents" value="${esc(
    query
  )}" />
  ${rows || `<div class="player-sub">No free agents match.</div>`}
</section>`;
}

export function renderDashboard(
  report,
  { nowMs = Date.now(), activeTab = "roster", query = "" } = {}
) {
  const stale = isReportStale(
    report?.generated_at,
    nowMs,
    Number(report?.stale_after_hours) || STALE_THRESHOLD_HOURS
  );
  const panels = [
    renderRosterTab(report?.roster || []),
    renderAddDropTab(report?.recommendation, { stale }),
    renderGameTotalsTab(report?.games || []),
    renderFreeAgentsTab(report?.free_agents || [], { query }),
  ];
  const panelHtml = panels
    .map((html, i) =>
      html.replace(
        'class="tab-panel"',
        `class="tab-panel${TAB_DEFS[i][0] === activeTab ? " active" : ""}"`
      )
    )
    .join("");
  const nav = TAB_DEFS.map(
    ([key, label]) =>
      `<button class="nav-btn${key === activeTab ? " active" : ""}" data-tab="${key}">${label}</button>`
  ).join("");

  return `<div class="pl-dashboard">
${renderHeader(report, { nowMs })}
<main class="pl-main">${panelHtml}</main>
<nav class="bottom-nav">${nav}</nav>
</div>`;
}

/* ---------- Browser glue (inert under node --test) ---------- */

if (typeof document !== "undefined") {
  let report = null;

  const mount = (query = "") => {
    const root = document.getElementById("app");
    const active =
      document.querySelector(".nav-btn.active")?.dataset.tab || "roster";
    root.innerHTML = renderDashboard(report, { activeTab: active, query });
    document.querySelectorAll(".nav-btn").forEach((btn) =>
      btn.addEventListener("click", () => {
        document
          .querySelectorAll(".nav-btn")
          .forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        document
          .querySelectorAll(".tab-panel")
          .forEach((p) => p.classList.remove("active"));
        document
          .querySelector(`[data-panel="${btn.dataset.tab}"]`)
          ?.classList.add("active");
      })
    );
    document.getElementById("fa-search")?.addEventListener("input", (e) => {
      mount(e.target.value);
      const input = document.getElementById("fa-search");
      input?.focus();
      input?.setSelectionRange(input.value.length, input.value.length);
    });
  };

  document.addEventListener("DOMContentLoaded", async () => {
    try {
      const resp = await fetch("./report.json", { cache: "no-store" });
      report = await resp.json();
    } catch {
      report = {};
    }
    mount();
  });

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("./sw.js").catch(() => {});
  }
}
