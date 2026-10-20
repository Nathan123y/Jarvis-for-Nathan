"""ESPN Fantasy Football for Jarvis: injuries, matchups, opponents, players,
free agents, standings and league activity for the user's league. Read-only.

ESPN has no official API; the espn-api package reads the same endpoints the
ESPN site uses. A private league needs the user's espn_s2 and SWID cookies.
"connect ESPN fantasy" takes them, with the league ID, from one copy of a
request's headers in Chrome; they are kept only in local Plugin Settings
(config/api_keys.json, git-ignored) and wiped from the clipboard.

Every answer hands the model the facts it is based on, so follow-ups like
"should I start X or Y" are answered from the real lineup, not from memory.
"""
from __future__ import annotations

import difflib
import re
import subprocess
from datetime import date

from memory.config_manager import get_plugin_config, save_plugin_config


PLUGIN = {
    "name": "espn_fantasy",
    "description": (
        "The user's ESPN Fantasy Football league, read-only. Use for 'any injuries on my "
        "team', 'who's out or questionable this week', 'how's my matchup', 'who am I "
        "playing', 'what's my opponent starting', 'tell me about <player>', 'best free "
        "agent running backs', 'league standings', 'any trades in my league', and to answer "
        "start/sit or trade questions: fetch the data with these actions, then reason from "
        "it. Actions: injuries (team: mine, opponent or league), lineup (team: mine or "
        "opponent), matchup, player (name), free_agents (position, count), standings, "
        "activity, connect, connect_done (after the user copied the headers), status. "
        "Never ask the user to say their cookies out loud."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": ["injuries", "lineup", "matchup", "player",
                                                  "free_agents", "standings", "activity",
                                                  "connect", "connect_done", "status"]},
            "team": {"type": "STRING", "enum": ["mine", "opponent", "league"]},
            "name": {"type": "STRING", "description": "For player: the player's name"},
            "position": {"type": "STRING", "description": "For free_agents: QB, RB, WR, TE, K or D/ST"},
            "count": {"type": "INTEGER", "description": "For free_agents: how many (default 8)"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "espn_fantasy",
    "title": "ESPN Fantasy Football (read-only)",
    "fields": [
        {"key": "league_id", "type": "text", "label": "League ID",
         "placeholder": "Filled in by 'connect ESPN fantasy'"},
        {"key": "espn_s2", "type": "password", "label": "espn_s2 cookie (private leagues)"},
        {"key": "swid", "type": "password", "label": "SWID cookie (private leagues)"},
    ],
}

# The fantasy home lists the user's teams; a team page without ?leagueId= is an error page.
_LEAGUE_HOME = "https://fantasy.espn.com/football/"
_STATUS = {
    "OUT": "Out", "DOUBTFUL": "Doubtful", "QUESTIONABLE": "Questionable",
    "INJURY_RESERVE": "IR", "SUSPENSION": "Suspended", "DAY_TO_DAY": "Day-to-day",
}
_BENCH = {"BE", "IR"}

_CONNECT_STEPS = (
    "CONNECT ESPN FANTASY (one time)\n\n"
    "1. In the ESPN tab that just opened, sign in, then click your team under My Teams\n"
    "   (the address should now contain leagueId=).\n"
    "2. Press Cmd+Option+I to open Developer Tools, then click the Network tab.\n"
    "3. In the filter box type:  lm-api-reads\n"
    "4. Press Cmd+R to reload the page so requests appear.\n"
    "5. Right-click a request whose name STARTS WITH NUMBERS (your league ID, like\n"
    "   1234567?view=mTeam) → Copy → Copy as cURL (or Copy request headers).\n"
    "6. Close Developer Tools and say \"done\".\n\n"
    "Your ESPN cookies are saved only on this Mac and wiped from your clipboard."
)


class _NeedsConnect(Exception):
    pass


def _season(today: date | None = None) -> int:
    """The NFL season a date belongs to: January and February finish last year's."""
    today = today or date.today()
    return today.year if today.month >= 3 else today.year - 1


def _league():
    from espn_api.football import League
    cfg = get_plugin_config("espn_fantasy")
    if not str(cfg.get("league_id") or "").strip():
        raise _NeedsConnect()
    return League(league_id=int(cfg["league_id"]), year=int(cfg.get("year") or _season()),
                  espn_s2=cfg.get("espn_s2") or None, swid=cfg.get("swid") or None)


def _mine(league):
    """The user's team: the one their SWID owns, else the saved team id."""
    cfg = get_plugin_config("espn_fantasy")
    swid = str(cfg.get("swid") or "").strip().lower()
    for team in league.teams:
        owners = team.owners or []
        if swid and any(str(o.get("id", "")).lower() == swid for o in owners if isinstance(o, dict)):
            return team
    tid = str(cfg.get("team_id") or "")
    return next((t for t in league.teams if str(t.team_id) == tid), None)


