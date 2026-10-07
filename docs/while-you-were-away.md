# While You Were Away, the background worker, and the website campaign

## What runs where
- **Jarvis window**: voice, HUD, the return briefing ("What did I miss?", "Show the details", "Who wants a call?").
- **Background worker** (`python3 -m worker run`, a separate process): a durable job queue (`config/worker/jobs.db`) and the website campaign. It works with the Jarvis window closed.
- **Event store** (`config/events.db`): what the briefing counts. Shared by the window and the worker, **on the same Mac only**.

## Turn it on (no app rebuild, permissions are not reset)
1. `git pull`, reopen Jarvis.
2. `python3 -m worker install` starts the worker at login and restarts it after a crash. `status`, `stop`, `restart`, `rollback`, `uninstall` are the other commands. `python3 -m worker kill on` is the kill switch: no new jobs and no sends until `kill off`.
3. Check: `python3 -m worker status`, `python3 -m worker selftest`, then `python3 -m worker jobs`.

## When it can and cannot work
| Mac state | Worker |
|---|---|
| Logged in, screen locked, Jarvis closed | runs |
| Asleep | paused; carries on after wake. Expired outreach slots are dropped, never replayed |
| Logged out, shut down, out of battery | does nothing |

Optional keep-awake: set `worker_keep_awake` (and `worker_keep_awake_min_battery`, default 40) in the Jarvis settings file. It only holds the Mac awake while a job runs (`caffeinate`), never edits power settings, and turns off on low battery.

## Website campaign
Pipeline: discover (OpenStreetMap, free) → verify → find the real site and audit it (robots.txt respected, only observable problems) → qualify → build a template concept site → objective checks (structure, contrast, no scripts, details match the listing, phone and mobile browser render) → publish a reachable preview → compose the offer → send under the policy → read replies → record the outcome.

Setup (once):
1. **Sender name and postal address**: Plugin Settings → Product promotion (required by commercial-email law).
2. **Spam Gmail** connected in Jarvis (`gmail_account` stays `spam`).
3. **Preview hosting** (free): create a *public* GitHub repository (e.g. `jarvis-previews`), turn on Pages for `main`, then set Plugin Settings → Website campaign: repository URL and `https://<you>.github.io/jarvis-previews`. Without this, sites are built but **nothing is sent**.
4. `python3 -m worker campaign dryrun` (three made-up businesses; nothing sent or published), then `python3 -m worker campaign review`.
5. Authorize once: say "turn on the website campaign" (you answer yes/no once) or run `python3 -m worker campaign enable`.

Limits (hard ceilings): up to 20 qualified businesses per batch; at most 20 emails a day across all campaigns including the older promotion emails; Mon-Fri 9-5 Pacific; $249 one-page site, one revision; no follow-ups; $0 budget; authorization lasts 30 days and is voided by any policy change. Every email: short, labelled as an independent concept, one verified observation, working preview, exact price, postal address, "commercial message", and "reply STOP". Stops itself on Gmail errors, a complaint, 3 bounces, or an expired/revoked authorization; any opt-out suppresses that business immediately. Calls, negotiation, custom commitments and payments are never handled by Jarvis; they appear in "Who wants a call?".

Sales: Jarvis has no payment API key. Download a payments CSV and run `python3 -m worker sales import payments.csv`; the briefing then counts verified sales and says "unknown" for any missing fee.

## Always-on deployment plan (not provisioned, $0 spent)
A Mac that sleeps pauses the campaign. For true 24/7: run the same worker on an always-on machine (a spare Mac mini, or a small cloud VM, roughly $5-10 a month, **not purchased**). Requirements: copy the repo and Gmail sign-in, run one worker only, and move the event store there. SQLite files must **not** be shared between machines; the window on your laptop would then read a synced copy, which needs a small export step that is not built yet. One executor per campaign: never run the worker on two machines.

## Costs
New spending: $0. Free services used: OpenStreetMap Overpass (polite, one query at a time), GitHub Pages, your Gmail, Playwright.

## Start it by voice, and the briefing
- Say **"Jarvis, start selling websites"** (or "run the website campaign now"). If the campaign is already authorized it resumes if paused, and finds, builds and pitches right away instead of waiting for tomorrow's run; emails still only go out in the sending hours, within the daily limit. If it is not authorized yet, Jarvis asks you once to approve it. Terminal equivalent: `python3 -m worker campaign start`. "Pause the website campaign" pauses it.
- The "while you were away" briefing now says how many businesses had no website (and how many a weak one), how many concepts were built and previewed, how many offers were sent, and who replied, naming the businesses that are interested or asked for a call. Counts come only from recorded events; if reply checking is down it says so rather than reporting zero.
