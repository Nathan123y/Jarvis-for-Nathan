"""Voice control for the internship crew that the background worker runs (worker/internships.py).

The crew finds Summer 2027 hardware / electrical / semiconductor internships in California, applies to the
Greenhouse ones from the user's info file and résumé (up to a few a day), texts every application and every
reply from a company, and texts a few hand-apply links each morning. It also follows the SJSU director's co-op /
internship sheet and emails: EE listings on a company's own site are applied to right away, the rest texted. Here: how it's going, pause, resume,
go live after the practice run, applications a day, and opening the info file.
"""
from __future__ import annotations

import subprocess
from collections import Counter

from memory.config_manager import save_plugin_config
from worker import internships as wi

PLUGIN = {
    "name": "internships",
    "description": (
        "The user's internship crew, which Jarvis runs by itself: it finds Summer 2027 hardware, electrical and "
        "semiconductor internships in California, fills in and submits applications from the user's info file and "
        "résumé, and texts each application and each company reply. Use for 'how are my internship applications "
        "going' / 'where did you apply' (status), 'which companies replied' (replies), 'pause the internship "
        "applications' (pause), 'resume them' (resume), 'internships go live' / 'start applying for real' after the "
        "practice run (go_live), 'do another practice run' (practice), 'apply to 10 a day' (per_day, value=number), "
        "and 'open my internship info file' (open_info). Actions: status, replies, pause, resume, go_live, practice, "
        "per_day, open_info."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": ["status", "replies", "pause", "resume", "go_live", "practice", "per_day", "open_info"]},
            "value": {"type": "INTEGER", "description": "For per_day: applications a day (0-40)"},
        },
        "required": [],
    },
}

PLUGIN_SETTINGS = {
    "namespace": wi.NS,
    "title": "Internship applications (Summer 2027)",
    "fields": [
        {"key": "per_day", "type": "text", "label": "Applications a day", "placeholder": "5"},
        {"key": "start", "type": "text", "label": "First application at (HH:MM)", "placeholder": "06:00"},
        {"key": "end", "type": "text", "label": "No new applications after (HH:MM)", "placeholder": "21:00"},
    ],
}


def _status() -> str:
    cfg, st = wi.settings(), wi.load_state()
    jobs = list(st["jobs"].values())
    c = Counter(j["status"] for j in jobs)
    applied = sorted((j for j in jobs if j.get("applied_at")), key=lambda j: -j["applied_at"])
    parts = []
    gaps = wi.profile_gaps()
    if gaps:
        parts.append("Waiting on your info file: still blank: " + ", ".join(gaps[:6]) + ".")
    parts.append(("Paused. " if not cfg["enabled"] else "")
                 + (f"Practice mode: it fills one application without submitting, then waits for 'go live'. "
                    if cfg["practice"] else f"Applying to up to {cfg['per_day']} a day. ")
                 + f"The Scout has {len(jobs)} Summer 2027 postings: {c['new']} waiting, {len(applied)} applied, "
                 f"{c['needs_info']} need your answers, {c['manual']} to apply to by hand.")
    mine = [j for j in jobs if j.get("source") == "director"]
    if mine:
        parts.append(f"Your director's list: {len(mine)} EE listings, {sum(1 for j in mine if j.get('applied_at'))} applied on "
                     f"company sites, {sum(1 for j in mine if j['ats'] == 'handshake')} to apply to on Handshake yourself.")
    if applied:
        parts.append("Latest: " + "; ".join(f"{j['company']} ({j['title'][:40]})" for j in applied[:5]) + ".")
    return " ".join(parts)


def _replies() -> str:
    st = wi.load_state()
    got = [(r, j) for j in st["jobs"].values() for r in j.get("replies") or [] if r["kind"] != "received"]
    if not got:
        return "No company has replied yet, apart from 'we got your application' confirmations."
    got.sort(key=lambda x: -x[0]["at"])
    return "Replies: " + "; ".join(f"{j['company']}: {r['kind']}" for r, j in got[:10]) + "."


def run(parameters: dict, player=None, session_memory=None) -> str:
    args = parameters or {}
    action = str(args.get("action") or "status").strip().lower()
    try:
        if action == "status":
            return _status()
        if action == "replies":
            return _replies()
        if action in ("pause", "resume"):
            save_plugin_config(wi.NS, {"enabled": action == "resume"})
            return ("Paused the internship applications. Replies are still watched once you resume."
                    if action == "pause" else "The internship crew is running again.")
        if action == "go_live":
            if wi.profile_gaps():
                return "Not yet: your info file still has blanks: " + ", ".join(wi.profile_gaps()[:6]) + "."
            save_plugin_config(wi.NS, {"practice": False})
            return (f"Live. It submits up to {wi.settings()['per_day']} applications a day from 6 AM and texts you each one, "
                    "plus every reply from a company.")
        if action == "practice":
            save_plugin_config(wi.NS, {"practice": True})
            st = wi.load_state()
            st.pop("practice_done", None)
            wi.save_state(st)
            return "Okay: the next application is filled in but not submitted, and I'll text you when it's ready to look at."
        if action == "per_day":
            try:
                n = max(0, min(40, int(args.get("value"))))
            except (TypeError, ValueError):
                return "How many applications a day? Up to 40."
            save_plugin_config(wi.NS, {"per_day": n})
            return f"Okay, up to {n} applications a day."
        if action == "open_info":
            subprocess.run(["/usr/bin/open", "-e", str(wi.PROFILE)], check=False)
            return "Opened your internship info file. Your résumé goes next to it as resume.pdf."
        return "I can tell you how the internship applications are going, pause or resume them, or go live."
    except Exception as exc:
        return f"The internship crew hit a problem: {exc}"
