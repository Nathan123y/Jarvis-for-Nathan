"""Viral videos: 3D "what if" shorts, made start to finish on this Mac and posted to TikTok.

Each video (the "narrated" style, copied from the reference the user picked on 2026-11-05: "Could Manchester United
win their fifth straight game against ten blind footballers?"):
  1. Claude writes the script: a hook question, a hypothetical setup, the one rule that sets up the joke, a deadpan
     turn, staccato play-by-play, a stat-card time skip, a climax where the joke backfires, and the final score.
     Each beat is one sentence, with optional broadcast cards (panel / pill / count) and a scorebug.
  2. Gemini's TTS (voice Fenrir, nudged toward the reference's pitch and pace) reads it all in one take, cut into
     sentences at the pauses; beat timings and the word-by-word yellow captions come from the real speech.
  3. Claude writes the 3D scene (a JS module) against the kit in assets/viral/kit.js, timed to those beats, with
     hard camera cuts every 1-2 s (K.shots) in a stylised stadium / arena (K.sets.stadium, court, ring ...).
  4. A test render catches errors (fed back to Claude to fix), then Claude looks at sample frames and fixes
     what looks wrong.
  5. Headless Chrome renders every frame on the GPU; numpy mixes voice + music + sound effects; ffmpeg encodes.
  6. It posts to TikTok through a Chrome profile that the user logged into once (`python3 -m worker.viral login`).

    python3 -m worker.viral make ["topic"]     make one video now (and post it, if posting is on)
    python3 -m worker.viral queue ["topic"]    make the next slot's video now, text it to you, post it at the slot
    python3 -m worker.viral login              log in to TikTok once, in a Chrome window
    python3 -m worker.viral post FILE "caption" post a finished video
    python3 -m worker.viral status             the last videos
"""
from __future__ import annotations

import base64
import json
import math
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ASSETS = ROOT / "assets" / "viral"
STATE = ROOT / "memory" / "viral.json"
WORK = ROOT / "memory" / "viral"                    # one folder per video: script, voice, scene, frames
OUT = Path.home() / "Movies" / "Jarvis Viral"       # the finished mp4s
PROFILE = ROOT / "memory" / "tiktok_profile"        # the Chrome profile logged into TikTok
IG_PROFILE = ROOT / "memory" / "instagram_profile"  # the Chrome profile logged into the videos' Instagram page
NS = "viral_videos"
FPS = 30
SR = 48000
DEFAULTS = {"enabled": True,
            "times": "11:00,16:00,20:30",           # one video at each (TikTok's busy hours)
            "post": True,                           # post to TikTok; False = text the video to the user instead
            "style": "narrated",                    # narrated (voiceover + word captions + broadcast cards, the
                                                    # 2026-11-05 Man United vs blind footballers reference) | captions
            "voice_mode": "none",                   # captions style only: none | gemini | edge (narrated always speaks)
            "voice": "Fenrir",                      # Gemini TTS voice: closest match to the reference narrator
            "voice_speed": 1.08,                     # tempo of the narration (pitch kept)
            "voice_pitch_hz": 0,                    # nudge each take toward this median pitch (0 = leave it). Was 180:
                                                    # shifting Fenrir up made him a "squeaky chipmunk" (user, 11-05)
            # no style tag: Gemini sometimes reads a [tag] aloud ("energetic fast TikTok narrator" opened the
            # Haaland video on 2026-11-05), so the narration is sent bare
            "tts_model": "gemini-3.8-flash-tts",
            "ai_label": True,
            "claude_model": "opus",                 # the strongest Claude on the membership writes the script + 3D scene
            "claude_effort": "high",                # was medium; more thinking = better scenes (uses more of the plan's limits)
            "instagram": True,                      # also post each video as a Reel on the videos' own Instagram page
                                                    # (a new page, NOT the user's personal one; user's choice 2026-11-05)
            "handle": "whatifishowedu"}                # the TikTok account, for reading view counts                       # TikTok requires the AI-generated label on AI-made videos
CHROME_ARGS = ["--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist"]


def settings() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(NS)
    except Exception:
        stored = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in stored.items() if k in DEFAULTS and v is not None})
    return out


def load_state(path: Path = STATE) -> dict:
    try:
        st = json.loads(path.read_text())
    except (OSError, ValueError):
        st = {}
    st.setdefault("videos", [])
    st.setdefault("slots", {})
    return st


