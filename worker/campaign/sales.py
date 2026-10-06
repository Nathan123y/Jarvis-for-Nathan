"""Verified sales from a payment export you download yourself (there is no payment API key in Jarvis).

    python3 -m worker sales import payments.csv

Only rows whose status says the payment succeeded count. Amounts are read as dollars. A row with no
fee or refund column is recorded with that figure *unknown*, and the briefing says so instead of
treating it as zero. Re-importing the same file never double-counts (each payment id is stored once).
"""
from __future__ import annotations

import csv
import hashlib
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.events import Store

PAID = {"paid", "succeeded", "complete", "completed"}
COLS = {
    "id": ("id", "charge id", "payment id", "payment intent id", "transaction id"),
    "amount": ("amount", "gross", "gross amount"),
    "fee": ("fee", "fees", "stripe fee"),
    "refund": ("amount refunded", "refunded", "refund"),
    "status": ("status",),
    "date": ("created (utc)", "created", "date", "paid at"),
    "email": ("customer email", "email"),
}


def _col(header: list[str], name: str) -> Optional[int]:
    low = [h.strip().lower() for h in header]
    for c in COLS[name]:
        if c in low:
            return low.index(c)
    return None


def _num(v: str) -> Optional[float]:
    v = (v or "").strip()
    if not v:
        return None
    try:
        return float(re.sub(r"[^\d.\-]", "", v))
    except ValueError:
        return None


def _ts(v: str, fallback: float) -> float:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime((v or "").strip(), fmt).timestamp()
        except ValueError:
            continue
    return fallback


def import_csv(path: Path, store: Store, now: Optional[float] = None) -> dict:
    now = now if now is not None else time.time()
    rows = list(csv.reader(Path(path).read_text(encoding="utf-8-sig").splitlines()))
    if not rows:
        return {"imported": 0, "skipped": 0, "error": "the file is empty"}
    header, body = rows[0], rows[1:]
    i = {k: _col(header, k) for k in COLS}
    if i["amount"] is None or i["id"] is None or i["status"] is None:
        return {"imported": 0, "skipped": len(body),
                "error": "needs id, amount and status columns (a Stripe payments export has them)"}
    new = skipped = 0
    for n, r in enumerate(body, start=2):
        get = lambda k: r[i[k]] if i[k] is not None and i[k] < len(r) else ""
        amount = _num(get("amount"))
        if get("status").strip().lower() not in PAID or amount is None or not get("id").strip():
            skipped += 1
            continue
        detail = {"gross": amount, "fee": _num(get("fee")), "refund": _num(get("refund")) if i["refund"] is not None else None}
        email = get("email").strip().lower()
        if store.record("sale_paid", source="payments_export", source_id=f"sale:{get('id').strip()}", ts=_ts(get("date"), now),
                        task_id=hashlib.sha256(email.encode()).hexdigest()[:12] if email else "", status="paid",
                        title="Website sale", detail=detail, evidence={"file": Path(path).name, "row": n}, private=True):
            new += 1
    store.sync_ok("sales", stale_after=14 * 86400, at=now)
    return {"imported": new, "skipped": skipped, "error": ""}
