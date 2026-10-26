"""The internship Applicant for Workday sites (<company>.wd<N>.myworkdayjobs.com): most big companies use them.

Each company's Workday is its own site with its own candidate account, so the first application to a company
creates one (the user's email and a strong password kept in the Mac's Keychain, never in a file), confirms the
"Verify your candidate account" email from the personal Gmail, and signs in. Every later application there
signs in with the same account.

The application is a run of pages (My Information, My Experience, Application Questions, Voluntary Disclosures,
Self Identify, Review). Every Workday site builds them from the same parts, marked with data-automation-id, so each
page is read the same way: its fields, whether they're required, and each drop-down's options. The Profile keeper
from intern_apply (Gemini, info file and résumé only) answers them, the page is filled, and Save and Continue
moves on. A required field it can't answer raises NeedsInfo; anything it can't get past raises Blocked and the job
goes to the user.
"""
from __future__ import annotations

import base64
import json
import re
import secrets
import string
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from worker.intern_apply import Blocked, NeedsInfo, _line, answer, cover_letter, letter_pdf, pick

WD_URL = re.compile(r"https?://([\w-]+)\.wd\d+\.myworkdayjobs\.com/")
KEYCHAIN = "Jarvis Workday {tenant}"
MANY = 60                      # a drop-down longer than this is searched by typing, not listed for Gemini
DONE = re.compile(r"application (?:has been |was )?(?:successfully )?submitted|thank you for applying|"
                  r"we(?:'|’)ve received your application|you(?:'|’)ve already applied|already applied", re.I)

FIELDS_JS = r"""() => {
  const out = [];
  const seen = new Set();
  const conts = [...document.querySelectorAll('[data-automation-id^="formField-"]')]
    .filter(c => !c.parentElement.closest('[data-automation-id^="formField-"]'));
  conts.forEach((c, i) => {
    if (!(c.offsetWidth || c.offsetHeight)) return;
    c.setAttribute('data-jarvis', String(i));
    const key = c.getAttribute('data-automation-id').slice(10);
    const lab = c.querySelector('label, legend');
    let label = lab ? lab.innerText.trim() : (c.getAttribute('aria-label') || '');
    const required = /\*\s*$/.test(label) || !!c.querySelector('[aria-required="true"], [required]');
    label = label.replace(/\*\s*$/, '').trim();
    if (!label && key) label = key;
    const panel = c.closest('[role="group"]');
    const head = panel && panel.querySelector('h3, h4, [data-automation-id$="panelHeader"]');
    const section = head ? head.innerText.trim().split('\n')[0] : '';
    let kind = 'text', value = '', options = [];
    if (c.querySelector('input[type=file]')) kind = 'file';
    else if (c.querySelector('button[aria-haspopup="listbox"]')) {
      kind = 'select'; value = c.querySelector('button[aria-haspopup="listbox"]').innerText.trim();
      if (/^select one$/i.test(value)) value = '';
    }
    else if (c.querySelector('[data-automation-id="multiselectInputContainer"]')) {
      kind = 'prompt';
      value = [...c.querySelectorAll('[data-automation-id="selectedItem"]')].map(e => e.innerText.trim()).join('; ');
    }
    else if (c.querySelector('input[type=radio]')) {
      kind = 'radio';
      options = [...c.querySelectorAll('input[type=radio]')].map(r => (r.labels && r.labels[0] ? r.labels[0].innerText : r.value).trim());
      const ch = c.querySelector('input[type=radio]:checked');
      value = ch && ch.labels && ch.labels[0] ? ch.labels[0].innerText.trim() : '';
    }
    else if (c.querySelector('[data-automation-id^="dateSection"]')) {
      kind = 'date';
      value = [...c.querySelectorAll('input[data-automation-id^="dateSection"]')].map(e => e.value).filter(Boolean).join('/');
    }
    else if (c.querySelector('input[type=checkbox]')) {
      const boxes = [...c.querySelectorAll('input[type=checkbox]')];
      kind = boxes.length > 1 ? 'checkboxes' : 'checkbox';
      options = boxes.map(r => (r.labels && r.labels[0] ? r.labels[0].innerText : '').trim());
      value = boxes.filter(b => b.checked).map(b => (b.labels && b.labels[0] ? b.labels[0].innerText : 'checked').trim()).join('; ');
    }
    else if (c.querySelector('textarea')) { kind = 'textarea'; value = c.querySelector('textarea').value; }
    else { const inp = c.querySelector('input'); if (!inp) return; value = inp.value; if (inp.type === 'password') return; }
    out.push({id: String(i), key, label, required, kind, value, options, section});
  });
  return out;
}"""

