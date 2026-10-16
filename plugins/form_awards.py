"""JARVIS award-form results: read a Google Form's responses and name the top nominees per category.

Built for the Friendsgiving end-of-year awards: every checkbox (or multiple-choice / dropdown)
question is one award category, and each ticked name is one vote. Read-only: it never changes
the form or its responses. The Google sign-in is separate from Gmail's and asks only for
read-only access to forms; its token lives in config/ and is ignored by Git.
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path

from memory.config_manager import get_plugin_setting, save_plugin_config

_ROOT = Path(__file__).resolve().parent.parent
_CREDENTIALS = _ROOT / "config" / "gmail_credentials.json"      # the same Google OAuth desktop client as Gmail
_TOKEN = _ROOT / "config" / "google_forms_token.json"
_SCOPES = (
    "https://www.googleapis.com/auth/forms.body.readonly",
    "https://www.googleapis.com/auth/forms.responses.readonly",
)
_NAMESPACE = "award_form"
_AUTH_LOCK = threading.Lock()
_EDIT_ID = re.compile(r"/forms/d/(?!e/)([A-Za-z0-9_-]{20,})")
_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_CHOICE_TYPES = {"CHECKBOX", "RADIO", "DROP_DOWN"}

PLUGIN = {
    "name": "form_awards",
    "description": (
        "Read the results of the user's Google Form awards vote (the Friendsgiving end-of-year award "
        "ceremony) and say the top nominees in each category, with vote counts. Use for 'award results', "
        "'Friendsgiving awards', 'who's winning the awards', 'top 4 in each category', 'how many people "
        "voted'. Actions: results (default), connect (sign in to Google once), set_form (save the form's "
        "EDIT link, the one ending in /edit). Read-only: it cannot change the form. Read names and counts "
        "exactly as returned; never invent nominees, votes or categories."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "results (default), connect, or set_form"},
            "form_link": {"type": "STRING", "description": "For set_form: the form's edit link (…/forms/d/ID/edit)"},
            "top": {"type": "INTEGER", "description": "How many nominees per category (default 4)"},
        },
        "required": [],
    },
}

PLUGIN_SETTINGS = {
    "namespace": _NAMESPACE,
    "title": "Award form (Google Forms, read-only)",
    "fields": [{"key": "form_link", "type": "text", "label": "Form edit link",
                "placeholder": "https://docs.google.com/forms/d/…/edit (not the /d/e/…/viewform link)"}],
}


# ── which form ────────────────────────────────────────────────────────────────
def form_id(link: str) -> str:
    """The form's id from its edit link (or a bare id). The /d/e/… link people fill in is a
    different, public id that Google's API does not accept, so it is refused in plain words."""
    text = str(link or "").strip()
    if "/forms/d/e/" in text:
        raise ValueError("That's the link people use to fill the form in. I need the edit link: open the form "
                         "in Google Forms and copy the address bar; it looks like docs.google.com/forms/d/…/edit.")
    match = _EDIT_ID.search(text)
    if match:
        return match.group(1)
    if _BARE_ID.fullmatch(text):
        return text
    raise ValueError("That doesn't look like a Google Form edit link (docs.google.com/forms/d/…/edit).")


# ── counting ──────────────────────────────────────────────────────────────────
def _key(name: str) -> str:
    return " ".join(str(name).split()).casefold()


def categories(form: dict) -> list[dict]:
    """[{id, title, options}] for every choice question, in the form's order."""
    out = []
    for item in form.get("items") or []:
        question = (item.get("questionItem") or {}).get("question") or {}
        choice = question.get("choiceQuestion") or {}
        if choice.get("type") in _CHOICE_TYPES and question.get("questionId"):
            out.append({"id": question["questionId"], "title": " ".join(str(item.get("title") or "Untitled").split()),
                        "options": [o.get("value", "") for o in choice.get("options") or [] if o.get("value")]})
    return out


def latest_per_person(responses: list[dict]) -> tuple[list[dict], int]:
    """If the form collected emails, keep each person's most recent submission only.
    Returns (responses to count, how many earlier duplicates were dropped)."""
    if not responses or not all(r.get("respondentEmail") for r in responses):
        return list(responses), 0
    latest: dict[str, dict] = {}
    for r in sorted(responses, key=lambda r: r.get("lastSubmittedTime") or r.get("createTime") or ""):
        latest[_key(r["respondentEmail"])] = r
    return list(latest.values()), len(responses) - len(latest)


def tally(form: dict, responses: list[dict], top: int = 4) -> dict:
    """{responses, dropped, categories: [{title, votes, top: [(name, votes)]}]}. Each ticked name is one
    vote. Names are matched ignoring case and spacing ("Jake", "jake "); the spelling shown is the
    form's own option, else the most common one typed. A tie for the last place keeps everyone tied."""
    counted, dropped = latest_per_person(responses)
    result = []
    for cat in categories(form):
        votes: dict[str, int] = {}
        spellings: dict[str, dict[str, int]] = {}
        for option in cat["options"]:
            spellings.setdefault(_key(option), {})[option] = 10 ** 6       # the form's spelling wins
        voters = 0
        for response in counted:
            answer = (response.get("answers") or {}).get(cat["id"]) or {}
            picked = {_key(a.get("value", "")): " ".join(str(a.get("value", "")).split())
                      for a in (answer.get("textAnswers") or {}).get("answers") or [] if str(a.get("value", "")).strip()}
            if picked:
                voters += 1
            for key, shown in picked.items():                             # ticking a name twice is still one vote
                votes[key] = votes.get(key, 0) + 1
                spellings.setdefault(key, {})[shown] = spellings.get(key, {}).get(shown, 0) + 1
        order = {_key(o): i for i, o in enumerate(cat["options"])}         # equal votes: the form's own order
        ranked = sorted(votes.items(), key=lambda kv: (-kv[1], order.get(kv[0], len(order)), kv[0]))
        cut = ranked[top - 1][1] if len(ranked) >= top else 0
        winners = [(max(spellings[k], key=spellings[k].get), n) for k, n in ranked if n >= cut and n > 0]
        result.append({"title": cat["title"], "voters": voters, "top": winners})
    return {"responses": len(counted), "dropped": dropped, "categories": result}


