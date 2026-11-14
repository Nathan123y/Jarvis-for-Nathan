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
                                              ["products sent to TikTok Shop", chan["tiktok"]], ["on the Pop-Up Store", chan["popup"]],
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
    return {"at": now.strftime("%H:%M:%S"), "gta": gta(now), "crews": crews, "memecoin": memecoin_feed(),
            "summary": {"agents": len(agents), "active_now": sum(a["light"] == "green" for a in agents),
                        "active_today": sum(a["light"] in ("green", "blue") for a in agents),
                        "problems": sum(a["problem"] for a in agents)}}


LAUNCH = "2026-11-19"                      # GTA 6 launch day; the guides and clip services use the same date


def gta(now: Optional[datetime] = None) -> dict:
    """Countdown plus what the crews do before and after launch."""
    today = (now or datetime.now()).date()
    days = (datetime.strptime(LAUNCH, "%Y-%m-%d").date() - today).days
    return {"launch": LAUNCH, "days": days, "live": days <= 0,
            "plan": [["Guides crew", "A narrated guide video a day plus three list Shorts (YouTube, TikTok, Instagram)"],
                     ["Clips crew", "From launch day: pulls Twitch and YouTube gameplay clips, cuts them vertical, posts 3 a day"],
                     ["Video crew", "Keeps the daily 'what if' videos going on the same accounts"],
                     ["Shop crew", "Original designs only (no GTA, Rockstar or Marvel names); TikTok Shop gets 25 a day"]]}


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agents HQ</title><style>
:root{--bg:#0a0f1c;--card:#111a2e;--ink:#e6ecf5;--mute:#8d9bb4;--line:#243452;--green:#46d68a;--blue:#5aa2ff;--grey:#58657f;--amber:#f2b34e;--wall:#16233a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,system-ui,sans-serif;padding:20px 16px}
main{max-width:1180px;margin:0 auto}h1{margin:0;font-size:26px}.sub{color:var(--mute);margin:2px 0 16px}
.top{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 16px;min-width:120px}
.stat b{display:block;font-size:24px}.stat span{color:var(--mute);font-size:12px}
.gta{background:linear-gradient(135deg,#2a1246,#0f2a4a);border:1px solid #4a3a7a;border-radius:14px;padding:14px 18px;margin-bottom:16px;display:flex;gap:22px;flex-wrap:wrap;align-items:center}
.gta .big{font-size:42px;font-weight:800;line-height:1;color:#ffd166}.gta small{display:block;color:#c9b8ff;font-size:12px}
.gta ul{margin:0;padding:0;list-style:none;flex:1;min-width:300px;font-size:13px}.gta li{padding:2px 0}.gta li b{color:#ffd166}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:14px}
.crew{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden}.crew h2{margin:0;padding:12px 16px 4px;font-size:16px}
.nums{display:flex;flex-wrap:wrap;gap:4px 14px;color:var(--mute);font-size:12px;padding:0 16px 8px}.nums b{color:var(--ink)}
svg.office{display:block;width:100%;height:auto;background:var(--wall)}
.typing{animation:bob .5s infinite alternate}@keyframes bob{to{transform:translateY(2px)}}
.live{stroke-dasharray:60;animation:dash 2s linear infinite}@keyframes dash{to{stroke-dashoffset:-120}}
.agent{display:flex;gap:10px;padding:8px 16px;border-top:1px solid var(--line)}.dot{width:9px;height:9px;border-radius:50%;margin-top:6px;flex:none;background:var(--grey)}
.dot.green{background:var(--green);box-shadow:0 0 0 4px rgba(70,214,138,.22)}.dot.blue{background:var(--blue)}
.name{font-weight:600}.role{color:var(--mute);font-size:12px}.last{font-size:12.5px;margin-top:2px;word-break:break-word}.last.problem{color:var(--amber)}
.when{color:var(--mute);font-size:11px;margin-left:6px;font-weight:400}
.feed{margin-top:16px;padding-bottom:8px}.msg{font-size:12.5px;padding:6px 16px;border-top:1px solid var(--line)}.msg em{color:var(--mute);font-style:normal}
</style></head><body><main><h1>Agents HQ</h1><div class="sub">Every crew at its desks. Typing = working in the last 15 minutes. Updates every 10 seconds. <span id="at"></span></div>
<div class="top" id="top"></div><div class="gta" id="gta"></div><div class="grid" id="grid"></div>
<div class="feed crew"><h2>Memecoin crew (live chatter)</h2><div id="feed"></div></div></main>
<script>
const ago=s=>s==null?'':s<90?'just now':s<5400?Math.round(s/60)+' min ago':s<172800?Math.round(s/3600)+' h ago':Math.round(s/86400)+' days ago';
const esc=t=>String(t).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const COL={green:'#46d68a',blue:'#5aa2ff',grey:'#58657f'};
function desk(x,a){const c=COL[a.light],on=a.light==='green';const lab=esc(a.name.length>13?a.name.slice(0,12)+'…':a.name);
return `<g transform="translate(${x} 0)"><rect x="-46" y="118" width="92" height="10" rx="2" fill="#3b2f2a"/><rect x="-40" y="128" width="6" height="34" fill="#2a211d"/><rect x="34" y="128" width="6" height="34" fill="#2a211d"/>
<rect x="-26" y="78" width="52" height="34" rx="3" fill="#0d1526" stroke="#33415c"/><polyline class="${on?'live':''}" points="-21,104 -13,98 -6,101 2,92 9,95 16,86 21,88" fill="none" stroke="${c}" stroke-width="2"/>
<g class="${on?'typing':''}"><rect x="-11" y="118" width="22" height="24" rx="5" fill="#c9d2de"/><circle cx="0" cy="128" r="3" fill="${c}"/><rect x="-10" y="96" width="20" height="17" rx="5" fill="#e3e9f1"/><rect x="-7" y="101" width="14" height="5" rx="2" fill="${c}"/><line x1="0" y1="96" x2="0" y2="89" stroke="#9aa5b4" stroke-width="2"/><circle cx="0" cy="87" r="2.6" fill="${c}"/></g>
<rect x="-40" y="150" width="80" height="16" rx="4" fill="rgba(7,12,24,.85)" stroke="${c}"/><text x="0" y="162" text-anchor="middle" fill="${c}" font-size="10.5" font-weight="700">${lab}</text></g>`}
function scene(agents){const n=agents.length,w=Math.max(300,n*130);let b='';for(let i=0;i<Math.floor((w-50)/16);i++){const h=12+((i*37)%30);b+=`<rect x="${26+i*16}" y="${70-h}" width="11" height="${h}" fill="#1c2a44"/>`}
return `<svg class="office" viewBox="0 0 ${w} 180"><rect x="20" y="14" width="${w-40}" height="56" rx="4" fill="#070b18" stroke="#243452"/>${b}<rect x="0" y="162" width="${w}" height="18" fill="#0b1220"/>${agents.map((a,i)=>desk(w/n*(i+.5),a)).join('')}</svg>`}
async function tick(){try{const d=await (await fetch('/api/agents')).json();
document.getElementById('at').textContent='Updated '+d.at;const s=d.summary,g=d.gta;
document.getElementById('top').innerHTML=[['Agents',s.agents],['Working now',s.active_now],['Active in 24 h',s.active_today],['Need a look',s.problems]].map(x=>`<div class="stat"><b>${x[1]}</b><span>${x[0]}</span></div>`).join('');
document.getElementById('gta').innerHTML=`<div><div class="big">${g.live?'LIVE':g.days}</div><small>${g.live?'GTA 6 is out':'days to GTA 6 ('+g.launch+')'}</small></div><ul>${g.plan.map(p=>`<li><b>${esc(p[0])}:</b> ${esc(p[1])}</li>`).join('')}</ul>`;
document.getElementById('grid').innerHTML=d.crews.map(c=>`<section class="crew"><h2>${esc(c.crew)}</h2><div class="nums">${c.numbers.map(n=>`<span><b>${esc(n[1])}</b> ${esc(n[0])}</span>`).join('')}</div>${scene(c.agents)}`+c.agents.map(a=>`<div class="agent"><span class="dot ${a.light}"></span><div><div class="name">${esc(a.name)}<span class="when">${ago(a.age_s)}</span></div><div class="role">${esc(a.role)}</div><div class="last ${a.problem?'problem':''}">${esc(a.last)}</div></div></div>`).join('')+`</section>`).join('');
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
