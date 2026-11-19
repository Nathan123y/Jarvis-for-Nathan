#!/usr/bin/env python3
"""Structural checks on POD designs plus the image paths to look at. Read-only."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
arg = sys.argv[1] if len(sys.argv) > 1 else "5"
designs = json.loads((ROOT / "memory" / "etsy_shop.json").read_text())["designs"]
pick = [d for d in designs if d["id"] == arg] if not arg.isdigit() else designs[-int(arg):]
if not pick:
    sys.exit(f"no design matches {arg!r}")

for d in pick:
    flags = []
    art = Path(d.get("art") or d.get("file", ""))
    if not art.exists():
        flags.append("art file missing on disk")
    if not d.get("art_checked"):
        flags.append("drawing was never checked")
    w, h = (d.get("size") or [0, 0])
    if min(w, h) < 2000:
        flags.append(f"small artwork {w}x{h}")
    slogan = d.get("slogan", "")
    if len(slogan) > 40:
        flags.append(f"long slogan ({len(slogan)} chars)")
    if slogan != slogan.upper():
        flags.append("slogan not uppercase")
    prods = {}
    for ch, kinds in (d.get("channels") or {}).items():
        for kind, p in kinds.items():
            prods.setdefault(kind, []).append((ch, p))
    want = set(d.get("only") or [])
    if want and want - set(prods):
        flags.append("wanted but not listed: " + ", ".join(sorted(want - set(prods))))
    print(f"\n== {d['id']}  [{d.get('style')}/{d.get('look')}/{d.get('season')}]  \"{slogan}\"")
    print(f"   audience: {d.get('audience')}   art: {art}")
    for v, info in (d.get("variants") or {}).items():
        print(f"   variant {v}: {info.get('size')}")
    for kind, lst in prods.items():
        for ch, p in lst:
            if not p.get("published"):
                flags.append(f"{ch}/{kind} not published")
            if not p.get("photos") and not p.get("extra_photos"):
                flags.append(f"{ch}/{kind} has no photos")
            extra = [x for x in p.get("extra_photos", []) if Path(x).exists()]
            print(f"   {ch}/{kind}: ${p.get('price', 0) / 100:.2f} art={p.get('art')} {p.get('url', '')}")
            extra.sort(key=lambda x: ("design" in x or "detail" in x))
            for x in extra[:2]:
                print(f"      look: {x}")
    print("   FLAGS: " + ("; ".join(flags) if flags else "none"))