def save_state(st: dict, path: Path = STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(path)


def ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


# ── 1. the script ────────────────────────────────────────────────────────────
SCRIPT_PROMPT = """You write viral short-form videos for a faceless TikTok account. Each is a 3D simulation shown
mostly first-person, with NO narrator: the story is told by short on-screen captions, a big counter (HUD), and real
sound (ambience + sound effects). Two reference videos that went viral:

1) "What if a spoonful of neutron star landed in your backyard?" - a countdown, then captions "A teaspoon of neutron
star just landed." "It weighs about a billion tons." (mass counter racing up) "The ground can't hold it up." "It rips
the hose out of your hand." "Everything nearby starts sliding in." Twist: "Viral videos say it falls to the core."
"But that's not what would happen." "Rewind. Here's the real version." Ends on an end card that loops to the hook.
2) "What if humans evolved 1,000 years every 3 seconds?" (33 s). COLD OPEN: it starts on the wildest moment
(+10,000 years, a crowd of strange people), then snaps back to 0 years on a normal city sidewalk, coffee in hand.
Captions are tiny and punchy: "1,000 years every 3 seconds." "37 generations. Just like that." "Everyone changes.
Except you." "Wisdom teeth? Gone." "New artery in every arm." "The pigeons didn't change." "They start staring."
"You're a living fossil." "Every single one of them..." "...descends from you." Only city ambience, footsteps, and a
scream at the end. No music, no voice.

Video formats (pick the one that fits):
- WHAT IF: an impossible scenario played out with real science ("What if the Moon was 10x closer?").
- VERSUS: a hypothetical matchup or survival test ("Could prime LeBron James beat Satan in a 1v1?", "Could 100 men
  beat a gorilla?", "How long would you last on Venus?"). Real public figures are fine in harmless, fun matchups
  (never insulting, sexual or defamatory); play it like a real broadcast with stats on the HUD and a twist ending.
  The title IS the question, worded the way a viewer would type it: "Can Prime LeBron Beat Satan in a 1v1?" (never a
  statement like "Prime LeBron vs Satan. Game to 11."). Right after the hook, introduce each side with REAL stats on
  the HUD: a real person gets their best season, named ("PRIME LEBRON - 2012-13 MIAMI", "26.8 PPG - 8.0 REB - 7.3 AST",
  "56.5% FG - MVP - CHAMPION"; one stat line per beat, numbers must be correct); the other side gets its
  legendary/scientific numbers ("SATAN - AGE: ETERNAL", "HEIGHT: ABOUT 9 FT"). Then play the matchup out.
WHAT PEOPLE WATCH: fantasy, absurd, funny and unrealistic beats realistic every time. Never a boring science lesson
or a dry "what happens to your body" video. The idea alone must make someone stop scrolling and laugh or go "wait,
what?". Mix famous people, mythical creatures, gods, monsters, aliens, animals with powers and everyday life, and
play it completely straight like a real broadcast or documentary (that's the joke). Real-feeling numbers on the
HUD, escalating chaos, a funny or shocking twist, an ending that makes people comment who'd win / argue / tag a
friend. Examples of the energy: "Can Prime LeBron Beat Satan in a 1v1?", "Could 100 Men Beat Prime Mike Tyson?",
"What if Zeus joined the NBA for one season?", "Can Shaq survive one round against a T-rex?", "What if your cat
got 1,000x stronger every day?", "Could Gordon Ramsay survive a dinner shift in Hell?", "What if pigeons started
paying rent?", "Could Usain Bolt outrun a meteor?", "What if gravity turned off for 10 seconds at a Lakers game?".
{topic_line}
Don't repeat these recent videos:
{history}

TikTok rules so the account isn't banned: edgy, dark, creepy, gross and shocking are all fine; no sexual content,
no gore close-ups, nothing that teaches self-harm or dangerous stunts, no hate.

What the 3D kit can show: places - suburban backyard, busy daytime city sidewalk (crowds, pigeons, cafe tables,
first-person hand holding a coffee or phone), city street with towers, open ocean, flat plain (desert, beach, snow,
Mars, Moon), deep space (planets, stars, sun, black holes), small bedroom, indoor basketball arena with fans,
boxing ring with crowd. Things - realistic 3D humans (men and women, skin tones, hair, beards, shirts/jerseys with
numbers, shorts, boxing gloves, devil skin with horns and glowing eyes; motion-captured walk, sprint, jump, punch,
jab, get hit, fall/die, roll, dance, sit, cast a spell, swim, drive, cheer, dunk, shoot), cheering crowds, cars,
houses, trees, a basketball, glowing objects, beams, meteors. Effects - explosions, fire, smoke, sparks, debris,
camera shake, flashes. Dinosaurs (T-rex, raptors, triceratops, stegosaurus, brontosaurus, parasaurolophus; walk,
run, attack, jump, die). Not possible: talking faces, other animals besides pigeons, liquids splashing, signs.

Rules for the script:
- 20-35 seconds total: 9-14 beats (short videos get watched to the end, which is what TikTok rewards). Beat 0 is
  the hook: the title, at most 10 words, ALWAYS over a COLD OPEN of the most dramatic, strangest moment of the
  video, with motion and a loud sound from the very first frame (no empty establishing shot, no slow fade-in);
  beat 1 then snaps back to the start ("Rewind." / "0 years." / "Day 1."). Cut anything that doesn't escalate.
- Every "say" is the on-screen caption: 2-9 words, punchy, concrete, real numbers. Facts must be correct; rough
  estimates say "about". Escalate every beat. Around 60% through, a twist ("Most videos get this wrong." / "But
  that's not what happens."). The last caption lands the payoff and loops back into the hook.
- About half the beats set a "hud": {{"label": "HUMAN EVOLUTION", "kind": "count", "from": 0, "to": 1000,
  "unit": " years", "decimals": 0, "sub": "GENERATION 37"}}, or kind "timer" (counts from "from" seconds to 0 during
  the beat; good for a countdown), or kind "text" with "text": "ROUND 1". A HUD number must match its caption.
- "emph": 1-2 words of the caption shown in blue.
- Sound, per beat: "ambience" = the id of a background sound from the list below that starts at this beat and keeps
  playing until another beat changes it ("none" = silence, "keep" = carry on); "level" = its loudness from this beat
  (0.3 hushed .. 1 normal .. 2.5 roaring), e.g. swell the crowd on the big play. "sfx" = one sound effect id from the
  list (a one-shot, landing on the beat) or a synth effect: boom, whoosh, riser, hit, tick, rumble, bounce (ball on
  hardwood), dribble (three bounces), swish (net), buzzer (arena horn), squeak (sneaker), punch, whistle (referee),
  bell (boxing bell); or "none".
  "sfx2" = up to 3 more hits later in the same beat: [{{"id": "...", "at": 1.2}}] (seconds after the beat starts).
  SOUND MUST MATCH THE PICTURE LITERALLY: every sound is what you would really hear at that moment in that place, at
  that intensity. A dunk in a packed arena = a roaring crowd, never polite or golf clapping; a quiet library = near
  silence; fire = crackling flames. Read each sound's description, energy, fits and avoid notes, and never use a
  sound whose "avoid" matches the moment. If nothing in the list really fits, use "none" or a synth effect: a missing
  sound is better than a wrong one. Sound effects only on real on-screen events (impacts, reveals, the ending). "music": "tension" for a quiet dark drone under the whole video,
  or "none" (the references have none; use it only for space or empty places).
{sounds}
- "scene": one sentence on what we see during that beat (camera and action).
- "end_fact": one line of small print for the end card (a real fact backing it up).
- "caption": the TikTok caption, at most 150 characters, ending in a question that baits comments.
- "hashtags": 4-6, lowercase, without the # (mix big ones like fyp with topic ones).

Reply with only this JSON:
{{"format": "whatif|versus", "topic": "...", "title": "...", "highlight": ["..."],
  "set": "backyard|street|city|ocean|plain|space|room|court|ring", "music": "none|tension",
  "beats": [{{"say": "...", "emph": ["..."], "hud": null, "ambience": "keep", "level": 1, "sfx": "none", "sfx2": [],
             "scene": "..."}}],
  "end_fact": "...", "caption": "...", "hashtags": ["..."]}}"""


NARRATED_PROMPT = """You write viral short-form videos for a faceless TikTok account: NARRATED 3D "hypothetical" simulations.
A deadpan male narrator tells a short story over a stylised 3D broadcast (stadium, players, crowd), sentence by
sentence, with small yellow word-by-word captions, TV-style stat cards, a scorebug, and hard camera cuts every 1-2 s.
This is the reference video that went viral; copy its writing EXACTLY in style, rhythm, structure and humour:

  "Could Manchester United win their fifth straight game against ten blind footballers?"
  "In this hypothetical, United have somehow won four straight."      [card: MANCHESTER UNITED / 4 CONSECUTIVE WINS / W W W W]
  "One more, and their fans can finally declare the club is back."    [a WE'RE BACK banner in the stands]
  "Their opponents?" "Ten elite blind footballers."                   [the opponents walk out of the tunnel; pill: BLIND FOOTBALL CLUB]
  "In blind football, players track a rattling ball and rely on shouted instructions."   [sound rings around the ball]
  "Usually, the crowd stays quiet so they can hear."                  [a PLEASE KEEP QUIET board]
  "Unfortunately, this is Old Trafford."
  "Kickoff." "Bruno Fernandes gets the ball." "The entire stadium boos."
  "All ten defenders immediately turn toward him." "Bruno dribbles left." "The boos follow." "He cuts right."
  "The boos get louder."
  "The opposing coach realizes United's own fans have become a seventy-thousand-person sonar system."
  "By halftime, United have seventy-eight percent possession and zero shots on target."   [card: HALF-TIME stats]
  "In the ninetieth minute, Bruno attempts one final through ball." "The crowd groans."
  "The defenders intercept and counter."
  "United's goalkeeper charges off his line, screaming, "Keeper!"" "The striker hears him coming."
  "One touch." "A chip over the goalkeeper." "Goal."
  "United's back line appeals for offside." "The referee points to the centre circle."
  "Final score: Manchester United zero, Blind Football Club one."     [card: FULL TIME result] (video ends right there)

Why it works, and what every script must do the same way:
- HOOK (sentence 0 = the title): a yes/no question about an absurd but specific matchup, with a real team/person, a
  number and a stake ("Could X win their fifth straight game against ten blind footballers?"). No intro, no "hey".
- SETUP in 2-3 sentences: "In this hypothetical, ..." + the stakes + who the opponent is ("Their opponents?" as a
  one-line tease, then the reveal).
- THE RULE: one real fact about the scenario that sets up the joke ("In blind football, players track a rattling
  ball and rely on shouted instructions."), then the deadpan turn that breaks it ("Usually, the crowd stays quiet so
  they can hear." "Unfortunately, this is Old Trafford.").
- PLAY-BY-PLAY: very short present-tense sentences (1-6 words) like a broadcast ("Kickoff." "He cuts right.") where
  the joke escalates beat by beat through ONE clear mechanism, then a sentence that names it in a funny precise way
  ("...have become a seventy-thousand-person sonar system").
- TIME SKIP with a stat card ("By halftime, ... seventy-eight percent possession and zero shots on target.").
- CLIMAX in the final minute where the same mechanism backfires in a new way (the keeper screaming "Keeper!" gives
  himself away), a quick finish, a failed appeal, and the final score as the LAST sentence. No moral, no
  "follow for more", no question at the end. The underdog / absurd side usually wins.
- Deadpan, dry, confident. Never explain the joke. Real names, real stadiums, real-sounding numbers. Plain words.

Topic: OUT OF POCKET. The single biggest thing that gets views is a premise so unhinged and funny that people
send it to their friends and argue in the comments. Our best video so far: "Could Josh Jacobs Beat a Defensive Line
of Pregnant Women?" Aim at that energy every time: a real star or team (NFL, NBA, football clubs, boxers, any sport)
vs an opponent or condition nobody would ever put in a sentence (a defensive line of pregnant women, eleven grandmas
on mobility scooters, forty kindergarteners who've had espresso, a team of Florida men, a referee who's the
opposing team's mom, a goalkeeper who's actually a golden retriever, a stadium where the crowd is all exes, a team
of mall cops, a pitch made of ice, 100 dads who all think they could've gone pro). Then ONE funny mechanism decides
it, played completely straight and deadpan like the reference (the funnier and more specific the better: nobody
is allowed to tackle a pregnant woman, so every play just walks straight past him). Examples of the bar:
"Could Josh Jacobs Beat a Defensive Line of Pregnant Women?", "Could Prime Shaq Get a Rebound Against 50 Grandmas?",
"Could Real Madrid Score If Every Defender Was Somebody's Mom?", "Could Mike Tyson Win a Fight Where He's Not
Allowed to Hurt Anyone's Feelings?", "Could the Chiefs Beat a Team of Mall Cops Who Take It Way Too Seriously?",
"Could Messi Dribble Past 11 Toddlers Who Are All Holding Ice Cream?". Never a bland or "fair" matchup.
Edgy and inappropriate-adjacent is GOOD; the joke lands on the situation and the star's struggle, never on punching
down at a group (the underdog side wins and looks great, like the blind footballers).
Real people only in harmless, fun situations (never insulting, sexual or defamatory).
{topic_line}
Don't repeat these recent videos:
{history}

TikTok rules: no sexual content, no gore, nothing dangerous, no hate.

Rules:
- 26-36 sentences, 160-210 words in all (about 60-75 seconds spoken). Each beat is ONE sentence (or one short tease
  like "Their opponents?"). Write numbers the way they're spoken ("seventy-eight percent", "ninetieth minute").
- "say": the sentence, exactly as the narrator reads it.
- "card": null, or a broadcast graphic shown during that beat (about 5-8 beats get one; "hold": how many beats it
  stays up, 1-4):
    {{"kind": "panel", "title": "MANCHESTER UNITED", "rows": [{{"text": "4 CONSECUTIVE WINS"}}], "chips": ["W","W","W","W"], "hold": 2}}
    {{"kind": "panel", "tag": "HALF-TIME", "title": "MANCHESTER UNITED", "rows": [{{"text": "78% POSSESSION", "bar": 0.78}},
      {{"text": "0 SHOTS ON TARGET", "hi": true}}], "hold": 2}}
    {{"kind": "panel", "tag": "FULL TIME", "title": "", "rows": [{{"text": "MANCHESTER UNITED", "score": 0}},
      {{"text": "BLIND FOOTBALL CLUB", "score": 1}}], "hold": 2}}
    {{"kind": "pill", "text": "BLIND FOOTBALL CLUB"}}  (a name tag when someone is introduced)
    {{"kind": "count", "label": "CROWD NOISE", "from": 0, "to": 74000, "unit": "", "sub": "FANS BOOING"}}  (a big number)
  Chips: W / L / D / ?. "hi": true highlights a row in yellow (the punchline stat). Numbers must match the sentence.
- "score": the scorebug from this beat on: {{"home": "MUN", "away": "BFC", "h": 0, "a": 0, "clock": "00:00"}}, "hide",
  or null (unchanged). Set it at kickoff, update it on goals and time skips.
- "scene": what we see during that sentence, as 1-2 concrete camera shots (e.g. "wide aerial of the pitch, players in
  kickoff positions" / "low close-up of Bruno, number 8, receiving the ball, then the stands booing"). Show exactly
  what's being said; text in the world (banners, boards, a big screen) is possible, e.g. a WE'RE BACK tifo.
- Sound per beat: "ambience" = id of a background sound below that starts at this beat and keeps playing until
  another beat changes it ("keep" = carry on, "none" = silence); "level" 0.3 (hushed) .. 1 .. 2.5 (roaring);
  "sfx" = one sound effect id from the list or a synth effect: boo (a whole stadium booing), groan (a crowd's
  disappointed "ohh"), kick (boot on ball), whistle (referee), boom, whoosh, hit, tick, rumble, bounce, dribble,
  swish, buzzer, squeak, punch, bell; or "none". "sfx2": up to 3 more hits later in the beat [{{"id": "...", "at": 1.2}}].
  Sound must match the picture literally (a goal in a packed stadium = a roar; boos when the sentence says boos).
{sounds}
- "music": "drive" (a low, tense rhythmic bed under the whole video, like the reference), "tension", or "none".
- "set": stadium (football), court (basketball arena), ring (boxing), or street, city, backyard, ocean, plain,
  space, room for other ideas.
- "caption": the TikTok caption, at most 150 characters, ending in a question that baits comments.
- "hashtags": 4-6, lowercase, without the # (mix big ones like fyp with topic ones).

Reply with only this JSON:
{{"format": "versus", "topic": "...", "title": "<sentence 0, the hook question>", "set": "stadium", "music": "drive",
  "beats": [{{"say": "...", "card": null, "score": null, "ambience": "keep", "level": 1, "sfx": "none", "sfx2": [],
             "scene": "..."}}],
  "caption": "...", "hashtags": ["..."]}}"""


def _json_from(text: str):
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    a, b = text.find("{"), text.rfind("}")
    return json.loads(text[a:b + 1])


def claude_run(prompt: str, images: Optional[list] = None, timeout_s: float = 900, effort: str = "") -> str:
    """One `claude -p` turn on the membership login (never an API key), with time for long code. "" on failure."""
    import os
    import tempfile
    from core import gemini
    exe = gemini._claude_bin()
    if gemini._claude_off() or not exe or gemini._cooling(gemini.CLAUDE):
        return ""
    with tempfile.TemporaryDirectory(prefix="jarvis-viral-") as tmp:
        names = []
        for i, img in enumerate(images or []):
            name = f"frames-{i + 1}{Path(img).suffix}"
            shutil.copy(img, Path(tmp) / name)
            names.append(name)
        if names:
            prompt = "First read these image files in the current folder: " + ", ".join(names) + "\n\n" + prompt
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        cfg = settings()
        model = ["--model", cfg["claude_model"]] if cfg.get("claude_model") else []
        try:
            r = subprocess.run([exe, "-p", "--output-format", "json", "--system-prompt", gemini._ONE_SHOT_SYSTEM,
                                "--tools", "Read" if names else "", "--strict-mcp-config", "--setting-sources", "",
                                "--no-session-persistence", "--disable-slash-commands",
                                "--effort", effort or cfg.get("claude_effort") or "medium"] + model,
                               input=prompt, capture_output=True, text=True, cwd=tmp, env=env, timeout=timeout_s)
            out = json.loads(r.stdout)
        except Exception as exc:
            print(f"[viral] Claude: {type(exc).__name__}: {str(exc)[:120]}")
            return ""
    result = str(out.get("result") or "").strip()
    if out.get("is_error"):
        if "limit" in result.lower():
            gemini._cool(gemini.CLAUDE, gemini._CLAUDE_LIMIT_COOLDOWN_S)
        print(f"[viral] Claude: {result[:120]}")
        return ""
    return result


class ClaudeBusy(RuntimeError):
    """Claude is at its usage limit; the video waits for it instead of being made worse."""


def ask_claude(prompt: str, timeout_s: float = 900, images: Optional[list] = None) -> str:
    """Claude on the membership. The Gemini fallback made poor videos (dark, empty scenes), so by default a video
    waits for Claude instead (setting gemini_fallback = True brings the fallback back)."""
    out = claude_run(prompt, images, timeout_s)
    if out:
        return out
    if not settings().get("gemini_fallback"):
        raise ClaudeBusy("Claude is at its usage limit right now")
    from core import gemini
    contents = prompt
    if images:
        from google.genai import types
        contents = [types.Part.from_bytes(data=Path(p).read_bytes(), mime_type="image/jpeg") for p in images] + [prompt]
    return gemini.text(contents, tier="gemini-3.5-flash", timeout_ms=int(timeout_s * 1000)) or ""


SETS = ("backyard", "street", "city", "ocean", "plain", "space", "room", "court", "ring", "stadium")
CHIPS = ("W", "L", "D", "?")


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def clean_card(c) -> Optional[dict]:
    """A broadcast graphic for one beat (panel / pill / count / timer / text), or None."""
    if not isinstance(c, dict):
        return None
    kind = c.get("kind") if c.get("kind") in ("panel", "pill", "count", "timer", "text") else (
        "panel" if c.get("rows") or c.get("title") else "text")
    out = {"kind": kind, "hold": int(max(1, min(4, _num(c.get("hold"), 1))))}
    if kind == "panel":
        rows = []
        for r in (c.get("rows") or [])[:3]:
            if not isinstance(r, dict) or not str(r.get("text") or "").strip():
                continue
            row = {"text": str(r["text"]).strip()[:30]}
            if r.get("bar") is not None:
                row["bar"] = max(0.0, min(1.0, _num(r.get("bar"))))
            if r.get("score") is not None:
                row["score"] = str(r["score"])[:3]
            if r.get("hi"):
                row["hi"] = True
            rows.append(row)
        out.update(title=str(c.get("title") or "").strip()[:28], tag=str(c.get("tag") or "").strip()[:14], rows=rows,
                   chips=[str(x).upper() for x in (c.get("chips") or []) if str(x).upper() in CHIPS][:6])
        if not rows and not out["chips"] and not out["title"]:
            return None
    elif kind in ("pill", "text"):
        out["text"] = str(c.get("text") or c.get("title") or "").strip()[:28]
        if not out["text"]:
            return None
    else:
        out.update({k: str(c.get(k) or "")[:30] for k in ("label", "unit", "sub")})
        out.update({"from": _num(c.get("from")), "to": _num(c.get("to")), "decimals": int(max(0, min(3, _num(c.get("decimals")))))})
        if max(abs(out["from"]), abs(out["to"])) >= 1e15:
            out = {"kind": "text", "hold": out["hold"], "text": out["sub"] or out["label"]}
    return out


def clean_score(v):
    """The scorebug from a beat on: a dict, "hide", or None (unchanged)."""
    if v == "hide":
        return "hide"
    if not isinstance(v, dict):
        return None
    return {"home": str(v.get("home") or "")[:4].upper(), "away": str(v.get("away") or "")[:4].upper(),
            "h": int(_num(v.get("h"))), "a": int(_num(v.get("a"))), "clock": str(v.get("clock") or "")[:6]}


def clean_script(s: dict, library: Optional[dict] = None, style: str = "captions") -> dict:
    library = library if library is not None else {x["id"]: x for x in sound_library()}
    if not isinstance(s, dict) or not s.get("title") or not isinstance(s.get("beats"), list) or len(s["beats"]) < 5:
        raise ValueError("the script came back incomplete")

    def amb(v):
        v = str(v or "keep")
        return v if v in ("keep", "none") or library.get(v, {}).get("kind") == "ambient" else "keep"

    def fx(v):
        v = str(v or "none")
        return v if v in SFX or v in library else "none"

    def more(v):              # extra hits inside the beat: [{"id": ..., "at": seconds after the beat starts}]
        out = []
        for x in (v if isinstance(v, list) else []):
            sid, at = (x.get("id"), x.get("at", 0)) if isinstance(x, dict) else (x, 0)
            try:
                at = max(0.0, min(4.0, float(at or 0)))
            except (TypeError, ValueError):
                at = 0.0
            if fx(sid) != "none":
                out.append({"id": fx(sid), "at": at})
        return out[:3]

    def level(v):
        try:
            return max(.3, min(2.5, float(v)))
        except (TypeError, ValueError):
            return 1.0

    beats = [{"say": str(b.get("say") or "").strip(), "emph": [str(x) for x in (b.get("emph") or [])][:3],
              "hud": b.get("hud") if isinstance(b.get("hud"), dict) else None,
              "ambience": amb(b.get("ambience")), "level": level(b.get("level", 1)), "sfx": fx(b.get("sfx")),
              "sfx2": more(b.get("sfx2")), "scene": str(b.get("scene") or "")}
             | ({"card": clean_card(b.get("card") or b.get("hud")), "score": clean_score(b.get("score"))} if style == "narrated" else {})
             for b in s["beats"] if isinstance(b, dict) and str(b.get("say") or "").strip()]
    beats = beats[:40 if style == "narrated" else 22]
    if style == "narrated":            # the first spoken sentence IS the hook; nothing gets inserted
        for b in beats:
            b["hud"] = None
        s = dict(s, title=beats[0]["say"])
    elif beats[0]["say"].strip().lower() != str(s["title"]).strip().lower():
        beats.insert(0, {"say": s["title"], "emph": [], "hud": None, "ambience": "keep", "level": 1.0, "sfx": "none", "sfx2": [],
                         "scene": "opening"})
    for b in beats:
        h = b["hud"]
        if h:
            h["kind"] = h.get("kind") if h.get("kind") in ("count", "timer", "text") else "text"
            for k in ("from", "to"):
                try:
                    h[k] = float(h.get(k) or 0)
                except (TypeError, ValueError):
                    h[k] = 0.0
            if h["kind"] != "text" and max(abs(h["from"]), abs(h["to"])) >= 1e15:
                h["kind"], h["text"] = "text", h.get("sub") or ""
    tags = [re.sub(r"[^a-z0-9_]", "", str(t).lower().lstrip("#")) for t in s.get("hashtags") or []]
    return {"style": style, "format": "versus" if s.get("format") == "versus" else "whatif",
            "topic": str(s.get("topic") or s["title"]), "title": str(s["title"]).strip(),
            "highlight": [str(x) for x in (s.get("highlight") or [])][:2],
            "set": s.get("set") if s.get("set") in SETS else "backyard",
            "music": s.get("music") if s.get("music") in ("tension", "drive") else "none",
            "beats": beats, "end_fact": str(s.get("end_fact") or "")[:160],
            "caption": str(s.get("caption") or s["title"])[:150], "hashtags": [t for t in tags if t][:6]}


def write_script(history: list, topic: str = "", ask: Callable = ask_claude, style: str = "captions") -> dict:
    topic_line = f"This video's topic: {topic}" if topic else "Choose the topic yourself."
    hist = "\n".join(f"- {h}" for h in history[-40:]) or "- (none yet)"
    prompt = NARRATED_PROMPT if style == "narrated" else SCRIPT_PROMPT
    last = None
    for _ in range(2):
        try:
            return clean_script(_json_from(ask(prompt.format(topic_line=topic_line, history=hist,
                                                             sounds=sounds_text()), timeout_s=420)), style=style)
        except ClaudeBusy:
            raise
        except Exception as exc:                 # one more try on a malformed reply
            last = exc
    raise RuntimeError(f"couldn't get a script: {last}")


# ── 2. the voice ─────────────────────────────────────────────────────────────
TTS_MODELS = ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts")   # 10 free requests a day each
EDGE_VOICE = "en-US-GuyNeural"                 # the closest Edge voice to the reference narrator


def gemini_tts(text: str, model: str, voice: str) -> bytes:
    """Text to wav bytes with Gemini's TTS. The text goes in bare: any style note in the prompt gets read out
    loud, and these models refuse a system instruction."""
    from core import gemini
    from google.genai import types
    client = gemini.client(timeout_ms=180000)                # keep a reference: a dropped client closes mid-request
    r = client.models.generate_content(
        model=model, contents=text,
        config=types.GenerateContentConfig(response_modalities=["AUDIO"], speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)))))
    data = r.candidates[0].content.parts[0].inline_data.data
    if data[:4] == b"RIFF":
        return data
    import io
    buf = io.BytesIO()                                    # raw 24 kHz PCM
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(data)
    return buf.getvalue()