ERRORS_JS = r"""() => [...document.querySelectorAll('[data-automation-id="errorMessage"], [data-automation-id="inputError"], [role="alert"]')]
  .map(e => e.innerText.trim()).filter(Boolean)"""


# ── the account ──────────────────────────────────────────────────────────────
def tenant_of(url: str) -> str:
    m = WD_URL.match(url)
    if not m:
        raise Blocked("not a Workday link")
    return m.group(1)


def password_for(tenant: str, email: str, create: bool = True) -> Optional[str]:
    svc = KEYCHAIN.format(tenant=tenant)
    r = subprocess.run(["security", "find-generic-password", "-s", svc, "-a", email, "-w"], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    if not create:
        return None
    # Workday rules: 8+ characters, upper and lower case, a number and a special character
    pw = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(14)) + "!aA7"
    subprocess.run(["security", "add-generic-password", "-s", svc, "-a", email, "-w", pw, "-U"], check=True)
    return pw


def _mail_text(msg: dict) -> str:
    parts, out = [msg.get("payload", {})], ""
    while parts:
        p = parts.pop()
        parts += p.get("parts", [])
        data = p.get("body", {}).get("data")
        if data:
            out += base64.urlsafe_b64decode(data).decode("utf-8", "ignore")
    return out


def verify_link(service, tenant: str, since: float, tries: int = 24) -> Optional[str]:
    """The link in the "Verify your candidate account" email Workday sends when an account is created."""
    api = service.users().messages()
    for _ in range(tries):
        for m in api.list(userId="me", q=f"newer_than:1d from:otp.workday.com {tenant}", maxResults=5).execute().get("messages", []):
            msg = api.get(userId="me", id=m["id"], format="full").execute()
            if int(msg.get("internalDate", 0)) / 1000 < since - 60:
                continue
            hit = re.search(r'https://[^\s"<>]+myworkdayjobs\.com[^\s"<>]*', _mail_text(msg))
            if hit:
                return hit.group(0).replace("&amp;", "&")
        time.sleep(5)
    return None


def _tap(page, sel: str, timeout: float = 3000) -> bool:
    loc = page.locator(sel).first
    try:
        loc.wait_for(state="visible", timeout=timeout)
        loc.click(force=True)
        return True
    except Exception:
        return False


def _settle(page, ms: int = 2500) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    page.wait_for_timeout(ms)


