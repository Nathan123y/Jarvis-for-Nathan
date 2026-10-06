"""Sort replies into what they are, with the evidence and how sure we are.

Deterministic rules only (no model, no cost). The quoted copy of our own message is stripped first, so
"reply STOP" in our footer can never classify a reply. Uncertain results go to review instead of being
guessed. A reply's text is only ever *read*: it cannot change campaign permissions, run anything, reveal
anything or authorize anything.
"""
from __future__ import annotations

import re
from typing import Optional

CLASSES = ("interested", "call_request", "question", "declined", "bounced", "opted_out", "automated",
           "complaint", "unclear")
REVIEW_BELOW = 0.7

BOUNCE_FROM = re.compile(r"(mailer-daemon|postmaster|mail delivery subsystem)", re.I)
BOUNCE_SUBJ = re.compile(r"(delivery status notification|undeliverable|returned mail|delivery failure|mail delivery failed|failure notice)", re.I)
OPT_OUT = re.compile(r"\b(stop|unsubscribe|remove me|remove us|do not (contact|email|message)|don'?t (contact|email|message)|"
                     r"take me off|opt[- ]?out|take (me|us) off|remove (me|us|this)|no more emails?|leave me alone)\b", re.I)
COMPLAINT = re.compile(r"\b(spam|report(ing)? you|abuse|harass|lawyer|attorney|cease and desist|illegal|"
                       r"can-?spam|violat|sue\b)", re.I)
AUTO = re.compile(r"(out of (the )?office|automatic reply|auto[- ]?reply|autoreply|away from (my )?(desk|email)|"
                  r"will (respond|reply|be back)|thank you for contacting|this is an automated)", re.I)
DECLINE = re.compile(r"\b(not interested|no thanks|no thank you|we'?re (all )?set|already have|pass on|"
                     r"don'?t need|do not need|not looking)\b", re.I)
CALL = re.compile(r"\b(call me|give me a call|call (us|back)|phone call|let'?s (talk|chat|meet)|can we (talk|chat|meet)|"
                  r"set up a (call|time|meeting)|schedule a (call|time|meeting)|free to talk|when (can|are) you (call|free)|"
                  r"my (number|cell) is|reach me at)\b", re.I)
INTEREST = re.compile(r"\b(interested|sounds good|looks good|looks great|love (it|this)|yes\b|yeah|sure|how much|"
                      r"what('?s| is) the (price|cost)|tell me more|more info|go ahead|let'?s do it|i'?d like)\b", re.I)


def strip_quoted(text: str) -> str:
    out = []
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith(">"):
            continue
        if re.match(r"on .{5,80}wrote:\s*$", s, re.I) or re.match(r"-{2,}\s*original message", s, re.I) \
                or re.match(r"from:\s.+", s, re.I) and out:
            break
        out.append(line)
    return " ".join(" ".join(out).split())


def snippet(text: str, limit: int = 160) -> str:
    s = strip_quoted(text).replace("[", "(").replace("]", ")")
    return s[:limit]


def classify(text: str, *, from_addr: str = "", subject: str = "", headers: Optional[dict] = None) -> dict:
    """{"classification", "confidence", "evidence", "needs_review"}"""
    headers = {k.lower(): str(v) for k, v in (headers or {}).items()}
    body = strip_quoted(text)
    ev = lambda m: (m.group(0) if hasattr(m, "group") else str(m))[:60]

    def out(c, conf, evidence):
        return {"classification": c, "confidence": conf, "evidence": evidence, "needs_review": conf < REVIEW_BELOW}

    if BOUNCE_FROM.search(from_addr) or BOUNCE_SUBJ.search(subject):
        return out("bounced", 0.97, from_addr or subject)
    if COMPLAINT.search(body):
        return out("complaint", 0.85, ev(COMPLAINT.search(body)))
    m = OPT_OUT.search(body)
    if m:
        # a long, friendly message that merely contains "stop" is still treated as an opt-out: the safe side
        return out("opted_out", 0.95 if len(body) < 80 or body.lower().startswith("stop") else 0.8, ev(m))
    auto = headers.get("auto-submitted", "").lower() not in ("", "no") or headers.get("x-autoreply") or headers.get("precedence", "").lower() in ("auto_reply", "bulk", "junk")
    m = AUTO.search(body) or AUTO.search(subject)
    if auto or m:
        return out("automated", 0.92 if auto else 0.8, ev(m) if m else "auto-reply header")
    m = DECLINE.search(body)
    if m:
        return out("declined", 0.9, ev(m))
    call, interest = CALL.search(body), INTEREST.search(body)
    if call:
        return out("call_request", 0.9 if len(body) < 400 else 0.75, ev(call))
    if interest:
        return out("interested", 0.85 if len(body) < 300 else 0.7, ev(interest))
    if "?" in body:
        return out("question", 0.75, "contains a question")
    return out("unclear", 0.3, body[:60])
