# Handoff: While You Were Away + background worker + website campaign

Branches (stacked, merge in order): `claude/while-you-were-away` (#74) → `claude/background-worker` (#75) → `claude/website-campaign`.

## Implemented and tested (fixtures, no network)
- A/B: event store, return briefing, 30 min away threshold, locked-screen handling, replay, open items (41 tests).
- C: durable job queue, leases, retries, outbox with duplicate prevention and uncertain-send reconciliation, schedules with catch-up, kill switch, launchd install/uninstall/restart/rollback, keep-awake (test_worker).
- D/E: campaign pipeline, policy and authorization, preview host, message validation, reply classification, stop conditions, dry run, controls, plugin, sales CSV import (tests/test_campaign.py, 68 tests).

## Verified only in the Linux sandbox
- The worker as a real process, the dry run with a real Chromium render (mobile and desktop screenshots looked right), a git preview host against a local bare repository.

## NOT verified on a real Mac / live (needs you)
- launchd install, `ioreg` lock detection, `pmset` battery parsing, `caffeinate`.
- Overpass, real site fetching, Gmail send/read for the spam account, GitHub Pages publishing and public reachability.
- Not built: call-time suggestions from your calendar; account-level Stripe API (CSV import instead); cross-machine event sync (Phase F is a plan only).

## Live-enabled
Nothing. The campaign is created in `draft` mode with no authorization; no real prospect has been contacted. It sends only after you complete setup and authorize once.

## Still needed from you
Sender name and postal address, spam Gmail connected, a public GitHub previews repo + Pages URL, then the one-time authorization.

## Rollback
`python3 -m worker kill on`, `python3 -m worker campaign stop` (revokes authorization), `python3 -m worker uninstall`.

## Known limits found in review (not fixed)
- DNS-rebinding could in theory slip past the audit's address check (it is resolved twice). Public-only checks use "is global".
- Only one source (OpenStreetMap + the site at its email domain) verifies a business; a business with no listed website and a free-mail address is only checked against its listing.