def sign_in(page, url: str, email: str, mail, log: Callable = print) -> None:
    """Opens the application and signs in, creating and verifying the account the first time."""
    tenant = tenant_of(url)
    page.goto(url.rstrip("/") + "/apply/applyManually", wait_until="domcontentloaded", timeout=60000)
    _settle(page)
    _tap(page, "[data-automation-id=legalNoticeAcceptButton]", 1500)
    if page.locator("[data-automation-id=applyFlowPage] [data-automation-id^=formField-]").count() and \
            not page.locator("[data-automation-id=password]").count():
        return                                                     # still signed in
    pw = password_for(tenant, email, create=False)
    if pw:
        _sign_in_form(page, email, pw)
        if page.locator("[data-automation-id=password]").count():
            raise Blocked(f"couldn't sign in to {tenant}'s careers site with the saved password")
        return
    # first application to this company: create the account
    pw = password_for(tenant, email)
    if not page.locator("[data-automation-id=verifyPassword]").count():
        _tap(page, "[data-automation-id=createAccountLink]")
        page.wait_for_timeout(1500)
    started = time.time()
    page.fill("[data-automation-id=email]", email)
    page.fill("[data-automation-id=password]", pw)
    page.fill("[data-automation-id=verifyPassword]", pw)
    box = page.locator("[data-automation-id=createAccountCheckbox]")
    if box.count() and not box.is_checked():
        box.check(force=True)
    page.click("[data-automation-id=createAccountSubmitButton]", force=True)
    _settle(page, 5000)
    errs = [e for e in page.evaluate(ERRORS_JS) if e]
    if any(re.search(r"already|exists", e, re.I) for e in errs):
        raise Blocked(f"an account with your email already exists on {tenant}'s site (made by hand?); its password isn't saved")
    if page.locator("[data-automation-id=verifyPassword]").count():
        raise Blocked(f"{tenant}'s site didn't accept the new account" + (f": {errs[0][:100]}" if errs else ""))
    log(f"internships: created a {tenant} Workday account")
    if page.locator("[data-automation-id=applyFlowPage] [data-automation-id^=formField-]").count() and \
            not page.locator("[data-automation-id=password]").count():
        return                                                     # no email check on this site
    link = verify_link(mail, tenant, started)
    if not link:
        raise Blocked(f"{tenant}'s account email never arrived")
    page.goto(link, wait_until="domcontentloaded", timeout=60000)
    _settle(page)
    page.goto(url.rstrip("/") + "/apply/applyManually", wait_until="domcontentloaded", timeout=60000)
    _settle(page)
    _sign_in_form(page, email, pw)


def _sign_in_form(page, email: str, pw: str) -> None:
    if not page.locator("[data-automation-id=password]").count() or page.locator("[data-automation-id=verifyPassword]").count():
        _tap(page, "[data-automation-id=signInLink]")
        page.wait_for_timeout(1500)
    if not page.locator("[data-automation-id=password]").count():
        return
    page.fill("[data-automation-id=email]", email)
    page.fill("[data-automation-id=password]", pw)
    page.click("[data-automation-id=signInSubmitButton]", force=True)
    try:
        page.locator("[data-automation-id=password]").wait_for(state="detached", timeout=20000)
    except Exception:
        pass
    _settle(page, 3000)
    _reopen_if_broken(page)


def _reopen_if_broken(page) -> None:
    """Workday sometimes shows "Something went wrong, please refresh" right after signing in."""
    for _ in range(3):
        if not re.search(r"something went wrong", page.inner_text("body"), re.I):
            return
        page.reload(wait_until="domcontentloaded")
        _settle(page, 3000)


# ── reading a page ───────────────────────────────────────────────────────────
def _box(page, f: dict):
    return page.locator(f'[data-jarvis="{f["id"]}"]')


def _list_options(page) -> list[str]:
    opts = page.locator('[role="listbox"] [role="option"], [data-automation-id="promptOption"]')
    return [t.strip() for t in opts.all_inner_texts() if t.strip() and not re.match(r"no items|select one$", t.strip(), re.I)]


def read_page(page) -> list[dict]:
    fields = page.evaluate(FIELDS_JS)
    for f in fields:
        if f["kind"] not in ("select", "prompt") or f["value"]:
            continue
        box = _box(page, f)
        try:
            if f["kind"] == "select":
                box.locator('button[aria-haspopup="listbox"]').click()
            else:
                box.locator('[data-automation-id="multiselectInputContainer"]').click()
            page.wait_for_timeout(800)
            opts = _list_options(page)
        except Exception:
            opts = []
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        f["searchable"] = f["kind"] == "prompt" and (not opts or len(opts) > MANY)
        f["options"] = [] if f["searchable"] or len(opts) > MANY else opts
        if f["kind"] == "select" and len(opts) > MANY:
            f["searchable"] = True
    return fields


