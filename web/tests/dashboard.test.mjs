/* PuckLuck frontend policy suite (SRS §8 Gate 3).
 * Run: node --test web/tests/
 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  isReportStale,
  renderAddDropTab,
  renderDashboard,
  renderFreeAgentsTab,
  renderGameTotalsTab,
  renderRosterTab,
} from "../app.js";

const NOW = Date.parse("2026-10-23T12:00:00Z");
const FRESH = new Date(NOW - 2 * 3600 * 1000).toISOString();
const STALE_25H = new Date(NOW - 25 * 3600 * 1000).toISOString();
const STALE_27H = new Date(NOW - 27 * 3600 * 1000).toISOString();

function makeReport(overrides = {}) {
  return {
    generated_at: FRESH,
    stale_after_hours: 26,
    team_summary: {
      team_name: "McDavid Fan Club",
      team_initials: "MF",
      league_name: "PuckLuck Dynasty",
      adds_used: 1,
      adds_limit: 4,
      matchup_score: "84.2 - 71.0",
    },
    roster: [
      {
        player_id: 8478402,
        display_name: "Connor McDavid",
        position: "C",
        selected_slot: "C",
        team_abbr: "EDM",
        opponent_abbr: "COL",
        projected_fp: 8.4,
        shots_on_goal: 4,
        ixg: 0.85,
        goals: 1,
        assists: 2,
      },
      {
        player_id: 9001,
        display_name: "Quiet Winger",
        position: "LW",
        selected_slot: "BN",
        team_abbr: "TOR",
        opponent_abbr: "CAR",
        projected_fp: 0,
        shots_on_goal: 0,
        ixg: 0,
        goals: 0,
        assists: 0,
      },
      {
        player_id: 8479979,
        display_name: "Stuart Skinner",
        position: "G",
        selected_slot: "G",
        team_abbr: "EDM",
        opponent_abbr: "COL",
        goalie_status: "CONFIRMED",
        projected_fp: 5.2,
        shots_on_goal: 0,
      },
    ],
    recommendation: {
      action: "HOLD_ASSET",
      explanation:
        "Short-term streaming gain of +4.2 FP tonight is rejected: dropping Evan Bouchard results in net loss of -6.8 FP over the next 2.5 weeks.",
      delta_value: -6.8,
      tonight_gain: 4.2,
      short_term_gain: 3.2,
      asset_depreciation_penalty: -10.0,
      horizon_weeks: 2.5,
      streamer_name: "Streamer Winger",
      drop_name: "Evan Bouchard",
    },
    games: [
      {
        game_id: 2026020002,
        start_time_utc: "2026-10-24T01:00:00Z",
        home_abbr: "EDM",
        away_abbr: "COL",
        status: "Scheduled",
        expected_total: 6.7,
        line: 6.5,
        prob_over: 0.56,
        prob_under: 0.44,
        ev: 0.061,
        is_positive_ev: true,
        home_goalie_status: "CONFIRMED",
        away_goalie_status: "PROJECTED",
      },
      {
        game_id: 2026020001,
        start_time_utc: "2026-10-23T17:00:00Z",
        home_abbr: "TOR",
        away_abbr: "CAR",
        status: "POSTPONED",
        expected_total: 7.1,
        line: 6.5,
        prob_over: 0.62,
        prob_under: 0.38,
        ev: 0.15,
        is_positive_ev: true,
        home_goalie_status: "PROJECTED",
        away_goalie_status: "PROJECTED",
      },
    ],
    free_agents: [
      {
        player_id: 10,
        display_name: "Alpha Streamer",
        position: "C",
        team_abbr: "NYR",
        percent_owned: 12.0,
        ixg_per60_ewma: 0.42,
        icorsi_per60_ewma: 8.1,
        pp_toi_share_ewma: 0.55,
      },
      {
        player_id: 11,
        display_name: "Zeta Winger",
        position: "LW",
        team_abbr: "TOR",
        percent_owned: 3.0,
        ixg_per60_ewma: 0.71,
        icorsi_per60_ewma: 9.4,
        pp_toi_share_ewma: 0.61,
      },
      {
        player_id: 12,
        display_name: "Mika Grinder",
        position: "C",
        team_abbr: "FLA",
        percent_owned: 8.0,
        ixg_per60_ewma: 0.55,
        icorsi_per60_ewma: 7.0,
        pp_toi_share_ewma: 0.2,
      },
    ],
    ...overrides,
  };
}

test("renders roster-first dashboard offline with required visual tokens", async () => {
  const css = await readFile(new URL("../styles.css", import.meta.url), "utf8");
  assert.ok(css.includes("#141619"), "missing deep-charcoal base");
  assert.ok(css.includes("#1c2026"), "missing ice-steel surface");
  assert.ok(css.includes("#4da6ff"), "missing frost-cyan accent");

  const manifest = JSON.parse(
    await readFile(new URL("../manifest.webmanifest", import.meta.url), "utf8")
  );
  assert.equal(manifest.display, "standalone");
  assert.equal(manifest.name, "PuckLuck");

  const html = renderDashboard(makeReport(), { nowMs: NOW });
  assert.ok(html.includes("01 Roster"));
  assert.ok(html.includes("02 Add / Drop"));
  assert.ok(html.includes("03 Game Totals"));
  assert.ok(html.includes("04 Free Agents"));
  assert.ok(html.includes("Adds Left: <strong>3/4</strong>"));
  // Roster is the primary view: its panel and nav button are active.
  assert.ok(html.includes('data-panel="roster"'));
  assert.ok(html.includes('class="tab-panel active"'));
  assert.ok(html.includes('data-tab="roster">01 Roster'));
});

test("enforces 26-hour stale data gate on add/drop recommendations", () => {
  assert.equal(isReportStale(STALE_25H, NOW), false);
  assert.equal(isReportStale(STALE_27H, NOW), true);
  assert.equal(isReportStale(undefined, NOW), true);

  const html = renderDashboard(makeReport({ generated_at: STALE_27H }), {
    nowMs: NOW,
  });
  assert.ok(html.includes("STALE DATA"));
  assert.ok(html.includes("stale-disabled"));
  assert.ok(html.includes('data-stale="true"'));
});

test("handles postponed games, zero-shot appearances, and unconfirmed goalies", () => {
  const report = makeReport();
  const totalsHtml = renderGameTotalsTab(report.games);
  assert.ok(totalsHtml.includes("POSTPONED"));
  // The postponed card must not surface a +EV edge tag even though the
  // underlying record has is_positive_ev=true.
  const postponedCard = totalsHtml.split('<article class="game-card">')[1];
  assert.ok(postponedCard.includes("POSTPONED"));
  assert.ok(!postponedCard.includes("+EV"), "postponed game leaked +EV tag");
  // The active game DOES carry the +EV badge.
  const activeCard = totalsHtml.split('<article class="game-card">')[2];
  assert.ok(activeCard.includes("+EV"));
  // Goalie badges on game cards.
  assert.ok(activeCard.includes("CONFIRMED"));
  assert.ok(activeCard.includes("PROJECTED"));

  // Zero-shot / zero-point appearance renders cleanly.
  const rosterHtml = renderRosterTab(report.roster);
  assert.ok(!rosterHtml.includes("NaN"));
  assert.ok(!rosterHtml.includes("undefined"));
  assert.ok(!rosterHtml.includes("null"));
  assert.ok(rosterHtml.includes(">0<"));
  assert.ok(rosterHtml.includes("0.0"));
  assert.ok(rosterHtml.includes("CONFIRMED"));
});

test("ranks and filters free agents by 15-game EWMA process metrics", () => {
  const html = renderFreeAgentsTab(makeReport().free_agents);
  const zeta = html.indexOf("Zeta Winger");
  const mika = html.indexOf("Mika Grinder");
  const alpha = html.indexOf("Alpha Streamer");
  assert.ok(zeta !== -1 && mika !== -1 && alpha !== -1);
  assert.ok(zeta < mika, "Zeta (0.71 ixg60) should outrank Mika (0.55)");
  assert.ok(mika < alpha, "Mika (0.55) should outrank Alpha (0.42)");

  const filtered = renderFreeAgentsTab(makeReport().free_agents, {
    query: "mika",
  });
  assert.ok(filtered.includes("Mika Grinder"));
  assert.ok(!filtered.includes("Zeta Winger"));
});

test("add/drop tab renders badges and math drawer", () => {
  const hold = renderAddDropTab(makeReport().recommendation);
  assert.ok(hold.includes("HOLD"));
  assert.ok(hold.includes("-6.8 FP"));
  assert.ok(hold.includes("+4.2 FP"));

  const swap = renderAddDropTab({
    ...makeReport().recommendation,
    action: "RECOMMEND_SWAP",
    delta_value: 18.0,
  });
  assert.ok(swap.includes("RECOMMEND_ADD"));
});
