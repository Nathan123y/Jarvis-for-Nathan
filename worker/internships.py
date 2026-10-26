"""The internship crew, run by the background worker: Scout -> Profile keeper -> Applicant -> Inbox watcher.

  Scout          once a day: the Summer 2027 internship list that SimplifyJobs keeps on GitHub, narrowed to
                 hardware / electrical / semiconductor roles in California open to bachelor's students
  Profile keeper the user's info file (config/internships/profile.md) and résumé (resume.pdf), and nothing else;
                 a required question they don't cover is added to the bottom of the info file for the user
  Applicant      up to `per_day` Greenhouse applications a day, spread from `start` to `end`
                 (worker/intern_apply.py); other sites (Workday, Tesla, ...) can't be done automatically, so a few
                 of those are texted each morning for the user to apply to by hand
  Inbox watcher  replies from those companies in the personal Gmail: interview, coding test, rejection, received
  Director list  the SJSU engineering co-op / internship Google Sheet the director keeps, and his emails about it
                 (school Gmail): every listing marked for EE is looked up on the company's own careers site
                 (Greenhouse or Ashby) and applied to right away, ahead of the daily pace and without the practice
                 stop (the user asked for these); the rest are texted with their Handshake link to apply by hand

`practice` (on until the user turns it off) fills one application and stops before Submit, for a look first.
LinkedIn and Handshake are never automated: their terms forbid it (the user chose this on 2026-10-25). Every text goes to the user's phone.

State: memory/internships.json; screenshots of every filled form: config/internships/applications/.
"""
from __future__ import annotations

import hashlib
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
WD_BROWSER = DIR / "workday-browser"   # its own profile: Workday sign-ins stay apart from Greenhouse
STATE = ROOT / "memory" / "internships.json"
SHEETS_TOKEN = DIR / "sheets_token.json"  # the school Google account, read-only Sheets access
SHEET_ID = "1zTUWkmcpoMHfp_m_XNFydN5R5OO51g5eTs_3wvFd6Rg"   # "Spring / Summer / Fall 2027 - Current Co-ops on Handshake"
SHEET_TAB = "Spring 27- Summer 27"
MAJOR = "EE"                          # the sheet's column of majors each employer recruits
DIRECTOR_QUERY = ("from:(universitycomm@sjsu.edu OR electrical.engineering@sjsu.edu) newer_than:3d "
                  "(internship OR internships OR co-op OR coop OR handshake)")
NS = "internships"
FEED = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"
TERM = "Summer 2027"
DEFAULTS = {"enabled": True, "per_day": 5, "start": "06:00", "end": "21:00", "practice": True}
SCOUT_AT = (5, 45)                   # before the first application of the day
DIGEST_AT = (8, 0)                   # the morning "apply to these yourself" text
MANUAL_PER_DAY = 3
AUTO = ("greenhouse", "ashby", "workday")   # application sites the Applicant can fill in
REPLIES_EVERY = 600
UPLOAD = re.compile(r"upload|attach|transcript|cover letter|portfolio|writing sample|\bcv\b", re.I)
REPLY_HOURS = (7, 23)                # reply texts wait for the morning instead of buzzing at night
NONE_KEYS = (("linkedin", "linkedin url"), ("github", "github url"), ("website", "personal website / portfolio url"), ("portfolio", "personal website / portfolio url"))  # form label word -> info-file line

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


def info_sig() -> str:
    """What the user has given so far: the info file's text and the résumé and transcript files."""
    parts = [PROFILE.read_text() if PROFILE.exists() else ""]
    parts += [f"{p.stat().st_size}:{p.stat().st_mtime}" if p.exists() else "-" for p in (RESUME, TRANSCRIPT)]
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()


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
    from worker.intern_apply import ASHBY_URL, GH_URL
    from worker.intern_workday import WD_URL
    return ("greenhouse" if GH_URL.match(url or "") else "ashby" if ASHBY_URL.match(url or "")
            else "workday" if WD_URL.match(url or "") else "manual")


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
    from worker.intern_apply import ASHBY_URL, GH_URL
    try:
        if m := GH_URL.match(job["url"]):
            html = json.loads(get(f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}")).get("content")
        elif m := ASHBY_URL.match(job["url"]):
            jobs = json.loads(get(f"https://api.ashbyhq.com/posting-api/job-board/{m.group(1)}")).get("jobs") or []
            html = next((j.get("descriptionHtml") for j in jobs if j.get("id") == m.group(2)), "")
        elif m := re.match(r"(https://([\w-]+)\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Za-z]{2}/)?([^/]+)/job/(.+)$", job["url"]):
            html = json.loads(get(f"{m[1]}/wday/cxs/{m[2]}/{m[3]}/job/{m[4]}")).get("jobPostingInfo", {}).get("jobDescription")
        else:
            return ""
    except Exception:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(unescape(html or "")))).strip()


