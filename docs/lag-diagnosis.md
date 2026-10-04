# When Jarvis lags, stutters or flickers

Jarvis writes a small timing log while it runs. This page explains how to read
it, how to tell whether the Finder app and `python3 main.py` really behave
differently on your Mac, and the one optional setting that helps if the
speaker runs dry.

The log holds **numbers and short labels only**: timings, counts, state names
and reasons such as `turn_complete` or `stall_gap`. It never contains what you
or Jarvis said, your notes, your mail or your messages, and it is never sent
anywhere. It lives at `~/Library/Logs/Jarvis/perf.jsonl` and is moved aside to
`perf.jsonl.1` if it ever passes about 20 MB. You can delete it whenever you like.

## Updating Jarvis

These changes are all in Python. You do **not** need to rebuild the app, and
Jarvis keeps its settings and the macOS permissions you already granted.

1. Quit Jarvis.
2. In the project folder: `git pull origin main`
3. Open Jarvis again (from Finder, or `python3 main.py`).

Only when a change touches the Swift launcher (`tools/jarvis_launcher.swift` or
`tools/notification_announcer.swift`) do you also run
`python3 tools/install_macos_app.py`. That script rebuilds only if something
changed, and says so. If macOS asks for Microphone or Screen Recording access
again after a rebuild, `tools/reset_macos_access.py` is the tool for that.

## Finding out where the lag comes from

Compare the two ways of starting Jarvis, using the same Mac and the same kind of
conversation:

1. Quit Jarvis. Start it with `python3 main.py`. Ask it three or four things,
   including one long answer. Quit it.
2. Start it from Jarvis.app in Finder. Ask the same things. Quit it.
3. In the project folder run:

   ```
   python3 tools/analyze_perf.py
   ```

You get one block per run and, when both launch paths are present, a
side-by-side line for each. What the lines mean:

| Line | In plain words |
| --- | --- |
| **underflows** | Times the speaker ran out of sound mid-sentence. This is the choppy voice. |
| *audio was waiting* | The sound had arrived but Jarvis was late passing it to the speaker. A bigger speaker buffer helps. |
| *nothing waiting* | The sound had not arrived yet. That is the network or the model, and a bigger buffer will not fix it. |
| **hand-over gap** | How long Jarvis took between finishing one piece of audio and starting the next. |
| **event loop stalls** | Moments when the part of Jarvis that carries the voice was blocked. Anything over about 50 ms can be heard. |
| **Qt thread stalls** | Moments when the window itself froze. The count of stalls *while the window was hidden* points at macOS slowing down background apps rather than at Jarvis. |
| **left SPEAKING via** | Why the HUD stopped showing SPEAKING. `turn_complete` is a normal ending. `stall_gap` means the server went quiet mid-answer. |
| **flicker** | The HUD left SPEAKING and came straight back within 3 seconds. |
| **entered SLEEPING via** | Which path put the HUD in SLEEPING (`sleep:...` from the sleep timer or button, `disconnected`, and so on). |
| **Reconnects** | Each time the live session was rebuilt, why, and how long it took. |

If the Finder run shows `x86_64` in its first line on an Apple Silicon Mac,
that run is going through Rosetta, which makes audio slower and less steady.

## The one optional setting: speaker buffer

If the analyzer reports underflows **with audio waiting**, ask the speaker for a
larger buffer. Try it for one run first:

```
JARVIS_OUTPUT_LATENCY=high python3 main.py
```

If speech is smoother and the first word is not noticeably later, keep it by
adding one line to `config/api_keys.json` (that file contains your API key, so
never paste or upload it):

```
"output_latency": "high"
```

Accepted values are `"low"`, `"high"`, or a number of seconds between 0.02 and
1.0 such as `0.15`. Remove the line to go back to the default. A value the
speaker refuses is ignored and Jarvis starts normally.

The trade-off is a slightly later first word, and the lip movement of the
animated head can lead the sound by a fraction more.

## What this release changed

- Releasing push-to-talk no longer shows SLEEPING while Jarvis is awake or
  answering. It now shows whatever is true: speaking, thinking or listening.
- Every HUD state change records its reason, and each speaker write records how
  much audio was queued, how long the write took and the gap since the last one.
- The event loop and the Qt thread each report when they were scheduled much
  later than they asked to be, and reconnects are recorded with a label.
- `tools/analyze_perf.py` turns the log into the summary above.
