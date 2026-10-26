"""The internship Applicant: fills in one Greenhouse or Ashby application in a hidden browser and submits it.

Greenhouse's question API leaves out parts of the real form (country, city, the education block), so the form
is read from the page itself: every labelled field, whether it's required, and each drop-down's own options.
The Profile keeper (`answer`) maps the user's info file and résumé onto those fields with Gemini; it may only
use facts from them, and says UNKNOWN when they don't cover a required question. Nothing is submitted while a
required field is empty, and a practice run stops right before Submit.

The page carries Google's invisible reCAPTCHA. A persistent browser profile and the full Chromium build keep
it as close to a normal visit as possible; when Greenhouse still refuses, the job is handed to the user.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Callable, Optional

GH_URL = re.compile(r"https?://(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/([\w-]+)/jobs/(\d+)")
ASHBY_URL = re.compile(r"https?://jobs\.ashbyhq\.com/([\w.%-]+)/([0-9a-f-]{36})")
MANY = 150                 # a drop-down longer than this (countries, schools) is searched by typing, not listed for Gemini
DONE = re.compile(r"thank you for applying|application (?:has been |was )?(?:successfully )?(?:received|submitted)|we(?:'|’)ve received your application", re.I)
CODE_ASKED = re.compile(r"security code|verification code", re.I)
REFUSED = re.compile(r"flagged|possible spam|recaptcha|are you a robot|unusual traffic", re.I)


class NeedsInfo(Exception):
    """Required questions the info file doesn't answer."""
    def __init__(self, questions: list[str]):
        super().__init__("; ".join(questions))
        self.questions = questions


class Blocked(Exception):
    """The form can't be finished automatically (robot check, unusual form); the user applies by hand."""


EXTRACT_JS = r"""() => {
  const out = [], groups = {};
  const form = document.querySelector('form#application-form, form');
  if (!form) return out;
  const labelOf = e => ((e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label') || '').trim();
  for (const e of form.querySelectorAll('input, textarea')) {
    if (e.type === 'hidden' || e.type === 'submit' || e.type === 'button' || e.type === 'search') continue;
    if (e.closest('.iti__dropdown-content, .iti__country-container')) continue;
    if (e.type === 'checkbox') {
      const name = e.name || e.id;
      const fs = e.closest('fieldset');
      const g = groups[name] || (groups[name] = {id: name, kind: 'checkboxes', options: [], ids: [],
        label: fs && fs.querySelector('legend') ? fs.querySelector('legend').innerText.trim() : labelOf(e),
        required: !!(fs && fs.getAttribute('aria-required') === 'true')});
      g.options.push(labelOf(e)); g.ids.push(e.id);
      continue;
    }
    if (!e.id) continue;
    const up = e.type === 'file' && document.getElementById('upload-label-' + e.id);   // an upload's question sits apart
    const label = up ? up.innerText.trim() : labelOf(e);
    if (!label) continue;
    const kind = e.getAttribute('role') === 'combobox' ? 'select' : e.type === 'file' ? 'file'
               : e.tagName === 'TEXTAREA' ? 'textarea' : e.type === 'tel' ? 'phone' : 'text';
    out.push({id: e.id, label: label.replace(/\*\s*$/, '').trim(), kind,
              required: e.getAttribute('aria-required') === 'true' || e.required || /\*\s*$/.test(label)});
  }
  return out.concat(Object.values(groups));
}"""

FILLED_JS = r"""(f) => {
  if (f.kind === 'checkboxes') return f.ids.some(i => document.getElementById(i) && document.getElementById(i).checked);
  const e = document.getElementById(f.id);
  if (f.kind === 'file') {                // the input is removed once a file is attached; the box shows its name
    if (e && e.files && e.files.length) return true;
    const lab = document.getElementById('upload-label-' + f.id);
    const box = lab ? lab.parentElement : e && e.closest('.file-upload__wrapper');
    return !!(box && /\.(pdf|docx?|txt|rtf)\b/i.test(box.innerText.replace(/accepted file types[^\n]*/i, '')));
  }
  if (!e) return false;
  if (f.kind === 'select') {
    const box = e.closest('[class*="select__control"]');
    return !!(box && (box.querySelector('[class*="value-container--has-value"], [class*="single-value"], [class*="multi-value"]')));
  }
  return !!(e.value && e.value.trim());
}"""


