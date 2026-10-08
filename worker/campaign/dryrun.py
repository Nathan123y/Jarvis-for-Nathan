"""Dry run: three made-up businesses go through the whole pipeline with no network, no hosting and
no email. Nothing can reach a real business: the fetcher and host are fakes, and the mailer raises
if anything tries to send. It leaves previews and message drafts on disk for you to read.

    python3 -m worker campaign dryrun
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

from core.events import Store as EventStore
from worker.campaign import discover, pipeline, policy as pol
from worker.campaign.fakes import FakeFetcher, FakeHost
from worker.campaign.store import CampaignStore
from worker.runtime import Worker

CID = "dryrun"
IDENTITY = {"sender_name": "Dry Run Sender", "postal_address": "100 Example Street, San Jose, CA 95112",
            "gmail_account": "spam"}

FIXTURES = [
    {"name": "Bay Leaf Plumbing", "category": "plumber", "address": "410 Market Street, San Jose, CA 95113", "city": "San Jose",
     "phone": "(408) 555-0101", "email": "hello@bayleafplumbing.example", "website": "", "source": "fixture",
     "source_id": "fx/1", "data": {"hours": "Mo-Fr 08:00-17:00"}},
    {"name": "Willow Glen Bakery", "category": "bakery", "address": "1200 Lincoln Avenue, San Jose, CA 95125", "city": "San Jose",
     "phone": "(408) 555-0102", "email": "orders@willowglenbakery.example", "website": "http://willowglenbakery.example",
     "source": "fixture", "source_id": "fx/2", "data": {}},
    {"name": "Alma Piano Studio", "category": "music_school", "address": "88 Alma Street, San Jose, CA 95110", "city": "San Jose",
     "phone": "(408) 555-0103", "email": "teach@almapiano.example", "website": "http://almapiano.example",
     "source": "fixture", "source_id": "fx/3", "data": {}},
]
# 1: no website at all.  2: the site never loads.  3: loads, but has no mobile layout.
PAGES = {
    "http://almapiano.example": "<html><head><title>Alma Piano</title></head><body><h1>Alma Piano Studio</h1>"
                                  "<p>Piano lessons for all ages in downtown San Jose. Call (408) 555-0103 to book a first lesson. "
                                  "We teach beginners through advanced students, in person, with a friendly approach and recitals twice a year. "
                                  "Lessons are thirty or forty-five minutes long and focus on reading music, technique and enjoyment.</p></body></html>",
    "http://almapiano.example/robots.txt": (404, ""),
}


class NoSendMailer:
    """A mailer that refuses to send, so a dry run can never email anyone."""

    def check(self):
        return "dryrun@example.test"

    def send(self, *a, **k):
        raise AssertionError("dry run must never send")

    def find_sent(self, row):
        return {"found": False, "authoritative": True}

    def thread_messages(self, thread_id):
        return []

    def inbox_since(self, days=3):
        return []


def run(base: Path, *, clock: Optional[Callable[[], float]] = None, visual: bool = False) -> dict:
    """Run the three-business dry run under `base`. Returns what happened. `visual` turns the
    browser check on (it needs Playwright); the static checks always run."""
    base = Path(base)
    clock = clock or time.time
    wdir = base / "worker"
    store = CampaignStore(base / "campaign.db")
    policy = pol.Policy(require_visual_check=visual)
    store.save_campaign(CID, "Dry run", policy.__dict__ | {}, clock())
    store.set_campaign(CID, clock(), mode="draft", status="active")
    env = pipeline.Env(store=store, provider=discover.FixtureProvider(FIXTURES), fetcher=FakeFetcher(PAGES),
                       host=FakeHost(public=True, base="https://previews.example.test/dryrun"), mailer=NoSendMailer(),
                       identity=lambda: dict(IDENTITY), workdir=base, clock=clock, sleep=lambda s: None, pace=0,
                       mail_domain=lambda e: "")
    worker = Worker(wdir, events=EventStore(base / "events.db"), handlers=pipeline.handlers(env), tick=0.01, clock=clock)
    worker.db.enqueue("campaign_discover", {"campaign_id": CID}, campaign_id=CID, unique_key="dry:discover")
    for _ in range(200):                                    # bounded: every step is a queued job
        if worker.run_once() is None:
            break
    worker.keep_awake.stop()
    rows = []
    for b in store.businesses(CID):
        offer = b["data"].get("offer") or {}
        rows.append({"name": b["name"], "stage": b["stage"], "status": b["status"], "reason": b["hold_reason"],
                     "preview": b["data"].get("preview_url", ""), "subject": offer.get("subject", ""),
                     "body": offer.get("body", ""), "problems": [p["code"] for p in (b["data"].get("audit") or {}).get("problems", [])]})
    return {"businesses": rows, "sent": len(getattr(env.mailer, "sent", [])), "sites_dir": str(base / "sites"),
            "events": EventStore(base / "events.db").events()}


def report(result: dict) -> str:
    lines = ["DRY RUN: 3 made-up businesses, nothing was sent or published anywhere.", ""]
    for r in result["businesses"]:
        lines.append(f"{r['name']}: {r['stage']}" + ("" if r["status"] == "ok" else f" ({r['status']}: {r['reason']})"))
        if r["problems"]:
            lines.append("  observed: " + ", ".join(r["problems"]))
        if r["body"]:
            lines += ["  Subject: " + r["subject"], *("  | " + l for l in r["body"].splitlines())]
        lines.append("")
    lines.append(f"Preview pages are in {result['sites_dir']} (open index.html in a browser).")
    return "\n".join(lines)
