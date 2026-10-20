"""ESPN fantasy plugin against a fake league: no network, no real account."""
import unittest
from datetime import date
from types import SimpleNamespace as NS
from unittest.mock import patch

from plugins import espn_fantasy as espn

SWID = "{ABCD-1234}"


def player(name, pos, slot="RB", status="ACTIVE", proj=10.0, bye=False, team="ATL", **kw):
    return NS(name=name, position=pos, proTeam=team, slot_position=slot, injuryStatus=status,
              projected_points=proj, points=0, on_bye_week=bye, **kw)


def team(tid, name, wins, losses, owner, pf=500.0):
    return NS(team_id=tid, team_name=name, wins=wins, losses=losses, ties=0, points_for=pf,
              owners=[{"id": owner, "firstName": name.split()[0], "lastName": "Owner"}])


class FakeLeague:
    current_week = 7

    def __init__(self, league_id=123, name="Friends League"):
        self.league_id = league_id
        self.me = team(1, "Nathan Ballers", 4, 2, SWID.lower())
        self.them = team(2, "Gridiron Kings", 5, 1, "{OTHER}", pf=620.0)
        self.teams = [self.me, self.them]
        self.settings = NS(name=name)
        self.box = NS(
            home_team=self.them, home_score=0.0, home_projected=118.4,
            home_lineup=[player("Josh Allen", "QB", "QB", proj=24.0, team="BUF"),
                         player("Puka Nacua", "WR", "WR", "QUESTIONABLE", proj=15.0, team="LAR")],
            away_team=self.me, away_score=0.0, away_projected=110.2,
            away_lineup=[player("Bijan Robinson", "RB", "RB", proj=19.5),
                         player("Amon-Ra St. Brown", "WR", "WR", "DOUBTFUL", proj=12.0, team="DET"),
                         player("Drake London", "WR", "WR", bye=True, proj=0.0),
                         player("Rhamondre Stevenson", "RB", "BE", "OUT", proj=0.0, team="NE")])
        self.player_map = {"Bijan Robinson": 1, 1: "Bijan Robinson",
                           "Amon-Ra St. Brown": 2, 2: "Amon-Ra St. Brown",
                           "Josh Allen": 3, 3: "Josh Allen",
                           "Drake Maye": 4, "Marcus Maye": 5}

    def box_scores(self, week=None):
        return [self.box]

    def player_info(self, name=None, playerId=None):
        if isinstance(playerId, list):
            return [NS(name="Marcus Maye", position="S", proTeam="NO", injuryStatus="ACTIVE", onTeamId=0,
                       total_points=0.0, avg_points=0.0, percent_owned=0.1, stats={}),
                    NS(name="Drake Maye", position="QB", proTeam="NE", injuryStatus="ACTIVE", onTeamId=0,
                       total_points=90.0, avg_points=18.0, percent_owned=85.0, stats={})]
        if name == "Bijan Robinson":
            return NS(name=name, position="RB", proTeam="ATL", injuryStatus="ACTIVE", onTeamId=1,
                      total_points=120.0, avg_points=20.0, percent_owned=99.9,
                      stats={7: {"projected_points": 19.5}})
        if name == "Josh Allen":
            return NS(name=name, position="QB", proTeam="BUF", injuryStatus="ACTIVE", onTeamId=2,
                      total_points=150.0, avg_points=25.0, percent_owned=100.0, stats={})
        return None

    def free_agents(self, size=50, position=None):
        return [NS(name="Hurt Guy", position="RB", proTeam="NYJ", injuryStatus="OUT",
                   percent_owned=40.0, stats={7: {"projected_points": 30.0}}),
                NS(name="Backup Back", position="RB", proTeam="KC", injuryStatus="ACTIVE",
                   percent_owned=12.0, stats={7: {"projected_points": 9.1}}),
                NS(name="Waiver Wonder", position="RB", proTeam="SF", injuryStatus="QUESTIONABLE",
                   percent_owned=30.0, stats={7: {"projected_points": 11.2}})]

    def standings(self):
        return [self.them, self.me]

    def recent_activity(self, size=25):
        # Newer ESPN data adds a bid amount to each move.
        return [NS(actions=[(self.them, "TRADED", NS(name="Josh Allen"), 0),
                            (self.me, "FA ADDED", NS(name="Backup Back"))])]