# Ashby: one `.ashby-application-form-field-entry` per question (a fieldset for radio groups), its title in
# `.ashby-application-form-question-title`; ids are UUIDs and the label's `for` names the input
ASHBY_EXTRACT_JS = r"""() => {
  const out = [];
  const all = [...document.querySelectorAll('.ashby-application-form-field-entry, fieldset[class*="_fieldEntry_"]')];
  for (const entry of all.filter(e => !all.some(o => o !== e && o.contains(e)))) {   // radio groups are bare fieldsets
    const t = entry.querySelector('.ashby-application-form-question-title');
    if (!t) continue;
    const label = t.innerText.replace(/\*\s*$/, '').trim();
    const required = /_required_/.test(t.className);
    const path = entry.getAttribute('data-field-path') || t.getAttribute('for') || label;
    const radios = [...entry.querySelectorAll('input[type=radio]')];
    const boxes = [...entry.querySelectorAll('input[type=checkbox]')];
    const lab = e => ((e.labels && e.labels[0] && e.labels[0].innerText) || '').trim();
    if (radios.length) { out.push({id: path, kind: 'radio', label, required, options: radios.map(lab), ids: radios.map(r => r.id)}); continue; }
    if (boxes.length > 1) { out.push({id: path, kind: 'checkboxes', label, required, options: boxes.map(lab), ids: boxes.map(r => r.id)}); continue; }
    if (boxes.length === 1) { out.push({id: path, kind: 'checkboxes', label, required, options: ['Yes'], ids: [boxes[0].id]}); continue; }
    const file = entry.querySelector('input[type=file]');
    if (file) { out.push({id: path, input: file.id, kind: 'file', label, required}); continue; }
    const yn = [...entry.querySelectorAll('button')].filter(b => /^(yes|no)$/i.test(b.innerText.trim()));
    if (yn.length === 2) { out.push({id: path, kind: 'yesno', label, required, options: ['Yes', 'No']}); continue; }
    const e = entry.querySelector('textarea, input:not([type=hidden])');
    if (!e) continue;
    const kind = e.getAttribute('role') === 'combobox' ? 'select' : e.tagName === 'TEXTAREA' ? 'textarea'
               : e.type === 'tel' ? 'phone' : 'text';
    out.push({id: path, input: e.id, kind, label, required: required || e.required});
  }
  return out;
}"""

ASHBY_FILLED_JS = r"""(f) => {
  const entry = document.querySelector('[data-field-path="' + f.id + '"]')
             || [...document.querySelectorAll('.ashby-application-form-field-entry')].find(x =>
                  (x.querySelector('.ashby-application-form-question-title') || {}).getAttribute
                  && x.querySelector('.ashby-application-form-question-title').getAttribute('for') === f.id);
  if (f.kind === 'radio' || f.kind === 'checkboxes') return f.ids.some(i => document.getElementById(i) && document.getElementById(i).checked);
  if (!entry) return false;
  if (f.kind === 'file') return /\.(pdf|docx?|txt|rtf)\b/i.test(entry.innerText);
  if (f.kind === 'yesno') return [...entry.querySelectorAll('button')].some(b => /active|selected/i.test(b.className) || b.getAttribute('aria-pressed') === 'true');
  const e = entry.querySelector('textarea, input:not([type=hidden])');
  return !!(e && e.value && e.value.trim());
}"""


def by_id(i: str) -> str:
    """A selector for an element id; ids like "864" aren't valid after "#"."""
    return '[id="' + i.replace('"', '\\"') + '"]'


def norm(s: str) -> str:
    s = str(s).replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()          # San José -> San Jose
    return " ".join(s.lower().split())


