"""The local-business website campaign, by voice.

  status    where it stands (counts by stage, held items, what is still needed)
  review    the campaign review: area, offer, sender, limits, stop conditions, sample messages
  enable    the ONE-TIME authorization (asks you yes/no once). After it, no per-email approval.
  pause / resume / stop (stop revokes the authorization) / mode (research or draft)
  dryrun    three made-up businesses end to end; nothing is sent or published

The work itself is done by the background worker (python3 -m worker install), not by this window.
"""
from __future__ import annotations

from core import confirm

PLUGIN = {
    "name": "website_campaign",
    "description": (
        "Control the autonomous local-business website campaign (finds San Jose/Bay Area businesses with poor or "
        "no websites, builds concept previews, and emails offers from the spam Gmail). Use for 'how is the website "
        "campaign going', 'show me the campaign review', 'turn on the website campaign', 'pause/resume/stop the "
        "campaign', 'run the dry run'. Actions: status, review, enable (asks the user to confirm once), pause, "
        "resume, stop, mode (value research|draft), dryrun. Say only what the tool returns. Never claim anything "
        "was sent unless the status says so."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": ["status", "review", "enable", "pause", "resume", "stop", "mode", "dryrun"]},
            "value": {"type": "STRING", "description": "For mode: research or draft"},
        },
        "required": ["action"],
    },
}

PLUGIN_SETTINGS = {
    "namespace": "website_campaign",
    "title": "Website campaign: preview hosting",
    "fields": [
        {"key": "preview_repo_url", "type": "text", "label": "Preview repository (git URL of a public GitHub Pages repo)",
         "placeholder": "git@github.com:you/jarvis-previews.git"},
        {"key": "preview_base_url", "type": "text", "label": "Preview address (https URL where that repo is served)",
         "placeholder": "https://you.github.io/jarvis-previews"},
    ],
}


def _env_and_db():
    from worker import jobs
    from worker.campaign import pipeline
    from worker.runtime import default_dir
    return pipeline.default_env(), jobs.JobDB(default_dir() / "jobs.db")


def run(parameters: dict, player=None, session_memory=None) -> str:
    args = parameters or {}
    action = str(args.get("action") or "status").strip().lower()
    try:
        from worker.campaign import control, dryrun
        if action == "dryrun":
            from worker.runtime import default_dir
            import shutil
            base = default_dir().parent / "dryrun"
            shutil.rmtree(base, ignore_errors=True)
            text = dryrun.report(dryrun.run(base))
            if player:
                player.show_content("WEBSITE CAMPAIGN DRY RUN", text[:3800])
            return "The dry run is on screen: three made-up businesses, previews and drafts, nothing sent."
        env, db = _env_and_db()
        if action == "status":
            text = control.status(env, db=db)
            if player:
                player.show_content("WEBSITE CAMPAIGN", text[:3800])
            return text
        if action == "review":
            text = control.review(env)
            if player:
                player.show_content("WEBSITE CAMPAIGN REVIEW", text[:3800])
            tail = text.split("BEFORE IT CAN START:")[-1].strip() if "BEFORE IT CAN START:" in text else ""
            return "The campaign review is on screen." + (f" Before it can start: {tail}" if tail else "")
        if action == "enable":
            missing = control.readiness(env, check_gmail=True)
            if missing:
                return "Can't enable yet. Still needed: " + "; ".join(missing) + "."
            if confirm.pending_title():
                return "A confirmation is already on screen. Please answer it first."
            review = control.review(env)
            if player:
                player.show_content("WEBSITE CAMPAIGN REVIEW", review[:3800])

            def go() -> str:
                e, d = _env_and_db()
                return control.enable(e, d, by="voice")
            return confirm.request(
                key="website-campaign-enable",
                title="LET JARVIS RUN THE WEBSITE CAMPAIGN ON ITS OWN?",
                detail=("It will find, build and email local businesses without asking you each time, from the spam Gmail, "
                        "within the limits in the review (full review in the content panel). You can stop it any time."),
                run=go)
        if action == "pause":
            return control.pause(env, by="voice")
        if action == "resume":
            return control.resume(env, by="voice")
        if action == "stop":
            return control.stop(env, by="voice")
        if action == "mode":
            return control.set_mode(env, str(args.get("value") or "").lower(), by="voice")
        return "Unknown action."
    except Exception as exc:
        return f"The campaign tool failed ({type(exc).__name__}). Nothing was sent."
