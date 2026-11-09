"""Jarvis's own brain: a background thinker that notices things on its own.

The voice model answers what it is asked. This runs beside it, on its own clock,
and does what a sharp person who knows you does between conversations: looks at
everything going on (the conversation you just had, open missions and how
overdue they are, the shop, the videos, the internship hunt, paper trading,
fantasy football, anything that failed in the background) and thinks about it.
Out of that come a few THOUGHTS: something it noticed, a heads-up, a
suggestion, each worded the way Jarvis would actually say it.

Those thoughts reach the user two ways (main.py does the delivery):
  * in conversation: after Jarvis answers, the freshest thought is slipped into
    the live session as private context ([INNER_THOUGHTS]), so on the next turn
    Jarvis can add "by the way..." when it fits, like a person would;
  * on its own: a real heads-up (urgency 4-5) is spoken after a short quiet
    spell, and a lesser one waits for the long-silence check-in instead of a
    generic "how's it going".

The thinking itself runs on the SMART ladder (Claude on the membership first,
Gemini behind it). It is rate limited and capped per day so it never eats the
usage the viral videos and the shop need.

Everything here except Brain.think_once()'s model call is plain data handling,
so it is testable without a network.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

BASE = Path(__file__).resolve().parent.parent
BRAIN_PATH = BASE / "memory" / "brain.json"

HEARTBEAT_ACTIVE_S = 45 * 60      # think this often while the user is around
HEARTBEAT_IDLE_S = 3 * 3600       # and this often when nobody has spoken for hours
AFTER_TALK_S = 90                 # think this long after a conversation goes quiet
MIN_GAP_S = 12 * 60               # never two thinks closer than this
DAILY_CAP = 30                    # thinks per day, all triggers together
THOUGHT_TTL_S = 8 * 3600          # an unshared thought goes stale after this
TOPIC_QUIET_S = 12 * 3600         # don't raise the same topic twice in this window
KEEP = 60                         # thoughts kept on disk

KINDS = ("heads_up", "suggestion", "noticed")

THINK_SYSTEM = """You are the private inner mind of {name}, a personal AI who lives on {user}'s Mac.
Nobody hears this part. This is where you think, the way a sharp, loyal person who
knows {user} well thinks between conversations: you look at what is going on in
their life and their projects and you NOTICE things. Patterns, things slipping,
things going well, things about to go wrong, connections they have not made, a
better way to do something, something they said earlier that matters now.

Think like a human being, not a monitoring system:
- Have opinions. "That shirt is the only thing that has sold, I'd lean into it"
  beats "sales data is available".
- Connect dots across areas (a deadline + a busy evening, a failing job + money).
- Notice what is NOT happening (no sales in a week, a homework past due, a video
  slot that never posted).
- Care about the person, not only the projects: sleep, an exam coming, a long
  day of talking to you at 2am.
- Only real things. Every thought must rest on a fact in the snapshot. If the
  snapshot is thin or nothing is worth saying, return no thoughts. Silence is
  better than filler.
- Never repeat something in RECENT THOUGHTS unless it got materially worse.

Each thought is worded EXACTLY as {name} would say it out loud to {user}: first
person, warm, plain, dry British understatement, one or two short sentences, a
STATEMENT that ends on the point or the suggestion. Never a question, never "let
me know", never "would you like me to", never an offer. No emoji. No mention of
snapshots, data, systems, or being an AI.

kind: "heads_up" = something needs attention or is going wrong; "suggestion" =
a concrete idea that would help; "noticed" = an observation worth sharing.
urgency 1-5: 5 = say it now (money at risk, deadline today, something broken),
4 = soon, 3 = next natural moment, 1-2 = only if it fits the conversation.

Reply with only JSON:
{{"thinking": "<your private train of thought, 2-5 sentences, honest and specific>",
  "thoughts": [{{"kind": "...", "urgency": 1, "topic": "<2-4 word label>",
                "text": "<what {name} would say>"}}]}}
At most 3 thoughts. Usually 0-2."""


# ── small helpers ───────────────────────────────────────────────────────────

def _read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _short(text, limit=140) -> str:
    return " ".join(str(text or "").split())[:limit]


def _topic_key(topic: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(topic or "").lower()).strip()


_WORD = re.compile(r"[a-z0-9][a-z0-9']{3,}")
_STOP = {"about", "there", "their", "which", "would", "could", "should", "might", "still",
         "these", "those", "today", "right", "think", "worth", "maybe", "being", "where", "while",
         "after", "before", "again", "since", "that", "this", "with", "have", "your", "just",
         "been", "from", "they", "them", "what", "when", "then", "than", "into", "some", "more",
         "it's", "you're", "i've", "i'd", "also", "very", "much", "only", "over"}


def content_words(text: str) -> set:
    return {w for w in _WORD.findall(str(text or "").lower()) if w not in _STOP}


def was_voiced(thought_text: str, spoken: str) -> bool:
    """True when what Jarvis just said clearly carries this thought: most of the
    thought's distinctive words came out of its mouth."""
    want = content_words(thought_text)
    if len(want) < 2:
        return False
    got = content_words(spoken)
    return len(want & got) / len(want) >= 0.4