def pick(options: list[str], want: str) -> Optional[str]:
    """The option that `want` names: exact, then starts-with, then contains (either way)."""
    w = norm(want)
    if not w:
        return None
    for test in (lambda o: o == w, lambda o: o.startswith(w), lambda o: w in o, lambda o: o in w):
        hits = [o for o in options if test(norm(o))]
        if hits:
            return min(hits, key=len)
    return None


# ── reading the form ─────────────────────────────────────────────────────────
def _options(page, el) -> list[str]:
    lb = el.get_attribute("aria-controls")
    if not lb:
        return []
    return [o.inner_text().strip() for o in page.query_selector_all(f"{by_id(lb)} [role=option]")]


def read_form(page) -> list[dict]:
    fields = page.evaluate(EXTRACT_JS)
    for f in fields:
        if f["kind"] != "select":
            continue
        el = page.query_selector(by_id(f["id"]))
        try:
            el.click()
            page.wait_for_timeout(400)
            opts = _options(page, el)
        except Exception:
            opts = []
        page.keyboard.press("Escape")
        real = [o for o in opts if o and not re.match(r"no options|type to search|loading", o, re.I)]
        f["searchable"] = not real or len(real) > MANY
        f["options"] = [] if f["searchable"] else real
    return fields


# ── answering (the Profile keeper) ───────────────────────────────────────────
ANSWER_PROMPT = """You fill in a job application for the applicant below, using ONLY the facts in their info file
and résumé. Never invent experience, numbers, dates, links or answers. The applicant agreed to this.

Return JSON: {{"answers": {{"<field id>": <value>}}, "unknown": ["<label of a REQUIRED field you can't answer>"]}}

Rules per field kind:
- text / phone / textarea: a string. Phone: digits only, no country code.
- select with "options": one option copied EXACTLY from its list.
- select with "searchable": true: the text to search for (e.g. "San Jose State University",
  "San Jose, California, United States", "United States").
- checkboxes: a list of options copied exactly; [] if none apply. A lone checkbox (option "Yes"): ["Yes"] to tick it.
- radio: one option copied EXACTLY from its list.
- yesno: "Yes" or "No".
- file: "resume" for a résumé/CV field, "transcript" for a transcript field; leave cover letters and portfolios
  out. A required upload that's neither: list it in "unknown".
- Education block: school, degree (e.g. Bachelor's Degree), discipline (the major), start month/year, end month/year
  (= expected graduation).
- Questions about a degree, GPA or test the applicant doesn't have (graduate GPA, SAT not given, ...): pick the
  "not applicable / did not take / do not recall" option.
- Voluntary self-identification (gender, race, Hispanic/Latino, veteran, disability): use the info file; when it says
  decline or is blank, pick the decline / "don't wish to answer" option.
- "How did you hear about us": the info file's default (pick the closest option).
- A required acknowledgement of a privacy notice or of how the application is processed: acknowledge / agree (the
  applicant agreed to apply). Opt-ins to marketing, SMS or WhatsApp messages, or talent newsletters: no.
- Essay / free-text questions: 60-140 words, first person, plain and specific, built only from the info file and
  résumé, tied to this company and role where the facts allow. No clichés, no claims that aren't in the facts.
- Optional fields you can't answer from the facts: leave them out. Required ones: list their labels in "unknown".

Job: {title} at {company} ({location})
Job description (excerpt): {description}

Info file:
{profile}

Résumé text:
{resume}

Fields (JSON):
{fields}
"""


def answer(fields: list[dict], job: dict, profile: str, resume: str, ask: Callable) -> dict:
    slim = [{k: f[k] for k in ("id", "label", "kind", "required", "options", "searchable") if k in f and f[k] not in (None, [])}
            for f in fields]
    out = ask(ANSWER_PROMPT.format(title=job["title"], company=job["company"], location=", ".join(job.get("locations") or []),
                                   description=(job.get("description") or "")[:3000], profile=profile, resume=resume[:6000],
                                   fields=json.dumps(slim, ensure_ascii=False)))
    if not isinstance(out, dict) or not isinstance(out.get("answers"), dict):
        raise RuntimeError("the Profile keeper didn't answer (Gemini busy?)")
    return out


