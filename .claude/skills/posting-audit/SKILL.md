---
name: posting-audit
description: Audit what Jarvis actually posted to TikTok, Instagram Reels and YouTube against the goal of at least 3 videos/day on each platform. Use when asked "did the videos post", "how are posts doing", "why isn't X posting", or as a daily check on viral/clip/guide videos.
---

# Posting audit

Run the audit script from the repo root:

```
python3 .claude/skills/posting-audit/audit.py          # last 3 days
python3 .claude/skills/posting-audit/audit.py 7        # last 7 days
```

It reads `memory/viral.json` (narrated stories), `memory/clips.json` (GTA clips) and `memory/guides.json`
(GTA guides/shorts) and prints, per day, how many videos were made and posted on each platform, then lists
videos that were made but not posted everywhere.

## How to report
- Lead with the verdict per platform per day: met the 3/day goal or not, with the numbers.
- List each stuck video (title, made time, which platform failed). Do not guess the cause from the JSON alone:
  check `memory/` logs and the worker log, and ask whether the account is signed in, restricted or suspended
  (known: TikTok restricted and Instagram suspended on the game-clips side since mid-Nov, so clips go to
  YouTube Shorts only; those platforms show "off" rather than a failure for clips).
- Remember files are auto-deleted once posted everywhere, so a missing file on a posted video is normal.
- Never re-post or retry a post without asking; posting is outward-facing.
- Today's partial day is not a miss; judge it against the scheduled times (viral 09:00, 13:00, 18:00;
  guides 12:00, 16:00, 17:00, 21:00).
