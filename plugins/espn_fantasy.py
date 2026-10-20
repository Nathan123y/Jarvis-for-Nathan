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
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from memory.config_manager import get_plugin_config, save_plugin_config


PLUGIN = {
    "name": "espn_fantasy",
    "description": (
        "The user's ESPN Fantasy Football league, read-only. Use for 'any injuries on my "
        "team', 'who's out or questionable this week', 'how's my matchup', 'who am I "
        "playing', 'what's my opponent starting', 'tell me about <player>', 'best free "
        "agent running backs', 'league standings', 'any trades in my league', 'which league do I "
        "have <player> in and how many points has he scored for me' (action player: says which "
        "leagues he's on the user's team in and his points in weeks they started him), and to answer "
        "start/sit or trade questions: fetch the data with these actions, then reason from "
        "it. Actions: injuries (team: mine, opponent or league), lineup (team: mine or "
        "opponent), matchup, player (name), free_agents (position, count), standings, "
        "activity, leagues (which leagues are connected), connect, connect_done (after the user "
        "copied the headers), status. The user may be in several leagues: pass league with "
        "the name they say, or omit it to answer for all of them. Never ask the user to say "
        "their cookies out loud."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": ["injuries", "lineup", "matchup", "player",
                                                  "free_agents", "standings", "activity",
                                                  "leagues", "connect", "connect_done", "status"]},
            "team": {"type": "STRING", "enum": ["mine", "opponent", "league"]},
            "league": {"type": "STRING", "description": "Which of the user's leagues, by name, when they name one; omit for all"},
            "name": {"type": "STRING", "description": "For player: the player's name"},
            "position": {"type": "STRING", "description": "For free_agents: QB, RB, WR, TE, K or D/ST"},
            "count": {"type": "INTEGER", "description": "For free_agents: how many (default 6)"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "espn_fantasy",
    "title": "ESPN Fantasy Football (read-only)",
    "fields": [
        {"key": "league_ids", "type": "text", "label": "League IDs (comma-separated)",
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
# Last status seen for each player on the user's teams, and recent changes.
STATE_FILE = Path(__file__).resolve().parent.parent / "memory" / "espn_status.json"
# A change stays in the briefings this long, so a skipped startup briefing
# doesn't swallow it.
CHANGE_HOURS = 24
_NEEDS_SWAP = ("Out", "Doubtful", "IR", "Suspended")

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


def _league_ids(cfg: dict) -> list[str]:
    # "league_id" is where a single league was kept before several were supported.
    ids = re.findall(r"\d+", f"{cfg.get('league_ids') or ''},{cfg.get('league_id') or ''}")
    return list(dict.fromkeys(ids))


def _load(league_id: str, cfg: dict):
    from espn_api.football import League
    return League(league_id=int(league_id), year=int(cfg.get("year") or _season()),
                  espn_s2=cfg.get("espn_s2") or None, swid=cfg.get("swid") or None)


def _name(league) -> str:
    return getattr(getattr(league, "settings", None), "name", "") or f"League {league.league_id}"


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(text or "").lower())) - {"my", "the", "league", "fantasy"}


def _leagues(which: str = "") -> list:
    """The connected leagues, fetched in parallel; only those whose name matches
    `which` when the user named one. A league that fails to load is skipped
    unless every one does."""
    cfg = get_plugin_config("espn_fantasy")
    ids = _league_ids(cfg)
    if not ids:
        raise _NeedsConnect()

    def load(league_id):
        try:
            return _load(league_id, cfg)
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=len(ids)) as pool:
        results = list(pool.map(load, ids))
    leagues = [r for r in results if not isinstance(r, Exception)]
    if not leagues:
        raise results[0]
    want = _words(which)
    if want:
        named = [lg for lg in leagues if want & _words(_name(lg))]
        if not named:
            raise ValueError(f"None of your leagues is called {which}. They are: "
                             + ", ".join(_name(lg) for lg in leagues) + ".")
        return named
    return leagues


def _mine(league, swid: str = ""):
    """The user's team: the one their ESPN account (SWID) owns."""
    swid = str(swid or get_plugin_config("espn_fantasy").get("swid") or "").strip().lower()
    for team in league.teams:
        owners = team.owners or []
        if swid and any(str(o.get("id", "")).lower() == swid for o in owners if isinstance(o, dict)):
            return team
    return None


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
            f"{them[0].wins}-{them[0].losses}). You're {call}.\n" + "\n".join(lines)
            + "\n\nYour opponent:\n" + "\n".join(_opponent_form(league, them)))