# ── filling a page ───────────────────────────────────────────────────────────
def _choose(page, want: str) -> bool:
    page.wait_for_timeout(700)
    opts = page.locator('[role="listbox"] [role="option"], [data-automation-id="promptOption"]')
    texts = [t.strip() for t in opts.all_inner_texts()]
    hit = pick([t for t in texts if t], want)
    if hit is None:
        return False
    opts.nth(texts.index(hit)).click(force=True)
    page.wait_for_timeout(600)
    return True


def _clean(value: str, label: str) -> str:
    """'Last name: Yousif' -> 'Yousif': an answer copied with its info-file label in front."""
    m = re.match(r"\s*([A-Za-z][\w /()'-]{0,40}):\s*(.+)$", value, re.S)
    if m and set(re.findall(r"[a-z]{3,}", m[1].lower())) & set(re.findall(r"[a-z]{3,}", label.lower())):
        return m[2].strip()
    return value


def _fill_one(page, f: dict, value, docs: dict) -> bool:
    box = _box(page, f)
    kind = f["kind"]
    if kind == "file":
        path = docs.get(str(value))
        if not path:
            return False
        for _ in range(box.locator('[data-automation-id="delete-file"]').count()):
            box.locator('[data-automation-id="delete-file"]').first.click(force=True)
            page.wait_for_timeout(800)
        inp = box.locator("input[type=file]").first
        files = [str(path)]
        # one shared upload ("Resume, Cover Letter and other documents") takes the cover letter alongside
        if value == "resume" and docs.get("cover_letter") and inp.get_attribute("multiple") is not None:
            files.append(str(docs["cover_letter"]))
        inp.set_input_files(files)
        page.wait_for_timeout(3000)
        return True
    if kind in ("text", "textarea"):
        el = box.locator("textarea" if kind == "textarea" else "input").first
        el.fill(_clean(str(value), f["label"]))
        el.press("Tab")
        return True
    if kind == "select":
        box.locator('button[aria-haspopup="listbox"]').click()
        if _choose(page, str(value)):
            return True
        if f.get("searchable"):
            page.keyboard.type(str(value)[:12], delay=60)             # a long list jumps to what's typed
            if _choose(page, str(value)):
                return True
        page.keyboard.press("Escape")
        return False
    if kind == "prompt":
        values = value if isinstance(value, list) else [value]
        done = False
        for v in values:
            inp = box.locator('[data-automation-id="multiselectInputContainer"] input, input').first
            inp.click()
            inp.fill(str(v))
            inp.press("Enter")
            page.wait_for_timeout(1500)
            if not _choose(page, str(v)):
                # the search's own top hit when it shares a word ("Electrical Engineering" -> "Electrical and
                # Electronics Engineering"); nothing when it doesn't
                first = page.locator('[data-automation-id="promptOption"]').first
                words = set(re.findall(r"[a-z]{4,}", str(v).lower()))
                if first.count() and words & set(re.findall(r"[a-z]{4,}", first.inner_text().lower())):
                    first.click(force=True)
                    page.wait_for_timeout(600)
                    done = True
            else:
                done = True
                # a category ("Job Board") may open a second list ("Indeed"): take the first leaf that fits
                page.wait_for_timeout(500)
                if page.locator('[data-automation-id="promptOption"]').count() and not \
                        box.locator('[data-automation-id="selectedItem"]').count():
                    _choose(page, str(v)) or page.locator('[data-automation-id="promptOption"]').first.click(force=True)
            page.keyboard.press("Escape")
        return done
    if kind == "radio":
        labels = box.locator("label")
        texts = [t.strip() for t in labels.all_inner_texts()]
        hit = pick(texts, str(value))
        if hit is None:
            return False
        labels.nth(texts.index(hit)).click(force=True)
        return True
    if kind in ("checkbox", "checkboxes"):
        want = value if isinstance(value, list) else ([f["options"][0] if f["options"] else "yes"] if value in (True, "Yes", "yes", "checked") else [])
        boxes = box.locator("input[type=checkbox]")
        for n in range(boxes.count()):
            label = f["options"][n] if n < len(f["options"]) else ""
            on = kind == "checkbox" and bool(want) or any(pick([label], w) for w in want)
            if on != boxes.nth(n).is_checked():
                boxes.nth(n).click(force=True)
        return True
    if kind == "date":
        m = re.match(r"\s*(\d{1,2})\s*/\s*(\d{4})\s*$", str(value))                     # MM/YYYY
        d = re.match(r"\s*(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{4})\s*$", str(value))     # MM/DD/YYYY
        # the inputs sit under a display layer: click the first part's display, then type (it moves along by itself)
        box.locator('[data-automation-id$="-display"][data-automation-id^="dateSection"]').first.click(force=True)
        if d:
            page.keyboard.type(f"{int(d[1]):02d}{int(d[2]):02d}{d[3]}", delay=60)
        elif m:
            page.keyboard.type(f"{int(m[1]):02d}{m[2]}", delay=60)
        else:
            return False
        page.keyboard.press("Tab")
        return True
    return False


