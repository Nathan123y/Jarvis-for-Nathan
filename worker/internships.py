"""The internship crew, run by the background worker: Scout -> Profile keeper -> Applicant -> Inbox watcher.

  Scout          once a day: the Summer 2027 internship list that SimplifyJobs keeps on GitHub, narrowed to
                 hardware / electrical / semiconductor roles in California open to bachelor's students
  Profile keeper the user's info file (config/internships/profile.md) and résumé (resume.pdf), and nothing else;
                 a required question they don't cover is added to the bottom of the info file for the user
  Applicant      up to `per_day` Greenhouse applications a day, spread from `start` to `end`
                 (worker/intern_apply.py); other sites (Workday, Tesla, ...) can't be done automatically, so a few
                 of those are texted each morning for the user to apply to by hand
  Inbox watcher  replies from those companies in the personal Gmail: interview, coding test, rejection, received

`practice` (on until the user turns it off) fills one application and stops before Submit, for a look first.
LinkedIn and Handshake are never automated: their terms forbid it. Every text goes to the user's phone.

State: memory/internships.json; screenshots of every filled form: config/internships/applications/.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from datetime import datetime
from email.utils import parseaddr
from html import unescape
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
DIR = ROOT / "config" / "internships"
PROFILE = DIR / "profile.md"
RESUME = DIR / "resume.pdf"
RESUME_TEXT = DIR / "resume.txt"
TRANSCRIPT = DIR / "transcript.pdf"     # optional: some applications require an unofficial transcript
SHOTS = DIR / "applications"
BROWSER = DIR / "browser"
STATE = ROOT / "memory" / "internships.json"
NS = "internships"
FEED = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"
TERM = "Summer 2027"
DEFAULTS = {"enabled": True, "per_day": 5, "start": "06:00", "end": "21:00", "practice": True}
SCOUT_AT = (5, 45)                   # before the first application of the day
DIGEST_AT = (8, 0)                   # the morning "apply to these yourself" text
MANUAL_PER_DAY = 3
REPLIES_EVERY = 600
UPLOAD = re.compile(r"upload|attach|transcript|cover letter|portfolio|writing sample|\bcv\b", re.I)
REPLY_HOURS = (7, 23)                # reply texts wait for the morning instead of buzzing at night

CA = re.compile(r",\s*CA\b|california", re.I)
FIELD = re.compile(r"hardware|electrical|electronic|silicon|asic|analog|mixed.signal|circuit|semiconductor|chip|vlsi|"
                   r"pcb|power|rf\b|signal integrity|validation|verification|test|process|device|packaging|fpga|soc|dft|"
                   r"physical design|layout|photonics|optical", re.I)
OTHER = re.compile(r"software|firmware|embedded|machine learning|\bml\b|\bai\b|data|product|quant|marketing|business|"
                   r"finance|sales|recruit|design intern - ux|ux|ui\b", re.I)
REQUIRED_KEYS = ("first name", "last name", "email", "phone", "city", "graduation month and year", "gpa",
                 "authorized to work in the us for any employer", "will you need visa sponsorship now or in the future")
REPLY_KINDS = (                      # first match wins
    ("security", re.compile(r"security code|verification code|verify your email", re.I), None),
    ("interview", re.compile(r"interview|schedule a (?:call|time|chat)|availability|next steps?|phone screen|recruiter call", re.I),
     "🎉 {company} wants to talk: interview / next steps"),
    ("coding test", re.compile(r"assessment|hackerrank|codesignal|coding challenge|technical (?:test|challenge)|online test", re.I),
     "🧪 {company} sent an assessment / coding test"),
    ("rejection", re.compile(r"unfortunately|not (?:be )?moving forward|other candidates|decided to|not selected|regret", re.I),
     "❌ {company} said no this time"),
    ("received", re.compile(r"received your application|thank you for (?:applying|your application|your interest)|application (?:was )?received", re.I),
     "📨 {company} confirmed they got your application"),
)


# ── settings and state ───────────────────────────────────────────────────────
def settings() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(NS)
    except Exception:
        stored = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in stored.items() if k in DEFAULTS and v not in (None, "")})
    try:
        out["per_day"] = max(0, min(40, int(out["per_day"])))
    except (TypeError, ValueError):
        out["per_day"] = DEFAULTS["per_day"]
    return out


def load_state(path: Path = STATE) -> dict:
    try:
        st = json.loads(path.read_text())
    except (OSError, ValueError):
        st = {}
    st.setdefault("jobs", {})
    st.setdefault("replies_seen", [])
    return st


def save_state(st: dict, path: Path = STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(path)


# ── the Profile keeper's file ────────────────────────────────────────────────
def profile_fields(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def profile_gaps(profile_path: Path = PROFILE, resume: Path = RESUME) -> list[str]:
    """What still has to be filled in before anything can be applied to."""
    try:
        f = profile_fields(profile_path.read_text())
    except OSError:
        return ["the info file"]
    answered = {k for k, v in f.items() if v}            # labels carry hints like "(yes / no)": match on the start
    gaps = [k for k in REQUIRED_KEYS if not any(a == k or a.startswith(k + " (") for a in answered)]
    if not resume.exists():
        gaps.append("resume.pdf")
    return gaps


def add_questions(questions: list[str], profile_path: Path = PROFILE) -> list[str]:
    """Adds required questions the file doesn't answer to its last section, once each. Returns the new ones."""
    text = profile_path.read_text()
    head = "## Extra answers (questions applications asked that this file didn't cover)"
    have = {k for k in profile_fields(text)}
    new = [q for q in dict.fromkeys(" ".join(q.split()).rstrip(":?*. ") for q in questions) if q.lower() not in have]
    if new:
        if head not in text:
            text = text.rstrip() + f"\n\n{head}\n"
        text = text.rstrip() + "\n" + "\n".join(f"{q}:" for q in new) + "\n"
        profile_path.write_text(text)
    return new


