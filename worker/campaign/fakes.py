"""Stand-ins for the network, Gmail and the preview host. Used by the tests and by the dry run, so
neither can ever contact a real business or send a real email."""
from __future__ import annotations

from worker.campaign.audit import FetchError, Page
from worker.campaign.mail import MailError


class FakeFetcher:
    """pages: {url: html or (status, html) or FetchError(kind)}. Unknown urls are unreachable."""

    def __init__(self, pages=None):
        self.pages, self.calls = dict(pages or {}), []

    def get(self, url: str) -> Page:
        self.calls.append(url)
        v = self.pages.get(url)
        if v is None:
            raise FetchError("unreachable", "no such host (fake)")
        if isinstance(v, FetchError):
            raise v
        status, html = v if isinstance(v, tuple) else (200, v)
        return Page(url, url, status, html, {}, 0.1)


class FakeMailer:
    """Records sends instead of making them. Set `fail` to a MailError (or list of results) to simulate."""

    def __init__(self, address="sender@example.test", fail=None):
        self.address, self.fail, self.sent, self.threads, self.inbox = address, fail, [], {}, []

    def check(self):
        if isinstance(self.fail, MailError) and self.fail.kind == "auth":
            raise self.fail
        return self.address

    def send(self, to, subject, body, idem_key):
        if isinstance(self.fail, MailError):
            raise self.fail
        mid = f"m{len(self.sent) + 1}"
        self.sent.append({"to": to, "subject": subject, "body": body, "idem_key": idem_key, "message_id": mid})
        self.threads[f"t{mid}"] = [{"id": mid, "ts": 1.0, "from": self.address, "subject": subject, "text": body,
                                    "headers": {}, "labels": ["SENT"]}]
        return {"message_id": mid, "thread_id": f"t{mid}", "from": self.address}

    def find_sent(self, row):
        for s in self.sent:
            if s["idem_key"] == row["idem_key"]:
                return {"found": True, "message_id": s["message_id"], "thread_id": f"t{s['message_id']}"}
        return {"found": False, "authoritative": True}

    def add_reply(self, thread_id, text, *, sender="owner@biz.test", subject="Re: hello", headers=None, mid=None, ts=2.0):
        msgs = self.threads.setdefault(thread_id, [])
        msgs.append({"id": mid or f"r{len(msgs)}", "ts": ts, "from": sender, "subject": subject, "text": text,
                     "headers": headers or {}, "labels": ["INBOX"]})

    def thread_messages(self, thread_id):
        return list(self.threads.get(thread_id, []))

    def add_inbox(self, text, *, sender, subject="hello", mid, ts=2.0):
        self.inbox.append({"id": mid, "ts": ts, "from": sender, "subject": subject, "text": text, "headers": {}, "labels": ["INBOX"]})

    def inbox_since(self, days=3):
        return list(self.inbox)


class FakeHost:
    """Pretends to be a public host. `public` False simulates 'no hosting configured'."""

    def __init__(self, public=True, base="https://previews.example.test", reachable=True):
        self.public, self.base, self.reachable, self.pages, self.removed = public, base, reachable, {}, []

    def summary(self):
        return f"{self.base} (fake host for tests)" if self.public else "none configured"

    def publish(self, slug, files):
        self.pages[slug] = files
        return f"{self.base}/{slug}/"

    def verify(self, url, slug):
        return (self.reachable, "reachable (fake)" if self.reachable else "not reachable (fake)")

    def remove(self, slug):
        self.removed.append(slug)
        self.pages.pop(slug, None)

    def expire(self, now, ttl_days):
        return []