# A reply that ends by fishing for more work. The voice prompt forbids these; when
# one slips out anyway, main.py reminds the live session in private.
_CHECK_IN = re.compile(
    r"(anything else|is there anything|what else can i|let me know if|would you like me to|"
    r"do you want me to|shall i\b|should i\b|want me to\b|need anything else|"
    r"how can i help|can i help with|happy to help|how else can i)[^.!?]*\?\s*$",
    re.IGNORECASE)


_CONFIRMING = re.compile(r"go ahead|send (it|this|that)|proceed|confirm|place the call|ring (them|him|her)",
                         re.IGNORECASE)


def ends_with_check_in(reply: str) -> bool:
    """True when the last sentence of a reply is a robotic check-in or offer."""
    reply = str(reply or "").strip()
    if not reply.endswith("?"):
        return False
    last = re.split(r"(?<=[.!?])\s+", reply)[-1]
    if _CONFIRMING.search(last):
        return False        # asking the OK for a send or a call is a real question
    return bool(_CHECK_IN.search(last))


# ── what the brain looks at ─────────────────────────────────────────────────

def _missions(today: str) -> list[str]:
    data = _read_json(BASE / "memory" / "mission_control.json", {}) or {}
    out = []
    for t in data.get("tasks") or []:
        if t.get("status", "open") != "open":
            continue
        due = t.get("due") or ""
        if due and due < today:
            days = (date.fromisoformat(today) - date.fromisoformat(due)).days
            state = f"OVERDUE by {days} day{'s' if days != 1 else ''} (was due {due})"
        elif due == today:
            state = "due TODAY"
        elif due:
            state = f"due {due}"
        else:
            state = "no deadline"
        out.append(f"- {_short(t.get('title'), 80)}: {state}")
    return out[:10]


def _canvas(now: float) -> list[str]:
    """Canvas as of its last read (cached by the plugin; no network here)."""
    try:
        from plugins.canvas_school import DONE, last_known_status
    except Exception:
        return []
    items, at = last_known_status()
    if not items or now - at > 2 * 86400:
        return []
    age = (now - at) / 3600
    out = [f"- (read {age:.0f}h ago)" if age >= 1 else "- (read within the hour)"]
    todo = [i for i in items if i.get("status") not in DONE][:10]
    done = [i for i in items if i.get("status") in DONE][-4:]
    for i in todo:
        out.append(f"- {i.get('status')}: {_short(i.get('name'), 70)} ({_short(i.get('course'), 30)}), due {i.get('due') or 'n/a'}")
    for i in done:
        out.append(f"- {i.get('status')}: {_short(i.get('name'), 70)}")
    return out


def _viral(now: float) -> list[str]:
    v = _read_json(BASE / "memory" / "viral.json", {}) or {}
    out = []
    today = date.fromtimestamp(now).isoformat()
    slots = (v.get("slots") or {}).get(today)
    if slots is not None:
        out.append(f"- videos posted in today's slots so far: {len(slots)}")
    vids = v.get("videos") or []
    if vids:
        last = vids[-1]
        out.append(f"- newest video: \"{_short(last.get('title'), 90)}\" "
                   f"(TikTok posted: {bool(last.get('posted'))}, Instagram posted: {bool(last.get('ig_posted'))})")
    held = v.get("held") or []
    if held:
        out.append(f"- {len(held)} finished video(s) held back, not posted")
    stats = [s for s in (v.get("tiktok") or {}).values() if isinstance(s, dict) and s.get("views") is not None]
    if stats:
        stats.sort(key=lambda s: s.get("first") or 0)
        recent = stats[-6:]
        views = [int(s.get("views") or 0) for s in recent]
        best = max(stats, key=lambda s: int(s.get("views") or 0))
        out.append(f"- TikTok views on the last {len(views)} videos: {views}")
        out.append(f"- best TikTok ever: {int(best.get('views') or 0)} views "
                   f"(\"{_short(best.get('desc'), 70)}\")")
    return out