# ── the director's list ──────────────────────────────────────────────────────
HS_JOB = re.compile(r"joinhandshake\.com/(?:job-search|jobs|stu/jobs)/(\d+)")
DEADLINE = re.compile(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s*(20\d\d)")
_NOISE = re.compile(r"\(#?[\w-]*\d[\w-]*\)|\b20\d\d\b|\b(?:summer|spring|fall|winter|intern(?:ship)?s?|co-?op|full[- ]time|start|usa?)\b", re.I)


def words(title: str) -> set:
    return set(re.findall(r"[a-z0-9]+", _NOISE.sub(" ", title.lower()))) - {"and", "the", "of", "for", "a"}


def same_role(want: set, got: set) -> bool:
    """Mostly the same words, or the board's shorter title inside the sheet's ("RTL Intern" / "RTL SoC Design Intern")."""
    return bool(got) and (len(want & got) / len(want | got) >= 0.6 or got <= want)


def sheet_listings(rows: list[list]) -> list[dict]:
    """The sheet's rows -> listings; each cell is (text, link). Header row: majors; a row has an X under each."""
    head = next((i for i, r in enumerate(rows) if any(c[0].strip() == MAJOR for c in r)), None)
    if head is None:
        return []
    col = [c[0].strip() for c in rows[head]].index(MAJOR)
    out = []
    for r in rows[head + 1:]:
        link = next((c[1] for c in r if c[1] and HS_JOB.search(c[1])), None)
        if not link or len(r) < 5:
            continue
        due = DEADLINE.search(r[1][0])
        out.append({"hs": HS_JOB.search(link).group(1), "company": r[2][0].strip(), "title": " ".join(r[4][0].split()),
                    "term": r[5][0].strip() if len(r) > 5 else "", "location": r[7][0].strip() if len(r) > 7 else "",
                    "deadline": datetime.strptime(" ".join(due.groups()), "%B %d %Y").date().isoformat() if due else "",
                    "ee": len(r) > col and r[col][0].strip().upper() == "X"})
    return out


def read_sheet() -> list[list]:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    svc = build("sheets", "v4", credentials=Credentials.from_authorized_user_file(str(SHEETS_TOKEN)), cache_discovery=False)
    got = svc.spreadsheets().get(spreadsheetId=SHEET_ID, ranges=[f"'{SHEET_TAB}'"], includeGridData=True,
                                 fields="sheets.data.rowData.values(formattedValue,hyperlink)").execute()
    return [[(v.get("formattedValue") or "", v.get("hyperlink")) for v in row.get("values", [])]
            for row in got["sheets"][0]["data"][0].get("rowData", [])]


def slugs(company: str) -> list[str]:
    base = re.sub(r"\b(inc|llc|corp|corporation|co|companies|company|technologies|technology|systems|ltd)\b\.?", "",
                  company.lower().split("(")[0])
    parts = re.findall(r"[a-z0-9]+", base)
    return list(dict.fromkeys(x for x in ("".join(parts), "-".join(parts), parts[0] if parts else "") if x))


def company_posting(company: str, title: str, get: Callable[[str], bytes]) -> Optional[tuple[str, str]]:
    """The same opening on the company's own Greenhouse or Ashby board: (url, its title), or None."""
    want = words(title)
    if not want:
        return None
    for slug in slugs(company):
        boards = ((f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", "jobs", "absolute_url"),
                  (f"https://api.ashbyhq.com/posting-api/job-board/{slug}", "jobs", "jobUrl"))
        for api, key, url_key in boards:
            try:
                jobs = json.loads(get(api)).get(key) or []
            except Exception:
                continue
            ref = re.search(r"#(\d{6,})", title)              # "(#4321158007) ...": the posting's own id
            hits = [j for j in jobs if same_role(want, words(j.get("title", "")))]
            hits.sort(key=lambda j: not (ref and ref.group(1) in str(j.get(url_key))))
            if hits:
                return hits[0][url_key], hits[0]["title"]
    return None


def director_sync(st: dict, rows: list[list], get: Callable[[str], bytes], today: str, now: float) -> tuple[list, list]:
    """Adds the sheet's new EE listings. Returns (found on a company site, Handshake-only) among the new ones."""
    auto, manual = [], []
    for it in sheet_listings(rows):
        key = "hs-" + re.sub(r"[^a-z0-9]+", "-", f"{it['company']} {it['title']}".lower()).strip("-")
        if not it["ee"] or key in st["jobs"]:
            continue
        hit = company_posting(it["company"], it["title"], get)
        if not hit and it["deadline"] and it["deadline"] < today:
            continue                                 # closed on Handshake and nowhere else to apply
        job = {"id": key, "company": it["company"], "title": it["title"], "locations": [it["location"]] if it["location"] else [],
               "handshake": f"https://app.joinhandshake.com/jobs/{it['hs']}", "deadline": it["deadline"], "term": it["term"],
               "source": "director", "found": now, "posted": now}
        if hit:
            job.update(url=hit[0], ats=ats_of(hit[0]), status="new", priority=True)
            auto.append(job)
        else:
            job.update(url=job["handshake"], ats="handshake", status="manual", why="Handshake only")
            manual.append(job)
        st["jobs"][key] = job
    return auto, manual


def director_text(auto: list, manual: list) -> str:
    lines = [f"📌 Your director's internship/co-op list has {len(auto) + len(manual)} new listing(s) for EE."]
    if auto:
        lines.append("Applying now on the company's own site: " + "; ".join(f"{j['company']} ({j['title'][:50]})" for j in auto) + ".")
    if manual:
        lines.append("Apply to these on Handshake yourself (soonest deadline first):")
        lines += [f"• {j['company']}: {j['title'][:60]}" + (f" (by {datetime.fromisoformat(j['deadline']):%b %-d})" if j["deadline"] else "") + f" {j['handshake']}"
                  for j in sorted(manual, key=lambda j: j["deadline"] or "9999")]
    return "\n".join(lines)


def director_emails(st: dict, service) -> list[str]:
    """Subjects of the director's new internship emails in the school Gmail (the first look only takes note)."""
    api = service.users().messages()
    ids = [m["id"] for m in api.list(userId="me", q=DIRECTOR_QUERY, maxResults=20).execute().get("messages", [])]
    first = "director_seen" not in st
    seen = set(st.get("director_seen") or [])
    new = [i for i in ids if i not in seen]
    st["director_seen"] = (list(seen) + new)[-200:]
    if first:
        return []
    out = []
    for i in new:
        msg = api.get(userId="me", id=i, format="metadata", metadataHeaders=["Subject"]).execute()
        out.append(next((h["value"] for h in msg["payload"]["headers"] if h["name"].lower() == "subject"), "(no subject)"))
    return out


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
            # "...the security code field on your application: XJwGkSYb After you enter the code, resubmit..."
            hit = (re.search(r"application:\s*([A-Za-z0-9]{6,12})\b", body)
                   or re.search(r"code(?: is)?:\s*([A-Za-z0-9]{6,12})\b", body))
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
        self._director_at = 0.0

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
        if SHEETS_TOKEN.exists():
            self._director(st, now, today)
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

    def _director(self, st: dict, now: datetime, today: str) -> None:
        """The director's sheet: re-read each morning and whenever he emails about internships."""
        emailed = []
        if REPLY_HOURS[0] <= now.hour < REPLY_HOURS[1] and self.clock() - self._director_at >= REPLIES_EVERY:
            self._director_at = self.clock()
            try:
                from plugins import gmail
                emailed = director_emails(st, gmail._service("school"))
                save_state(st, self.state_path)
            except Exception as exc:
                self.log(f"internships: director email check failed ({type(exc).__name__})")
        due = st.get("director_day") != today and (_at(now, DIGEST_AT) or not st.get("director_day"))
        if not emailed and not due:
            return
        try:
            auto, manual = director_sync(st, read_sheet(), self.get, today, self.clock())
        except Exception as exc:
            self.log(f"internships: director sheet failed: {exc!r}")
            return
        st["director_day"] = today
        save_state(st, self.state_path)
        self.log(f"internships: director's sheet: {len(auto)} on company sites, {len(manual)} Handshake-only")
        if auto or manual:
            self.notify((f"📧 Your director emailed: \"{emailed[0][:80]}\"\n" if emailed else "") + director_text(auto, manual))

    def _refresh_needs_info(self, st: dict) -> None:
        """When the user edits the info file (or adds the résumé or transcript), the jobs that were waiting on it get
        another try. Questions the crew adds to the file itself don't count, or the waiting jobs would set each
        other off all day."""
        sig = info_sig()
        if st.get("info_sig") == sig:
            return
        first = "info_sig" not in st
        st["info_sig"] = sig
        for j in st["jobs"].values():
            if j["status"] == "needs_info" and not first:
                j["status"] = "new"
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
        start, end = _hm(cfg["start"]), _hm(cfg["end"])
        # the director's listings go first, at once (10 minutes apart), live, and outside the daily count
        first = sorted((j for j in st["jobs"].values() if j["status"] == "new" and j.get("priority") and j["ats"] in AUTO),
                       key=lambda j: j.get("deadline") or "9999")
        if first and _at(now, start) and not _at(now, end) and self.clock() - st.get("last_try", 0) >= 600:
            st["last_try"] = self.clock()
            self.apply_one(st, first[0], False, today)
            return
        if cfg["practice"] and st.get("practice_done"):
            return
        # `per_day` submitted applications, spread from start to end; a job that needed the user or a hand
        # application doesn't use up a slot, but tries stay 10 minutes apart and capped, so a run of
        # refusals can't hammer anyone
        jobs = [j for j in st["jobs"].values() if not j.get("priority")]
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
        queue = sorted((j for j in st["jobs"].values() if j["status"] == "new" and j["ats"] in AUTO),
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
        ask = lambda prompt: gemini.as_json(prompt, tier=gemini.SMART, timeout_ms=120_000)
        docs = {"resume": RESUME, "transcript": TRANSCRIPT}
        try:
            if job["ats"] == "workday":
                from worker import intern_workday
                profile = PROFILE.read_text()
                out = intern_workday.apply(job, profile, resume_text(), docs, WD_BROWSER, SHOTS, ask,
                                           mail=gmail._service("personal"), email=profile_fields(profile).get("email", ""),
                                           practice=practice, log=self.log)
            else:
                out = ia.apply(job, PROFILE.read_text(), resume_text(), docs, BROWSER, SHOTS, ask=ask,
                               get_code=lambda since: security_code(gmail._service("personal"), since),
                               practice=practice)
        except ia.NeedsInfo as e:
            files = [q for q in e.questions if UPLOAD.search(q)]          # a document to add, not a line to fill in
            asks = [q for q in e.questions if q not in files]
            had = profile_fields(PROFILE.read_text())
            if asks and not files and all(had.get(" ".join(q.split()).rstrip(":?*. ").lower(), "").lower() == "none"
                                          or any(w in q.lower() and had.get(k, "").lower() == "none" for w, k in NONE_KEYS)
                                          for q in asks):
                # it requires something the user said they don't have (a LinkedIn profile): skip it quietly
                job.update(status="closed", why="requires " + "; ".join(asks))
                self.log(f"internships: skipped {job['company']} ({job['title'][:60]}), it requires " + "; ".join(asks))
                return "skipped"
            add_questions(asks)
            st["info_sig"] = info_sig()                                  # the crew's own edit, not the user's
            # stamped after the file is written, so adding the question doesn't count as the user answering it
            job.update(status="needs_info", asked_at=self.clock(), missing=e.questions)
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
        self.notify(f"✅ Applied: {job['title'][:70]} at {job['company']} ({location(job)})"
                    + (" from your director's list" if job.get("source") == "director" else "") + f". That's {count} so far.")
        return "applied"
