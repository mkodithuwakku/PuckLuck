/* PuckLuck frontend policy suite (SRS §8 Gate 3, betting-hub overhaul).
 * Run: node --test web/tests/dashboard.test.mjs
 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  AUTO_REFRESH_INTERVAL_MS,
  classifyPicks,
  TEAM_COLORS,
  isReportStale,
  renderAddDropTab,
  renderBestPicksTab,
  renderDashboard,
  renderFreeAgentsTab,
  renderGameTotalsTab,
  renderPlayerHeadshot,
  renderPointScorersTab,
  renderRosterTab,
  renderTeamLogo,
  resolveActiveTeam,
  seasonSlug,
} from "../app.js";

const NOW = Date.parse("2026-10-23T12:00:00Z");
const FRESH = new Date(NOW - 2 * 3600 * 1000).toISOString();
const STALE_25H = new Date(NOW - 25 * 3600 * 1000).toISOString();
const STALE_27H = new Date(NOW - 27 * 3600 * 1000).toISOString();

function makeReport(overrides = {}) {
  return {
    generated_at: FRESH,
    stale_after_hours: 26,
    active_slate_date: "2026-10-23",
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
        lambda_home: 3.55,
        lambda_away: 3.15,
        expected_total: 6.7,
        line: 6.5,
        prob_over: 0.56,
        prob_under: 0.44,
        over_odds: -105,
        under_odds: -115,
        ev: 0.061,
        is_positive_ev: true,
        best_side: "OVER",
        line_source: "Draft Kings",
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
      {
        game_id: 2026020003,
        start_time_utc: "2026-10-23T16:00:00Z",
        home_abbr: "CHI",
        away_abbr: "FLA",
        status: "Final",
        expected_total: 6.9,
        line: 6.5,
        prob_over: 0.6,
        prob_under: 0.4,
        over_odds: -110,
        under_odds: -110,
        ev: 0.12,
        is_positive_ev: true,
        best_side: "OVER",
        line_source: "21 books",
        final_away_score: 3,
        final_home_score: 2,
        final_total: 5,
        lean_hit: false,
        home_goalie_status: "CONFIRMED",
        away_goalie_status: "CONFIRMED",
      },
      {
        game_id: 2026020004,
        start_time_utc: "2026-10-23T18:00:00Z",
        home_abbr: "DAL",
        away_abbr: "NYI",
        status: "Live",
        expected_total: 6.4,
        line: 6.5,
        prob_over: 0.49,
        prob_under: 0.51,
        ev: 0.09,
        is_positive_ev: true,
        best_side: "UNDER",
        line_source: "18 books",
        home_goalie_status: "CONFIRMED",
        away_goalie_status: "CONFIRMED",
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
        projected_fp: 3.1,
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
        projected_fp: 4.8,
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
        projected_fp: 3.9,
      },
    ],
    point_scorers: [
      {
        player_id: 8478402,
        display_name: "Connor McDavid",
        position: "C",
        team_abbr: "EDM",
        opponent_abbr: "COL",
        projected_goals: 0.62,
        projected_assists: 1.05,
        projected_points: 1.67,
        projected_sog: 4.1,
        ixg_per60_ewma: 1.15,
        prob_goal: 0.46,
        prob_assist: 0.65,
        prob_point: 0.81,
        market_goal_odds: 125,
        market_assist_odds: -145,
        market_point_odds: -185,
        market_point_src: "Bovada",
        point_ev: 0.245,
        fair_point_odds: -426,
        best_prop_market: "1+ Point",
        best_prop_odds: -185,
        best_prop_ev: 0.245,
        is_positive_ev: true,
      },
      {
        player_id: 8477492,
        display_name: "Nathan MacKinnon",
        position: "C",
        team_abbr: "COL",
        opponent_abbr: "EDM",
        projected_goals: 0.58,
        projected_assists: 0.82,
        projected_points: 1.4,
        projected_sog: 4.6,
        ixg_per60_ewma: 1.08,
        prob_goal: 0.44,
        prob_assist: 0.56,
        prob_point: 0.75,
        market_goal_odds: 110,
        market_assist_odds: -130,
        market_point_odds: -260,
        fair_point_odds: -300,
        best_prop_market: "Anytime Goal",
        best_prop_odds: 110,
        best_prop_ev: -0.076,
        is_positive_ev: false,
      },
      {
        player_id: 8479318,
        display_name: "Auston Matthews",
        position: "C",
        team_abbr: "TOR",
        opponent_abbr: "CAR",
        projected_goals: 0.74,
        projected_assists: 0.44,
        projected_points: 1.18,
        projected_sog: 4.9,
        ixg_per60_ewma: 1.32,
        prob_goal: 0.52,
        prob_assist: 0.36,
        prob_point: 0.69,
        market_goal_odds: 105,
        market_assist_odds: 150,
        market_point_odds: -190,
        fair_goal_odds: -108,
        best_prop_market: "Anytime Goal",
        best_prop_odds: 105,
        best_prop_ev: 0.066,
        is_positive_ev: true,
      },
    ],
    ...overrides,
  };
}

test("renders picks-first dashboard offline with required visual tokens, logos, headshots, and five tabs", async () => {
  const css = await readFile(new URL("../styles.css", import.meta.url), "utf8");
  assert.ok(css.includes("#08171c"), "missing midnight base");
  assert.ok(css.includes("#c4f76b"), "missing lime brand accent");
  assert.ok(css.includes("#4da6ff"), "missing frost-cyan accent");

  const manifest = JSON.parse(
    await readFile(new URL("../manifest.webmanifest", import.meta.url), "utf8")
  );
  assert.equal(manifest.display, "standalone");
  assert.equal(manifest.name, "PuckLuck");

  assert.equal(Object.keys(TEAM_COLORS).length, 32);
  assert.equal(AUTO_REFRESH_INTERVAL_MS, 300_000);

  const logoHtml = renderTeamLogo("EDM");
  assert.ok(
    logoHtml.includes("https://assets.nhle.com/logos/nhl/svg/EDM_dark.svg")
  );
  assert.ok(logoHtml.includes("--team-color:#FF4C00"));
  assert.ok(
    logoHtml.includes("onload"),
    "logo must hide initials underlay once loaded"
  );

  const shotHtml = renderPlayerHeadshot(
    { player_id: 8478402, display_name: "Connor McDavid", team_abbr: "EDM" },
    "20262027"
  );
  assert.ok(
    shotHtml.includes(
      "https://assets.nhle.com/mugs/nhl/20262027/EDM/8478402.png"
    )
  );
  assert.ok(shotHtml.includes(">CM<"), "initials fallback missing");

  assert.equal(seasonSlug("2026-10-23"), "20262027");
  assert.equal(seasonSlug("2027-02-14"), "20262027");

  const html = renderDashboard(makeReport(), { nowMs: NOW });
  assert.ok(html.includes('data-tab="picks">Picks'));
  assert.ok(html.includes('data-tab="scorers">Players'));
  assert.ok(html.includes('data-tab="totals">Totals'));
  assert.ok(html.includes('data-tab="roster">Roster'));
  assert.ok(html.includes('data-tab="adddrop">Moves'));
  assert.ok(html.includes("PuckLuck"));
  assert.ok(html.includes("LIVE · 5m Sync"));
  assert.ok(html.includes('data-panel="picks"'));
  assert.ok(html.includes('class="tab-panel active"'));
});

test("switches between two Yahoo fantasy teams connecting roster, add/drop, and waiver targets", () => {
  const twoTeamReport = makeReport({
    teams: [
      {
        team_key: "465.l.109822.t.6",
        team_summary: {
          team_name: "PuckLuck Builders",
          team_initials: "PB",
          league_name: "Builders League",
          adds_used: 1,
          adds_limit: 4,
          matchup_score: "92.4 - 81.0",
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
        ],
        recommendation: {
          action: "HOLD_ASSET",
          explanation: "Hold Builders roster core.",
          delta_value: -4.2,
          tonight_gain: 2.1,
          short_term_gain: 1.8,
          asset_depreciation_penalty: -6.0,
          horizon_weeks: 2.0,
          streamer_name: "Builders Streamer",
          drop_name: "Builders Hold",
        },
        waiver_targets: [
          {
            player_id: 201,
            display_name: "Builders Waiver Gem",
            position: "RW",
            team_abbr: "VGK",
            percent_owned: 9.0,
            ixg_per60_ewma: 0.78,
            icorsi_per60_ewma: 10.1,
            pp_toi_share_ewma: 0.62,
            projected_fp: 4.5,
          },
        ],
      },
      {
        team_key: "465.l.29899.t.11",
        team_summary: {
          team_name: "Ice Sharks",
          team_initials: "IS",
          league_name: "Sharks Champions",
          adds_used: 3,
          adds_limit: 5,
          matchup_score: "64.0 - 68.5",
        },
        roster: [
          {
            player_id: 8477492,
            display_name: "Nathan MacKinnon",
            position: "C",
            selected_slot: "C",
            team_abbr: "COL",
            opponent_abbr: "EDM",
            projected_fp: 7.9,
            shots_on_goal: 5,
            ixg: 0.79,
            goals: 1,
            assists: 1,
          },
        ],
        recommendation: {
          action: "RECOMMEND_SWAP",
          explanation: "Swap in Sharks Streamer for +9.5 net FP.",
          delta_value: 9.5,
          tonight_gain: 4.8,
          short_term_gain: 11.0,
          asset_depreciation_penalty: -1.5,
          horizon_weeks: 2.5,
          streamer_name: "Sharks Streamer",
          drop_name: "Sharks Drop",
        },
        waiver_targets: [
          {
            player_id: 202,
            display_name: "Sharks Waiver Target",
            position: "LW",
            team_abbr: "DAL",
            percent_owned: 14.0,
            ixg_per60_ewma: 0.84,
            icorsi_per60_ewma: 11.2,
            pp_toi_share_ewma: 0.58,
            projected_fp: 5.1,
          },
        ],
      },
    ],
  });

  const t0 = resolveActiveTeam(twoTeamReport, 0);
  const t1 = resolveActiveTeam(twoTeamReport, 1);
  assert.equal(t0.team_summary.team_name, "PuckLuck Builders");
  assert.equal(t1.team_summary.team_name, "Ice Sharks");

  for (const activeTab of ["picks", "scorers", "totals"]) {
    const html = renderDashboard(twoTeamReport, { nowMs: NOW, activeTab });
    const header = html.slice(html.indexOf("<header"), html.indexOf("</header>"));
    assert.ok(!header.includes("team-switch-btn"));
    assert.ok(!header.includes("PuckLuck Builders"));
    assert.ok(!header.includes("Ice Sharks"));
  }
  const htmlTeam0 = renderDashboard(twoTeamReport, { nowMs: NOW, teamIndex: 0, activeTab: "roster" });
  assert.ok(htmlTeam0.includes("team-switch-btn active"));
  assert.ok(htmlTeam0.includes("PuckLuck Builders"));
  assert.ok(htmlTeam0.includes("Adds <strong>3/4</strong>"));
  assert.ok(htmlTeam0.includes("Builders Streamer"));
  assert.ok(htmlTeam0.includes("Builders Waiver Gem"));
  assert.ok(!htmlTeam0.includes("Sharks Waiver Target"));

  const htmlTeam1 = renderDashboard(twoTeamReport, { nowMs: NOW, teamIndex: 1, activeTab: "adddrop" });
  assert.ok(htmlTeam1.includes("team-switch-btn active"));
  assert.ok(htmlTeam1.includes('<article class="adddrop-card">'));
  assert.ok(htmlTeam1.includes("Ice Sharks"));
  assert.ok(htmlTeam1.includes("Adds <strong>2/5</strong>"));
  assert.ok(htmlTeam1.includes("RECOMMEND_ADD"));
  assert.ok(htmlTeam1.includes("Sharks Streamer"));
  assert.ok(htmlTeam1.includes("Sharks Waiver Target"));
  assert.ok(!htmlTeam1.includes("Builders Waiver Gem"));
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
  // Best-picks ordering: +EV card first, postponed card last.
  const activeCard = totalsHtml.split('<article class="game-card')[1];
  assert.ok(activeCard.includes("game-card-ev"));
  assert.ok(!activeCard.includes("POSTPONED"));

  // Muted tier sorts by start time: Final (16:00) → POSTPONED (17:00) → LIVE (18:00).
  const finalCard = totalsHtml.split('<article class="game-card')[2];
  assert.ok(finalCard.includes("FINAL"));
  assert.ok(finalCard.includes("FLA 3 – CHI 2"), "missing final score");
  assert.ok(finalCard.includes("total 5.0"), "missing final total");
  assert.ok(finalCard.includes("LEAN OVER 6.5 ✗"), "missing lean outcome");
  assert.ok(!finalCard.includes("+EV"), "final game leaked +EV tag");
  assert.ok(!finalCard.includes("BEST PICK:"), "final game leaked odds");

  const postponedCard = totalsHtml.split('<article class="game-card')[3];
  assert.ok(postponedCard.includes("POSTPONED"));
  assert.ok(!postponedCard.includes("+EV"), "postponed game leaked +EV tag");
  assert.ok(
    !postponedCard.includes("MODEL LEANS"),
    "postponed game leaked model strip"
  );

  const liveCard = totalsHtml.split('<article class="game-card')[4];
  assert.ok(liveCard.includes("LIVE"));
  assert.ok(
    !liveCard.includes("MODEL LEANS"),
    "live game leaked model strip (stale prices)"
  );

  assert.ok(activeCard.includes("+EV"));
  assert.ok(activeCard.includes("BEST PICK:"));
  assert.ok(activeCard.includes("CONFIRMED"));
  assert.ok(activeCard.includes("PROJECTED"));
  assert.ok(activeCard.includes("MODEL"));
  assert.ok(activeCard.includes("LINE"));
  assert.ok(activeCard.includes("MODEL LEANS OVER"), "missing edge direction");
  assert.ok(activeCard.includes("+0.2"), "missing signed model-line delta");
  assert.ok(activeCard.includes("O 56%"), "missing P(Over) meter label");
  assert.ok(activeCard.includes("Draft Kings"), "missing line source tag");

  // Missing line_source falls back to an honest EST. LINE tag.
  const estHtml = renderGameTotalsTab([
    { ...report.games[0], line_source: undefined },
  ]);
  assert.ok(estHtml.includes("EST. LINE"));

  const rosterHtml = renderRosterTab(report.roster);
  assert.ok(!rosterHtml.includes("NaN"));
  assert.ok(!rosterHtml.includes("undefined"));
  assert.ok(!rosterHtml.includes("null"));
  assert.ok(rosterHtml.includes(">0<"));
  assert.ok(rosterHtml.includes("0.0"));
  assert.ok(rosterHtml.includes("CONFIRMED"));

  const seasonRoster = renderRosterTab(report.roster, {
    season: "20262027",
  });
  assert.ok(
    seasonRoster.includes("mugs/nhl/20262027/EDM/8478402.png"),
    "roster rows missing player headshots"
  );
});

test("ranks and filters free agents / waiver targets by 15-game EWMA process metrics", () => {
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

  const addDropWithWaivers = renderAddDropTab(makeReport().recommendation, {
    waiverTargets: makeReport().free_agents,
    query: "zeta",
  });
  assert.ok(addDropWithWaivers.includes("Zeta Winger"));
  assert.ok(!addDropWithWaivers.includes("Mika Grinder"));
});

test("renders point scorers with Goal/Assist/Point probabilities and flags +EV profit opportunities", () => {
  const report = makeReport();
  const allHtml = renderPointScorersTab(report.point_scorers);
  assert.ok(allHtml.includes("1+ Pt: <b>81%</b>"));
  assert.ok(allHtml.includes("Goal: <b>46%</b>"));
  assert.ok(allHtml.includes("Assist: <b>65%</b>"));
  assert.ok(allHtml.includes("PROFIT EDGE:"));
  assert.ok(allHtml.includes("+24.5% EV"));
  assert.ok(allHtml.includes("player-headshot"), "missing scorer headshots");
  assert.ok(allHtml.includes(" est"), "baseline prop odds not labeled");

  // Default ALL/POINTS sort: McDavid (81%) > MacKinnon (75%) > Matthews (69%)
  const mcdIdx = allHtml.indexOf("Connor McDavid");
  const mackIdx = allHtml.indexOf("Nathan MacKinnon");
  const mattIdx = allHtml.indexOf("Auston Matthews");
  assert.ok(mcdIdx < mackIdx && mackIdx < mattIdx);

  // GOALS filter: Matthews (52% goal) outranks McDavid (46%) and MacKinnon (44%)
  const goalsHtml = renderPointScorersTab(report.point_scorers, {
    filter: "GOALS",
  });
  assert.ok(
    goalsHtml.indexOf("Auston Matthews") < goalsHtml.indexOf("Connor McDavid")
  );

  // +EV filter: excludes MacKinnon (is_positive_ev: false) and keeps McDavid + Matthews
  const evHtml = renderPointScorersTab(report.point_scorers, { filter: "+EV" });
  assert.ok(evHtml.includes("Connor McDavid"));
  assert.ok(evHtml.includes("Auston Matthews"));
  assert.ok(!evHtml.includes("Nathan MacKinnon"));
});

test("main picks lead the board with real prices and exclude inactive players and model leans", () => {
  const report = makeReport();
  const picksHtml = renderBestPicksTab(report.games, report.point_scorers, {
    season: "20262027",
  });

  assert.ok(picksHtml.includes("tag-total"), "missing TOTAL pick tag");
  assert.ok(picksHtml.includes("tag-prop"), "missing PROP pick tag");
  assert.ok(picksHtml.includes("OVER 6.5"), "missing totals pick");
  assert.ok(!picksHtml.includes("Auston Matthews"), "postponed player leaked onto picks");
  assert.ok(picksHtml.includes("crest-pair"), "totals pick missing team crests");
  assert.ok(
    picksHtml.includes("mugs/nhl/20262027/EDM/8478402.png"),
    "prop pick missing headshot"
  );

  // Main player props precede totals, followed by a separate longshot section.
  const totalsIdx = picksHtml.indexOf("Game Totals");
  const propsIdx = picksHtml.indexOf("Player Props");
  const mcdIdx = picksHtml.indexOf("1+ Point");
  const gameIdx = picksHtml.indexOf("OVER 6.5");
  assert.ok(
    totalsIdx !== -1 && propsIdx !== -1 && propsIdx < totalsIdx,
    "missing/unsorted pick sections"
  );
  assert.ok(
    mcdIdx !== -1 && gameIdx !== -1,
    "missing expected picks"
  );
  assert.ok(mcdIdx < gameIdx, "all main player picks should precede totals");
  assert.ok(gameIdx < picksHtml.indexOf('aria-label="Longshots"'));
  assert.ok(picksHtml.includes("Estimated hit chance <strong>81%</strong>"));

  // Postponed, final, and live games never leak onto the board
  // (fixture marks them +EV — status must still exclude them).
  assert.ok(!picksHtml.includes("CAR @ TOR"));
  assert.ok(!picksHtml.includes("FLA @ CHI"), "finished game leaked onto picks");
  assert.ok(!picksHtml.includes("NYI @ DAL"), "live game leaked onto picks");

  assert.ok(!picksHtml.includes("Model Leans"));
  assert.ok(!picksHtml.includes("tag-lean"));
  assert.ok(!picksHtml.includes("Nathan MacKinnon"));
  assert.ok(picksHtml.includes("calibration by market and probability range has not yet been validated"));
});

test("classifies each real market at the 50% boundary without hiding a main pick behind a longshot", () => {
  const report = makeReport();
  const player = {
    ...report.point_scorers[0],
    prob_point: 0.5, point_ev: 0.1, market_point_src: "Bovada", market_point_odds: 120,
    prob_goal: 0.173, goal_ev: 0.6435, market_goal_src: "Bovada", market_goal_odds: 850,
    prob_assist: 0.7, assist_ev: 1.1, market_assist_src: "baseline", market_assist_odds: 200,
    best_prop_market: "GOALS", best_prop_ev: 0.6435,
  };
  const justBelow = {
    ...player, player_id: 2, display_name: "Boundary Player",
    prob_point: 0.4999, point_ev: 0.0998,
    market_goal_src: "baseline", market_assist_src: "baseline",
  };
  const higherEv = {
    ...player, player_id: 3, display_name: "Higher Main EV",
    point_ev: 0.2, market_point_odds: 140, market_goal_src: "baseline",
  };
  const players = [player, justBelow, higherEv];
  const { main, longshots } = classifyPicks(report.games, players);
  assert.deepEqual(main.props.map((p) => p.player_id), [3, player.player_id]);
  assert.equal(main.props[1].best_prop_market, "POINTS");
  assert.equal(main.props[1].best_prop_odds, 120);
  assert.deepEqual(longshots.props.map((p) => p.player_id), [player.player_id, 2]);
  assert.equal(longshots.props[0].best_prop_market, "GOALS");
  assert.equal(longshots.props[0].best_prop_odds, 850);
  assert.equal(main.totals.length, 1);
  const html = renderBestPicksTab(report.games, players);
  const mainHtml = html.split('aria-label="Main picks"')[1].split('aria-label="Longshots"')[0];
  assert.ok(mainHtml.includes("50%"));
  assert.ok(!mainHtml.includes("+850"));
  assert.ok(!mainHtml.includes("Boundary Player"));
  assert.ok(html.includes("49.99%"));
  const dashboard = renderDashboard({ ...report, point_scorers: players });
  assert.ok(dashboard.includes('<b>3</b><span>MAIN PICKS</span>'));
  assert.ok(dashboard.includes('<b>2</b><span>LONGSHOTS</span>'));
  assert.ok(dashboard.includes('data-tab="picks">Picks<span class="nav-count">3</span>'));
});

test("both pick tiers reject missing provenance, invalid values, wrong matchups and started games", () => {
  const report = makeReport();
  const player = report.point_scorers[0];
  for (const patch of [
    { market_point_src: "baseline" }, { market_point_src: undefined },
    { market_point_odds: 0 }, { market_point_odds: NaN },
    { point_ev: 0.0349 }, { point_ev: NaN },
    { prob_point: NaN }, { prob_point: undefined }, { prob_point: 1.1 },
    { opponent_abbr: "VAN" }, { team_abbr: "VAN" },
  ]) {
    const groups = classifyPicks(report.games, [{ ...player, ...patch }]);
    assert.equal(groups.main.props.length + groups.longshots.props.length, 0);
  }
  for (const status of ["Live", "Final", "POSTPONED", "Unknown"]) {
    const groups = classifyPicks([{ ...report.games[0], status }], [player]);
    assert.deepEqual(groups, { main: { props: [], totals: [] }, longshots: { props: [], totals: [] } });
  }
  const lowProbabilityTotal = {
    ...report.games[0], prob_over: 0.4, over_odds: 200, ev: 0.2,
  };
  assert.equal(classifyPicks([lowProbabilityTotal], []).longshots.totals.length, 1);
  assert.equal(classifyPicks([{ ...lowProbabilityTotal, line_source: "consensus" }], []).longshots.totals.length, 0);
  const html = renderBestPicksTab([lowProbabilityTotal], []);
  assert.ok(html.includes("No main picks meet the 50%"));
  assert.ok(!html.includes("tag-lean"));
  assert.ok(renderBestPicksTab([], []).includes("No qualifying longshots"));
});