def split_by_pauses(x, rate: int, words, min_gap: float = .1, lines: Optional[list] = None):
    """Cuts one recording of n lines into n pieces at pauses. Every pause of at least `min_gap` is a candidate cut;
    a dynamic programme picks the n-1 cuts whose pieces best match each line's expected length (from its letters,
    with extra time for commas) while preferring the longest pauses. None when no cut set fits the lines."""
    import numpy as np
    n = len(words)
    if n == 1:
        return [x]
    hop = int(rate * .01)
    rms = np.array([np.sqrt(np.mean(x[i:i + hop] ** 2)) for i in range(0, len(x) - hop, hop)])
    if not len(rms):
        return None
    quiet = rms < max(.004, np.percentile(rms, 90) * .06)
    runs, i = [], 0                                       # (length in s, centre sample) of every pause
    while i < len(quiet):
        if quiet[i]:
            j = i
            while j < len(quiet) and quiet[j]:
                j += 1
            if (j - i) * .01 >= min_gap and i > 0 and j < len(quiet):
                runs.append(((j - i) * .01, ((i + j) // 2) * hop))
            i = j
        else:
            i += 1
    if len(runs) < n - 1:
        return None
    weight = ([len(re.sub(r"\W", "", l)) + 4 * len(re.findall(r"[,;:]", l)) + 3 for l in lines] if lines
              else [w * 5.0 for w in words])
    total = len(x) / rate
    expect = [total * w / sum(weight) for w in weight]
    pos = [0] + [c for _, c in runs] + [len(x)]
    gap = [0.0] + [g for g, _ in runs] + [0.0]
    m = len(pos)
    INF = float("inf")
    # best[k][j]: lowest cost with line k ending at cut j; the pieces must stay in order
    best = [[INF] * m for _ in range(n)]
    back = [[-1] * m for _ in range(n)]

    def cost(a, b, k):
        d = (pos[b] - pos[a]) / rate
        return ((d - expect[k]) / (expect[k] + .35)) ** 2
    for j in range(1, m):
        best[0][j] = cost(0, j, 0) - .6 * gap[j]
    for k in range(1, n):
        lo = k                                            # at least k cuts before
        for j in range(lo + 1, m):
            if k == n - 1 and j != m - 1:
                continue
            bonus = -.6 * gap[j] if j != m - 1 else 0
            for a in range(lo, j):
                if best[k - 1][a] == INF:
                    continue
                c = best[k - 1][a] + cost(a, j, k) + bonus
                if c < best[k][j]:
                    best[k][j], back[k][j] = c, a
    if best[n - 1][m - 1] == INF:
        return None
    cuts, j = [], m - 1
    for k in range(n - 1, 0, -1):
        j = back[k][j]; cuts.append(j)
    cuts = [pos[c] for c in reversed(cuts)]
    pieces = [x[a:b] for a, b in zip([0] + cuts, cuts + [len(x)])]
    for p, e in zip(pieces, expect):                      # a piece far off its line's length = a bad cut
        if not (e * .4 - .15 <= len(p) / rate <= e * 2.4 + .3):
            return None
    return pieces


def edge_tts(text: str, out: Path, rate: str = "-6%") -> None:
    import asyncio
    import edge_tts as et
    mp3 = out.with_suffix(".mp3")
    asyncio.run(et.Communicate(text, EDGE_VOICE, rate=rate).save(str(mp3)))
    subprocess.run([ffmpeg(), "-loglevel", "error", "-y", "-i", str(mp3), "-ac", "1", "-ar", "24000", str(out)], check=True)
    mp3.unlink(missing_ok=True)


def say_tts(text: str, out: Path) -> None:
    aiff = out.with_suffix(".aiff")
    subprocess.run(["say", "-v", "Daniel", "-r", "165", "-o", str(aiff), text], check=True, timeout=60)
    subprocess.run([ffmpeg(), "-loglevel", "error", "-y", "-i", str(aiff), "-ac", "1", "-ar", "24000", str(out)], check=True)
    aiff.unlink(missing_ok=True)


def _write_wav(path: Path, x, rate: int) -> None:
    import numpy as np
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())


def median_f0(x, rate: int) -> float:
    """the median pitch (Hz) of the voiced parts of a recording; 0 when it can't tell"""
    import numpy as np
    step = max(1, rate // 16000)
    x = x[::step]; sr = rate // step
    hop, win, out = sr // 100, sr // 20, []
    frames = range(0, len(x) - win, hop)
    rms = np.array([np.sqrt(np.mean(x[i:i + win] ** 2)) for i in frames])
    if not len(rms):
        return 0.0
    thr = np.percentile(rms, 80) * .5
    lo, hi = sr // 320, sr // 65
    for k, i in enumerate(frames):
        if rms[k] < thr:
            continue
        fr = x[i:i + win] * np.hanning(win)
        ac = np.correlate(fr, fr, "full")[win - 1:]
        j = lo + int(np.argmax(ac[lo:hi]))
        if ac[j] > .7 * ac[0]:
            out.append(sr / j)
    return float(np.median(out)) if len(out) > 20 else 0.0


def _tempo(path: Path, factor: float, pitch_hz: float = 0) -> None:
    """speeds a wav up (or down) in place keeping its pitch, and nudges its pitch toward `pitch_hz` (by at most
    +4 / -2 semitones; 0 = leave the pitch alone)"""
    import numpy as np
    with wave.open(str(path), "rb") as w:
        rate, raw = w.getframerate(), w.readframes(w.getnframes())
    shift = 1.0
    if pitch_hz:
        f0 = median_f0(np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768, rate)
        if f0:
            shift = max(2 ** (-2 / 12), min(2 ** (4 / 12), pitch_hz / f0))
            if abs(shift - 1) < .02:
                shift = 1.0
    if abs(factor - 1) < .005 and shift == 1.0:
        return
    chain = f"atempo={factor:.3f}" if shift == 1.0 else f"asetrate={rate * shift:.0f},aresample={rate},atempo={factor / shift:.4f}"
    tmp = path.with_suffix(".tempo.wav")
    subprocess.run([ffmpeg(), "-loglevel", "error", "-y", "-i", str(path), "-filter:a", chain, str(tmp)], check=True)
    tmp.replace(path)


def voice_lines(lines: list[str], job: Path, cfg: dict, log: Callable = print) -> list[Path]:
    """Every line in ONE voice. Gemini reads the whole narration in one request (its free tier allows 10 a
    day), cut into lines at the pauses; else Microsoft's Edge voice line by line; else the Mac's `say`."""
    paths = [job / f"voice_{i:02d}.wav" for i in range(len(lines))]
    words = [max(1, len(l.split())) for l in lines]
    models = [] if cfg["tts_model"] == "none" else [cfg["tts_model"]] + [m for m in TTS_MODELS if m != cfg["tts_model"]]
    for model in models:
        try:
            whole = job / "voice_all.wav"
            whole.write_bytes(gemini_tts("\n\n".join(lines), model, cfg["voice"]))   # bare text: no style notes
            _tempo(whole, float(cfg.get("voice_speed") or 1),
                   float(cfg.get("voice_pitch_hz") or 0) if cfg.get("style") == "narrated" else 0)
            with wave.open(str(whole), "rb") as w:
                rate = w.getframerate()
            import numpy as np
            with wave.open(str(whole), "rb") as w:
                x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
            pieces = split_by_pauses(x, rate, words, lines=lines)
            if pieces is None:
                log(f"viral: couldn't cut {model}'s narration into lines cleanly; trying the next voice")
                continue
            for p, piece in zip(paths, pieces):
                _write_wav(p, piece, rate)
            return paths
        except Exception as exc:
            log(f"viral: {model} voice failed ({str(exc)[:90]})")
    try:
        speed = float(cfg.get("voice_speed") or 1)
        for text, p in zip(lines, paths):
            edge_tts(text, p, rate=f"{round((speed * 1.03 - 1) * 100):+d}%" if cfg.get("style") == "narrated" else "-6%")
        log("viral: voiced with the Edge narrator")
        return paths
    except Exception as exc:
        log(f"viral: the Edge voice failed ({str(exc)[:90]}); using the Mac's voice")
    for text, p in zip(lines, paths):
        say_tts(text, p)
    return paths


def read_wav(path: Path):
    """mono float32 at SR"""
    import numpy as np
    with wave.open(str(path), "rb") as w:
        n, rate, ch, width = w.getnframes(), w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(n)
    x = np.frombuffer(raw, dtype={1: np.int8, 2: np.int16, 4: np.int32}[width]).astype(np.float32)
    x /= float(2 ** (8 * width - 1))
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    x = _trim(x, rate)
    if rate != SR:
        t_new = np.arange(int(len(x) * SR / rate)) / SR
        x = np.interp(t_new, np.arange(len(x)) / rate, x).astype(np.float32)
    return x


def _trim(x, rate: int, thresh: float = 0.01):
    import numpy as np
    loud = np.where(np.abs(x) > thresh)[0]
    if not len(loud):
        return x
    a, b = max(0, loud[0] - int(.03 * rate)), min(len(x), loud[-1] + int(.08 * rate))
    return x[a:b]


def timeline(lengths: list, hook_min: float = 3.2, gap: float = 0.38, end_card: float = 2.6) -> tuple[list, float]:
    """Beat start/end times from the spoken lengths: each beat lasts its line plus a breath; the hook at least
    `hook_min` (the countdown needs a moment). Returns (beats [(start, end)], total duration)."""
    t, out = 0.25, []
    for i, n in enumerate(lengths):
        d = max(n + gap, hook_min if i == 0 else 1.6)
        out.append((round(t, 3), round(t + d, 3)))
        t += d
    return out, round(t + 0.4 + end_card, 3)


def narrated_timeline(lengths: list, says: list, gap: float = .24, tail: float = 1.6) -> tuple[list, float]:
    """Narrated style: sentences back to back with the reference's short breaths (a question gets a longer one, so
    the tease lands); the last line holds a moment, then the video ends (TikTok loops it)."""
    t, out = .1, []
    for n, say in zip(lengths, says):
        g = gap + (.22 if str(say).rstrip().endswith("?") else 0)
        d = max(n + g, .55)
        out.append((round(t, 3), round(t + d, 3)))
        t += d
    return out, round(t + tail, 3)


def caption_chunks(say: str, start: float, spoken: float, end: float, first: bool = False) -> list[dict]:
    """A sentence as on-screen chunks of 1-3 words timed to the speech (by word length; a comma or full stop is a
    pause), like the reference's small yellow captions. Each chunk stays up until the next one."""
    words = str(say).split()
    if not words:
        return []
    groups, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= 3 or len(" ".join(cur)) >= 20 or re.search(r"[,.?!:;]$", w):
            groups.append(cur); cur = []
    if cur:
        groups.append(cur)
    weight = lambda w: len(re.sub(r"\W", "", w)) + 2 + (3 if re.search(r"[,.?!:;]$", w) else 0)
    total = sum(weight(w) for w in words)
    t0, acc, out = start + .05, 0, []
    for g in groups:
        a = t0 + spoken * acc / total
        acc += sum(weight(w) for w in g)
        out.append({"text": " ".join(g), "t0": round(0.0 if first and not out else a, 3)})
    for i, c in enumerate(out):
        c["t1"] = out[i + 1]["t0"] if i + 1 < len(out) else round(end, 3)
    return out


# ── the sound library (public-domain clips from Wikimedia Commons, labelled by Gemini) ─────────────────
SFX_DIR = ROOT / "memory" / "viral_sfx"


def sound_library() -> list[dict]:
    try:
        return json.loads((SFX_DIR / "index.json").read_text())
    except (OSError, ValueError):
        return []


def sounds_text() -> str:
    lib = sound_library()

    def line(x):
        extra = "".join(f"; {k}: {', '.join(x[k]) if isinstance(x[k], list) else x[k]}" for k in ("fits", "avoid") if x.get(k))
        e = f", energy {x['energy']}/5" if x.get("energy") else ""
        return f"  {x['id']}: {x['what']} ({x['dur']:.0f}s{e}{extra})"
    amb = [line(x) for x in lib if x["kind"] == "ambient"]
    one = [line(x) for x in lib if x["kind"] == "oneshot"]
    return ("  Background sounds (ambience ids):\n" + ("\n".join(amb) or "  (none yet)") +
            "\n  One-shot sound effects (sfx ids):\n" + ("\n".join(one) or "  (none yet)"))


def load_sound(sid: str):
    """a library clip as mono float32 at SR"""
    import numpy as np
    f = SFX_DIR / f"{sid}.mp3"
    raw = subprocess.run([ffmpeg(), "-loglevel", "quiet", "-i", str(f), "-f", "s16le", "-ac", "1", "-ar", str(SR), "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768


def caption_lengths(beats: list) -> list[float]:
    """Seconds each caption stays up without a narrator: enough to read it, never dragging."""
    out = []
    for i, b in enumerate(beats):
        words = len(str(b["say"]).split())
        out.append(min(3.6, max(2.6 if i == 0 else 1.5, 0.9 + words * 0.3)))
    return out


# ── 3. music and sound effects ───────────────────────────────────────────────
SFX = ("none", "boom", "whoosh", "riser", "hit", "tick", "rumble", "bounce", "dribble", "swish", "buzzer", "squeak", "punch", "whistle",
       "bell", "boo", "groan", "kick")


def _crowd_vowel(dur: float, rng, f1: float, f2: float, voices: int = 46, glide: float = 0.0, attack: float = .5):
    """a stadium-sized crowd holding one vowel: many detuned voices, each a few formant-shaped harmonics"""
    import numpy as np
    n = int(SR * dur); t = np.arange(n) / SR
    out = np.zeros(n, np.float32)
    for v in range(voices):
        f0 = rng.uniform(92, 175) * (1 + rng.uniform(-.02, .02))
        bend = 1 - glide * np.clip((t - rng.uniform(.1, .4)) / max(.3, dur - .4), 0, 1)
        vib = 1 + .012 * np.sin(2 * np.pi * rng.uniform(4, 6.5) * t + rng.uniform(0, 6))
        ph = 2 * np.pi * np.cumsum(f0 * bend * vib) / SR
        on = rng.uniform(0, attack)
        env = np.clip((t - on) / .25, 0, 1) * np.clip((dur - t) / rng.uniform(.25, .7), 0, 1)
        voice = np.zeros(n, np.float32)
        for k in range(1, 15):
            fk = f0 * k
            g = np.exp(-((fk - f1) / 140) ** 2) + .55 * np.exp(-((fk - f2) / 220) ** 2) + .04
            voice += np.sin(k * ph) * g / k ** .3
        out += voice * env * rng.uniform(.6, 1)
    out += _lowpass(rng.standard_normal(n).astype(np.float32), .08) * .6 * np.clip(t / attack, 0, 1) * np.clip((dur - t) / .5, 0, 1)
    return (_lowpass(out, .25) / voices * 2.2).astype(np.float32)


def sfx(kind: str, seed: int = 0):
    import numpy as np
    rng = np.random.default_rng(seed)
    if kind == "boom":
        n = int(SR * 3.2); t = np.arange(n) / SR
        f = 32 + 60 * np.exp(-t * 4)
        body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 1.6)
        crack = rng.standard_normal(n) * np.exp(-t * 9) * .5
        return (body * .7 + _lowpass(crack, 0.15)).astype(np.float32)
    if kind == "hit":
        n = int(SR * 1.2); t = np.arange(n) / SR
        return (np.sin(2 * np.pi * 55 * t) * np.exp(-t * 5) + _lowpass(rng.standard_normal(n), .3) * np.exp(-t * 14) * .6).astype(np.float32)
    if kind in ("whoosh", "riser"):
        dur = 1.1 if kind == "whoosh" else 2.4
        n = int(SR * dur); t = np.arange(n) / SR
        env = np.sin(np.pi * t / dur) ** 2 if kind == "whoosh" else (t / dur) ** 2.5
        noise = rng.standard_normal(n)
        out = np.zeros(n, np.float32)
        for k in range(8):                               # a sweeping band of noise
            a, b = k * n // 8, (k + 1) * n // 8
            cut = .012 + .07 * (k / 7 if kind == "riser" else math.sin(math.pi * k / 7))   # dark, airy, not static
            out[a:b] = _lowpass(noise[a:b], cut)
        out = _lowpass(out, .12)
        return (out * env * (1.4 if kind == "whoosh" else 1.0)).astype(np.float32)
    if kind == "tick":
        n = int(SR * .25); t = np.arange(n) / SR
        return (np.sin(2 * np.pi * 1800 * t) * np.exp(-t * 60) * .35).astype(np.float32)
    if kind == "rumble":
        n = int(SR * 4); t = np.arange(n) / SR
        return (_lowpass(rng.standard_normal(n), .01) * 6 * np.sin(np.pi * t / 4) ** 2).astype(np.float32)
    if kind in ("bounce", "dribble"):                    # a basketball on hardwood: a ringing low thump (x3 for dribble)
        one_n = int(SR * .35); t = np.arange(one_n) / SR
        one = (np.sin(2 * np.pi * (95 + 40 * np.exp(-t * 30)) * t) * np.exp(-t * 18)
               + np.sin(2 * np.pi * 340 * t) * np.exp(-t * 40) * .25
               + _lowpass(rng.standard_normal(one_n), .25) * np.exp(-t * 90) * .5)
        if kind == "bounce":
            return one.astype(np.float32)
        out = np.zeros(int(SR * 1.5), np.float32)
        for k, at in enumerate((0, .45, .9)):
            a = int(at * SR); out[a:a + one_n] += one * (1 - .1 * k)
        return out
    if kind == "swish":                                  # ball through the net: a soft airy brush
        n = int(SR * .45); t = np.arange(n) / SR
        x = rng.standard_normal(n); hp = x - _lowpass(x, .2)
        return (_lowpass(hp, .6) * np.sin(np.pi * t / .45) ** 1.5 * .9).astype(np.float32)
    if kind == "buzzer":                                 # arena horn
        n = int(SR * 1.3); t = np.arange(n) / SR
        w = sum(np.sign(np.sin(2 * np.pi * f * t)) * a for f, a in ((233, 1), (466, .5), (349, .4)))
        env = np.minimum(1, t * 60) * np.minimum(1, (1.3 - t) * 12)
        return (_lowpass(w, .35) * env * .35).astype(np.float32)
    if kind == "squeak":                                 # sneaker on the court
        n = int(SR * .22); t = np.arange(n) / SR
        f = 2100 + 900 * np.sin(np.pi * t / .22)
        return (np.sin(2 * np.pi * np.cumsum(f) / SR) * np.sin(np.pi * t / .22) * .3).astype(np.float32)
    if kind == "punch":                                  # glove on body: thud + slap
        n = int(SR * .5); t = np.arange(n) / SR
        return (np.sin(2 * np.pi * 70 * t) * np.exp(-t * 22) + _lowpass(rng.standard_normal(n), .5) * np.exp(-t * 45) * .8).astype(np.float32)
    if kind == "whistle":                                # referee whistle
        n = int(SR * .7); t = np.arange(n) / SR
        f = 2900 + 120 * np.sign(np.sin(2 * np.pi * 32 * t))
        env = np.minimum(1, t * 40) * np.minimum(1, (.7 - t) * 15)
        return (np.sin(2 * np.pi * np.cumsum(f) / SR) * env * .25).astype(np.float32)
    if kind == "boo":                                    # a whole stadium booing ("boooo"), about 3 s
        x = _crowd_vowel(3.2, rng, 330, 800, attack=.45)
        return (x * (1 + .25 * np.sin(2 * np.pi * .9 * np.arange(len(x)) / SR))).astype(np.float32)
    if kind == "groan":                                  # a crowd's disappointed "ohhh", sliding down
        return _crowd_vowel(1.9, rng, 520, 950, glide=.22, attack=.18)
    if kind == "kick":                                   # boot on ball: a hollow thump and a leathery slap
        n = int(SR * .4); t = np.arange(n) / SR
        thump = np.sin(2 * np.pi * (140 + 160 * np.exp(-t * 60)) * t) * np.exp(-t * 26)
        slap = _lowpass(rng.standard_normal(n), .55) * np.exp(-t * 160) * .9
        return ((thump + slap) * .9).astype(np.float32)
    if kind == "bell":                                   # boxing ring bell
        n = int(SR * 2.2); t = np.arange(n) / SR
        return (sum(np.sin(2 * np.pi * f * t) * a for f, a in ((880, 1), (2210, .5), (3300, .3))) * np.exp(-t * 2.2) * .3).astype(np.float32)
    return np.zeros(1, np.float32)


def _lowpass(x, cut: float):
    """one-pole low-pass; cut 0..1 of Nyquist-ish"""
    import numpy as np
    from scipy.signal import lfilter
    a = max(1e-4, min(.99, cut))
    return lfilter([a], [1, a - 1], x).astype(np.float32)


def music(seconds: float, seed: int = 0):
    """Dark ambient bed: a slow minor chord pad over a sub drone, with a soft pulse that builds."""
    import numpy as np
    rng = np.random.default_rng(seed)
    n = int(seconds * SR); t = np.arange(n) / SR
    roots = [[57, 60, 64], [53, 57, 60], [48, 52, 55], [55, 59, 62]]       # Am F C G
    if seed % 2:
        roots = [[50, 53, 57], [46, 50, 53], [53, 57, 60], [48, 52, 55]]   # Dm Bb F C
    hz = lambda m: 440 * 2 ** ((m - 69) / 12)
    out = np.zeros(n, np.float32)
    bar = 6.0
    for i in range(int(seconds / bar) + 1):
        chord = roots[i % 4]
        a, b = int(i * bar * SR), min(n, int((i + 1) * bar * SR + SR * 1.5))
        if a >= n:
            break
        tt = t[a:b] - i * bar
        env = np.minimum(1, tt / 1.8) * np.minimum(1, np.maximum(0, (bar + 1.5 - tt) / 1.8))
        for m in chord:
            for det in (-0.12, 0.12):
                f = hz(m - 12) * 2 ** (det / 12)
                out[a:b] += (np.sin(2 * np.pi * f * tt) + .3 * np.sin(4 * np.pi * f * tt)) * env * .05
        out[a:b] += np.sin(2 * np.pi * hz(chord[0] - 24) * tt) * env * .06
    build = .4 + .6 * (t / max(seconds, 1))
    pulse = (np.sin(2 * np.pi * 1.0 * t) > .96).astype(np.float32)
    pulse = _lowpass(pulse * rng.uniform(.8, 1, n).astype(np.float32), .05) * 1.2
    out = out * build + pulse * build * .5
    air = _lowpass(_lowpass(rng.standard_normal(n).astype(np.float32), .01), .05) * .25
    return (out + air).astype(np.float32)


def music_drive(seconds: float, seed: int = 0):
    """The reference's bed: a low, tense, rhythmic electronic loop (~120 bpm) - muted kick, off-beat hats, a minor
    eighth-note bass - that builds toward the end and stops dead on the last line."""
    import numpy as np
    rng = np.random.default_rng(seed)
    n = int(seconds * SR); t = np.arange(n) / SR
    pad = music(seconds, seed)                           # the minor pad underneath
    out = np.zeros(n, np.float32); out[:min(n, len(pad))] = pad[:n] * .55
    beat = .5
    roots = [45, 41, 48, 43] if seed % 2 == 0 else [38, 46, 41, 36]
    hz = lambda m: 440 * 2 ** ((m - 69) / 12)
    kick_n = int(SR * .3); kt = np.arange(kick_n) / SR
    kick = np.sin(2 * np.pi * (48 + 70 * np.exp(-kt * 30)) * kt) * np.exp(-kt * 11)
    hat_n = int(SR * .06); ht = np.arange(hat_n) / SR
    hat_src = rng.standard_normal(hat_n); hat = (hat_src - _lowpass(hat_src, .3)) * np.exp(-ht * 70) * .22
    note_n = int(SR * beat / 2); nt = np.arange(note_n) / SR
    for i in range(int(seconds / (beat / 2))):
        a = int(i * beat / 2 * SR)
        if a >= n:
            break
        prog = a / n
        bar = int(i / 16) % 4
        f = hz(roots[bar]) * (2 if i % 8 == 6 else 1)
        saw = sum(np.sin(2 * np.pi * f * k * nt) / k for k in range(1, 6))
        note = saw * np.exp(-nt * 9) * (.10 + .06 * prog)
        out[a:a + note_n] += note[:max(0, n - a)]
        if i % 2 == 0 and prog > .08:
            out[a:a + kick_n] += kick[:max(0, n - a)] * (.35 + .3 * prog)
        if i % 2 == 1 and prog > .25:
            out[a:a + hat_n] += hat[:max(0, n - a)] * (.6 + .8 * prog)
    return _lowpass(out, .5).astype(np.float32)


def mix(voice: Optional[list], beats: list, script: dict, duration: float, out: Path, seed: int = 0) -> Path:
    """voice lines (or None: captions only) + ambience beds + sound effects + optional drone, to one wav."""
    import numpy as np
    n = int(duration * SR) + SR
    vtrack = np.zeros(n, np.float32)
    fx = np.zeros(n, np.float32)
    amb = np.zeros(n, np.float32)
    lib = {x["id"]: x for x in sound_library()}
    cache: dict = {}

    def clip(sid):
        if sid not in cache:
            try:
                x = load_sound(sid)
                cache[sid] = x / (float(np.sqrt(np.mean(x ** 2))) + 1e-6)         # loudness-matched
            except Exception:
                cache[sid] = None
        return cache[sid]

    # ambience: each one plays from its beat until the next change, looped, with half-second crossfades
    spans, cur = [], ("none", 0.0)
    gain = np.ones(n, np.float32)                       # per-beat ambience loudness ("level"), glided over 0.4 s
    lv = 1.0
    for (start, _), b in zip(beats, script["beats"]):
        a_id = b.get("ambience", "keep")
        if a_id != "keep" and a_id != cur[0]:
            spans.append((cur[0], cur[1], start))
            cur = (a_id, start)
        lv = float(b.get("level", lv) or lv)
        gain[int(start * SR):] = lv
    spans.append((cur[0], cur[1], duration))
    k = int(SR * .4)
    gain = np.convolve(np.pad(gain, (k, k), mode="edge"), np.ones(k) / k, mode="same")[k:-k].astype(np.float32)
    xf = int(SR * .5)
    for sid, t0, t1 in spans:
        x = clip(sid) if sid not in ("none", "keep") else None
        if x is None or t1 <= t0:
            continue
        a, b_ = int(t0 * SR), min(n, int(t1 * SR) + xf)
        reps = int(np.ceil((b_ - a) / len(x))) + 1
        seg = np.tile(x, reps)[:b_ - a].copy()
        ramp = min(xf, len(seg) // 2)
        seg[:ramp] *= np.linspace(0, 1, ramp); seg[-ramp:] *= np.linspace(1, 0, ramp)
        amb[a:b_] += seg * .07
    amb *= gain[:len(amb)]
    # sound effects: a library one-shot lands its hit on the beat; synth ones as before
    hits = []
    for (start, _), b in zip(beats, script["beats"]):
        hits.append((b.get("sfx", "none"), start))
        hits += [(h["id"], start + h.get("at", 0)) for h in b.get("sfx2") or []]
    for sid, start in hits:
        if sid == "none":
            continue
        if sid in lib:
            x = clip(sid)
            if x is None:
                continue
            x = x[:int(SR * 6)] * .16
            a = max(0, int((start - lib[sid].get("hit_at", 0)) * SR))
        else:
            x = sfx(sid, seed) * .9
            a = int(start * SR)
            if sid == "riser":
                a = max(0, a - len(x))
        if a >= n:
            continue
        fx[a:a + len(x)] += x[:max(0, n - a)]
    if voice:
        for (start, _), v in zip(beats, voice):
            v = v * (.12 / (float(np.sqrt(np.mean(v ** 2))) + 1e-6))
            v = np.tanh(v * 1.6) / 1.6
            a = int((start + .08) * SR)
            vtrack[a:a + len(v)] += v[:max(0, n - a)]
    env = np.convolve(np.abs(vtrack), np.ones(int(SR * .25)) / (SR * .25), mode="same")
    duck = 1 - .55 * np.clip(env / (env.max() + 1e-6) * 4, 0, 1)
    if script.get("music") == "drive":
        bed = np.zeros(n, np.float32); d = music_drive(n / SR + .01, seed)[:n]; bed[:len(d)] = d * .38
        bed[max(0, int((beats[-1][0] + .2) * SR)):] *= .25    # drops away under the final line, like the reference
    else:
        bed = music(n / SR, seed)[:n] * (.3 if script.get("music") == "tension" or (voice and script.get("style") != "narrated") else 0)
    air = _lowpass(_lowpass(np.random.default_rng(seed).standard_normal(n).astype(np.float32), .01), .05) * .02
    mixd = (vtrack / (np.abs(vtrack).max() or 1)) * .9 + (bed + amb) * duck + fx * (.35 + .65 * duck) + air
    fade = int(SR * (.8 if script.get("style") == "narrated" else 2.0))     # everything fades out at the end
    mixd[-fade:] *= np.linspace(1, 0, fade)
    mixd = mixd[:int(duration * SR)]
    mixd = np.tanh(mixd / (np.abs(mixd).max() + 1e-6) * 1.4) / np.tanh(1.4) * .89    # soft-limit, then -1 dB
    pcm = (mixd * 32767).astype(np.int16)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())
    return out


# ── 4. the scene ─────────────────────────────────────────────────────────────
KIT_API = """You write ONE JavaScript ES module for a 3D renderer (three.js r170). The module is:

export default function (K) {
  const { THREE, scene, camera } = K;
  // build the world once here ...
  return { update(t, dt) { /* move things each frame; t = seconds since the video started */ } };
}

No imports, no DOM, no fetch, no timers, no requestAnimationFrame: the kit calls update(t) for every frame in
order (30 fps). Math.random is seeded (fine to use while building). The frame is 1080x1920 portrait. Units
are metres; y is up; the ground is y=0. The kit draws the hook title, captions and HUD counter itself (don't
draw text). Keep the subject in the upper-middle of the frame: captions sit at 59-68% of the height, and
TikTok's buttons cover the right edge and the bottom 20%.

K: THREE, scene, camera, renderer, bloom (UnrealBloomPass: bloom.strength), noise (ImprovedNoise .noise(x,y,z)),
  beats ([{start, end, say, scene}] in seconds), duration,
  K.at(i) / K.end(i): start / end of beat i (i may be negative: -1 = last beat)
  K.p(t, a, b, ease?) -> 0..1 progress of t between a and b; eases: linear in out inOut expoIn expoOut back
  K.ease.out(x) etc, K.lerp(a,b,x), K.rand(a,b), K.v3([x,y,z]) -> Vector3
  K.onUpdate(fn(t, dt))  extra per-frame callback
  K.mat(color, opts) -> MeshStandardMaterial;  K.glowMat(color, intensity) -> unlit glowing (blooms)
  K.add(object3d, [x,y,z]?, shadows=true) adds to the scene and returns it
Places (each one sets the sky and lighting too; call exactly one, first):
  K.sets.backyard({env})  house at z=-22 behind a fence (z=-14, x=-14..14), trees, chairs, dense grass r=16 around 0,0,0
  K.sets.city({night})    street along -z, towers both sides (x=±16..20), cars, centre line
  K.sets.ocean({env, waves})  K.sets.plain({env, ground: 'sand'|'snow'|'mars'|'moon'|'rock'})
  K.sets.space()          black sky + stars (add planets/suns yourself)
  K.sets.stadium({home, alt, name, boards: [..], banners: [..], mosaic: 'TEXT'}) -> a packed football stadium in
                          daylight: pitch x -34..34, z -52.5..52.5 (centre spot 0,0,0; goals at z = +52.5 and -52.5,
                          posts at x = ±3.66), two tiers of stands on all four sides full of fans in `home` colours
                          (they jump on K.cheer), roof, LED boards (boards texts) around the pitch, banners between the
                          tiers, `mosaic` text spelled by the fans in the side stands, the players' tunnel at x=-38.2,
                          z=0 (mouth facing +x), dugouts at x≈-39, z=±9. Front-row fans for close-ups: the lower stand
                          starts 41 m from the centre line on x (rows step .8 m back and .45 m up from y≈1) and 60 m on z;
                          put K.crowd spectators there (or on the touchline) for reaction shots.
                          Returns {ball (a K.soccerBall), goals, tunnel, bench, crowdAt(x, z) -> a seat Vector3}.
  K.sets.room({env})      bedroom x,z in -4..4, y 0..3: bed at (-2.2,-2.6), desk at (2.6,-3.3), window on the back wall
  K.sets.street({people}) busy daytime sidewalk along -z: walk the camera down x=6 at eye height 1.75 (y=.16 pavement);
                          road x<3, shopfronts x=10; returns {people (walking crowd), birds (pigeons near 5.5,-7)}
  K.sets.court({fans})    basketball arena: court x -7.5..7.5, z -14..14, hoops at z=±13 (rim y 3.05); returns {ball, fans}
                          fans.cheer(true/false)
  K.sets.ring()           boxing ring in a dark arena: ring floor y=1.25, x,z -3.5..3.5; returns {fans, spot}
  env names: day golden dusk overcast night space underwater hell. K.envInfo = {sun, hemi, sky?, sunDir}
Pieces (all return the object; pos is [x,y,z]):
  K.ground(type, size)  K.grass(center, radius, count)  K.tree(pos, scale)  K.house(pos, {w,d,h,color,rot,lit})
  K.fence(a, b)  K.chair(pos, color, rot)  K.car(pos, color, rot)  K.box(pos, [w,h,d], color)  K.ball(pos, r, color)
  K.person(pos, opts) -> a REALISTIC skinned, textured 3D human (1.8 m at s=1, faces +z; turn with .rotation.y or
      .lookAt(x, 0, z)). opts: sex 'm'|'f'; skin 'light'|'pale'|'tan'|'brown'|'dark' (or 'red'/'devil'/'green'/'blue'/
      'grey' for creatures); hair 'buzzed'|'parted'|'long'|'buns'|'bald', hairColor, beard: true; shirt colour (null =
      shirtless), sleeves 'none' (tank/jersey)|'short'|'long'; pants colour, legs 'shorts'|'long'; shoes colour;
      gloves colour (boxing gloves); number (printed on the shirt front+back, e.g. 23 or '666'), numberColor '#fff';
      horns: true; eyes: glow colour (0xff3300); s: size (1.1 = a 2 m athlete, 1.3 = a giant).
      Animate it EVERY frame (it holds the last pose otherwise), with time in seconds:
      .play(clip, time, weight=1) - clips: idle talk walk strut jog sprint jump_start jump_air jump_land punch jab
        hit_chest hit_head death roll dance sit sit_talk sit_down stand_up crouch sneak cast cast_idle swing guard
        push swim tread drive pickup interact kneel torch. Loops repeat; one-shots run 0..their length
        (punch 1 s, jab .87, death 2.4, jump_start 1.33, jump_land 1.27, hit_chest .33, roll 1.47, cast .5).
        Blend two by calling .play(a, t) then .play(b, t, 0.5).
      .walk(t, speed) .run(t, speed) shortcuts (move .position yourself: walk ~1.4 m/s, run ~6 m/s at speed 1)
      .pose(name, x 0..1): idle punch jab fight fall hit jump (start->air->land over x) arms_up cheer dunk shoot
        dribble point sit dance cast
      .aimArm('l'|'r', [x,y,z] in its own space: +x its left, +y up, +z forward, amount) after .play to point an arm
      .bone('hand_r'|'Head'|'spine_03'|'foot_l'...) to attach things (bone.add(mesh)); .handPos('r') -> world Vector3
  K.crowd([x0,z0], [x1,z1], count, {walk, speed, face:[x,z], s, jerseys}) -> array of realistic people (walkers stroll
      along z and wrap; walk:false + face = spectators). K.cheer(true|false|0..1, t0) makes ALL spectators jump and
      wave from t0 (call it when the big moment lands, K.cheer(false) to calm them).
  K.dino(kind, pos, {s, rot, color}) -> an animated dinosaur (faces +z): trex (4.2 m tall), raptor (1.7 m),
      triceratops (3 m), stegosaurus (4 m), apatosaurus (9 m, long neck), parasaurolophus (4.2 m). Call
      .play('Idle'|'Walk'|'Run'|'Attack'|'Jump'|'Death', t) every frame (t in seconds; loops wrap); move .position
      yourself (walk ~2 m/s, run ~8 m/s). Herds: make many and offset their t.
  K.birds([x,y,z], count, radius) -> pigeons; .scatter(t0) makes them fly off
  K.sign(text, [x,y,z], {w, h, bg, fg, size, rot, tilt, glow, border}) -> a flat text sign in the world (banner,
      board, tifo in the stands, big screen; '\n' for two lines); sign.setText('...') redraws it (a big screen
      ticking up "GAME 5 WWWW?"). It faces +z; rot = yaw (Math.PI faces -z), tilt = lean back.
  K.rings(pos | null, {follow: object, offset, t0, t1, every, life, size, color, opacity, flat}) -> sound / sonar
      rings pulsing out (around a rattling ball, a shouting player, a booing stand; flat = on the ground, false =
      facing the camera)
  K.soccerBall(pos, r=.11) -> a low-poly match ball; ball.path([{t, pos:[x,y,z], h?}]) moves it along keyframes (h =
      arc height on the way to that key: a chip or a cross); it spins as it rolls
  K.goal(pos, dir)  K.tunnel(pos, rot, {color})
  K.hand({item: 'coffee'|'phone'|'none'}) -> first-person forearm in the lower right; call hand.bob(t) each frame
  K.glow(pos, {color, size, light, range}) -> glowing point with halo + light; .setPower(x) scales it (0 hides)
  K.beam(from, to, {color, width})   K.streak({color, size, tail}) -> meteor; call .fly(from, to, progress) each frame
  K.planet(pos, {r, kind: 'earth'|'mars'|'moon'|'gas', spin})  K.sun(pos, {r, color})  K.blackHole(pos, {r})
  K.stars()  K.ocean({size, color, waves})
Effects, all scheduled by time (they animate themselves):
  K.explosion(pos, {t0, size, color, debris, smoke, shake})  K.sparks(pos, {t0, count, speed, color, life, gravity, dir, spread})
  K.debris(pos, {t0, count, speed, color, size, dir, spread})  K.smoke(pos, {t0, t1, rate, rise, size, color, life})
  K.fire(pos, {t0, t1, size})  K.shake(t0, dur, amp)  K.handheld(amount 0..1, default .6)
  K.flash(t0, dur, '#fff')  K.fadeOut(t0, dur)  K.fadeIn(t0, dur)  K.vignette(on)  K.hideText = true/false
Camera: K.shots([{t, pos, look, fov?, to?: {pos?, look?, fov?}, follow?: object, lookOffset?, chase?: [dx,dy,dz]}, ...])
  = TV-style HARD CUTS: each shot holds from its t until the next shot's t, drifting from pos/look to `to` (a slow
  push-in or pan). follow: keep looking at an object (a player, the ball) + lookOffset; chase: the camera rides
  along at follow + chase. Use K.shots for the narrated style.
  K.camPath([{t, pos:[x,y,z], look:[x,y,z], fov?, ease?}, ...]) glides between keyframes every frame
  (eye height 1.6 for first-person). Or set camera.position / camera.lookAt yourself inside update().
  Default fov 62. Shake and a gentle handheld sway are added on top automatically.

Make it look like a polished viral simulation video:
- There is no narrator: the captions carry the story, so every beat needs its own clear visual moment.
- If beat 0 is a cold open (the most dramatic moment), show that state first, then reset everything at K.at(1)
  with a quick K.flash or whip of the camera, and play the story forward from the beginning.
- First-person shots (eye height 1.6-1.75, K.hand for a held item, people walking past) feel the most real.
- Something visibly changes every 2-3 seconds; each beat's "scene" note must actually be shown at that beat's time.
- Move the camera with purpose: slow push-ins, reveals, a dramatic pull-back to show scale near the end.
- Impacts land exactly on the beat that has sfx boom/hit: K.explosion or K.flash + K.shake at K.at(i).
- Glowing things (toneMapped false / glowMat) bloom nicely; use light and contrast, avoid an all-dark frame
  and an all-white one (no camera inside a bright fog or a pale sprite filling the screen; keep fog dense
  enough to be moody but with shapes readable through it).
- Huge things (planets, waves, blasts) need the camera far enough back; check every object is in front of the
  camera at its moment. Keep geometry counts sane (< 200k instances, < 300 meshes).
- The end card covers the last ~2.6 s, so the final shot can simply hold."""

NARRATED_RULES = """
THIS VIDEO IS THE NARRATED STYLE (like a viral "hypothetical" football short): a narrator reads one sentence per beat;
the kit draws the small yellow word-by-word captions (at 62-68% of the height), the broadcast cards and the scorebug
(top 10-30% of the frame) itself. Keep the important action in the middle band (25-60% of the height).
- EDIT LIKE THE REFERENCE: K.shots with a hard cut at EVERY beat start, and a second cut inside any beat longer than
  2 s (every 1-2 s overall). Vary the shot every time: wide aerial of the whole pitch, TV broadcast angle from the
  side stand (pos about [-20..20, 8-14, 30-45] looking at the play), low close-up at boot height of the ball, medium
  shot of a player from the front (camera 2-4 m away at y 1.2-1.6), reaction close-up of fans in the front row,
  over-the-shoulder, behind-the-goal, a tilt up to a sign in the stands. Slight push-ins (`to`) keep it alive.
- Every sentence is SHOWN literally at its time: who is talked about is on screen, doing it (dribbling, booing
  fans with arms up, the keeper charging, the referee pointing). Players animate EVERY frame (run/jog/idle/etc.).
- Players are K.person with team kits (shirt colour, number, legs 'shorts', pants = shorts colour, shoes colour);
  props on bones (e.g. a black blindfold box on bone('Head')). Keep the ball (K.soccerBall / the set's ball) moving
  with ball.path in sync with the passes and shots described.
- In-world text sells the jokes: K.sign banners/tifos/boards ("WE'RE BACK", "PLEASE KEEP QUIET"), a big screen.
- K.rings for anything about sound (rattling ball, shouting, booing, a "sonar" effect).
- FRAMING like a TV broadcast: most shots are MEDIUM (one to three players filling half the frame height: camera 4-7 m
  from them at y 1.3-2, looking at chest height) or the broadcast angle; at most a quarter are wide aerials. A
  close-up keeps the camera at least 2.2 m from anyone's body (never inside or brushing a character, never looking
  up from below the waist) and a ball close-up is 1-1.5 m from the ball with players visible behind it. Every
  shot's subject must be in front of the camera and in the middle band at that moment (players move: aim at where
  they ARE, use follow).
- Nothing glowing or floating that the story doesn't need: no glowing lines on the pitch, no light blobs, no
  shirtless or oddly coloured extras. Kits: each team ONE shirt colour (goalkeepers may differ), matching shorts.
- Bright, clean, saturated daylight look. No fades to black, no slow intros; the first frame already has action.
- The video ends on the last sentence's shot (the final score card is drawn by the kit); just hold it."""

SCENE_PROMPT = """{api}

The video, beat by beat (times in seconds; total {duration}s):
Title (hook, beat 0): {title}
Place: {set}
{beats}

Write the module now. Reply with only the code in one ```js block."""

FIX_PROMPT = """{api}

Your module for this video failed when rendering:
{error}

The video, beat by beat (total {duration}s):
{beats}

Here is the module:
```js
{code}
```
Fix it. Reply with only the full corrected module in one ```js block."""

REVIEW_PROMPT = """{api}

These are {n} sample frames (left to right, top to bottom) of a short 3D video, at these times: {times}.
The video, beat by beat:
{beats}

Here is the module that drew them:
```js
{code}
```
Judge it like a viewer scrolling TikTok: is the action readable and on time? Look for empty or black frames,
the subject off-screen, behind the camera or hidden under the captions, objects at absurd scale, the camera
inside geometry, nothing happening, or anything that looks broken. If it's good, reply with exactly: OK
Otherwise reply with the full improved module in one ```js block (keep what works)."""


def review_times(times: list, duration: float, n: int = 8) -> list[float]:
    """n moments spread over the whole video (the middle of evenly picked beats) for the visual check."""
    mids = [round(min(duration - .1, (s + e) / 2), 2) for s, e in times]
    if len(mids) <= n:
        return mids
    return sorted({mids[round(i * (len(mids) - 1) / (n - 1))] for i in range(n)})


def _code_from(text: str) -> str:
    m = re.search(r"```(?:js|javascript)?\s*(.*?)```", text or "", re.S)
    code = (m.group(1) if m else text or "").strip()
    if "export default" not in code:
        raise ValueError("no scene module in the reply")
    return code


def beats_text(script: dict, times: list) -> str:
    rows = []
    for i, (b, (s, e)) in enumerate(zip(script["beats"], times)):
        hud = f" | HUD: {json.dumps(b['hud'])}" if b.get("hud") else ""
        if b.get("card"):
            hud += f" | card (drawn by the kit): {json.dumps(b['card'])}"
        if b.get("score"):
            hud += f" | scorebug: {json.dumps(b['score'])}"
        rows.append(f"beat {i} [{s:.2f}-{e:.2f}s] sfx={b['sfx']} | caption: \"{b['say']}\" | show: {b['scene']}{hud}")
    return "\n".join(rows)


# ── 5. rendering ─────────────────────────────────────────────────────────────
class Stage:
    """Headless Chrome with the kit loaded; frames come back as JPEG bytes."""

    def __init__(self, job: Path):
        self.job = job

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=True, args=CHROME_ARGS)
        self.page = self.browser.new_page(viewport={"width": 1080, "height": 1920})
        self.errors: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: m.type == "error" and self.errors.append(m.text))
        self.page.route("http://stage.local/**", self._route)
        return self

    def _route(self, r):
        rel = r.request.url.split("stage.local/", 1)[1].split("?")[0]
        f = (self.job / rel[4:]) if rel.startswith("job/") else (ASSETS / rel)
        if not f.is_file() or ".." in rel:
            return r.fulfill(status=404, body="")
        ctype = {".js": "text/javascript", ".html": "text/html", ".json": "application/json", ".png": "image/png", ".jpg": "image/jpeg",
                 ".gltf": "model/gltf+json", ".glb": "model/gltf-binary", ".fbx": "application/octet-stream"}.get(f.suffix, "application/octet-stream")
        r.fulfill(path=str(f), headers={"Content-Type": ctype, "Cache-Control": "no-store"})

    def boot(self, data: dict, scene_file: str = "scene.js") -> None:
        self.errors.clear()
        self.page.goto("http://stage.local/stage.html")
        self.page.wait_for_function("window.__kitLoaded === true", timeout=30000)
        self.page.evaluate("([d, u]) => window.__boot(d, u)", [data, f"http://stage.local/job/{scene_file}?v={time.time()}"])
        self._check()

    def _check(self):
        if self.errors:
            raise RuntimeError("; ".join(self.errors[:3])[:900])

    def frame(self, t: float, grab: bool = True, q: float = .92) -> Optional[bytes]:
        self.page.evaluate(f"window.__frame({t:.5f})")
        self._check()
        if not grab:
            return None
        return base64.b64decode(self.page.evaluate(f"window.__grab({q})").split(",", 1)[1])

    def __exit__(self, *a):
        try:
            self.browser.close()
        finally:
            self._pw.stop()


def preview(job: Path, data: dict, times: list, fps: int = 10) -> list[Path]:
    """Steps the scene up to each time (at a coarse frame rate) and saves those frames."""
    out, t, step = [], 0.0, 1 / fps
    with Stage(job) as st:
        st.boot(data)
        for target in sorted(times):
            while t < target - 1e-6:
                st.frame(t, grab=False)
                t = round(t + step, 5)
            p = job / f"preview_{target:06.2f}.jpg"
            p.write_bytes(st.frame(target, q=.8))
            out.append(p)
            t = round(t + step, 5)
    return out


def contact_sheet(frames: list[Path], out: Path) -> Path:
    from PIL import Image
    ims = [Image.open(f).convert("RGB").resize((270, 480)) for f in frames]
    sheet = Image.new("RGB", (270 * 4, 480 * ((len(ims) + 3) // 4)))
    for i, im in enumerate(ims):
        sheet.paste(im, ((i % 4) * 270, (i // 4) * 480))
    sheet.save(out, quality=85)
    return out


def render(job: Path, data: dict, audio: Path, out: Path, log: Callable = print) -> Path:
    n = int(math.ceil(data["duration"] * FPS))
    cmd = [ffmpeg(), "-loglevel", "error", "-y", "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "mjpeg", "-i", "-",
           "-i", str(audio), "-vf", "scale=in_range=pc:out_range=tv,format=yuv420p", "-c:v", "libx264", "-preset", "medium",
           "-crf", "19", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
           "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    started = time.time()
    try:
        with Stage(job) as st:
            st.boot(data)
            for i in range(n):
                proc.stdin.write(st.frame(i / FPS))
                if i and i % (FPS * 20) == 0:
                    log(f"viral: rendered {i}/{n} frames ({time.time() - started:.0f}s)")
    finally:
        proc.stdin.close()
        proc.wait(timeout=600)
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError("ffmpeg couldn't encode the video")
    return out


# ── the whole video ──────────────────────────────────────────────────────────
def make(topic: str = "", log: Callable = print, ask: Callable = ask_claude, history: Optional[list] = None,
         resume: Optional[Path] = None, keep_scene: bool = False) -> dict:
    """One finished video. `resume` = an earlier work folder: its script (and voice, if complete) are reused."""
    cfg = settings()
    st = load_state()
    history = history if history is not None else [v["title"] for v in st["videos"]]
    job = Path(resume) if resume else WORK / datetime.now().strftime("%Y%m%d-%H%M%S")
    stamp = job.name
    job.mkdir(parents=True, exist_ok=True)

    if (job / "script.json").exists():
        script = json.loads((job / "script.json").read_text())
    else:
        log("viral: writing the script")
        script = write_script(history, topic, ask, style=cfg["style"])
        (job / "script.json").write_text(json.dumps(script, indent=1))
    log(f"viral: \"{script['title']}\" ({len(script['beats'])} beats)")

    voice = None
    narrated = script.get("style") == "narrated"
    if narrated or cfg["voice_mode"] in ("gemini", "edge"):
        paths = [job / f"voice_{i:02d}.wav" for i in range(len(script["beats"]))]
        if not all(p.exists() for p in paths):
            gem = narrated and cfg["voice_mode"] != "edge" or cfg["voice_mode"] == "gemini"
            paths = voice_lines([b["say"] for b in script["beats"]], job, dict(cfg, style=script.get("style"),
                                tts_model=cfg["tts_model"] if gem else "none"), log)
        voice = [read_wav(p) for p in paths]
        if narrated:
            times, duration = narrated_timeline([len(v) / SR for v in voice], [b["say"] for b in script["beats"]])
        else:
            times, duration = timeline([len(v) / SR for v in voice])
    else:
        times, duration = timeline(caption_lengths(script["beats"]), hook_min=2.6, gap=0)
    seed = int(time.time()) % 100000
    data = {"seed": seed, "fps": FPS, "duration": duration, "title": script["title"], "highlight": script["highlight"],
            "end_fact": script["end_fact"], "end_card": 0 if narrated else 2.6, "style": script.get("style", "captions"),
            "beats": [{"start": s, "end": e, "say": b["say"], "emph": b["emph"], "hud": b["hud"], "scene": b["scene"]}
                      for b, (s, e) in zip(script["beats"], times)]}
    if narrated:
        for i, (d, b, v) in enumerate(zip(data["beats"], script["beats"], voice)):
            d.update(card=b.get("card"), score=b.get("score"),
                     chunks=caption_chunks(b["say"], d["start"], len(v) / SR, d["end"], first=i == 0))
    (job / "data.json").write_text(json.dumps(data, indent=1))
    btxt = beats_text(script, times)
    api = KIT_API
    if narrated:
        for line in ("- There is no narrator: the captions carry the story, so every beat needs its own clear visual moment.\n",
                     "- The end card covers the last ~2.6 s, so the final shot can simply hold."):
            api = api.replace(line, "")
        api += "\n" + NARRATED_RULES

    if keep_scene and (job / "scene.js").exists():
        log("viral: keeping the scene that's already written")
    else:
        log(f"viral: {duration:.0f}s long; building the 3D scene")
        code = _code_from(ask(SCENE_PROMPT.format(api=api, duration=duration, title=script["title"],
                                                  set=script["set"], beats=btxt), timeout_s=900))
        sample = review_times(times, duration, n=12 if narrated else 8)
        frames = None
        for attempt in range(3):
            (job / "scene.js").write_text(code)
            try:
                frames = preview(job, data, sample)
                break
            except Exception as exc:
                log(f"viral: scene error ({exc}); asking for a fix")
                if attempt == 2:
                    raise RuntimeError(f"the 3D scene kept failing: {exc}")
                code = _code_from(ask(FIX_PROMPT.format(api=api, error=str(exc)[:900], duration=duration, beats=btxt,
                                                        code=code), timeout_s=900))
        # one look at the frames, and a revision if something's off
        sheet = contact_sheet(frames, job / "review.jpg")
        try:
            reply = ask(REVIEW_PROMPT.format(api=api, n=len(sample), times=", ".join(f"{x:.1f}s" for x in sample), beats=btxt, code=code),
                        timeout_s=900, images=[sheet])
            if reply.strip().upper() != "OK" and "```" in reply:
                revised = _code_from(reply)
                (job / "scene_v1.js").write_text(code)
                (job / "scene.js").write_text(revised)
                try:
                    preview(job, data, sample[:3])
                    code = revised
                    log("viral: revised the scene after looking at it")
                except Exception as exc:
                    log(f"viral: the revision broke ({exc}); keeping the first version")
                    (job / "scene.js").write_text(code)
        except Exception as exc:                    # incl. Claude busy: the scene is Claude's already, so go on
            log(f"viral: skipped the visual review: {exc}")

    log("viral: mixing the sound")
    audio = mix(voice, times, script, duration, job / "audio.wav", seed)
    for _ in range(2):                    # listen to it; swap any sound that doesn't fit what's on screen
        fixed = sound_check(audio, times, script, log)
        if not fixed:
            break
        script = fixed
        (job / "script.json").write_text(json.dumps(script, indent=1))
        audio = mix(voice, times, script, duration, job / "audio.wav", seed)
    OUT.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", script["title"].lower()).strip("-")[:50]
    out = OUT / f"{stamp}-{slug}.mp4"
    log("viral: rendering")
    t0 = time.time()
    render(job, data, audio, out, log)
    log(f"viral: done in {time.time() - t0:.0f}s -> {out}")
    rec = {"title": script["title"], "topic": script["topic"], "path": str(out), "job": str(job), "made": time.time(),
           "duration": duration, "caption": caption_for(script), "posted": False}
    st = load_state()
    st["videos"].append(rec)
    save_state(st)
    return rec


SOUND_CHECK = """You are the sound editor of a short viral 3D video. Listen to the attached soundtrack (ignore any
narrator's voice; judge the background sounds and effects) and compare it, beat by beat, with what is on screen:
{beats}

The sound library (only these ids may be used; "none" = silence, synth effects: boom, whoosh, riser, hit, tick, rumble, bounce, dribble, swish, buzzer, squeak,
punch, whistle, bell):
{sounds}

Every sound must be what you would really hear at that moment, at the right intensity (a dunk in a packed arena =
roaring crowd, never polite or golf clapping). Flag ONLY beats that sound clearly wrong or clearly missing a key sound.
Reply ONLY JSON: {{"ok": true|false, "fixes": [{{"beat": 3, "problem": "golf claps under a dunk",
"ambience": "<id>|keep|none|unchanged", "level": 0.3-2.5 or null, "sfx": "<id>|none|unchanged",
"sfx2": [{{"id": "<id>", "at": 0.8}}] or null}}]}}"""


def sound_check(audio: Path, times: list, script: dict, log: Callable = print) -> Optional[dict]:
    """Gemini listens to the mix next to the beat list. Returns the script with the bad sounds swapped, or None."""
    try:
        from core import gemini
        from google.genai import types
        mp3 = audio.with_suffix(".check.mp3")
        subprocess.run([ffmpeg(), "-loglevel", "quiet", "-y", "-i", str(audio), "-ac", "1", "-b:a", "64k", str(mp3)], check=True)
        lib = {x["id"]: x for x in sound_library()}

        def name(sid):
            return f"{sid} ({lib[sid]['what']})" if sid in lib else sid
        rows = []
        for i, (b, (t0, t1)) in enumerate(zip(script["beats"], times)):
            extra = ", ".join(f"{name(h['id'])} at +{h['at']:.1f}s" for h in b.get("sfx2") or [])
            rows.append(f"beat {i} [{t0:.1f}-{t1:.1f}s] caption \"{b['say']}\" | on screen: {b.get('scene', '')} | "
                        f"ambience {name(b.get('ambience', 'keep'))} level {b.get('level', 1)} | sfx {name(b.get('sfx', 'none'))}"
                        + (f" | more: {extra}" if extra else ""))
        prompt = SOUND_CHECK.format(beats="\n".join(rows), sounds=sounds_text())
        c = gemini.client(timeout_ms=240000)
        r = None
        for model in ("gemini-3.5-flash", "gemini-3.5-flash-lite"):
            try:
                r = c.models.generate_content(model=model, contents=[types.Part.from_bytes(data=mp3.read_bytes(), mime_type="audio/mpeg"), prompt],
                                              config=types.GenerateContentConfig(response_mime_type="application/json"))
                break
            except Exception as exc:
                log(f"viral: sound check on {model} failed ({str(exc)[:80]})")
        if r is None:
            return None
        verdict = _json_from(r.text)
    except Exception as exc:
        log(f"viral: skipped the sound check: {exc}")
        return None
    fixes = [f for f in verdict.get("fixes") or [] if isinstance(f, dict)]
    if verdict.get("ok") or not fixes:
        log("viral: sound check passed")
        return None
    new = json.loads(json.dumps(script))
    for f in fixes:
        try:
            b = new["beats"][int(f.get("beat"))]
        except (TypeError, ValueError, IndexError):
            continue
        log(f"viral: sound fix, beat {f.get('beat')}: {str(f.get('problem', ''))[:90]}")
        for k in ("ambience", "sfx"):
            if f.get(k) not in (None, "", "unchanged"):
                b[k] = f[k]
        if f.get("level") is not None:
            b["level"] = f["level"]
        if isinstance(f.get("sfx2"), list):
            b["sfx2"] = f["sfx2"]
    try:
        return clean_script(new, lib, style=script.get("style", "captions"))
    except Exception:
        return None


def caption_for(script: dict) -> str:
    tags = " ".join("#" + t for t in script.get("hashtags") or [])
    return f"{script['caption']} {tags}".strip()[:2000]


# ── 6. TikTok ────────────────────────────────────────────────────────────────
class NotLoggedIn(RuntimeError):
    pass


def _tiktok_context(p, visible: bool):
    PROFILE.mkdir(parents=True, exist_ok=True)
    args = ["--disable-blink-features=AutomationControlled"]
    if not visible:
        args.append("--window-position=-2400,0")          # a real window (TikTok balks at headless), kept off screen
    return p.chromium.launch_persistent_context(str(PROFILE), channel="chrome", headless=False, args=args,
                                                viewport={"width": 1280, "height": 900}, ignore_default_args=["--enable-automation"])


def logged_in(ctx) -> bool:
    return any(c["name"] == "sessionid" and c.get("value") for c in ctx.cookies("https://www.tiktok.com"))


def tiktok_login(wait_s: int = 900) -> bool:
    """Opens TikTok's login page in a Chrome window; returns once the user has logged in."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _tiktok_context(p, visible=True)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.goto("https://www.tiktok.com/login")
        end = time.time() + wait_s
        while time.time() < end:
            if logged_in(ctx):
                time.sleep(3)
                ctx.close()
                return True
            time.sleep(2)
        ctx.close()
    return False


def tiktok_post(video: Path, caption: str, ai_label: bool = True, log: Callable = print, dry_run: bool = False) -> str:
    """Uploads through TikTok Studio in the logged-in profile. Returns a note; raises on failure (with a screenshot)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _tiktok_context(p, visible=False)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        shot = WORK / f"tiktok-{int(time.time())}.png"
        try:
            if not logged_in(ctx):
                raise NotLoggedIn("TikTok isn't logged in (run: python3 -m worker.viral login)")
            pg.goto("https://www.tiktok.com/tiktokstudio/upload?from=webapp", wait_until="domcontentloaded", timeout=60000)
            if "login" in pg.url:
                raise NotLoggedIn("TikTok sent us to the login page")
            pg.locator("input[type=file]").first.set_input_files(str(video), timeout=60000)
            editor = pg.locator("div[contenteditable='true']").first
            editor.wait_for(state="visible", timeout=180000)
            editor.click()
            pg.keyboard.press("Meta+A")
            pg.keyboard.press("Backspace")
            pg.wait_for_timeout(500)
            for word in caption.split(" "):
                pg.keyboard.type(word, delay=8)
                pg.keyboard.type(" ")
                if word.startswith("#"):
                    pg.wait_for_timeout(600)               # the hashtag menu closes after the space
            typed = editor.inner_text()
            if video.stem in typed or caption.split(" ")[0] not in typed:
                raise RuntimeError(f"the caption didn't go in (the box says: {typed[:80]!r})")
            if ai_label:
                _ai_label(pg, log)
            # wait for the upload to finish: the Post button becomes enabled
            post = pg.locator("button[data-e2e='post_video_button']").first
            end = time.time() + 600
            while time.time() < end and (post.get_attribute("disabled") is not None
                                         or post.get_attribute("aria-disabled") == "true"
                                         or post.get_attribute("data-disabled") == "true"):
                pg.wait_for_timeout(2000)
            pg.wait_for_timeout(1500)
            if dry_run:
                pg.screenshot(path=str(shot), full_page=True)
                return f"dry run: ready to post (screenshot {shot})"
            pg.wait_for_timeout(15000)                      # let TikTok's own checks settle; early clicks get ignored
            done = re.compile(r"tiktokstudio/content|/manage")
            for attempt in range(4):
                post.scroll_into_view_if_needed(timeout=10000)
                pg.wait_for_timeout(1000)
                _close_modals(pg, log)
                post.click(timeout=15000)
                pg.wait_for_timeout(2500)
                _close_modals(pg, log, posting=True)
                try:
                    pg.wait_for_url(done, timeout=45000)
                    return "posted"
                except Exception:
                    log(f"viral: Post click #{attempt + 1} didn't go through, trying again")
            raise RuntimeError("TikTok never accepted the Post click")
        except Exception:
            try:
                pg.screenshot(path=str(shot))
            except Exception:
                pass
            raise
        finally:
            ctx.close()


# ---------------- Instagram Reels (the same video, posted right after TikTok) ----------------

def _ig_context(p, visible: bool):
    IG_PROFILE.mkdir(parents=True, exist_ok=True)
    args = ["--disable-blink-features=AutomationControlled"]
    if not visible:
        args.append("--window-position=-2400,0")
    return p.chromium.launch_persistent_context(str(IG_PROFILE), channel="chrome", headless=False, args=args,
                                                viewport={"width": 1280, "height": 900}, ignore_default_args=["--enable-automation"])


def ig_logged_in(ctx) -> bool:
    return any(c["name"] == "sessionid" and c.get("value") for c in ctx.cookies("https://www.instagram.com"))


def ig_login(wait_s: int = 900) -> bool:
    """Opens Instagram's login page in a Chrome window; returns once the user has logged in."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _ig_context(p, visible=True)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.goto("https://www.instagram.com/accounts/login/")
        end = time.time() + wait_s
        while time.time() < end:
            if ig_logged_in(ctx):
                time.sleep(5)
                ctx.close()
                return True
            time.sleep(2)
        ctx.close()
    return False


def _ig_click(pg, texts, timeout: int = 4000) -> bool:
    """Clicks the first visible button/link whose text is one of `texts`."""
    for t in texts:
        loc = pg.locator(f"[role=dialog] [role=button]:text-is('{t}'), [role=dialog] button:text-is('{t}'), "
                         f"button:text-is('{t}'), [role=button]:text-is('{t}')")
        for i in range(loc.count()):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    el.click(timeout=timeout)
                    return True
            except Exception:
                continue
    return False


def ig_post(video: Path, caption: str, ai_label: bool = True, log: Callable = print, dry_run: bool = False) -> str:
    """Uploads the video as a Reel through instagram.com in the logged-in profile. Raises on failure (with a screenshot)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _ig_context(p, visible=False)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        shot = WORK / f"instagram-{int(time.time())}.png"
        try:
            if not ig_logged_in(ctx):
                raise NotLoggedIn("Instagram isn't logged in (run: python3 -m worker.viral iglogin)")
            pg.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(6000)
            if "login" in pg.url:
                raise NotLoggedIn("Instagram sent us to the login page")
            _ig_click(pg, ["Not Now", "Not now"])          # "turn on notifications" / "save login info"
            pg.locator("svg[aria-label='New post'], svg[aria-label='Create']").first.click(timeout=20000)
            pg.wait_for_timeout(1500)
            _ig_click(pg, ["Post"])                         # newer menus ask Post / Live / Ad
            pg.wait_for_timeout(1000)
            pg.locator("input[type=file]").last.set_input_files(str(video), timeout=60000)
            pg.wait_for_timeout(5000)
            _ig_click(pg, ["OK"])                           # "Video posts are now shared as reels"
            try:                                            # keep the full 9:16 frame (the default crop is square)
                pg.locator("svg[aria-label='Select crop']").first.click(timeout=15000)
                pg.wait_for_timeout(800)
                pg.locator("text=Original").first.click(timeout=5000)
                pg.wait_for_timeout(800)
            except Exception:
                log("viral: Instagram's crop menu wasn't there; posting with its default frame")
            for _ in range(2):                              # crop -> edit -> caption
                if not _ig_click(pg, ["Next"], timeout=10000):
                    raise RuntimeError("Instagram's Next button wasn't there")
                pg.wait_for_timeout(3000)
            box = pg.locator("div[aria-label='Write a caption...'], div[contenteditable='true']").first
            box.click(timeout=20000)
            pg.keyboard.type(caption[:2200], delay=6)
            if ai_label:
                try:
                    pg.locator("text=Advanced settings").first.click(timeout=5000)
                    pg.wait_for_timeout(800)
                    sw = pg.locator("text=/AI label/i").locator("xpath=following::input[@role='switch'][1]").first
                    if sw.count() and sw.get_attribute("aria-checked") != "true":
                        sw.click(timeout=5000)
                except Exception:
                    log("viral: couldn't find Instagram's AI label switch")
            if dry_run:
                pg.screenshot(path=str(shot))
                return f"dry run: ready to share (screenshot {shot})"
            if not _ig_click(pg, ["Share"], timeout=15000):
                raise RuntimeError("Instagram's Share button wasn't there")
            done = pg.locator("text=/reel has been shared|post has been shared|Reel shared/i").first
            done.wait_for(state="visible", timeout=600000)
            return "posted"
        except Exception:
            try:
                pg.screenshot(path=str(shot))
            except Exception:
                pass
            raise
        finally:
            ctx.close()


def _close_modals(pg, log: Callable, posting: bool = False) -> None:
    """TikTok Studio pop-ups: never leave the page ("Are you sure you want to exit?" -> Cancel); after the Post
    click, accept a "post now?" confirmation; dismiss anything else."""
    for _ in range(3):
        modal = pg.locator("#tux-portal-container [role=dialog], .TUXModal, [class*='TUXModal-content']").first
        try:
            if not modal.is_visible():
                return
            text = modal.inner_text(timeout=2000)
        except Exception:
            return
        log(f"viral: TikTok pop-up: {text[:120]!r}")
        low = text.lower()
        if "exit" in low or "discard" in low or "leave" in low:
            choice = ("Cancel", "Stay", "Keep editing")
        elif posting:
            choice = ("Post now", "Post anyway", "Post", "Continue", "Confirm", "OK", "Got it")
        else:
            choice = ("Got it", "OK", "Close", "Cancel", "Not now")
        for label in choice:
            b = modal.locator(f"button:has-text('{label}')")
            try:
                if b.count():
                    b.first.click(timeout=3000)
                    pg.wait_for_timeout(1500)
                    break
            except Exception:
                pass
        else:
            pg.keyboard.press("Escape")
            pg.wait_for_timeout(1000)


def _ai_label(pg, log: Callable) -> bool:
    """Turns on 'AI-generated content' in the upload settings (TikTok requires it for AI-made video)."""
    try:
        sw = pg.locator("[data-e2e='aigc_container'] .Switch__content").first
        if not sw.is_visible():                                # it sits in the collapsed "Show more" section
            pg.locator("text=/^Show more$/i").first.click(timeout=5000)
            sw.wait_for(state="visible", timeout=8000)
        sw.scroll_into_view_if_needed(timeout=5000)
        if sw.get_attribute("aria-checked") != "true":
            sw.click(timeout=5000)
            pg.wait_for_timeout(800)
            for label in ("Turn on", "Confirm", "OK"):            # a "label this content?" dialog
                try:
                    pg.locator(f"button:has-text('{label}')").last.click(timeout=2000)
                    break
                except Exception:
                    pass
        on = sw.get_attribute("aria-checked") == "true"
        if not on:
            log("viral: the AI-generated switch didn't turn on")
        return on
    except Exception as exc:
        log(f"viral: couldn't set the AI-generated label: {str(exc)[:120]}")
        return False


# ── how the videos are doing ─────────────────────────────────────────────────
MILESTONES = (10_000, 100_000, 1_000_000, 10_000_000)


def parse_count(text: str) -> int:
    """TikTok's '1.2K' / '3.4M' / '987' -> an int"""
    m = re.match(r"\s*([\d.,]+)\s*([KMB]?)", str(text or "").upper())
    if not m:
        return 0
    n = float(m.group(1).replace(",", ""))
    return int(n * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)])


def read_profile_stats(handle: str = "") -> list[dict]:
    """Views of each video on the account's profile page: [{id, url, views, desc}] newest first."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _tiktok_context(p, visible=False)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            pg.goto(f"https://www.tiktok.com/@{handle}" if handle else "https://www.tiktok.com/profile",
                    wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(6000)
            for _ in range(4):                                   # load a few screens of videos
                pg.mouse.wheel(0, 2500)
                pg.wait_for_timeout(1500)
            return pg.evaluate("""() => [...document.querySelectorAll('a[href*="/video/"]')].map(a => {
                const v = a.querySelector('[data-e2e="video-views"]'), img = a.querySelector('img');
                return { url: a.href.split('?')[0], id: (a.href.match(/video\\/(\\d+)/) || [])[1] || '',
                         views: v ? v.textContent.trim() : '', desc: img ? (img.alt || '') : '' };
              }).filter(x => x.id)""")
        finally:
            ctx.close()


def viral_news(stats: list[dict], seen: dict, now: float) -> tuple[list[str], dict]:
    """Texts for videos that crossed a milestone or are far above the account's usual, and the updated record.
    `seen` = {video id: {"first": ts, "views": n, "told": [milestones already texted], "desc": ...}}"""
    msgs = []
    for x in stats:
        r = seen.setdefault(x["id"], {"first": now, "told": [], "desc": x.get("desc", "")[:120], "url": x.get("url", "")})
        r["views"] = parse_count(x["views"]) if isinstance(x["views"], str) else int(x["views"] or 0)
    views = sorted(r["views"] for r in seen.values())
    usual = views[len(views) // 2] if views else 0
    for vid, r in seen.items():
        hits = [m for m in MILESTONES if r["views"] >= m and m not in r["told"]]
        if hits:
            r["told"] += hits
            msgs.append(f"🔥 TikTok: \"{r['desc'][:80]}\" just passed {hits[-1]:,} views ({r['views']:,} now). "
                        f"This topic is working. {r['url']}")
        elif len(views) >= 5 and usual >= 100 and r["views"] >= 5 * usual and "5x" not in r["told"]:
            r["told"].append("5x")
            msgs.append(f"📈 TikTok: \"{r['desc'][:80]}\" has {r['views']:,} views, 5x your usual ({usual:,}). "
                        f"{r['url']}")
    return msgs, seen


def sequel_topic(desc: str) -> str:
    """The brief for a Part 2 of a video that did well (its TikTok caption tells what it was)."""
    return (f"PART 2 of our video that went viral (its caption: \"{desc[:150]}\"). Same characters, same style, same "
            "kind of hook, but the next chapter: e.g. Game 2 of the series, the rematch, the next level, what happens "
            "the day after. The title starts with \"Part 2:\" and still asks the question. Open with a quick recap "
            "of how Part 1 ended, then a new twist and a new ending that teases Part 3.")


# ── the worker's thread: a video at each of the day's times ─────────────────
def due_slots(times: str, done: list, now: datetime, late_ok_h: float = 3) -> list[str]:
    """Today's slots that are due and not yet done; a slot more than `late_ok_h` hours old is skipped."""
    out = []
    for s in (x.strip() for x in str(times).split(",") if x.strip()):
        try:
            h, m = (int(x) for x in s.split(":"))
        except ValueError:
            continue
        at = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if s not in done and at <= now and (now - at).total_seconds() <= late_ok_h * 3600:
            out.append(s)
    return out


class ViralService:
    PASS_SECONDS = 60

    def __init__(self, log: Callable[[str], None] = print, notify: Optional[Callable[[str, str], None]] = None):
        self.log, self.notify = log, notify or (lambda kind, text: None)
        self._stop = threading.Event()
        self._busy = threading.Lock()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="viral-videos").start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.PASS_SECONDS):
            try:
                self.run_once()
            except Exception as exc:
                self.log(f"viral: pass failed: {exc!r}")

    STATS_EVERY = 2 * 3600

    def check_stats(self, now: Optional[float] = None) -> list[str]:
        now = now or time.time()
        st = load_state()
        if now - st.get("stats_at", 0) < self.STATS_EVERY or not st["videos"] or not any(v.get("posted") for v in st["videos"]):
            return []
        st["stats_at"] = now
        save_state(st)
        try:
            stats = read_profile_stats(settings().get("handle", ""))
        except Exception as exc:
            self.log(f"viral: couldn't read the view counts: {exc}")
            return []
        st = load_state()
        msgs, st["tiktok"] = viral_news(stats, st.get("tiktok") or {}, now)
        for vid, r in st["tiktok"].items():           # a video that took off gets a Part 2 at the next slot
            if r.get("told") and not r.get("sequel"):
                r["sequel"] = now
                st.setdefault("sequels", []).append(sequel_topic(r.get("desc", "")))
                msgs = [m + " Making a Part 2 for the next slot." if r.get("desc", "")[:80] in m else m for m in msgs]
        save_state(st)
        for m in msgs:
            self.notify("viral", m)
        return msgs

    def run_once(self, now: Optional[datetime] = None) -> str:
        try:
            self.check_stats()
        except Exception as exc:
            self.log(f"viral: stats pass failed: {exc!r}")
        cfg = settings()
        if not cfg["enabled"] or time.time() < load_state().get("claude_busy_until", 0) or not self._busy.acquire(blocking=False):
            return ""
        try:
            now = now or datetime.now()
            today = now.date().isoformat()
            st = load_state()
            done = st["slots"].get(today, [])
            due = due_slots(cfg["times"], done, now)
            if not due:
                return ""
            st["slots"] = {today: done + due}                  # several due at once (Mac was asleep) = one video
            save_state(st)
            return self.produce(cfg)
        finally:
            self._busy.release()

    def produce(self, cfg: dict, topic: str = "") -> str:
        st = load_state()
        queued = [v for v in st.get("queue") or [] if Path(v["path"]).exists()]
        if queued:                                         # a video made ahead (and sent to the user to look over)
            rec = queued[0]
            st["queue"] = queued[1:]
            save_state(st)
            return self.deliver(rec, cfg)
        if not topic and st.get("sequels"):             # a sequel to a video that took off goes first
            topic = st["sequels"].pop(0)
            save_state(st)
        elif not topic and st.get("topic_queue"):       # then topics the user asked for
            topic = st["topic_queue"].pop(0)
            save_state(st)
        try:
            rec = make(topic, log=self.log)
        except ClaudeBusy as exc:
            self.log(f"viral: {exc}; trying this slot again in 20 minutes")
            st = load_state()
            st["claude_busy_until"] = time.time() + 20 * 60
            for day, done in st["slots"].items():           # un-mark the slot so a later pass retries it
                st["slots"][day] = done[:-1] if done else done
            if topic:                                       # put the topic back for the retry
                st.setdefault("sequels" if topic.startswith("PART 2") else "topic_queue", []).insert(0, topic)
            save_state(st)
            return "waiting"
        except Exception as exc:
            self.log(f"viral: couldn't make a video: {exc}")
            self.notify("viral", f"🎬 Couldn't make today's TikTok video: {str(exc)[:160]}")
            return "failed"
        return self.deliver(rec, cfg)

    def deliver(self, rec: dict, cfg: dict) -> str:
        out = self._deliver_tiktok(rec, cfg)
        if cfg["post"] and cfg.get("instagram"):
            self.post_instagram(rec, cfg)
        return out

    def post_instagram(self, rec: dict, cfg: dict) -> str:
        """The same video as a Reel on the videos' Instagram page (a failure there never stops TikTok)."""
        try:
            ig_post(Path(rec["path"]), rec["caption"], cfg["ai_label"], self.log)
            _mark(rec["path"], ig_posted=True)
            self.log(f"viral: posted to Instagram: {rec['title']}")
            return "posted"
        except NotLoggedIn:
            self.log("viral: Instagram isn't logged in yet (python3 -m worker.viral iglogin)")
            return "not logged in"
        except Exception as exc:
            self.log(f"viral: Instagram post failed: {exc}")
            self.notify("viral", f"🎬 Couldn't post \"{rec['title']}\" to Instagram ({str(exc)[:100]}). TikTok is unaffected.")
            return "failed"

    def _deliver_tiktok(self, rec: dict, cfg: dict) -> str:
        if cfg["post"]:
            try:
                tiktok_post(Path(rec["path"]), rec["caption"], cfg["ai_label"], self.log)
                _mark(rec["path"], posted=True)
                try:                                       # the user asked for a text every time one goes up
                    from core import phone_alerts
                    phone_alerts.send_text(f"🎬 Just posted to TikTok: \"{rec['title']}\"\n"
                                           f"https://www.tiktok.com/@{cfg.get('handle') or 'whatifishowedu'}")
                except Exception as exc:
                    self.log(f"viral: couldn't text the post note: {exc}")
                return "posted"
            except NotLoggedIn:
                note = "TikTok isn't logged in yet, so here it is. Log in once: python3 -m worker.viral login"
            except Exception as exc:
                self.log(f"viral: posting failed: {exc}")
                note = f"Couldn't post it to TikTok ({str(exc)[:100]}), so here it is"
        else:
            note = "Ready to post"
        try:
            from core import phone_alerts
            phone_alerts.send_file(rec["path"], f"🎬 \"{rec['title']}\" — {note}\n\n{rec['caption']}")
            return "texted"
        except Exception as exc:
            self.log(f"viral: couldn't text the video: {exc}")
            return "made"


def _mark(path: str, **fields) -> None:
    st = load_state()
    for v in st["videos"]:
        if v["path"] == path:
            v.update(fields, at=time.time())
    save_state(st)


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    if cmd == "topic":                               # python3 -m worker.viral topic "Could ..." -> made at the next slot
        st = load_state()
        st.setdefault("topic_queue", []).append(" ".join(argv[1:]))
        save_state(st)
        print("next topics:", st["topic_queue"])
        return 0
    if cmd == "queue":                                     # make the next slot's video now and text it for review
        rec = make(" ".join(a for a in argv[1:] if not a.startswith("--")))
        st = load_state()
        st.setdefault("queue", []).append(rec)
        save_state(st)
        from core import phone_alerts
        phone_alerts.send_file(rec["path"], f"🎬 Tonight's TikTok, for you to look over: \"{rec['title']}\". "
                               f"It posts at the next slot unless you say otherwise.\n\n{rec['caption']}")
        print("queued and texted:", rec["path"])
        return 0
    if cmd == "make":
        svc = ViralService()
        resume = next((a.split("=", 1)[1] for a in argv if a.startswith("--resume=")), None)
        rec = make(" ".join(a for a in argv[1:] if not a.startswith("--")), resume=Path(resume) if resume else None,
                   keep_scene="--keep-scene" in argv)
        print(json.dumps(rec, indent=1))
        if settings()["post"] and "--no-post" not in argv:
            print(svc.deliver(rec, settings()))
        return 0
    if cmd == "login":
        print("A Chrome window is opening: log in to TikTok there (it waits up to 15 minutes).")
        print("Logged in." if tiktok_login() else "Timed out before you logged in.")
        return 0
    if cmd == "iglogin":
        print("A Chrome window is opening: log in to the videos' Instagram page there (it waits up to 15 minutes).")
        print("Logged in." if ig_login() else "Timed out before you logged in.")
        return 0
    if cmd == "igpost" and len(argv) >= 2:
        words = [a for a in argv[2:] if not a.startswith("--")]
        print(ig_post(Path(argv[1]), " ".join(words), settings()["ai_label"], dry_run="--dry-run" in argv))
        return 0
    if cmd == "post" and len(argv) >= 2:
        print(tiktok_post(Path(argv[1]), argv[2] if len(argv) > 2 else "", settings()["ai_label"]))
        return 0
    for v in load_state()["videos"][-10:]:
        print(datetime.fromtimestamp(v["made"]).strftime("%m-%d %H:%M"), "posted" if v.get("posted") else "not posted", v["title"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