def _opponent_form(league, them) -> list[str]:
    team, lineup = them[0], them[3]
    rank = next((i for i, t in enumerate(league.standings(), 1) if t.team_id == team.team_id), None)
    played = [x for x in (team.scores or [])[:max(league.current_week - 1, 0)] if x]
    form = [f"{team.wins}-{team.losses}" + (f", {rank} of {len(league.teams)} in the standings" if rank else "")
            + (f", {team.streak_length}-game {str(team.streak_type).lower()} streak" if team.streak_length else "")]
    if played:
        form.append(f"Averaging {sum(played) / len(played):.1f} a week; last week {played[-1]:.1f}")
    starters = [p for p in lineup if getattr(p, "slot_position", "") not in _BENCH]
    if starters:
        top = max(starters, key=lambda p: getattr(p, "projected_points", 0) or 0)
        form.append(f"Their top projected starter: {top.name} ({top.position}, {top.projected_points:.1f})")
    hurt = [h for h in _problems(lineup) if h.endswith("STARTER")]
    form.append("Injured or bye-week starters: " + ("; ".join(h.rsplit(", ", 1)[0] for h in hurt) if hurt else "none"))
    return form


def _match_names(league, name: str) -> list[str]:
    """ESPN only looks players up by exact full name; people say "bijan" or
    "st brown". Every player the words could mean: an exact name, else each
    first- or last-name hit, else the closest spelling."""
    names = [n for n in league.player_map if isinstance(n, str)]
    want = " ".join(re.findall(r"[a-z0-9]+", name.lower()))
    keyed = {" ".join(re.findall(r"[a-z0-9]+", n.lower())): n for n in names}
    if want in keyed:
        return [keyed[want]]
    parts = [k for k in keyed if want and (k.endswith(" " + want) or k.startswith(want + " ")
                                          or f" {want} " in f" {k} ")]
    if parts:
        return [keyed[k] for k in parts]
    close = difflib.get_close_matches(want, list(keyed), n=1, cutoff=0.75)
    return [keyed[close[0]]] if close else []


def _find_player(league, name: str):
    names = _match_names(league, name)
    if not names:
        return None
    if len(names) == 1:
        p = league.player_info(name=names[0])
    else:
        # "Maye" is Drake Maye or Marcus Maye: the fantasy-relevant one is the
        # one most leagues roster. One request covers every candidate.
        found = league.player_info(playerId=[league.player_map[n] for n in names[:10]]) or []
        found = found if isinstance(found, list) else [found]
        p = max(found, key=lambda c: getattr(c, "percent_owned", 0) or 0, default=None)
    if isinstance(p, list):
        p = p[0] if p else None
    return p


