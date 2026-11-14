"""Agents HQ: one page that shows what every automated crew is doing right now.

    http://localhost:8460          (the worker serves it; nothing to install, nothing leaves this Mac)

Each crew is one of the worker's services (the shop, the daily videos, game clips, game guides, internships, alerts) plus the
memecoin crew that lives in ~/optionsbots. A crew's agents are its roles. For each role the page shows when it last did
something and what it said, read from the worker's log, plus a few numbers from each crew's own state file. The page asks
for fresh data every 10 seconds.

An agent's light: green = active in the last 15 minutes, blue = active in the last 24 hours, grey = quiet. A last message
that reports a failure is shown in amber.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "config" / "worker" / "worker.log"
PORT = 8460
TAIL_BYTES = 900_000                      # the log's last stretch is plenty for "what did each agent do last"
LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (\w+) (jarvis\.[\w.]+) (.*)$")
PROBLEM = re.compile(r"fail|couldn't|could not|error|refused|flaw|timed out|wrong", re.I)

# crew -> (logger prefix, [(agent, what it does, pattern on the log message[, a different logger prefix to look in])])
CREWS = [
    ("Shop crew", "jarvis.shop", [
        ("Trend Scout", "reads what is going viral and picks the jokes", r"trend|scout|memes"),
        ("Designer", "turns an idea into a drawing and a listing", r"designed '|drawing \d|idea refused|today's design"),
        ("Publisher", "lists each design on Etsy, eBay, the Pop-Up Store and TikTok Shop (its texts come from the alerts log)",
         r"Just listed|listing", "jarvis.alerts"),
        ("Art repair", "redraws old listings that look bad", r"redrew|redraw"),
        ("Storefront", "keeps the website and Pinterest feed up to date", r"storefront"),
        ("Analyst", "reviews results every evening and rewrites the notebook", r"Analyst|notebook"),
    ]),
    ("Video crew", "jarvis.viral", [
        ("Scriptwriter", "writes each 'what if' video from the topic queue", r"script|topic|idea|writing"),
        ("Builder", "renders the 3D scenes, voice and sound", r"render|mixing|scene|frames"),
        ("Quality checker", "watches and listens before anything is posted", r"quality check|sound check|sound fix"),
        ("TikTok poster", "posts to TikTok", r"TikTok|tiktok"),
        ("Instagram poster", "posts the Reel", r"Instagram"),
    ]),
    ("Game clips crew", "jarvis.clip", [
        ("Clip scout and cutter", "finds gameplay clips, cuts them vertical and posts them", r"."),
    ]),
    ("Game guides crew", "jarvis.guides", [
        ("Guide maker", "writes, narrates, edits and uploads the guide videos", r"."),
    ]),
    ("Internship crew", "jarvis.internships", [
        ("Applier", "applies to Summer 2027 internships", r"appl|submit"),
        ("Inbox reader", "reads company replies", r"repl|inbox|email"),
        ("Scout", "looks for new openings", r"scout|found|new"),
    ]),
    ("Phone and alerts", "jarvis.alerts", [
        ("Texter", "sends you the important news and sale alerts", r"."),
    ]),
    ("Worker", "jarvis.worker", [
        ("Supervisor", "keeps all of the above running", r"."),
    ]),
]


def read_log(path: Path = LOG, limit: int = TAIL_BYTES) -> list:
    """[(datetime, level, logger, message)] from the end of the log, oldest first. Lines that continue a message
    (tracebacks) are skipped."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - limit))
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        m = LINE.match(line)
        if m:
            try:
                out.append((datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"), m.group(2), m.group(3), m.group(4)))
            except ValueError:
                pass
    return out


def light(age_s: Optional[float]) -> str:
    if age_s is None:
        return "grey"
    return "green" if age_s <= 900 else "blue" if age_s <= 86400 else "grey"


def agents_status(lines: list, now: Optional[datetime] = None) -> list:
    """The crews with each agent's last activity. [{crew, agents: [{name, role, light, age_s, last, problem}]}]"""
    now = now or datetime.now()
    out = []
    for crew, prefix, agents in CREWS:
        rows = []
        for name, role, pattern, *own in agents:
            where = own[0] if own else prefix
            mine = [l for l in lines if l[2].startswith(where)]
            rx = re.compile(pattern, re.I)
            hit = next((l for l in reversed(mine) if rx.search(l[3])), None)
            if hit:
                age = max(0.0, (now - hit[0]).total_seconds())
                msg = re.sub(r"^\w+: ", "", hit[3])[:220]
                rows.append({"name": name, "role": role, "light": light(age), "age_s": age, "last": msg,
                             "problem": hit[1] in ("ERROR", "WARNING") or bool(PROBLEM.search(hit[3]))})
            else:
                rows.append({"name": name, "role": role, "light": "grey", "age_s": None, "last": "nothing in the recent log",
                             "problem": False})
        out.append({"crew": crew, "agents": rows})
    return out


def _json(path: Path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def numbers(now: Optional[datetime] = None) -> list:
    """[{crew, items: [[label, value]]}]: a few facts from each crew's own state file."""
    now = now or datetime.now()
    today = now.date()
    out = []
    shop = _json(ROOT / "memory" / "etsy_shop.json") or {}
    designs = shop.get("designs") or []
    chan = {c: sum(len((d.get("products") if c == "etsy" else (d.get("channels") or {}).get(c)) or {}) for d in designs)
            for c in ("etsy", "ebay", "popup", "tiktok")}
    made_today = sum(1 for d in designs if d.get("made") and datetime.fromtimestamp(d["made"]).date() == today)
    out.append({"crew": "Shop crew", "items": [["designs made", len(designs)], ["made today", made_today],
                                              ["products on TikTok Shop", chan["tiktok"]], ["on the Pop-Up Store", chan["popup"]],
                                              ["on eBay", chan["ebay"]], ["on Etsy", chan["etsy"]],
                                              ["sales so far", len(shop.get("sales_log") or [])]]})
    viral = _json(ROOT / "memory" / "viral.json") or {}
    vids = viral.get("videos") or []
    posted_today = sum(1 for v in vids if v.get("made") and datetime.fromtimestamp(v["made"]).date() == today)
    out.append({"crew": "Video crew", "items": [["videos made", len(vids)], ["made today", posted_today],
                                               ["topics queued", len(viral.get("topic_queue") or [])],
                                               ["held for review", len(viral.get("held") or [])]]})
    inbox = Path.home() / "Movies" / "Jarvis Clips" / "inbox"
    try:
        waiting = sum(1 for p in inbox.iterdir() if p.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm"))
    except OSError:
        waiting = 0
    out.append({"crew": "Game clips crew", "items": [["clips waiting to be cut", waiting]]})
    guides = Path.home() / "Movies" / "Jarvis Guides"
    try:
        made = sum(1 for p in guides.rglob("*.mp4"))
    except OSError:
        made = 0
    out.append({"crew": "Game guides crew", "items": [["guide videos on disk", made]]})
    intern = _json(ROOT / "memory" / "internships.json") or {}
    out.append({"crew": "Internship crew", "items": [["jobs tracked", len(intern.get("jobs") or {})],
                                                    ["replies seen", len(intern.get("replies_seen") or [])]]})
    return out


def memecoin_feed(limit: int = 6) -> list:
    """The memecoin crew's latest messages to each other (it lives in ~/optionsbots)."""
    data = _json(Path.home() / "optionsbots" / "data" / "memebot" / "agents.json") or {}
    feed = data.get("feed") or []
    return [{"ts": f.get("ts", ""), "from": f.get("from", ""), "to": f.get("to", ""), "text": str(f.get("text", ""))[:200]}
            for f in feed[-limit:]][::-1]


def snapshot() -> dict:
    now = datetime.now()
    crews = agents_status(read_log(), now)
    counts = {c["crew"]: c["items"] for c in numbers(now)}
    for c in crews:
        c["numbers"] = counts.get(c["crew"], [])
    agents = [a for c in crews for a in c["agents"]]
    return {"at": now.strftime("%H:%M:%S"), "crews": crews, "memecoin": memecoin_feed(),
            "summary": {"agents": len(agents), "active_now": sum(a["light"] == "green" for a in agents),
                        "active_today": sum(a["light"] in ("green", "blue") for a in agents),
                        "problems": sum(a["problem"] for a in agents)}}


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agents HQ</title><style>
:root{--bg:#f6f4ef;--card:#fff;--ink:#1b1b1f;--mute:#6d6d76;--line:#e4e1d8;--green:#2f9e5b;--blue:#3b7ddd;--grey:#b9b7ae;--amber:#c47a00}
@media(prefers-color-scheme:dark){:root{--bg:#141517;--card:#1e2023;--ink:#ececee;--mute:#9a9aa3;--line:#2c2e33;--green:#46c27a;--blue:#6aa2f2;--grey:#5a5c63;--amber:#e0a030}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,system-ui,sans-serif;padding:24px 16px}
main{max-width:1100px;margin:0 auto}h1{margin:0 0 4px;font-size:26px}.sub{color:var(--mute);margin-bottom:18px}
.top{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:22px}.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 18px;min-width:130px}
.stat b{display:block;font-size:26px}.stat span{color:var(--mute);font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
.crew{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}.crew h2{margin:0 0 10px;font-size:17px}
.nums{display:flex;flex-wrap:wrap;gap:6px 14px;color:var(--mute);font-size:13px;margin-bottom:10px}.nums b{color:var(--ink)}
.agent{display:flex;gap:10px;padding:9px 0;border-top:1px solid var(--line)}.dot{width:10px;height:10px;border-radius:50%;margin-top:6px;flex:none;background:var(--grey)}
.dot.green{background:var(--green);box-shadow:0 0 0 4px color-mix(in srgb,var(--green) 25%,transparent)}.dot.blue{background:var(--blue)}
.name{font-weight:600}.role{color:var(--mute);font-size:13px}.last{font-size:13px;margin-top:2px;word-break:break-word}.last.problem{color:var(--amber)}
.when{color:var(--mute);font-size:12px;margin-left:6px;font-weight:400}
.feed{margin-top:22px}.feed h2{font-size:17px}.msg{font-size:13px;padding:6px 0;border-top:1px solid var(--line)}.msg em{color:var(--mute);font-style:normal}
</style></head><body><main><h1>Agents HQ</h1><div class="sub">What every crew is doing right now. Updates every 10 seconds. <span id="at"></span></div>
<div class="top" id="top"></div><div class="grid" id="grid"></div>
<div class="feed crew"><h2>Memecoin crew (live chatter)</h2><div id="feed"></div></div></main>
<script>
const ago=s=>s==null?'':s<90?'just now':s<5400?Math.round(s/60)+' min ago':s<172800?Math.round(s/3600)+' h ago':Math.round(s/86400)+' days ago';
const esc=t=>String(t).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function tick(){try{const d=await (await fetch('/api/agents')).json();
document.getElementById('at').textContent='Updated '+d.at;const s=d.summary;
document.getElementById('top').innerHTML=[['Agents',s.agents],['Active now',s.active_now],['Active in 24 h',s.active_today],['Need a look',s.problems]].map(x=>`<div class="stat"><b>${x[1]}</b><span>${x[0]}</span></div>`).join('');
document.getElementById('grid').innerHTML=d.crews.map(c=>`<section class="crew"><h2>${esc(c.crew)}</h2><div class="nums">${c.numbers.map(n=>`<span><b>${esc(n[1])}</b> ${esc(n[0])}</span>`).join('')}</div>`+c.agents.map(a=>`<div class="agent"><span class="dot ${a.light}"></span><div><div class="name">${esc(a.name)}<span class="when">${ago(a.age_s)}</span></div><div class="role">${esc(a.role)}</div><div class="last ${a.problem?'problem':''}">${esc(a.last)}</div></div></div>`).join('')+`</section>`).join('');
document.getElementById('feed').innerHTML=d.memecoin.length?d.memecoin.map(m=>`<div class="msg"><em>${esc(m.from)} to ${esc(m.to)}:</em> ${esc(m.text)}</div>`).join(''):'<div class="msg"><em>Nothing yet.</em></div>';
}catch(e){document.getElementById('at').textContent='(waiting for the worker)'}}
tick();setInterval(tick,10000);
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] == "/api/agents":
            body, kind = json.dumps(snapshot()).encode(), "application/json"
        elif self.path.split("?")[0] in ("/", "/index.html"):
            body, kind = PAGE.encode(), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start(log=print, port: int = PORT) -> Optional[ThreadingHTTPServer]:
    """Serve the page on localhost in a background thread. Never raises: a busy port just means no page."""
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    except OSError as exc:
        log(f"agents HQ: couldn't open port {port}: {exc}")
        return None
    threading.Thread(target=server.serve_forever, daemon=True, name="agents-hq").start()
    log(f"agents HQ: http://localhost:{port}")
    return server


if __name__ == "__main__":
    import sys
    if "--json" in sys.argv:
        print(json.dumps(snapshot(), indent=1)[:4000])
    else:
        start()
        print(f"http://localhost:{PORT}  (Ctrl-C to stop)")
        while True:
            time.sleep(3600)