class EspnTests(unittest.TestCase):
    def setUp(self):
        self.league = FakeLeague()
        self.cfg = {"league_id": "123", "swid": SWID, "espn_s2": "secret"}
        for p in (patch.object(espn, "_leagues", side_effect=lambda which="": [self.league]),
                  patch.object(espn, "get_plugin_config", side_effect=lambda ns: self.cfg)):
            p.start()
            self.addCleanup(p.stop)

    def test_my_injuries_list_starters_before_bench_with_byes(self):
        out = espn.run({"action": "injuries"})
        lines = out.split("\n")[1:]
        self.assertEqual(lines, [
            "Amon-Ra St. Brown (WR, DET) — Doubtful, STARTER",
            "Drake London (WR, ATL) — on bye, STARTER",
            "Rhamondre Stevenson (RB, NE) — Out, bench",
        ])

    def test_opponent_and_league_injuries(self):
        self.assertIn("Puka Nacua (WR, LAR) — Questionable, STARTER", espn.run({"action": "injuries", "team": "opponent"}))
        league = espn.run({"action": "injuries", "team": "league"})
        self.assertIn("Gridiron Kings: Puka Nacua (WR, LAR) — Questionable", league)
        self.assertIn("Nathan Ballers: Amon-Ra St. Brown", league)
        self.assertNotIn("Stevenson", league)                   # bench players aren't league news

    def test_matchup_names_opponent_and_projection(self):
        out = espn.run({"action": "matchup"})
        self.assertIn("Week 7: Nathan Ballers vs Gridiron Kings (Gridiron Owner, 5-1)", out)
        self.assertIn("You're projected to lose by 8.2", out)

    def test_player_lookup_is_forgiving_and_says_who_owns_him(self):
        out = espn.run({"action": "player", "name": "bijan"})
        self.assertIn("Bijan Robinson · RB · ATL", out)
        self.assertIn("In your league: on your team", out)
        self.assertIn("Week 7 projection: 19.5", out)
        self.assertIn("on Gridiron Kings", espn.run({"action": "player", "name": "josh alen"}))
        self.assertIn("couldn't find", espn.run({"action": "player", "name": "nobody special"}))
        # Two players share the surname: the widely rostered one wins.
        self.assertIn("Drake Maye · QB · NE", espn.run({"action": "player", "name": "maye"}))

    def test_free_agents_skip_the_injured_out_and_sort_by_projection(self):
        out = espn.run({"action": "free_agents", "position": "rb"})
        self.assertNotIn("Hurt Guy", out)
        self.assertLess(out.index("Waiver Wonder"), out.index("Backup Back"))

    def test_standings_and_activity(self):
        self.assertIn("2. Nathan Ballers (Nathan Owner) 4-2 · 500.0 PF  ← you", espn.run({"action": "standings"}))
        self.assertIn("Gridiron Kings: traded Josh Allen", espn.run({"action": "activity"}))

    def test_not_connected_and_failures_are_plain(self):
        with patch.object(espn, "_leagues", side_effect=espn._NeedsConnect):
            self.assertIn("connect ESPN fantasy", espn.run({"action": "matchup"}))
        with patch.object(espn, "_leagues", side_effect=RuntimeError("401 Unauthorized secret")):
            out = espn.run({"action": "matchup"})
        self.assertIn("login may have expired", out)
        self.assertNotIn("secret", out)

    def test_connect_reads_league_and_cookies_from_both_header_formats(self):
        saved = {}
        raw = ("\n".join([":path", "/apis/v3/games/ffl/seasons/2026/segments/0/leagues/987654?view=mTeam",
                          "cookie", f"SWID={SWID}; espn_s2=AEB%2Fxyz; other=1"]))
        self.cfg = {}
        with patch.object(espn.subprocess, "run", return_value=NS(stdout=raw)), \
             patch.object(espn, "save_plugin_config", side_effect=lambda ns, v: saved.update(v)), \
             patch.object(espn, "_discover", return_value=[]), \
             patch("espn_api.football.League", return_value=self.league):
            out = espn._connect_done()
        self.assertEqual(saved["league_ids"], "987654")
        self.assertEqual(saved["year"], "2026")
        self.assertEqual(saved["espn_s2"], "AEB%2Fxyz")
        self.assertEqual(saved["swid"], SWID)
        self.assertIn("Connected 1 league: Friends League (you're Nathan Ballers, 4-2)", out)
        self.assertIn("other leagues", out)
        with patch.object(espn.subprocess, "run", return_value=NS(stdout="nothing useful")):
            self.assertIn("don't see your ESPN login or league", espn._connect_done())

    def test_connect_accepts_curl_and_finds_the_league_in_chrome(self):
        saved = {}
        # Copy as cURL of a request without the league in its URL; espn_s2 is
        # last, so the closing quote must not stick to it.
        curl = ("curl 'https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2026/players?view=players_wl' "
                f"-H 'accept: application/json' -b 'SWID={SWID}; espn_s2=AEB%2Fxyz'")
        def fake_run(cmd, **kw):
            if cmd[0] == "pbpaste":
                return NS(stdout=curl)
            if cmd[0] == "osascript":
                return NS(stdout="https://fantasy.espn.com/football/team?leagueId=555&teamId=3\n")
            return NS(stdout="")
        self.cfg = {}
        with patch.object(espn.subprocess, "run", side_effect=fake_run), \
             patch.object(espn, "save_plugin_config", side_effect=lambda ns, v: saved.update(v)), \
             patch.object(espn, "_discover", return_value=[]), \
             patch("espn_api.football.League", return_value=self.league):
            espn._connect_done()
        self.assertEqual((saved["league_ids"], saved["espn_s2"], saved["swid"]), ("555", "AEB%2Fxyz", SWID))

    def test_connect_adds_every_league_the_account_is_in(self):
        saved = {}
        self.cfg = {"league_ids": "111"}             # one connected earlier
        raw = f"curl 'https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/2026/segments/0/leagues/222' -b 'SWID={SWID}; espn_s2=AEB'"
        leagues = {111: FakeLeague(111, "Work League"), 222: FakeLeague(222, "Family League"),
                   333: FakeLeague(333, "Dynasty League")}
        with patch.object(espn.subprocess, "run", return_value=NS(stdout=raw)), \
             patch.object(espn, "save_plugin_config", side_effect=lambda ns, v: saved.update(v)), \
             patch.object(espn, "_discover", return_value=["222", "333", "444"]), \
             patch("espn_api.football.League",
                   side_effect=lambda league_id, **kw: leagues.get(league_id) or (_ for _ in ()).throw(RuntimeError("gone"))):
            out = espn._connect_done()
        self.assertEqual(saved["league_ids"], "111,222,333")      # 444 didn't load: left out
        self.assertIn("Connected 3 leagues: Work League", out)
        self.assertNotIn("other leagues", out)

    def test_discover_reads_football_leagues_for_this_season(self):
        data = {"preferences": [
            {"metaData": {"entry": {"gameId": 1, "seasonId": 2026, "groups": [{"groupId": 111}]}}},
            {"metaData": {"entry": {"gameId": 1, "seasonId": 2025, "groups": [{"groupId": 999}]}}},
            {"metaData": {"entry": {"gameId": 2, "seasonId": 2026, "groups": [{"groupId": 888}]}}},
            {"metaData": {"entry": {"gameId": 1, "seasonId": 2026, "groups": [{"groupId": 222}]}}}]}
        import requests
        with patch.object(requests, "get", return_value=NS(json=lambda: data)) as get:
            self.assertEqual(espn._discover(SWID, "s2", 2026), ["111", "222"])
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        with patch.object(requests, "get", side_effect=OSError("offline")):
            self.assertEqual(espn._discover(SWID, "s2", 2026), [])

    def test_several_leagues_answer_together_or_by_name(self):
        work, family = FakeLeague(111, "Work League"), FakeLeague(222, "Family League")
        with patch.object(espn, "_leagues", side_effect=lambda which="": [work, family] if not which
                          else [lg for lg in (work, family) if which.split()[0].lower() in espn._name(lg).lower()]):
            both = espn.run({"action": "injuries"})
            self.assertIn("== Work League ==", both)
            self.assertIn("== Family League ==", both)
            one = espn.run({"action": "injuries", "league": "family"})
            self.assertNotIn("Work League", one)
            self.assertIn("You're Nathan Ballers, 4-2", espn.run({"action": "leagues"}))

    def test_season_rolls_over_in_march(self):
        self.assertEqual(espn._season(date(2027, 1, 10)), 2026)
        self.assertEqual(espn._season(date(2026, 9, 10)), 2026)


