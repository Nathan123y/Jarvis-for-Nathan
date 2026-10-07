"""Local-business website campaign: discover -> verify -> audit -> qualify -> research -> build ->
check -> preview -> offer -> monitor replies -> record outcome.

Everything here is $0 to run: public/open data sources, a deterministic template renderer (no paid
text API), free static hosting for previews, and the user's own Gmail. Nothing is sent unless the
campaign was explicitly authorized (policy.py), and every external action is checked against the
kill switch, the campaign's limits and the suppression list first.
"""
from __future__ import annotations


def handlers() -> dict:
    from worker.campaign import pipeline
    return pipeline.handlers()


def seed_schedules(db) -> None:
    from worker.campaign import pipeline
    pipeline.seed_schedules(db)