def resume_text(resume: Path = RESUME, cache: Path = RESUME_TEXT) -> str:
    """The résumé as text, read once by Gemini (and again whenever the PDF changes)."""
    if cache.exists() and cache.stat().st_mtime >= resume.stat().st_mtime:
        return cache.read_text()
    from google.genai import types
    from core import gemini
    text = gemini.text([types.Part.from_bytes(data=resume.read_bytes(), mime_type="application/pdf"),
                        "Copy this résumé's text exactly, in reading order, as plain text. No commentary."],
                       tier=gemini.SMART) or ""
    if text.strip():
        cache.write_text(text)
    return text


# ── Scout ────────────────────────────────────────────────────────────────────
def wanted(item: dict, prof: dict) -> bool:
    if not item.get("active") or not item.get("is_visible", True) or TERM not in (item.get("terms") or []):
        return False
    if item.get("category") not in ("Hardware", "Hardware Engineering"):
        return False
    if not any(CA.search(loc) for loc in item.get("locations") or []):
        return False
    degrees = item.get("degrees") or []
    if degrees and not any("bachelor" in d.lower() for d in degrees):
        return False
    title = item.get("title", "")
    if OTHER.search(title) and not FIELD.search(title):
        return False
    sponsor = (item.get("sponsorship") or "").lower()
    if prof.get("will you need visa sponsorship now or in the future", "").lower().startswith("y") and "does not offer" in sponsor:
        return False
    if "citizenship" in sponsor and "citizen" not in prof.get("citizenship (us citizen / permanent resident / other)", "").lower():
        return False
    return True


def ats_of(url: str) -> str:
    from worker.intern_apply import GH_URL
    return "greenhouse" if GH_URL.match(url or "") else "manual"


def scout(st: dict, get: Callable[[str], bytes], prof: dict, now: float) -> int:
    items = json.loads(get(FEED))
    new = 0
    for it in items:
        if not wanted(it, prof):
            continue
        jid = str(it.get("id"))
        if jid in st["jobs"]:
            continue
        st["jobs"][jid] = {"id": jid, "company": it.get("company_name", ""), "title": it.get("title", ""),
                           "url": it.get("url", ""), "locations": it.get("locations") or [],
                           "posted": it.get("date_posted") or 0, "ats": ats_of(it.get("url", "")),
                           "status": "new", "found": now}
        new += 1
    open_ids = {str(it.get("id")) for it in items if it.get("active")}
    for j in st["jobs"].values():                     # a posting that closed is no longer worth applying to
        if j["status"] == "new" and j["id"] not in open_ids:
            j["status"] = "closed"
    st["scouted"] = now
    return new