class LeagueLoadingTests(unittest.TestCase):
    """The real _leagues: parallel loading, picking by name, skipping a dead league."""

    def setUp(self):
        self.work, self.family = FakeLeague(111, "Work League"), FakeLeague(222, "Family League")
        self.loaded = {"111": self.work, "222": self.family}
        cfg = {"league_ids": "111,222,333", "league_id": "111", "swid": SWID}
        for p in (patch.object(espn, "get_plugin_config", return_value=cfg),
                  patch.object(espn, "_load", side_effect=self.load)):
            p.start()
            self.addCleanup(p.stop)

    def load(self, league_id, cfg):
        if league_id not in self.loaded:
            raise RuntimeError("league deleted")
        return self.loaded[league_id]

    def test_all_named_and_unknown(self):
        self.assertEqual(espn._leagues(), [self.work, self.family])        # 333 skipped, 111 once
        self.assertEqual(espn._leagues("my family league"), [self.family])
        with self.assertRaisesRegex(ValueError, "They are: Work League, Family League"):
            espn._leagues("dynasty")

    def test_every_league_failing_reports_the_error(self):
        self.loaded = {}
        with self.assertRaisesRegex(RuntimeError, "league deleted"):
            espn._leagues()


if __name__ == "__main__":
    unittest.main()