WD_RULES = """
Workday notes:
- "value" is what the field holds now. Leave a field out when its value is already right; give a new value when
  it's empty or wrong.
- kind "prompt" is a searchable list: give the text to search for (e.g. "San Jose State University",
  "Electrical Engineering", "Company Website"), or one of its "options" exactly when listed.
- kind "date": "MM/YYYY" (or "MM/DD/YYYY" when the label asks for a day, e.g. today's date on a signature). When the
  résumé gives only a year (e.g. "2022-Present"), use January of that year (01/2022); never call a date unknown
  when its year is known.
- Today's date is {today}. A signature / "Name" field on a disability or consent form: the applicant's full name.
- Name fields: only the name itself ("Yousif"), never the info-file label in front of it. Prefix, suffix and middle
  name: leave out unless the info file gives them.
- Phone device type: Mobile. Country phone code: United States of America (+1).
- "Have you previously worked for <company>": No, unless the info file lists that company.
- Terms and conditions / "I have read and consent" checkboxes: tick them (the applicant agreed to apply).
- Work experience fields (job title, company, dates, description): the résumé's jobs, most recent first.
- Education fields: the school, degree, field of study and dates from the info file. Degree: the option that matches
  the info file's degree exactly (Bachelor of Science -> "Bachelor of Science (B.S.)" or "BS", never Bachelor of
  Arts); copy the option text.
- Field of study: the major (e.g. "Electrical Engineering").
"""


def fill_page(page, fields: list[dict], job: dict, profile: str, resume: str, ask: Callable, docs: dict) -> list[str]:
    """Answers and fills one page; returns the labels of required fields it couldn't fill."""
    todo = [f for f in fields if f["kind"] != "file" or not f["value"]]
    if not todo:
        return []
    out = answer(todo, job, profile + WD_RULES.format(today=time.strftime("%m/%d/%Y")), resume, ask)
    unknown = [u for u in out.get("unknown") or [] if u in {f["label"] for f in fields if f["required"] and not f["value"]}]
    if unknown:
        raise NeedsInfo(unknown)
    missed = []
    for f in todo:
        if f["id"] not in out["answers"]:
            continue
        v = out["answers"][f["id"]]
        if v in (None, "", []):
            continue
        try:
            ok = _fill_one(page, f, v, docs)
        except Exception:
            ok = False
        if not ok and f["required"]:
            missed.append(f["label"])
    return missed


# ── the run ──────────────────────────────────────────────────────────────────
def _step(page) -> str:
    try:
        return page.locator("[data-automation-id=progressBarActiveStep]").first.inner_text().split("\n")[-1].strip()
    except Exception:
        return ""