def job_description(job: dict, get: Callable[[str], bytes]) -> str:
    from worker.intern_apply import GH_URL
    m = GH_URL.match(job["url"])
    if not m:
        return ""
    try:
        d = json.loads(get(f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}"))
    except Exception:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(unescape(d.get("content") or "")))).strip()


# ── Inbox watcher ────────────────────────────────────────────────────────────
def classify(subject: str, snippet: str) -> tuple[str, Optional[str]]:
    text = f"{subject} {snippet}"
    for kind, rx, line in REPLY_KINDS:
        if rx.search(text):
            return kind, line
    return "other", "✉️ {company} emailed you about your application"


def company_in(job_company: str, sender: str, subject: str, snippet: str) -> bool:
    name = re.sub(r"[^a-z0-9 ]", "", job_company.lower()).strip()
    if not name:
        return False
    hay = re.sub(r"[^a-z0-9@. ]", " ", f"{sender} {subject} {snippet}".lower())
    # the name as whole words, or squashed into the sender's domain (Muon Space -> @muonspace.com)
    return (re.search(rf"\b{re.escape(name)}\b", hay) is not None
            or re.search(rf"@[\w.-]*{re.escape(name.replace(' ', ''))}", hay) is not None)


def check_replies(st: dict, service, notify: Callable[[str], None]) -> int:
    applied = [j for j in st["jobs"].values() if j["status"] in ("applied", "replied")]
    if not applied:
        return 0
    names = sorted({j["company"] for j in applied})
    q = ("newer_than:3d -from:me -category:promotions -category:social ("
         + " OR ".join(f'"{n}"' for n in names[:40]) + ")")
    api = service.users().messages()
    seen = set(st["replies_seen"])
    n = 0
    for m in api.list(userId="me", q=q, maxResults=25).execute().get("messages", []):
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        msg = api.get(userId="me", id=m["id"], format="metadata", metadataHeaders=["From", "Subject"]).execute()
        h = {x["name"].lower(): x["value"] for x in msg["payload"]["headers"]}
        name, addr = parseaddr(h.get("from", ""))
        subject, snippet = h.get("subject", ""), unescape(msg.get("snippet", ""))
        job = next((j for j in sorted(applied, key=lambda j: -j.get("applied_at", 0))
                    if company_in(j["company"], f"{name} {addr}", subject, snippet)), None)
        if not job:
            continue
        kind, line = classify(subject, snippet)
        if not line:
            continue
        job.setdefault("replies", []).append({"at": time.time(), "kind": kind, "subject": subject[:140]})
        if kind != "received":
            job["status"] = "replied"
            job["outcome"] = kind
        notify(line.format(company=job["company"]) + f" ({job['title'][:60]}): \"{subject[:80]}\"")
        n += 1
    st["replies_seen"] = list(seen)[-500:]
    return n


def security_code(service, since: float) -> Optional[str]:
    """Greenhouse sometimes emails a code that has to be typed in before the application goes through."""
    from worker.campaign.mail import _text
    api = service.users().messages()
    for _ in range(24):
        for m in api.list(userId="me", q="newer_than:1d (security code) greenhouse", maxResults=5).execute().get("messages", []):
            msg = api.get(userId="me", id=m["id"], format="full").execute()
            if int(msg.get("internalDate", 0)) / 1000 < since - 30:
                continue
            body = _text(msg.get("payload", {})) or msg.get("snippet", "")
            hit = re.search(r"code[^A-Za-z0-9]{1,40}([A-Za-z0-9]{6,10})\b", body)
            if hit:
                return hit.group(1)
        time.sleep(5)
    return None