def _shop(now: float) -> list[str]:
    s = _read_json(BASE / "memory" / "etsy_shop.json", {}) or {}
    out = []
    sales = s.get("sales_log") or []
    if sales:
        last = sales[-1]
        days = (now - float(last.get("at") or now)) / 86400
        out.append(f"- {len(sales)} sale(s) logged in total; last one {days:.0f} days ago "
                   f"on {last.get('channel')}: \"{_short(last.get('items'), 60)}\" ${last.get('price')}")
    else:
        out.append("- no sales logged yet")
    out.append(f"- designs made: {len(s.get('designs') or [])}; spin-offs made: {s.get('spinoffs_made', 0)}")
    if s.get("last_fail_day"):
        out.append(f"- last day something failed in the shop worker: {s['last_fail_day']}")
    return out


def _internships() -> list[str]:
    d = _read_json(BASE / "memory" / "internships.json", {}) or {}
    jobs = list((d.get("jobs") or {}).values())
    if not jobs:
        return []
    from collections import Counter
    c = Counter(j.get("status") for j in jobs)
    out = [f"- applications by status: {dict(c)}"]
    replied = [j for j in jobs if j.get("status") == "replied"]
    for j in replied[:3]:
        out.append(f"- reply from {_short(j.get('company'), 40)} ({_short(j.get('title'), 50)})")
    manual = c.get("manual", 0)
    if manual:
        out.append(f"- {manual} need applying by hand (the auto-apply can't do them)")
    return out


def _fantasy(now: float) -> list[str]:
    d = _read_json(BASE / "memory" / "espn_status.json", {}) or {}
    out = []
    cutoff = datetime.fromtimestamp(now - 36 * 3600).isoformat()
    for ch in (d.get("changes") or [])[-6:]:
        if str(ch.get("at") or "") >= cutoff:
            out.append(f"- {ch.get('at', '')[:16]}: {_short(ch.get('text'), 140)}")
    return out


def _trading() -> list[str]:
    st = _read_json(BASE / "config" / "trading_day" / "state.json", {}) or {}
    out = []
    seen = st.get("last_seen") or {}
    if seen.get("equity") and st.get("start_equity"):
        pct = (float(seen["equity"]) / float(st["start_equity"]) - 1) * 100
        out.append(f"- day-trading paper account: ${float(seen['equity']):,.0f} "
                   f"({pct:+.1f}% since {st.get('started_at')}), last seen {str(seen.get('t'))[:10]}")
    return out


def _attention(now: float) -> list[str]:
    try:
        from core.events import Store
        store = Store()
        items = store.open_items(("job_failed", "job_paused", "connection_missing",
                                  "decision_needed", "campaign_stopped"), max_age_days=3, now=now)
    except Exception:
        return []
    out = []
    for e in items[:6]:
        when = datetime.fromtimestamp(float(e.get("ts") or now)).strftime("%a %H:%M")
        detail = e.get("detail") or {}
        what = detail.get("text") or detail.get("error") or detail.get("why") or e.get("status") or ""
        out.append(f"- {when} {e.get('source')}: {e.get('kind')} {_short(what, 100)}")
    return out


def _system() -> list[str]:
    out = []
    try:
        import psutil
        b = psutil.sensors_battery()
        if b is not None:
            out.append(f"- battery {b.percent:.0f}% ({'charging' if b.power_plugged else 'on battery'})")
        disk = psutil.disk_usage(str(Path.home()))
        out.append(f"- disk free: {disk.free / 1e9:.0f} GB")
    except Exception:
        pass
    return out


def _memory() -> str:
    try:
        from memory.memory_manager import format_memory_for_prompt, load_memory
        return _short(format_memory_for_prompt(load_memory()), 2500)
    except Exception:
        return ""


def gather(recent_turns: list[str], now: Optional[float] = None) -> str:
    """One plain-text snapshot of everything the brain can see right now."""
    now = now or time.time()
    dt = datetime.fromtimestamp(now)
    today = dt.date().isoformat()
    sections = [f"NOW: {dt.strftime('%A %B %d %Y, %I:%M %p')}"]

    def add(title, fn):
        try:
            lines = fn()
        except Exception as e:
            lines = [f"- (could not read: {type(e).__name__})"]
        if lines:
            sections.append(title + "\n" + "\n".join(lines))

    if recent_turns:
        sections.append("CONVERSATION JUST NOW (newest last):\n" +
                        "\n".join(_short(t, 300) for t in recent_turns[-14:]))
    add("OPEN MISSIONS:", lambda: _missions(today))
    add("CANVAS SCHOOLWORK:", lambda: _canvas(now))
    add("NEEDS ATTENTION (background jobs):", lambda: _attention(now))
    add("VIRAL VIDEOS (TikTok + Instagram, goal 3 a day):", lambda: _viral(now))
    add("PRINT-ON-DEMAND SHOP:", lambda: _shop(now))
    add("INTERNSHIP AUTO-APPLY (Summer 2027):", _internships)
    add("FANTASY FOOTBALL (last 36h):", lambda: _fantasy(now))
    add("PAPER TRADING:", _trading)
    add("THIS MAC:", _system)
    mem = _memory()
    if mem:
        sections.append("WHAT I KNOW ABOUT THEM:\n" + mem)
    return "\n\n".join(sections)