def _add_sections(page, resume: str) -> None:
    """My Experience: the Education and Work Experience sections start empty, each behind an Add button."""
    wanted = ["Education"] + (["Work-Experience"] if re.search(r"^EXPERIENCE", resume, re.M) else [])
    for name in wanted:
        group = page.locator(f'[role="group"][aria-labelledby="{name}-section"]').first
        try:
            if not group.count() or group.locator("[data-automation-id^=formField-]").count():
                continue
            group.locator('[data-automation-id="add-button"]').first.click(force=True)
            page.wait_for_timeout(1500)
        except Exception:
            continue


def apply(job: dict, profile: str, resume_text: str, docs: dict, browser_dir: Path, shots: Path, ask: Callable,
          mail, email: str, practice: bool, log: Callable = print) -> dict:
    """One Workday application. Returns {"screenshot", "cover_letter"}; raises NeedsInfo or Blocked."""
    from playwright.sync_api import sync_playwright
    shots.mkdir(parents=True, exist_ok=True)
    shot = shots / f"{job['id']}.png"
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(browser_dir), headless=True, channel="chromium")
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            sign_in(page, job["url"], email, mail, log)
            if DONE.search(page.inner_text("body")):
                return {"screenshot": str(shot), "already": True}
            if not page.locator("[data-automation-id=pageFooterNextButton]").count():
                page.screenshot(path=str(shot), full_page=True)
                raise Blocked("the application form didn't open")
            letter = None
            if "cover_letter" not in docs:
                (shots / job["id"]).mkdir(exist_ok=True)
                name = " ".join(_line(profile, k) for k in ("First name", "Last name")).strip() or "Applicant"
                letter = letter_pdf(ctx, cover_letter(job, profile, resume_text, ask), profile,
                                    shots / job["id"] / f"{name} - Cover Letter.pdf")
                docs = {**docs, "cover_letter": letter}
            seen = {}
            for _ in range(14):
                step = _step(page)
                seen[step] = seen.get(step, 0) + 1
                if seen[step] > 3:
                    page.screenshot(path=str(shot), full_page=True)
                    errs = page.evaluate(ERRORS_JS)
                    raise Blocked(f"stuck on the {step or 'same'} page" + (f": {errs[0][:120]}" if errs else ""))
                if re.search(r"review", step, re.I):
                    page.screenshot(path=str(shot), full_page=True)
                    if practice:
                        return {"screenshot": str(shot), "cover_letter": str(letter or "")}
                    page.locator("[data-automation-id=pageFooterNextButton]").click(force=True)
                    _settle(page, 5000)
                    if DONE.search(page.inner_text("body")) or not page.locator("[data-automation-id=pageFooterNextButton]").count():
                        page.screenshot(path=str(shot), full_page=True)
                        return {"screenshot": str(shot), "cover_letter": str(letter or "")}
                    errs = page.evaluate(ERRORS_JS)
                    raise Blocked("Submit didn't go through" + (f": {errs[0][:120]}" if errs else ""))
                if re.search(r"experience", step, re.I):
                    _add_sections(page, resume_text)
                missed = fill_page(page, read_page(page), job, profile, resume_text, ask, docs)
                if missed:
                    # a second look: answers can open new fields, and a slow list may have missed the first time
                    missed = fill_page(page, [f for f in read_page(page) if f["required"] and not f["value"]],
                                       job, profile, resume_text, ask, docs)
                page.locator("[data-automation-id=pageFooterNextButton]").click(force=True)
                _settle(page, 3000)
                if _step(page) == step:
                    errs = page.evaluate(ERRORS_JS)
                    if errs:
                        fill_page(page, [f for f in read_page(page) if f["required"] and not f["value"]],
                                  job, profile, resume_text, ask, docs)
                        page.locator("[data-automation-id=pageFooterNextButton]").click(force=True)
                        _settle(page, 3000)
            raise Blocked("the application had more pages than expected")
        finally:
            ctx.close()