def _for_me(league, mine, player_id) -> list[tuple[int, float, bool]]:
    """(week, points, started) for every week the player was on the user's
    roster, including weeks before a trade or drop. One request per week, in
    parallel."""
    def week(w):
        for box in league.box_scores(w):
            for team, lineup in ((box.home_team, box.home_lineup), (box.away_team, box.away_lineup)):
                if getattr(team, "team_id", None) == mine.team_id:
                    for bp in lineup:
                        if getattr(bp, "playerId", None) == player_id:
                            return (w, float(bp.points or 0), bp.slot_position not in _BENCH)
                    return None
        return None

    weeks = range(1, league.current_week + 1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        return [r for r in pool.map(week, weeks) if r]


def _player_report(league, name: str) -> tuple[str, str] | None:
    """(where he is in this league, the detailed lines), or None if not found."""
    p = _find_player(league, name)
    if not p:
        return None
    owner = next((t for t in league.teams if t.team_id == getattr(p, "onTeamId", None)), None)
    mine = _mine(league)
    yours = bool(owner and mine and owner.team_id == mine.team_id)
    whose = ("on your team" if yours else
             f"on {owner.team_name} ({_owner(owner)})" if owner else "a free agent")
    week = league.current_week
    proj = (getattr(p, "stats", {}) or {}).get(week, {}).get("projected_points")
    lines = [f"{p.name} · {p.position} · {p.proTeam}",
             f"Status: {_status(p) or 'healthy'}",
             f"In this league: {whose}",
             f"Season: {getattr(p, 'total_points', 0):.1f} pts, {getattr(p, 'avg_points', 0):.1f} per game",
             f"Owned in {getattr(p, 'percent_owned', 0):.0f}% of ESPN leagues"]
    if proj is not None:
        lines.insert(3, f"Week {week} projection: {proj:.1f}")
    if mine:
        weeks = _for_me(league, mine, p.playerId)
        if weeks:
            started = [(w, pts) for w, pts, s in weeks if s]
            benched = [(w, pts) for w, pts, s in weeks if not s]
            total = sum(pts for _, pts in started)
            lines.append(f"Points for you: {total:.1f} in {len(started)} start"
                         f"{'s' if len(started) != 1 else ''}"
                         + (f" (week {week} still in progress)" if any(w == week for w, _ in started) else ""))
            lines.append("By week: " + ", ".join(
                f"wk {w} {pts:.1f}" + ("" if s else " (bench)") for w, pts, s in weeks))
            if benched:
                lines.append(f"Left on your bench: {sum(pts for _, pts in benched):.1f} pts "
                             f"in {len(benched)} week{'s' if len(benched) != 1 else ''}")
    return whose, "\n".join(lines)


def _player(league, name: str, player) -> str:
    if len(name.strip()) < 2:
        return "Which player should I look up?"
    report = _player_report(league, name)
    if report is None:
        return f"I couldn't find a player called {name} on ESPN."
    _display(player, name.upper(), report[1].split("\n"))
    return report[1]


def _player_everywhere(leagues, name: str, player) -> str:
    """One player across every league: where he's yours first, then the rest."""
    if len(name.strip()) < 2:
        return "Which player should I look up?"
    with ThreadPoolExecutor(max_workers=len(leagues)) as pool:
        reports = list(pool.map(lambda lg: _player_report(lg, name), leagues))
    found = [(lg, r) for lg, r in zip(leagues, reports) if r]
    if not found:
        return f"I couldn't find a player called {name} on ESPN."
    yours = [_name(lg) for lg, (whose, _) in found if whose == "on your team"]
    head = (f"You have him in: {', '.join(yours)}." if yours
            else "He isn't on any of your teams.")
    others = [f"{_name(lg)}: {whose}" for lg, (whose, _) in found if whose != "on your team"]
    if others:
        head += " Elsewhere — " + "; ".join(others) + "."
    found.sort(key=lambda item: item[1][0] != "on your team")
    text = head + "\n\n" + "\n\n".join(f"== {_name(lg)} ==\n{lines}" for lg, (_, lines) in found)
    _display(player, name.upper(), text.split("\n"))
    return text


def _free_agents(league, position: str, count: int, player) -> str:
    pos = (position or "").upper().replace("DST", "D/ST").replace("DEF", "D/ST") or None
    week = league.current_week
    agents = league.free_agents(size=60, position=pos)

    def proj(p):
        return (getattr(p, "stats", {}) or {}).get(week, {}).get("projected_points") or 0

    healthy = [p for p in agents if _status(p) not in ("Out", "IR", "Suspended")]
    best = sorted(healthy, key=proj, reverse=True)[:max(1, min(count or 6, 15))]
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


def _summary(league) -> str:
    mine = _mine(league)
    return (f"You're {mine.team_name}, {mine.wins}-{mine.losses}, week {league.current_week}."
            if mine else f"Week {league.current_week}; I couldn't tell which team is yours.")


def _load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as exc:
        print(f"[ESPN] couldn't save player statuses: {exc!r}")


def roster_alerts(now: datetime | None = None) -> dict:
    """For the briefings: status changes on the user's teams in the last day,
    starters who need swapping out, and questionable starters to watch.

    Compares every rostered player with the status last seen, records what
    changed, and keeps each change for CHANGE_HOURS. The first check only
    records a baseline. Returns {"changes", "swap", "watch"} lists of short lines."""
    now = now or datetime.now()
    leagues = _leagues()
    state = _load_state()
    seen, kept = state.get("seen", {}), state.get("changes", [])
    swap, watch, fresh, new_seen = [], [], [], {}
    for lg in leagues:
        try:
            me, _ = _week_sides(lg)
        except ValueError:
            continue
        key, where = str(lg.league_id), _name(lg)
        before_all, now_all = seen.get(key), {}
        for p in me[3]:
            status = _status(p) or "healthy"
            slot = getattr(p, "slot_position", "")
            starter = slot not in _BENCH
            pid = str(getattr(p, "playerId", "") or p.name)
            now_all[pid] = status
            before = (before_all or {}).get(pid)
            if before_all is not None and before is not None and before != status:
                if status == "healthy":
                    what = "is back from IR" if before == "IR" else f"is healthy again (was {before})"
                else:
                    what = f"is now {status} (was {before})"
                if starter and status in _NEEDS_SWAP + ("Questionable",):
                    what += ", and he's in your starting lineup"
                fresh.append(f"{p.name} ({where}) {what}")
            if starter and (status in _NEEDS_SWAP or getattr(p, "on_bye_week", False)):
                why = status if status != "healthy" else "on bye"
                swap.append(f"{p.name} ({where}, {why})")
            elif starter and status == "Questionable":
                watch.append(f"{p.name} ({where})")
            elif slot == "IR" and status not in _NEEDS_SWAP:
                swap.append(f"{p.name} ({where}) is {status.lower()} but still in your IR spot")
        new_seen[key] = now_all
    cutoff = now - timedelta(hours=CHANGE_HOURS)
    kept = [c for c in kept if isinstance(c, dict) and c.get("at", "") >= cutoff.isoformat()]
    kept += [{"at": now.isoformat(timespec="seconds"), "text": t} for t in fresh]
    _save_state({"seen": {**seen, **new_seen}, "changes": kept})
    return {"changes": [c["text"] for c in kept], "swap": swap, "watch": watch}


def briefing_lines(alerts: dict) -> tuple[str, list[str]]:
    """(one spoken sentence, panel lines) for the briefings; ("", []) when quiet."""
    changes, swap, watch = alerts.get("changes", []), alerts.get("swap", []), alerts.get("watch", [])
    spoken = []
    if changes:
        spoken.append("Status changes: " + "; ".join(changes[:4]))
    if swap:
        spoken.append("Starters to swap out: " + "; ".join(swap[:4]))
    if watch:
        spoken.append("Questionable starters: " + "; ".join(watch[:3]))
    panel = ([f"  Changed: {c}" for c in changes] + [f"  Swap out: {s}" for s in swap]
             + [f"  Questionable: {w}" for w in watch])
    return ("Fantasy football. " + ". ".join(spoken) + "." if spoken else ""), panel


def _activity(league, player) -> str:
    lines = []
    for a in league.recent_activity(size=12):
        # (team, action, player), plus a bid amount on newer ESPN data.
        for team, action, p, *_ in a.actions:
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


def _discover(swid: str, s2: str, season: int) -> list[str]:
    """Every football league this ESPN account has a team in this season, from
    the account lookup the ESPN apps use. Empty if ESPN won't say."""
    import requests
    try:
        r = requests.get(f"https://fan.api.espn.com/apis/v2/fans/{quote(swid)}",
                         params={"displayHiddenPrefs": "true", "context": "fantasy",
                                 "useCookieAuth": "true", "source": "fantasyapp-ios"},
                         cookies={"SWID": swid, "espn_s2": s2}, timeout=8, allow_redirects=False)
        data = r.json()
    except Exception:
        return []
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            # Fantasy entries: gameId 1 is football; groups are the leagues.
            if node.get("gameId") == 1 and isinstance(node.get("groups"), list) \
                    and node.get("seasonId") in (None, season):
                found.extend(str(g["groupId"]) for g in node["groups"]
                             if isinstance(g, dict) and g.get("groupId"))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return list(dict.fromkeys(found))


def _try_load(league_id: str, cfg: dict):
    try:
        return _load(league_id, cfg)
    except Exception as exc:
        print(f"[ESPN] league {league_id} didn't load: {exc!r}")
        return None


def _connect_done() -> str:
    try:
        raw = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        raw = ""
    cfg = get_plugin_config("espn_fantasy")
    s2 = re.search(r"espn_s2=([^;\s'\"]+)", raw)
    swid = re.search(r"SWID=(\{[0-9A-Fa-f-]+\})", raw)
    copied = re.search(r"leagues/(\d+)", raw) or re.search(r"leagueId=(\d+)", raw)
    league_id = copied.group(1) if copied else ""
    if not league_id and s2:
        league_id = _league_in_chrome()
    if not (league_id or s2):
        return ("I don't see your ESPN login or league in what you copied. In Developer Tools, "
                "filter for lm-api-reads, reload, right-click a request whose name starts with "
                "numbers, choose Copy, then Copy as cURL, and say done again.")
    year = re.search(r"seasons/(\d{4})", raw)
    values = {"year": year.group(1) if year else str(cfg.get("year") or _season()),
              "espn_s2": s2.group(1) if s2 else str(cfg.get("espn_s2") or ""),
              "swid": swid.group(1) if swid else str(cfg.get("swid") or "")}
    found = _discover(values["swid"], values["espn_s2"], int(values["year"])) \
        if values["swid"] and values["espn_s2"] else []
    ids = list(dict.fromkeys(_league_ids(cfg) + ([league_id] if league_id else []) + found))
    if not ids:
        return ("I have your ESPN login but couldn't find a league. Open your team in Chrome so "
                "the address shows leagueId=, copy a request whose name starts with numbers, "
                "and say done again.")
    trial = {**cfg, **values}
    with ThreadPoolExecutor(max_workers=len(ids)) as pool:
        loaded = list(pool.map(lambda i: _try_load(i, trial), ids))
    connected = [(i, lg) for i, lg in zip(ids, loaded) if lg is not None]
    if not connected:
        return ("ESPN didn't accept that login. Make sure you're signed in and copied a request "
                "from your own league, then say done again.")
    save_plugin_config("espn_fantasy", {**values, "league_ids": ",".join(i for i, _ in connected),
                                        "league_id": ""})
    try:
        subprocess.run(["pbcopy"], input="", text=True, timeout=5)        # cookies off the clipboard
    except Exception:
        pass
    parts = []
    for _, league in connected:
        mine = _mine(league, values["swid"])
        parts.append(f"{_name(league)} (you're {mine.team_name}, {mine.wins}-{mine.losses})"
                     if mine else f"{_name(league)} (I couldn't tell which team is yours)")
    text = (f"Connected {len(connected)} league{'s' if len(connected) != 1 else ''}: "
            + "; ".join(parts) + ".")
    if len(connected) == 1 and not found:
        text += (" If you're in other leagues, open each one in Chrome, copy a request the same "
                 "way and say done; I'll add it.")
    return text


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
            count = len(_league_ids(get_plugin_config("espn_fantasy")))
            return (f"ESPN Fantasy is connected to {count} league{'s' if count != 1 else ''}." if count
                    else "ESPN Fantasy isn't connected. Say 'connect ESPN fantasy' to set it up.")
        handlers = {
            "injuries": lambda lg, show: _injuries(lg, team, show),
            "lineup": lambda lg, show: _lineup(lg, team, show),
            "matchup": lambda lg, show: _matchup(lg, show),
            "player": lambda lg, show: _player(lg, str(args.get("name") or ""), show),
            "free_agents": lambda lg, show: _free_agents(lg, str(args.get("position") or ""),
                                                         int(args.get("count") or 0), show),
            "standings": lambda lg, show: _standings(lg, show),
            "activity": lambda lg, show: _activity(lg, show),
            "leagues": lambda lg, show: _summary(lg),
        }
        if action not in handlers:
            return ("ESPN commands: injuries, lineup, matchup, player, free_agents, standings, "
                    "activity, leagues, connect, status.")
        leagues = _leagues(str(args.get("league") or ""))
        if len(leagues) == 1 and action != "leagues":
            return handlers[action](leagues[0], player)
        if action == "player":
            return _player_everywhere(leagues, str(args.get("name") or ""), player)

        def one(lg):
            try:
                return f"== {_name(lg)} ==\n{handlers[action](lg, None)}"
            except ValueError as exc:
                return f"== {_name(lg)} ==\n{exc}"

        with ThreadPoolExecutor(max_workers=len(leagues)) as pool:
            text = "\n\n".join(pool.map(one, leagues))
        _display(player, "ESPN · YOUR LEAGUES", text.split("\n"))
        return text
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