# ── the brain ───────────────────────────────────────────────────────────────

def _default_think(system: str, prompt: str) -> Optional[dict]:
    from core import gemini
    return gemini.as_json(prompt, tier=gemini.SMART,
                          config={"system_instruction": system, "response_mime_type": "application/json"},
                          timeout_ms=150_000, default=None)


class Brain:
    """Thinks in the background and keeps a short list of thoughts to share."""

    def __init__(self, path: Path = BRAIN_PATH, *, think: Callable[[str, str], Optional[dict]] = None,
                 snapshot: Callable[[list], str] = None, clock: Callable[[], float] = time.time,
                 name: str = "Jarvis", user: str = "the user"):
        self.path = Path(path)
        self._think = think or _default_think
        self._snapshot = snapshot or gather
        self._clock = clock
        self.name, self.user = name, user
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._turns: list[str] = []
        self._last_talk = 0.0
        self._talk_pending = False
        self._busy = False
        self.state = self._load()

    # persistence
    def _load(self) -> dict:
        d = _read_json(self.path, {}) or {}
        d.setdefault("thoughts", [])
        d.setdefault("last_think", 0.0)
        d.setdefault("day", "")
        d.setdefault("count", 0)
        d.setdefault("thinking", "")
        return d

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e:
            print(f"[Brain] could not save: {e!r}")

    # input from the conversation
    def heard(self, turns: list[str]) -> None:
        """The conversation moved on; think about it once it goes quiet."""
        with self._lock:
            self._turns = list(turns or [])[-14:]
            self._last_talk = self._clock()
            self._talk_pending = True

    # when to think
    def due(self) -> Optional[str]:
        """Why a think should happen now, or None."""
        now = self._clock()
        with self._lock:
            today = date.fromtimestamp(now).isoformat()
            if self.state["day"] == today and self.state["count"] >= DAILY_CAP:
                return None
            since = now - float(self.state["last_think"] or 0)
            if since < MIN_GAP_S:
                return None
            if self._talk_pending and now - self._last_talk >= AFTER_TALK_S:
                return "conversation"
            active = now - self._last_talk < 2 * 3600
            if since >= (HEARTBEAT_ACTIVE_S if active else HEARTBEAT_IDLE_S):
                return "heartbeat"
        return None

    # thinking
    def think_once(self, reason: str = "manual") -> list[dict]:
        """Look at everything, think, keep any new thoughts. Returns the new ones."""
        with self._lock:
            if self._busy:
                return []
            self._busy = True
            now = self._clock()
            today = date.fromtimestamp(now).isoformat()
            if self.state["day"] != today:
                self.state["day"], self.state["count"] = today, 0
            self.state["count"] += 1
            self.state["last_think"] = now
            self._talk_pending = False
            turns = list(self._turns)
            recent = self.state["thoughts"][-12:]
            self._save()
        try:
            snap = self._snapshot(turns)
            past = "\n".join(
                f"- [{'said' if t.get('shared') else 'not said yet'}] {t.get('topic')}: {t.get('text')}"
                for t in recent) or "(none)"
            prompt = f"{snap}\n\nRECENT THOUGHTS (yours, don't repeat):\n{past}\n\nWhy you're thinking now: {reason}."
            system = THINK_SYSTEM.format(name=self.name, user=self.user)
            reply = self._think(system, prompt)
        except Exception as e:
            print(f"[Brain] think failed: {type(e).__name__}: {str(e)[:120]}")
            reply = None
        finally:
            with self._lock:
                self._busy = False
        return self.absorb(reply)

    def absorb(self, reply) -> list[dict]:
        """Keep the valid, non-repeated thoughts from one think."""
        if not isinstance(reply, dict):
            return []
        now = self._clock()
        fresh = []
        with self._lock:
            if reply.get("thinking"):
                self.state["thinking"] = _short(reply["thinking"], 800)
            for raw in (reply.get("thoughts") or [])[:3]:
                if not isinstance(raw, dict):
                    continue
                text = _short(raw.get("text"), 320)
                if len(text) < 12 or ends_with_check_in(text):
                    continue
                kind = raw.get("kind") if raw.get("kind") in KINDS else "noticed"
                try:
                    urgency = max(1, min(5, int(raw.get("urgency") or 2)))
                except (TypeError, ValueError):
                    urgency = 2
                topic = _short(raw.get("topic") or kind, 40)
                if self._recently_raised(topic, now):
                    continue
                t = {"id": f"{int(now * 1000)}-{len(fresh)}", "at": now, "kind": kind,
                     "urgency": urgency, "topic": topic, "text": text,
                     "shared": None, "primed": None}
                self.state["thoughts"].append(t)
                fresh.append(t)
            self.state["thoughts"] = self.state["thoughts"][-KEEP:]
            self._save()
        for t in fresh:
            print(f"[Brain] {t['kind']} ({t['urgency']}) {t['topic']}: {t['text']}")
        return fresh

    def _recently_raised(self, topic: str, now: float) -> bool:
        key = _topic_key(topic)
        for t in self.state["thoughts"]:
            if _topic_key(t.get("topic")) != key:
                continue
            if t.get("shared") and now - float(t["shared"]) < TOPIC_QUIET_S:
                return True
            if not t.get("shared") and now - float(t.get("at") or 0) < THOUGHT_TTL_S:
                return True     # already waiting to be said
        return False

    # what to share
    def pending(self, min_urgency: int = 1) -> list[dict]:
        """Unshared, unexpired thoughts, most urgent and newest first."""
        now = self._clock()
        with self._lock:
            live = [t for t in self.state["thoughts"]
                    if not t.get("shared") and now - float(t.get("at") or 0) < THOUGHT_TTL_S
                    and int(t.get("urgency") or 1) >= min_urgency]
        return sorted(live, key=lambda t: (-int(t["urgency"]), -float(t["at"])))

    def unprimed(self) -> list[dict]:
        return [t for t in self.pending() if not t.get("primed")]

    def mark(self, thought_id: str, field: str) -> None:
        with self._lock:
            for t in self.state["thoughts"]:
                if t.get("id") == thought_id:
                    t[field] = self._clock()
            self._save()

    def heard_reply(self, spoken: str) -> list[dict]:
        """Jarvis just said `spoken`; any primed thought it carried is now shared."""
        done = []
        for t in self.pending():
            if t.get("primed") and was_voiced(t["text"], spoken):
                self.mark(t["id"], "shared")
                done.append(t)
        return done

    @property
    def thinking(self) -> str:
        return self.state.get("thinking") or ""

    # the loop
    def run_forever(self, stop: threading.Event) -> None:
        stop.wait(60)               # let Jarvis finish starting first
        while not stop.is_set():
            why = self.due()
            if why:
                self.think_once(why)
            stop.wait(20)