# ── the service ──────────────────────────────────────────────────────────────
def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Jarvis internship scout (personal use)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _at(now: datetime, hm: tuple[int, int]) -> bool:
    return now.hour * 60 + now.minute >= hm[0] * 60 + hm[1]


def _hm(s: str) -> tuple[int, int]:
    h, m = (int(x) for x in str(s).split(":"))
    return h, m


def location(job: dict) -> str:
    locs = job.get("locations") or []
    return locs[0] + (f" +{len(locs) - 1}" if len(locs) > 1 else "") if locs else "California"


class InternshipService:
    PASS_SECONDS = 60

    def __init__(self, log: Callable[[str], None], notify: Callable[[str, str], object],
                 state_path: Path = STATE, get: Callable[[str], bytes] = _get, clock: Callable[[], float] = time.time):
        self.log, self._notify, self.state_path, self.get, self.clock = log, notify, state_path, get, clock
        self._stop = threading.Event()
        self._replies_at = 0.0

    def notify(self, text: str) -> None:
        self._notify(NS, text)

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="internships").start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                self.log(f"internships: pass failed: {exc!r}")
            self._stop.wait(self.PASS_SECONDS)

    def run_once(self, now: Optional[datetime] = None) -> None:
        cfg = settings()
        if not cfg["enabled"]:
            return
        now = now or datetime.fromtimestamp(self.clock())
        today = now.date().isoformat()
        st = load_state(self.state_path)
        gaps = profile_gaps()
        prof = {} if gaps == ["the info file"] else profile_fields(PROFILE.read_text())
        if (st.get("scout_day") != today and _at(now, SCOUT_AT)) or not st.get("scouted"):
            new = scout(st, self.get, prof, self.clock())
            st["scout_day"] = today
            save_state(st, self.state_path)
            self.log(f"internships: the Scout found {new} new Summer 2027 postings")
        if gaps:
            if st.get("gaps_texted") != today and _at(now, DIGEST_AT):
                st["gaps_texted"] = today
                save_state(st, self.state_path)
                self.notify("🎓 The internship agents are ready but waiting on your info file: still blank: "
                            + ", ".join(gaps[:6]) + ". It's config/internships/profile.md in the Jarvis folder.")
            return
        self._refresh_needs_info(st)
        if st.get("digest_day") != today and _at(now, DIGEST_AT) and not cfg["practice"]:
            self._digest(st, today)
        if REPLY_HOURS[0] <= now.hour < REPLY_HOURS[1] and self.clock() - self._replies_at >= REPLIES_EVERY:
            self._replies_at = self.clock()
            try:
                from plugins import gmail
                if check_replies(st, gmail._service("personal"), self.notify):
                    save_state(st, self.state_path)
            except Exception as exc:
                self.log(f"internships: reply check failed ({type(exc).__name__})")
            save_state(st, self.state_path)
        self._maybe_apply(st, cfg, now, today)

    def _refresh_needs_info(self, st: dict) -> None:
        """When the user edits the info file, the jobs that were waiting on it get another try."""
        changed = max(p.stat().st_mtime for p in (PROFILE, RESUME, TRANSCRIPT) if p.exists())
        again = [j for j in st["jobs"].values() if j["status"] == "needs_info" and j.get("asked_at", 0) < changed]
        for j in again:
            j["status"] = "new"
        if again:
            save_state(st, self.state_path)

    def _digest(self, st: dict, today: str) -> None:
        st["digest_day"] = today
        todo = sorted((j for j in st["jobs"].values() if j["status"] == "new" and j["ats"] == "manual" and not j.get("texted")),
                      key=lambda j: -j.get("posted", 0))[:MANUAL_PER_DAY]
        for j in todo:
            j["texted"] = self.clock()
        save_state(st, self.state_path)
        if todo:
            self.notify("🔗 Summer 2027 internships to apply to yourself today (their sites can't be done automatically):\n"
                        + "\n".join(f"• {j['company']}: {j['title'][:70]} ({location(j)}) {j['url']}" for j in todo))

    def _maybe_apply(self, st: dict, cfg: dict, now: datetime, today: str) -> None:
        if cfg["practice"] and st.get("practice_done"):
            return
        # `per_day` submitted applications, spread from start to end; a job that needed the user or a hand
        # application doesn't use up a slot, but tries stay 10 minutes apart and capped, so a run of
        # refusals can't hammer anyone
        start, end = _hm(cfg["start"]), _hm(cfg["end"])
        jobs = st["jobs"].values()
        applied = sum(1 for j in jobs if j.get("applied_at") and datetime.fromtimestamp(j["applied_at"]).date().isoformat() == today)
        tries = sum(1 for j in jobs if j.get("tried_day") == today)
        n = cfg["per_day"]
        if not n or applied >= n or tries >= 3 * n or not _at(now, start) or _at(now, end):
            return
        gap = ((end[0] * 60 + end[1]) - (start[0] * 60 + start[1])) / n
        if now.hour * 60 + now.minute < start[0] * 60 + start[1] + applied * gap:
            return
        if self.clock() - st.get("last_try", 0) < 600:
            return
        st["last_try"] = self.clock()
        queue = sorted((j for j in st["jobs"].values() if j["status"] == "new" and j["ats"] == "greenhouse"),
                       key=lambda j: -j.get("posted", 0))
        if queue:
            self.apply_one(st, queue[0], cfg["practice"], today)

    def apply_one(self, st: dict, job: dict, practice: bool, today: str) -> str:
        from core import gemini
        from plugins import gmail
        from worker import intern_apply as ia
        job["tried_day"] = today
        job["attempts"] = job.get("attempts", 0) + 1
        save_state(st, self.state_path)
        job["description"] = job_description(job, self.get)
        try:
            out = ia.apply(job, PROFILE.read_text(), resume_text(), {"resume": RESUME, "transcript": TRANSCRIPT}, BROWSER, SHOTS,
                           ask=lambda prompt: gemini.as_json(prompt, tier=gemini.SMART, timeout_ms=120_000),
                           get_code=lambda since: security_code(gmail._service("personal"), since),
                           practice=practice)
        except ia.NeedsInfo as e:
            job.update(status="needs_info", asked_at=self.clock(), missing=e.questions)
            files = [q for q in e.questions if UPLOAD.search(q)]          # a document to add, not a line to fill in
            asks = [q for q in e.questions if q not in files]
            add_questions(asks)
            lines = []
            if asks:
                lines.append("it asks things your info file doesn't cover (" + "; ".join(q.rstrip(".") for q in asks[:5])
                             + "). They're at the bottom of the file now: fill them in")
            if files:
                lines.append("it needs a file: " + "; ".join(q.rstrip(".") for q in files[:3])
                             + (" (save it as transcript.pdf in the internships folder)" if any("transcript" in q.lower() for q in files) else ""))
            self.notify(f"❓ {job['company']} ({job['title'][:60]}): " + ", and ".join(lines) + ", and I'll apply.")
            return "needs_info"
        except ia.Blocked as e:
            job.update(status="manual", why=str(e))
            self.notify(f"🔗 Couldn't submit {job['company']}: {job['title'][:60]} automatically ({e}). Apply here: {job['url']}")
            return "manual"
        except Exception as e:
            self.log(f"internships: {job['company']} failed: {e!r}")
            if job["attempts"] >= 2:
                job.update(status="manual", why=f"failed twice ({type(e).__name__})")
            else:
                job["status"] = "new"
            return "error"
        finally:
            job.pop("description", None)
            save_state(st, self.state_path)
        if practice:
            st["practice_done"] = self.clock()
            job.update(status="new", practice_shot=out["screenshot"])
            save_state(st, self.state_path)
            self.notify(f"🧪 Practice run done: filled in {job['company']}: {job['title'][:60]} without submitting. "
                        "Say 'internships go live' once it looks right.")
            return "practice"
        job.update(status="applied", applied_at=self.clock(), screenshot=out["screenshot"])
        save_state(st, self.state_path)
        count = sum(1 for j in st["jobs"].values() if j["status"] in ("applied", "replied"))
        self.notify(f"✅ Applied: {job['title'][:70]} at {job['company']} ({location(job)}). That's {count} so far.")
        return "applied"