def _owner(team) -> str:
    owners = [o for o in (team.owners or []) if isinstance(o, dict)]
    if not owners:
        return ""
    o = owners[0]
    return (f"{o.get('firstName', '')} {o.get('lastName', '')}".strip()
            or str(o.get("displayName") or ""))


def _box(league, team):
    """This week's box score for `team`, and whether it is the home side."""
    for box in league.box_scores():
        if getattr(box.home_team, "team_id", None) == team.team_id:
            return box, True
        if getattr(box.away_team, "team_id", None) == team.team_id:
            return box, False
    return None, True


def _status(player) -> str:
    return _STATUS.get(str(getattr(player, "injuryStatus", "") or "").upper(), "")


def _line(p) -> str:
    slot = getattr(p, "slot_position", "") or getattr(p, "lineupSlot", "")
    proj = getattr(p, "projected_points", None)
    bits = [f"{slot:>5}  {p.name} ({p.position}, {p.proTeam})"]
    if proj is not None:
        bits.append(f"proj {proj:.1f}")
    if getattr(p, "points", 0):
        bits.append(f"scored {p.points:.1f}")
    if getattr(p, "on_bye_week", False):
        bits.append("BYE")
    if (s := _status(p)):
        bits.append(s.upper())
    return " · ".join(bits)


def _problems(lineup) -> list[str]:
    """Starters first: anyone injured, suspended or on bye."""
    flagged = [p for p in lineup if _status(p) or getattr(p, "on_bye_week", False)]
    flagged.sort(key=lambda p: getattr(p, "slot_position", "") in _BENCH)
    out = []
    for p in flagged:
        role = "bench" if getattr(p, "slot_position", "") in _BENCH else "STARTER"
        why = "on bye" if getattr(p, "on_bye_week", False) and not _status(p) else _status(p)
        out.append(f"{p.name} ({p.position}, {p.proTeam}) — {why}, {role}")
    return out


def _display(player, title, lines):
    if player:
        try:
            player.show_content(title, "\n".join(lines)[:3500])
        except Exception:
            pass


def _week_sides(league):
    mine = _mine(league)
    if mine is None:
        raise ValueError("I couldn't tell which team in the league is yours. Say 'connect ESPN "
                         "fantasy' again while signed in to your own ESPN account.")
    box, home = _box(league, mine)
    if box is None:
        raise ValueError(f"There's no matchup for {mine.team_name} in week {league.current_week}.")
    me = (box.home_team, box.home_score, box.home_projected, box.home_lineup) if home else \
        (box.away_team, box.away_score, box.away_projected, box.away_lineup)
    them = (box.away_team, box.away_score, box.away_projected, box.away_lineup) if home else \
        (box.home_team, box.home_score, box.home_projected, box.home_lineup)
    return me, them


def _injuries(league, team: str, player) -> str:
    week = league.current_week
    if team == "league":
        lines = []
        for box in league.box_scores():
            for side, lineup in ((box.home_team, box.home_lineup), (box.away_team, box.away_lineup)):
                hurt = [p for p in _problems(lineup) if p.endswith("STARTER")]
                if hurt and side:
                    lines.append(f"{side.team_name}: " + "; ".join(h.rsplit(', ', 1)[0] for h in hurt))
        _display(player, f"LEAGUE INJURIES · WEEK {week}", lines or ["No injured starters."])
        return (f"Week {week} injured or bye-week starters across the league:\n" + "\n".join(lines)
                if lines else f"No team has an injured or bye-week starter in week {week}.")
    me, them = _week_sides(league)
    side = them if team == "opponent" else me
    who = "Your opponent" if team == "opponent" else "You"
    issues = _problems(side[3])
    _display(player, f"{side[0].team_name.upper()} · WEEK {week}", issues or ["Everyone is healthy."])
    if not issues:
        return f"{who} ({side[0].team_name}) have no injured, suspended or bye-week players for week {week}."
    return f"Week {week}, {side[0].team_name}:\n" + "\n".join(issues)


def _lineup(league, team: str, player) -> str:
    me, them = _week_sides(league)
    side = them if team == "opponent" else me
    lines = [_line(p) for p in sorted(side[3], key=lambda p: getattr(p, "slot_position", "") in _BENCH)]
    _display(player, f"{side[0].team_name.upper()} · WEEK {league.current_week}", lines)
    return (f"{side[0].team_name} ({_owner(side[0])}), week {league.current_week}, "
            f"projected {side[2]:.1f}:\n" + "\n".join(lines))