def _names(top: list[tuple]) -> str:
    return ", ".join(f"{name} {n}" for name, n in top) or "no votes yet"


def panel_text(summary: dict, top: int) -> str:
    lines = [f"{summary['responses']} response{'s' if summary['responses'] != 1 else ''}"
             + (f" ({summary['dropped']} earlier resubmission{'s' if summary['dropped'] != 1 else ''} not counted)"
                if summary["dropped"] else ""), ""]
    for cat in summary["categories"]:
        lines.append(f"{cat['title'].upper()}  ({cat['voters']} voted)")
        if not cat["top"]:
            lines.append("  no votes yet")
        for name, n in cat["top"]:
            place = 1 + sum(1 for _, other in cat["top"] if other > n)    # ties share a place
            lines.append(f"  {place}. {name} — {n} vote{'s' if n != 1 else ''}")
        lines.append("")
    tied = [c["title"] for c in summary["categories"] if len(c["top"]) > top]
    if tied:
        lines.append(f"Ties for #{top} are all listed: {', '.join(tied)}.")
    return "\n".join(lines).strip()


def spoken(summary: dict, top: int) -> str:
    if not summary["categories"]:
        return "I found the form, but it has no checkbox or multiple-choice categories to count."
    n = summary["responses"]
    head = f"{n} {'person has' if n == 1 else 'people have'} voted. Top {top} in each category: "
    return head + " ".join(f"{c['title']}: {_names(c['top'])}." for c in summary["categories"])


# ── Google ────────────────────────────────────────────────────────────────────
def _write_token(creds) -> None:
    _TOKEN.parent.mkdir(parents=True, exist_ok=True)
    temporary = _TOKEN.with_name(_TOKEN.name + ".tmp")
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(creds.to_json())
        os.replace(temporary, _TOKEN)
    finally:
        if temporary.exists():
            temporary.unlink()


def _service(*, allow_login: bool = False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    with _AUTH_LOCK:
        creds = None
        if _TOKEN.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(_TOKEN))
            except (OSError, ValueError):
                creds = None
        if creds and not creds.has_scopes(_SCOPES):
            creds = None
        if creds and not creds.valid and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                _write_token(creds)
            except Exception:
                creds = None
        if not creds or not creds.valid:
            if not allow_login:
                raise RuntimeError("Google Forms isn't connected yet. Ask me to 'connect the award form' first.")
            if not _CREDENTIALS.exists():
                raise RuntimeError("The Google sign-in file config/gmail_credentials.json is missing.")
            flow = InstalledAppFlow.from_client_secrets_file(str(_CREDENTIALS), _SCOPES)
            creds = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True,
                                          prompt="consent select_account")
            _write_token(creds)
    return build("forms", "v1", credentials=creds, cache_discovery=False)


def fetch(service, fid: str) -> tuple[dict, list[dict]]:
    form = service.forms().get(formId=fid).execute()
    responses, token = [], None
    for _ in range(50):
        page = service.forms().responses().list(formId=fid, pageSize=5000, pageToken=token).execute()
        responses += page.get("responses") or []
        token = page.get("nextPageToken")
        if not token:
            break
    return form, responses


def _friendly(exc: Exception) -> str:
    text = str(exc)
    if "has not been used" in text or "is disabled" in text or "SERVICE_DISABLED" in text:
        return ("Google says the Forms API is switched off for Jarvis's Google Cloud project. Turn on "
                "'Google Forms API' in the Cloud console (same project as Gmail), then ask again.")
    if "404" in text or "not found" in text.lower():
        return "Google couldn't find that form. Check the edit link, and that you signed in with the account that owns it."
    if "403" in text:
        return "Google refused: the account I'm signed in with can't see that form's responses. Reconnect with the form's owner."
    return f"Sir, I couldn't read the form ({type(exc).__name__})."


def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "results").strip().lower()
    try:
        top = max(1, min(int(params.get("top") or 4), 10))
    except (TypeError, ValueError):
        top = 4
    try:
        if action == "set_form":
            fid = form_id(params.get("form_link", ""))
            save_plugin_config(_NAMESPACE, {"form_link": f"https://docs.google.com/forms/d/{fid}/edit"})
            result = "Saved the award form. Ask me for the award results any time."
        elif action == "connect":
            _service(allow_login=True)
            result = "Connected to Google Forms, read-only. Ask me for the award results."
        elif action == "results":
            link = get_plugin_setting(_NAMESPACE, "form_link", "")
            if not link:
                return ("I don't know which form yet. Give me its edit link, the one ending in /edit, "
                        "or paste it in Plugin Settings under Award form.")
            form, responses = fetch(_service(), form_id(link))
            summary = tally(form, responses, top)
            result = spoken(summary, top)
            if player:
                try:
                    title = (form.get("info") or {}).get("title") or "AWARD RESULTS"
                    player.show_content(str(title).upper()[:48], panel_text(summary, top))
                except Exception:
                    pass
        else:
            result = "I can give the award results, connect to Google Forms, or save the form's edit link."
    except (ValueError, RuntimeError) as exc:
        result = str(exc)
    except Exception as exc:                                    # noqa: BLE001 - spoken, never a traceback
        result = _friendly(exc)
    if player:
        try:
            player.write_log(f"JARVIS: {result}")
        except Exception:
            pass
    return result