# ── filling ──────────────────────────────────────────────────────────────────
def searches(value: str) -> list[str]:
    """What to type into a searchable drop-down, most specific first: "San José State University" ->
    the text as given, without accents, then its first words ("San Jose State", "San Jose", "San")."""
    plain = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    words = plain.split(",")[0].split()
    return list(dict.fromkeys(x for x in (value.split(",")[0], plain.split(",")[0], *(" ".join(words[:n]) for n in (3, 2, 1))) if x))


def _select(page, f: dict, value: str) -> bool:
    el = page.query_selector(by_id(f["id"]))
    if not el:
        return False
    want = value if f.get("searchable") else (pick(f["options"], value) or value)
    for typed in searches(want):                    # a long list may show only its first page (schools): search it too
        el.click()
        el.fill("")
        el.type(typed[:40], delay=25)
        opts = []
        for _ in range(8):                         # some lists load from the server as you type
            page.wait_for_timeout(500)
            opts = [o for o in _options(page, el) if not re.match(r"no options|loading", o, re.I)]
            if opts:
                break
        choice = pick(opts, want) or (opts[0] if f.get("searchable") and len(opts) == 1 else None)
        if not choice:
            continue
        lb = el.get_attribute("aria-controls")
        for o in page.query_selector_all(f"{by_id(lb)} [role=option]"):
            if o.inner_text().strip() == choice:
                o.click()
                page.wait_for_timeout(250)
                return True
    page.keyboard.press("Escape")
    return False


def fill(page, fields: list[dict], answers: dict, docs: dict) -> list[str]:
    """Fills what it can; returns the labels of required fields that are still empty. `docs`: {"resume": path, ...}"""
    for f in fields:
        v = answers.get(f["id"])
        if v in (None, "", []) and f["kind"] != "file":
            continue
        try:
            if f["kind"] == "file":
                doc = docs.get(v if isinstance(v, str) else ("resume" if f["id"] == "resume" else ""))
                if doc and Path(doc).exists():
                    page.set_input_files(by_id(f["id"]), str(doc))
                    page.wait_for_timeout(1500)
            elif f["kind"] == "select":
                _select(page, f, str(v))
            elif f["kind"] == "checkboxes":
                for label, cid in zip(f["options"], f["ids"]):
                    if any(norm(label) == norm(x) for x in (v if isinstance(v, list) else [v])):
                        page.check(f"[id='{cid}']")
            elif f["kind"] == "phone":
                page.fill(by_id(f["id"]), re.sub(r"\D", "", str(v))[-10:])
            else:
                page.fill(by_id(f["id"]), str(v))
        except Exception:
            pass                                   # judged by the required-field check below
    return [f["label"] for f in fields if f["required"] and not page.evaluate(FILLED_JS, f)]


def ashby_fill(page, fields: list[dict], answers: dict, docs: dict) -> list[str]:
    for f in fields:
        v = answers.get(f["id"])
        if v in (None, "", []) and f["kind"] != "file":
            continue
        try:
            if f["kind"] == "file":
                doc = docs.get(v if isinstance(v, str) else ("resume" if "resume" in f["id"] else ""))
                if doc and Path(doc).exists():
                    page.set_input_files(by_id(f["input"]), str(doc))
                    page.wait_for_timeout(2500)
            elif f["kind"] in ("radio", "checkboxes"):
                want = v if isinstance(v, list) else [v]
                for label, cid in zip(f["options"], f["ids"]):
                    if pick([label], str(want[0])) if f["kind"] == "radio" else any(norm(label) == norm(x) for x in want):
                        page.click(f'label[for="{cid}"]')
                        if f["kind"] == "radio":
                            break
            elif f["kind"] == "yesno":
                entry = page.locator(f'[data-field-path="{f["id"]}"]')
                entry.locator("button", has_text=re.compile(rf"^{'Yes' if norm(v).startswith('y') else 'No'}$")).first.click()
            elif f["kind"] == "select":
                el = page.locator(by_id(f["input"]))
                el.click()
                el.fill(str(v)[:40])
                page.wait_for_timeout(1500)
                opts = page.locator("[role=option]")
                names = [opts.nth(i).inner_text().strip() for i in range(min(opts.count(), 30))]
                choice = pick(names, str(v)) or (names[0] if names else None)
                if choice:
                    opts.nth(names.index(choice)).click()
            elif f["kind"] == "phone":
                page.fill(by_id(f["input"]), re.sub(r"\D", "", str(v))[-10:])
            else:
                page.fill(by_id(f["input"]), str(v))
        except Exception:
            pass
    return [f["label"] for f in fields if f["required"] and not page.evaluate(ASHBY_FILLED_JS, f)]