def _matchup(league, player) -> str:
    me, them = _week_sides(league)
    week = league.current_week
    lines = [f"{me[0].team_name}: {me[1]:.1f} (projected {me[2]:.1f})",
             f"{them[0].team_name} ({_owner(them[0])}): {them[1]:.1f} (projected {them[2]:.1f})"]
    _display(player, f"WEEK {week} MATCHUP", lines)
    lead = me[2] - them[2]
    call = (f"projected to win by {lead:.1f}" if lead > 0 else
            f"projected to lose by {-lead:.1f}" if lead < 0 else "projected dead even")
    return (f"Week {week}: {me[0].team_name} vs {them[0].team_name} ({_owner(them[0])}, "
            f"{them[0].wins}-{them[0].losses}). You're {call}.\n" + "\n".join(lines))


def _match_name(league, name: str) -> str | None:
    """ESPN only looks players up by exact full name; people say "bijan" or
    "st brown". Prefer a surname or full-name hit, then the closest spelling."""
    names = [n for n in league.player_map if isinstance(n, str)]
    want = " ".join(re.findall(r"[a-z0-9]+", name.lower()))
    keyed = {" ".join(re.findall(r"[a-z0-9]+", n.lower())): n for n in names}
    if want in keyed:
        return keyed[want]
    parts = [k for k in keyed if want and (k.endswith(" " + want) or k.startswith(want + " ")
                                          or f" {want} " in f" {k} ")]
    if len(parts) == 1:
        return keyed[parts[0]]
    close = difflib.get_close_matches(want, parts or list(keyed), n=1, cutoff=0.75)
    return keyed[close[0]] if close else None


def _player(league, name: str, player) -> str:
    if len(name.strip()) < 2:
        return "Which player should I look up?"
    full = _match_name(league, name)
    if not full:
        return f"I couldn't find a player called {name} on ESPN."
    p = league.player_info(name=full)
    if isinstance(p, list):
        p = p[0] if p else None
    if not p:
        return f"I couldn't find a player called {name} on ESPN."
    owner = next((t for t in league.teams if t.team_id == getattr(p, "onTeamId", None)), None)
    mine = _mine(league)
    whose = ("on your team" if owner and mine and owner.team_id == mine.team_id else
             f"on {owner.team_name} ({_owner(owner)})" if owner else "a free agent")
    week = league.current_week
    proj = (getattr(p, "stats", {}) or {}).get(week, {}).get("projected_points")
    lines = [f"{p.name} · {p.position} · {p.proTeam}",
             f"Status: {_status(p) or 'healthy'}",
             f"In your league: {whose}",
             f"Season: {getattr(p, 'total_points', 0):.1f} pts, {getattr(p, 'avg_points', 0):.1f} per game",
             f"Owned in {getattr(p, 'percent_owned', 0):.0f}% of ESPN leagues"]
    if proj is not None:
        lines.insert(3, f"Week {week} projection: {proj:.1f}")
    _display(player, p.name.upper(), lines)
    return "\n".join(lines)


def _free_agents(league, position: str, count: int, player) -> str:
    pos = (position or "").upper().replace("DST", "D/ST").replace("DEF", "D/ST") or None
    week = league.current_week
    agents = league.free_agents(size=60, position=pos)

    def proj(p):
        return (getattr(p, "stats", {}) or {}).get(week, {}).get("projected_points") or 0

    healthy = [p for p in agents if _status(p) not in ("Out", "IR", "Suspended")]
    best = sorted(healthy, key=proj, reverse=True)[:max(1, min(count or 8, 15))]
    if not best:
        return f"No healthy free agents{' at ' + pos if pos else ''} right now."
    lines = [f"{p.name} ({p.position}, {p.proTeam}) · week {week} proj {proj(p):.1f}"
             + (f" · {_status(p)}" if _status(p) else "")
             + f" · {getattr(p, 'percent_owned', 0):.0f}% owned" for p in best]
    _display(player, f"FREE AGENTS{' · ' + pos if pos else ''}", lines)
    return f"Best available{' ' + pos if pos else ''} by week {week} projection:\n" + "\n".join(lines)


def _standings(league, player) -> str:
    mine = _mine(league)
    lines = [f"{i}. {t.team_name} ({_owner(t)}) {t.wins}-{t.losses}" + (f"-{t.ties}" if t.ties else "")
             + f" · {t.points_for:.1f} PF" + ("  ← you" if mine and t.team_id == mine.team_id else "")
             for i, t in enumerate(league.standings(), 1)]
    _display(player, "STANDINGS", lines)
    return "Standings:\n" + "\n".join(lines)


def _activity(league, player) -> str:
    lines = []
    for a in league.recent_activity(size=12):
        for team, action, p in a.actions:
            name = getattr(p, "name", p)
            lines.append(f"{getattr(team, 'team_name', 'A team')}: {action.lower().replace('_', ' ')} {name}")
    _display(player, "LEAGUE ACTIVITY", lines or ["Nothing recent."])
    return "Recent league moves, newest first:\n" + "\n".join(lines[:20]) if lines else "No recent adds, drops or trades."