# ── the words handed to the live session ────────────────────────────────────

def priming_text(thoughts: list[dict], thinking: str = "") -> str:
    """Private context for the live voice session: what Jarvis is thinking about."""
    lines = "\n".join(f"- ({t['kind']}, urgency {t['urgency']}) {t['text']}" for t in thoughts)
    mind = f"\nWhat's been on your mind: {thinking}" if thinking else ""
    return (
        "[INNER_THOUGHTS] Your own thoughts from thinking in the background. Private: do not "
        "reply to this message and do not speak now.\n" + lines + mind +
        "\nOn your next reply, once you have dealt with what the user asked, you may add ONE of "
        "these in a sentence, in your own words, as a natural 'by the way' or 'heads-up', when it "
        "fits the moment. Skip it if the user is busy, stressed or mid-task on something else. It "
        "is a statement, not a question. Never say you were thinking in the background.")


def speak_text(thought: dict) -> str:
    """A live-session turn that makes Jarvis voice one thought unprompted."""
    return (
        "[INNER_THOUGHT] Something you noticed on your own and decided to bring up now, unprompted. "
        "Say it to the user in your own words, in their language, in one or two short sentences, "
        "like a friend who just spotted it. A statement that ends on the point or the suggestion: "
        "no question, no offer, no 'let me know'. Do not call any tools. Do not mention this "
        f"message.\nThe thought ({thought['kind']}): {thought['text']}")


STYLE_NUDGE = (
    "[STYLE_NOTE] Private, do not reply or speak. Your last reply ended with a check-in or an "
    "offer question. Don't do that: when a task is done, the result is the last sentence. Ask a "
    "question only when you genuinely cannot carry on without the answer.")