# ── submitting ───────────────────────────────────────────────────────────────
def _body(page) -> str:
    try:
        return page.inner_text("body")
    except Exception:
        return ""


def submit(page, get_code: Callable[[float], Optional[str]], timeout_s: float = 45, button: str = "button[type=submit]") -> None:
    started = time.time()
    page.click(button)
    code_sent = False
    while time.time() - started < timeout_s + (120 if code_sent else 0):
        page.wait_for_timeout(1500)
        text = _body(page)
        if "confirmation" in page.url or DONE.search(text):
            return
        if REFUSED.search(text):
            raise Blocked("the site's robot check refused the automatic submit")
        if not code_sent and CODE_ASKED.search(text):
            box = page.query_selector("input[id*=security], input[name*=security], input[id*=code], input[autocomplete=one-time-code]")
            if box:
                code = get_code(started)
                if not code:
                    raise Blocked("the email security code never arrived")
                box.fill(code)
                page.click("button[type=submit]")
                code_sent = True
    errors = [e.inner_text().strip() for e in page.query_selector_all("[class*=error], [role=alert]") if e.inner_text().strip()]
    raise Blocked("no confirmation after Submit" + (f": {errors[0][:120]}" if errors else ""))


def apply(job: dict, profile: str, resume_text: str, docs: dict, browser_dir: Path, shots: Path,
          ask: Callable, get_code: Callable[[float], Optional[str]], practice: bool) -> dict:
    """One application. Returns {"screenshot", "answers"}; raises NeedsInfo or Blocked."""
    from playwright.sync_api import sync_playwright
    shots.mkdir(parents=True, exist_ok=True)
    shot = shots / f"{job['id']}{'-practice' if practice else ''}.png"
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(browser_dir), headless=True, channel="chromium",
                                                   viewport={"width": 1280, "height": 900}, locale="en-US",
                                                   timezone_id="America/Los_Angeles")
        try:
            page = ctx.new_page()
            ashby = ASHBY_URL.match(job["url"])
            page.goto(f"https://jobs.ashbyhq.com/{ashby.group(1)}/{ashby.group(2)}/application" if ashby else job["url"],
                      wait_until="networkidle", timeout=60000)
            button = ".ashby-application-form-submit-button" if ashby else "button[type=submit]"
            if not page.query_selector(button):
                raise Blocked("the posting has no application form (closed or moved)")
            fields = page.evaluate(ASHBY_EXTRACT_JS) if ashby else read_form(page)
            out = answer(fields, job, profile + ("\n(A transcript PDF is on file.)" if Path(docs.get("transcript", "")).exists()
                                                 else "\n(No transcript PDF on file.)"), resume_text, ask)
            known = {f["label"] for f in fields if f["required"]}
            unknown = [u for u in out.get("unknown") or [] if u in known]
            if unknown:
                raise NeedsInfo(unknown)
            missing = (ashby_fill if ashby else fill)(page, fields, out["answers"], docs)
            page.screenshot(path=str(shot), full_page=True)
            if missing:
                raise NeedsInfo(missing)
            if not practice:
                submit(page, get_code, button=button)
                page.screenshot(path=str(shot.with_name(shot.stem + "-done.png")), full_page=True)
            return {"screenshot": str(shot), "answers": out["answers"]}
        finally:
            ctx.close()