_CHROME_URLS = '''
if application "Google Chrome" is not running then return ""
set out to ""
tell application "Google Chrome"
  repeat with w in windows
    repeat with t in tabs of w
      set out to out & (URL of t) & linefeed
    end repeat
  end repeat
end tell
return out'''


def _league_in_chrome() -> str:
    """The league ID from an open ESPN fantasy tab (…?leagueId=123…), since not
    every copied request carries it."""
    try:
        urls = subprocess.run(["osascript", "-e", _CHROME_URLS], capture_output=True,
                              text=True, timeout=8).stdout
    except Exception:
        return ""
    found = re.search(r"fantasy\.espn\.com/football/\S*?leagueId=(\d+)", urls)
    return found.group(1) if found else ""


def _connect_done() -> str:
    try:
        raw = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        raw = ""
    s2 = re.search(r"espn_s2=([^;\s'\"]+)", raw)
    league = re.search(r"leagues/(\d+)", raw) or re.search(r"leagueId=(\d+)", raw)
    league_id = league.group(1) if league else ""
    if not league_id and s2:
        league_id = _league_in_chrome()
    if not league_id:
        return ("I don't see your ESPN login or league in what you copied. In Developer Tools, "
                "filter for lm-api-reads, reload, right-click a request whose name starts with "
                "numbers, choose Copy, then Copy as cURL, and say done again.")
    swid = re.search(r"SWID=(\{[0-9A-Fa-f-]+\})", raw)
    year = re.search(r"seasons/(\d{4})", raw)
    values = {"league_id": league_id, "year": year.group(1) if year else str(_season()),
              "espn_s2": s2.group(1) if s2 else "", "swid": swid.group(1) if swid else ""}
    from espn_api.football import League
    try:
        league = League(league_id=int(values["league_id"]), year=int(values["year"]),
                        espn_s2=values["espn_s2"] or None, swid=values["swid"] or None)
    except Exception as exc:
        print(f"[ESPN] connect failed: {exc!r}")
        return ("ESPN didn't accept those headers. Make sure you're signed in and copied a "
                "request from your own league, then say done again.")
    save_plugin_config("espn_fantasy", values)
    try:
        subprocess.run(["pbcopy"], input="", text=True, timeout=5)        # cookies off the clipboard
    except Exception:
        pass
    mine = _mine(league)
    name = getattr(getattr(league, "settings", None), "name", "") or "your league"
    if mine is None:
        return (f"Connected to {name}, but I couldn't tell which team is yours. Make sure you "
                "copied the headers while signed in to your own account.")
    save_plugin_config("espn_fantasy", {"team_id": str(mine.team_id)})
    return (f"Connected to {name}. You're {mine.team_name}, {mine.wins}-{mine.losses}, "
            f"in week {league.current_week}.")


def run(parameters: dict, player=None, session_memory=None) -> str:
    args = parameters or {}
    action = str(args.get("action") or "matchup").strip().lower()
    team = str(args.get("team") or "mine").strip().lower()
    try:
        if action == "connect":
            subprocess.run(["open", "-a", "Google Chrome", _LEAGUE_HOME], capture_output=True, timeout=10)
            _display(player, "ESPN FANTASY", [_CONNECT_STEPS])
            return ("I opened ESPN Fantasy and put the steps on screen: open Developer Tools, copy "
                    "the request headers of an lm-api-reads request, then say done.")
        if action == "connect_done":
            return _connect_done()
        if action == "status":
            cfg = get_plugin_config("espn_fantasy")
            return ("ESPN Fantasy is connected." if cfg.get("league_id") else
                    "ESPN Fantasy isn't connected. Say 'connect ESPN fantasy' to set it up.")
        league = _league()
        if action == "injuries":
            return _injuries(league, team, player)
        if action == "lineup":
            return _lineup(league, team, player)
        if action == "matchup":
            return _matchup(league, player)
        if action == "player":
            return _player(league, str(args.get("name") or ""), player)
        if action == "free_agents":
            return _free_agents(league, str(args.get("position") or ""), int(args.get("count") or 8), player)
        if action == "standings":
            return _standings(league, player)
        if action == "activity":
            return _activity(league, player)
        return ("ESPN commands: injuries, lineup, matchup, player, free_agents, standings, "
                "activity, connect, status.")
    except _NeedsConnect:
        return ("Your ESPN league isn't connected yet. Say 'connect ESPN fantasy' and I'll walk "
                "you through it; it takes a minute, once.")
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        print(f"[ESPN] {action} failed: {exc!r}")
        name = type(exc).__name__
        if "401" in str(exc) or "Unauthorized" in name or "Private" in str(exc):
            return "ESPN turned me away; your login may have expired. Say 'connect ESPN fantasy' again."
        return f"ESPN didn't respond ({name}). Try again in a moment."
