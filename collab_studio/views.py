"""HTML for every page. Plain functions returning strings; every dynamic value goes through h()."""
import json
import time
from html import escape

from .services import STATUS_LABELS, STATUS_ORDER

CUSTOMER_STEPS = ["received", "designing", "ready_for_review", "final_proof_ready", "awaiting_payment",
                  "preparing_production", "in_production", "shipped"]


def h(v) -> str:
    return escape("" if v is None else str(v), quote=True)


def money(c, cur="usd"):
    if c is None:
        return "—"
    sym = {"usd": "$", "eur": "€", "gbp": "£"}.get(cur, "")
    return f"{sym}{c / 100:,.2f}" + ("" if sym else f" {cur.upper()}")


def when(ts):
    return time.strftime("%b %d, %Y %H:%M", time.localtime(ts)) if ts else ""


def layout(title, body, st, *, private=False, admin=False, wide=False, nav=True):
    name = h(st["business_name"])
    if admin:
        top = f"""<header class="topbar admin-bar"><a class="logo" href="/admin">{name}<span>Studio admin</span></a>
<nav><a href="/admin">Requests</a><a href="/admin/settings">Settings</a><a href="/admin/emails">Emails</a>
<form method="post" action="/admin/logout" class="inline">{CSRF_SLOT}<button class="link">Log out</button></form></nav></header>"""
    elif nav:
        top = f"""<header class="topbar"><a class="logo" href="/">{name}</a>
<nav>{'' if private else '<a href="/#gallery">Gallery</a><a href="/#how">How it works</a><a href="/#garments">Garments</a><a href="/#faq">FAQ</a>'}
<a class="btn btn-small" href="/create">Create My Design</a></nav></header>"""
    else:
        top = ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{h(title)} · {name}</title>
{'<meta name="robots" content="noindex,nofollow">' if private or admin else ''}
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Anton&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/style.css?v=5"></head>
<body class="{'admin' if admin else ''}">{top}<main class="{'wide' if wide else ''}">{body}</main>
<footer class="foot"><div><strong>{name}</strong> · Original fan-inspired apparel concepts. Not affiliated with, licensed or
endorsed by any team, league, club or player.</div><div><a href="mailto:{h(st['support_email'])}">{h(st['support_email'])}</a></div></footer>
<div class="lightbox" id="lightbox" hidden><button class="lb-close" aria-label="Close">×</button><img alt=""></div>
<script src="/static/app.js?v=3" defer></script></body></html>"""


CSRF_SLOT = "<!--csrf-->"


def demo_badge(st):
    return '<span class="badge badge-demo" title="Demonstration price — not final">Demo price</span>' \
        if st["prices_are_demo"] else ""


# ── Landing ─────────────────────────────────────────────────────────────────────────────────────────────────────────
GALLERY = [("starfire", "Starfire", "Basketball jersey"), ("garnet-royale", "Garnet Royale", "Soccer jersey"),
           ("sunset-tide", "Sunset Tide", "Soccer jersey"), ("nighthowl", "Nighthowl", "Basketball jersey"),
           ("silver-crown", "Silver Crown", "Soccer jersey"), ("red-district", "Red District", "Basketball jersey"),
           ("firetide", "Firetide", "Basketball jersey"), ("golden-gate-waves", "Bay Gold", "Basketball jersey")]
IDEAS = ["FC Barcelona × Dallas Cowboys", "Anthony Edwards × Giannis", "Real Madrid × Las Vegas Raiders",
         "Kobe × Ronaldinho", "Your Sunday league team", "Your business × your city", "Your mascot, reimagined"]


def landing(st):
    gal = "".join(f"""<figure class="card-flip reveal" tabindex="0">
<div class="flip"><img src="/static/gallery/{s}-front.jpg" alt="{h(n)} front" loading="lazy">
<img class="back" src="/static/gallery/{s}-back.jpg" alt="{h(n)} back" loading="lazy"></div>
<figcaption><strong>{h(n)}</strong><span>{h(k)} · tap to flip</span></figcaption></figure>""" for s, n, k in GALLERY)
    garments = "".join(f"""<article class="garment reveal"><img src="/static/{h(g['image'])}" alt="" loading="lazy">
<div><h3>{h(g['label'])}</h3><p>{h(g['blurb'])}</p><p class="price">from {money(round(g['price'] * 100), st['currency'])} {demo_badge(st)}</p></div></article>"""
                       for g in st["garments"].values() if g.get("enabled", True))
    shops = [s for s in st["shop_links"] if s.get("url")]
    shop_html = "" if not shops else f"""<section class="section shops"><h2 class="eyebrow">Ready-made designs</h2>
<p>Prefer something off the rack? Find our original designs here:</p><div class="shop-links">
{''.join(f'<a class="btn btn-ghost" href="{h(s["url"])}" rel="noopener" target="_blank">{h(s["label"])}</a>' for s in shops)}</div></section>"""
    faq = "".join(f"<details class='reveal'><summary>{h(q)}</summary><p>{h(a.replace('{revisions}', revisions_phrase(st['revision_limit'])))}</p></details>"
                  for q, a in st["faq"])
    ideas = "".join(f"<span>{h(i)}</span>" for i in IDEAS * 2)
    body = f"""
<section class="hero">
  <div class="hero-copy">
    <p class="eyebrow">Custom AI-designed sportswear</p>
    <h1>Your collab.<br><em>Your jersey.</em></h1>
    <p class="lede">Tell us the crossover you've always wanted to wear. Our design studio turns it into two original
    concepts in our vintage signature style — you pick, tweak, approve, and we make it.</p>
    <div class="cta-row"><a class="btn btn-big" href="/create">Create My Design</a><a class="btn btn-ghost" href="#gallery">See the gallery</a></div>
    <p class="fine">Free to submit. You only pay after you approve your final design.</p>
  </div>
  <div class="hero-art" aria-hidden="true">
    <svg class="waves" viewBox="0 0 600 600" preserveAspectRatio="none"><path d="M0,380 C150,300 260,470 420,360 S600,300 600,300 L600,600 L0,600Z"/><path class="w2" d="M0,450 C170,380 300,540 460,430 S600,400 600,400 L600,600 L0,600Z"/></svg>
    <img class="hero-model" src="/static/gallery/starfire-model.jpg" alt="">
    <img class="hero-flat" src="/static/gallery/garnet-royale-front.jpg" alt="">
  </div>
</section>
<div class="marquee" aria-label="Ideas customers send"><div>{ideas}</div></div>
<section class="section" id="gallery"><p class="eyebrow">Gallery</p><h2>Washed colours. Warped stripes. Big numbers.</h2>
<p class="sub">Examples of our signature style, front and back. All original designs.</p><div class="gallery">{gal}</div></section>
<section class="section how" id="how"><p class="eyebrow">How it works</p><h2>From idea to your doorstep</h2><ol class="steps">
<li class="reveal"><b>01</b><h3>Send your idea</h3><p>Pick a garment and style, tell us the collab, add your name and number.</p></li>
<li class="reveal"><b>02</b><h3>Get two concepts</h3><p>We design two distinct front-and-back concepts and email you a private link. {h(st['turnaround_design'])}</p></li>
<li class="reveal"><b>03</b><h3>Choose or tweak</h3><p>Pick your favourite or ask for changes — {h(revisions_phrase(st['revision_limit']).lower())} included.</p></li>
<li class="reveal"><b>04</b><h3>Approve &amp; order</h3><p>Confirm size and details on your final proof, pay securely, and track it to your door.</p></li></ol></section>
<section class="section" id="garments"><p class="eyebrow">Garments</p><h2>Three cuts. One signature.</h2><div class="garments">{garments}</div></section>
{shop_html}
<section class="section faq" id="faq"><p class="eyebrow">FAQ</p><h2>Good questions</h2>{faq}</section>
<section class="section final-cta"><h2>What's your collab?</h2><a class="btn btn-big" href="/create">Create My Design</a></section>"""
    return layout("Your collab. Your jersey.", body, st, wide=True)


def revisions_phrase(n):
    n = int(n)
    return "No revisions" if n == 0 else ("One revision" if n == 1 else f"{n} revisions")


# ── Builder ─────────────────────────────────────────────────────────────────────────────────────────────────────────
SWATCHES = ["navy", "garnet", "royal blue", "black", "cream", "silver", "gold", "red", "green", "purple", "sky blue",
            "orange"]


def builder(st, values=None, errors=None):
    v, e = values or {}, errors or {}

    def err(k):
        return f'<p class="err" role="alert">{h(e[k])}</p>' if k in e else ""

    def val(k):
        return h(v.get(k, ""))

    garments = "".join(f"""<label class="pick"><input type="radio" name="garment" value="{h(k)}" required
{'checked' if v.get('garment') == k else ''}><span><img src="/static/{h(g['image'])}" alt=""><strong>{h(g['label'])}</strong>
<small>{h(g['blurb'])}</small><small class="price">{money(round(g['price'] * 100), st['currency'])}{' · demo price' if st['prices_are_demo'] else ''}</small></span></label>"""
                       for k, g in st["garments"].items() if g.get("enabled", True))
    styles = "".join(f"""<label class="pick pick-style"><input type="radio" name="style" value="{h(k)}" required
{'checked' if v.get('style', 'vintage_waves') == k else ''}><span><strong>{h(s['label'])}</strong><small>{h(s['blurb'])}</small></span></label>"""
                     for k, s in st["styles"].items())
    sw = "".join(f'<button type="button" class="chip" data-color="{h(c)}">{h(c)}</button>' for c in SWATCHES)
    body = f"""<section class="narrow"><p class="eyebrow">Design request</p><h1 class="page-title">Create your design</h1>
<p class="sub">No account needed. We'll email you a private link to your concepts. {h(st['turnaround_design'])}</p>
{'<div class="alert">Please fix the highlighted fields below.</div>' if e else ''}
<form id="builder" method="post" action="/create" enctype="multipart/form-data" novalidate>
<div class="step-panel" id="step-edit">
<fieldset><legend>1 · Your idea</legend>
<label for="idea">Collaboration or design idea <span class="req">required</span></label>
<input id="idea" name="idea" maxlength="500" required value="{val('idea')}" placeholder="e.g. FC Barcelona × Dallas Cowboys">
{err('idea')}
<label class="check"><input type="checkbox" name="own_brand" value="1" {'checked' if v.get('own_brand') else ''}> It's my own team, business, mascot or original idea</label>
<p class="hint">Fan-inspired ideas are designed with original artwork only — no official logos, crests or wordmarks.</p></fieldset>
<fieldset><legend>2 · Garment <span class="req">required</span></legend><div class="picks">{garments}</div>{err('garment')}</fieldset>
<fieldset><legend>3 · Style <span class="req">required</span></legend><div class="picks picks-3">{styles}</div>{err('style')}</fieldset>
<fieldset><legend>4 · Colours</legend><label for="colors">Preferred colours <span class="opt">optional</span></label>
<input id="colors" name="colors" maxlength="200" value="{val('colors')}" placeholder="e.g. navy, garnet, silver accents">
<div class="chips" aria-label="Quick add colours">{sw}</div>
<p class="hint">Leave blank and we'll take colour cues from your idea.</p></fieldset>
<fieldset><legend>5 · Back name &amp; number</legend><div class="row2">
<div><label for="back_name">Back name <span class="opt">optional</span></label><input id="back_name" name="back_name" maxlength="14" value="{val('back_name')}" placeholder="YOUR NAME" autocapitalize="characters">{err('back_name')}</div>
<div><label for="back_number">Number <span class="opt">optional</span></label><input id="back_number" name="back_number" inputmode="numeric" maxlength="2" pattern="\\d{{1,2}}" value="{val('back_number')}" placeholder="10">{err('back_number')}</div></div></fieldset>
<fieldset><legend>6 · References &amp; notes</legend>
<label for="references">Reference images <span class="opt">optional · up to 5 · PNG, JPEG or WebP · 15 MB each</span></label>
<input id="references" name="references" type="file" accept="image/png,image/jpeg,image/webp" multiple>
<div class="thumbs" id="ref-previews"></div>{err('references')}
<label for="instructions">Anything else? <span class="opt">optional</span></label>
<textarea id="instructions" name="instructions" rows="4" maxlength="2000" placeholder="Placement, vibe, things to avoid…">{val('instructions')}</textarea>{err('instructions')}</fieldset>
<fieldset><legend>7 · Your details</legend><div class="row2">
<div><label for="customer_name">Name <span class="req">required</span></label><input id="customer_name" name="customer_name" autocomplete="name" required maxlength="80" value="{val('customer_name')}">{err('customer_name')}</div>
<div><label for="email">Email <span class="req">required</span></label><input id="email" name="email" type="email" autocomplete="email" required maxlength="254" value="{val('email')}">{err('email')}</div></div></fieldset>
<div class="actions"><button type="button" class="btn btn-big" id="to-summary">Review my request</button></div>
</div>
<div class="step-panel" id="step-summary" hidden><h2>Check your request</h2><dl class="summary" id="summary"></dl>
<p class="hint">Submitting is free. We'll email you when your concepts are ready.</p>
<div class="actions"><button type="button" class="btn btn-ghost" id="back-edit">Edit</button><button class="btn btn-big" type="submit">Submit request</button></div></div>
</form></section>"""
    return layout("Create your design", body, st)


# ── Customer: private design page ───────────────────────────────────────────────────────────────────────────────────
def timeline(status):
    cur = status if status in CUSTOMER_STEPS else ("designing" if status == "revision_in_progress" else status)
    idx = CUSTOMER_STEPS.index(cur) if cur in CUSTOMER_STEPS else 0
    items = []
    for i, s in enumerate(CUSTOMER_STEPS):
        label = STATUS_LABELS[s]
        if s == "designing" and status == "revision_in_progress":
            label = STATUS_LABELS["revision_in_progress"]
        cls = "done" if i < idx else ("now" if i == idx else "")
        items.append(f'<li class="{cls}"><span></span>{h(label)}</li>')
    return f'<ol class="timeline" aria-label="Order progress">{"".join(items)}</ol>'


def concept_card(v, token, req, *, actions=True, can_revise=True, history=False):
    img = lambda a: f"/p/{h(token)}/asset/{a}"
    chosen = req["chosen_version_id"] == v["id"]
    badges = []
    if chosen:
        badges.append('<span class="badge badge-chosen">Your choice</span>')
    if v["is_demo"]:
        badges.append('<span class="badge badge-demo">Demo placeholder — not generated from your request</span>')
    if v["parent_id"]:
        badges.append('<span class="badge">Revision</span>')
    act = ""
    if actions:
        act = f"""<div class="concept-actions">
<form method="post" action="/p/{h(token)}/choose"><input type="hidden" name="version_id" value="{v['id']}">
<button class="btn {'btn-ghost' if chosen else ''}" {'disabled' if chosen else ''}>{'Chosen ✓' if chosen else 'Choose This Design'}</button></form>
{'<button type="button" class="btn btn-ghost" data-toggle="rev-' + str(v['id']) + '">Request Changes</button>' if can_revise else ''}
</div>
{f'''<form method="post" action="/p/{h(token)}/revise" class="revise" id="rev-{v['id']}" hidden>
<input type="hidden" name="version_id" value="{v['id']}"><label for="note-{v['id']}">What should we change on design {h(v['label'])}?</label>
<textarea id="note-{v['id']}" name="note" rows="3" minlength="5" maxlength="2000" required placeholder="e.g. Swap the navy for black, make the number bigger, keep the waves."></textarea>
<button class="btn">Send changes</button></form>''' if can_revise else ''}"""
    return f"""<article class="concept {'is-chosen' if chosen else ''} {'is-history' if history else ''}" id="v{v['id']}">
<header><h3>Design {h(v['label'])}</h3><span class="ver">Version {v['version_no']}</span>{''.join(badges)}</header>
<div class="viewer" data-viewer><div class="tabs" role="tablist"><button type="button" class="on" data-view="front">Front</button><button type="button" data-view="back">Back</button></div>
<button type="button" class="zoom" data-zoom><img data-front src="{img(v['front_asset_id'])}" alt="Design {h(v['label'])} front">
<img data-back hidden src="{img(v['back_asset_id'])}" alt="Design {h(v['label'])} back" loading="lazy"><span class="zoom-hint">Tap to zoom</span></button></div>
{act}</article>"""


def details_box(req, st, refs, token):
    g = st["garments"].get(req["garment"], {})
    s = st["styles"].get(req["style"], {})
    ref_html = "".join(f'<button type="button" class="zoom ref" data-zoom><img src="/p/{h(token)}/asset/{a["id"]}" alt="Reference"></button>'
                       for a in refs)
    return f"""<aside class="details"><h3>Your request</h3><dl>
<dt>Idea</dt><dd>{h(req['idea'])}</dd><dt>Garment</dt><dd>{h(g.get('label', req['garment']))}</dd>
<dt>Style</dt><dd>{h(s.get('label', req['style']))}</dd><dt>Colours</dt><dd>{h(req['colors'] or 'Designer’s choice')}</dd>
<dt>Back name</dt><dd>{h(req['back_name'] or '—')}</dd><dt>Number</dt><dd>{h(req['back_number'] or '—')}</dd>
{f"<dt>Notes</dt><dd>{h(req['instructions'])}</dd>" if req['instructions'] else ''}</dl>
{f'<div class="thumbs">{ref_html}</div>' if refs else ''}<p class="fine">Reference {h(req['ref'])}</p></aside>"""


def review_page(st, req, token, versions, job, revisions, refs, updates, notice="", error=""):
    rid_status = req["status"]
    sent = [v for v in versions if v["sent_at"]]
    latest_round = max((v["round"] for v in sent), default=0)
    current = [v for v in sent if v["round"] == latest_round]
    older = [v for v in sent if v["round"] != latest_round]
    rev_left = max(0, req["revision_limit"] - req["revisions_used"])
    locked = rid_status in ("preparing_production", "in_production", "shipped")
    can_act = not locked and not job
    progress = ""
    if job:
        failed = job["status"] == "failed"
        progress = f"""<div class="progress-card" data-poll="/p/{h(token)}/status.json" data-job-status="{h(job['status'])}">
<h2>{'We hit a snag' if failed else ('Working on your revision' if job['kind'] == 'revision' else 'Designing your concepts')}</h2>
<div class="bar"><span style="width:{0 if failed else job['progress']}%"></span></div>
<p class="stage">{h('Our design team has been notified and will get it moving again. You don’t need to do anything.' if failed else (job['stage'] or 'In the queue'))}</p>
<p class="fine">You can close this page — we'll email you when it's ready.</p></div>"""
    elif rid_status == "revision_in_progress":
        progress = """<div class="progress-card"><h2>Your designer is reviewing your revision</h2>
<p class="stage">We'll email you as soon as it's ready. Your earlier versions stay below.</p></div>"""
    elif not sent:
        progress = """<div class="progress-card"><h2>Your designer is reviewing your concepts</h2>
<p class="stage">Every design is checked by a person before it reaches you. We'll email you as soon as it's ready.</p></div>"""
    cta = ""
    if rid_status == "final_proof_ready":
        cta = f'<div class="callout"><div><h3>Your final proof is ready</h3><p>Confirm your size and details, then order.</p></div><a class="btn btn-big" href="/p/{h(token)}/approve">Review final proof</a></div>'
    elif rid_status == "awaiting_payment":
        cta = f'<div class="callout"><div><h3>Approved — ready to order</h3><p>Your approval is saved. Complete payment to start production.</p></div><a class="btn btn-big" href="/p/{h(token)}/approve">Go to checkout</a></div>'
    elif locked:
        cta = f'<div class="callout"><div><h3>{h(STATUS_LABELS[rid_status])}</h3><p>Your design is locked for production.</p></div><a class="btn btn-big" href="/p/{h(token)}/order">View my order</a></div>'
    elif rid_status == "designing" and req["chosen_version_id"] and not job:
        cta = '<div class="callout"><div><h3>Final proof in progress</h3><p>We’re preparing your final proof and will email you when it’s ready to approve.</p></div></div>'
    demo_note = ""
    if any(v["is_demo"] for v in current):
        demo_note = """<div class="alert alert-demo"><strong>Demo mode.</strong> These images are placeholders drawn by the
system to demonstrate the flow. They were <em>not</em> generated from your request.</div>"""
    cards = "".join(concept_card(v, token, req, actions=can_act, can_revise=rev_left > 0 and can_act) for v in current)
    hist = ""
    if older:
        notes = {r["version_id"]: r for r in revisions}
        rows = "".join(concept_card(v, token, req, actions=can_act, can_revise=False, history=True)
                       + (f'<p class="rev-note">Your change request on {h(v["label"])}: “{h(notes[v["id"]]["note"])}”</p>' if v["id"] in notes else "")
                       for v in reversed(older))
        hist = f'<details class="history"><summary>Previous versions ({len(older)})</summary><div class="concepts">{rows}</div></details>'
    rev_line = ""
    if sent and not locked:
        rev_line = f'<p class="fine">{h(revisions_phrase(rev_left))} remaining.' + (" Need more? Reply to your design email." if rev_left == 0 else "") + "</p>"
    ups = "".join(f"<li><time>{when(u['created_at'])}</time>{h(u['message'])}</li>" for u in updates[:8])
    body = f"""<section class="private-head"><p class="eyebrow">Private design page · {h(req['ref'])}</p>
<h1 class="page-title">{h(req['idea'])}</h1>{timeline(rid_status)}</section>
{f'<div class="alert alert-ok">{h(notice)}</div>' if notice else ''}{f'<div class="alert">{h(error)}</div>' if error else ''}
{cta}<div class="review-grid"><div>{progress}{demo_note}
{f'<h2 class="section-title">{"Your revised design" if latest_round > 1 else "Your concepts"}</h2><div class="concepts">{cards}</div>{rev_line}' if current else ''}
{hist}</div>
<div>{details_box(req, st, refs, token)}<aside class="details"><h3>Updates</h3><ul class="updates">{ups}</ul></aside></div></div>"""
    return layout(f"Design {req['ref']}", body, st, private=True, wide=True)


def approve_page(st, req, token, version, proof, approval, order, notice="", error=""):
    g = st["garments"][req["garment"]]
    img = lambda a: f"/p/{h(token)}/asset/{a}"
    sizes = "".join(f'<option {"selected" if approval and approval["size"] == s else ""}>{h(s)}</option>' for s in g["sizes"])
    chart = g.get("size_chart") or []
    if chart:
        head, *rows = chart
        chart_html = ('<table class="sizes"><thead><tr>' + "".join(f"<th>{h(c)}</th>" for c in head) + "</tr></thead><tbody>"
                      + "".join("<tr>" + "".join(f"<td>{h(c)}</td>" for c in r) + "</tr>" for r in rows) + "</tbody></table>")
    else:
        chart_html = ('<p class="fine">Measurements for this garment are confirmed with our manufacturer before production. '
                      f'Between sizes? Email <a href="mailto:{h(st["support_email"])}">{h(st["support_email"])}</a> before paying.</p>')
    unit = round(g["price"] * 100)
    qty = approval["quantity"] if approval else 1
    tax_rate = st.get("tax_rate_percent")
    approved_now = approval and order and order["approval_id"] == approval["id"]
    quote = ""
    if approved_now:
        tax_row = (f"<dt>Tax</dt><dd>{money(order['tax_cents'], order['currency'])}</dd>" if order["tax_cents"] is not None
                   else f"<dt>Tax</dt><dd class='fine'>{h(st['tax_note'])}</dd>")
        quote = f"""<div class="quote"><h3>Your order {demo_badge(st) if order['prices_are_demo'] else ''}</h3><dl>
<dt>{h(g['label'])} × {order['quantity']}</dt><dd>{money(order['subtotal_cents'], order['currency'])}</dd>
<dt>Shipping</dt><dd>{money(order['shipping_cents'], order['currency'])}</dd>{tax_row}
<dt class="total">Total</dt><dd class="total">{money(order['total_cents'], order['currency'])}</dd></dl>
{'<p class="fine">These are demonstration prices while the shop is being set up.</p>' if order['prices_are_demo'] else ''}
<form method="post" action="/p/{h(token)}/checkout"><button class="btn btn-big btn-block">Pay {money(order['total_cents'], order['currency'])}</button></form>
<p class="fine">{h(st['turnaround_production'])}</p></div>"""
    body = f"""<section class="private-head"><p class="eyebrow">Final approval · {h(req['ref'])}</p>
<h1 class="page-title">Approve your final proof</h1>{timeline(req['status'])}</section>
{f'<div class="alert alert-ok">{h(notice)}</div>' if notice else ''}{f'<div class="alert">{h(error)}</div>' if error else ''}
<div class="review-grid"><div><div class="concept is-chosen"><header><h3>Design {h(version['label'])}</h3>
<span class="ver">Version {version['version_no']} · Proof #{proof['id']}</span>
{'<span class="badge badge-demo">Demo placeholder — not generated from your request</span>' if version['is_demo'] else ''}</header>
<div class="viewer" data-viewer><div class="tabs"><button type="button" class="on" data-view="front">Front</button><button type="button" data-view="back">Back</button></div>
<button type="button" class="zoom" data-zoom><img data-front src="{img(proof['front_asset_id'])}" alt="Proof front"><img data-back hidden src="{img(proof['back_asset_id'])}" alt="Proof back"><span class="zoom-hint">Tap to zoom</span></button></div>
<p class="fine">This is the exact design you're approving. Colours on screen can vary slightly from printed fabric.</p></div></div>
<div><form method="post" action="/p/{h(token)}/approve" class="details approve-form" data-unit="{unit}" data-ship="{round(g['shipping'] * 100)}">
<h3>{'Approved ✓ — change anything below to re-approve' if approved_now else 'Confirm your details'}</h3>
<input type="hidden" name="version_id" value="{version['id']}"><input type="hidden" name="proof_id" value="{proof['id']}">
<dl><dt>Garment</dt><dd>{h(g['label'])}</dd></dl>
<div class="row2"><div><label for="bn">Back name</label><input id="bn" name="back_name" maxlength="14" value="{h(req['back_name'])}"></div>
<div><label for="bnum">Number</label><input id="bnum" name="back_number" maxlength="2" inputmode="numeric" value="{h(req['back_number'])}"></div></div>
<p class="hint">Changing the name or number means we update the proof first, then ask you to approve again.</p>
<div class="row2"><div><label for="size">Size</label><select id="size" name="size" required><option value="">Choose…</option>{sizes}</select></div>
<div><label for="qty">Quantity</label><input id="qty" name="quantity" type="number" min="1" max="20" value="{qty}" required></div></div>
<details class="sizechart"><summary>Size chart</summary>{chart_html}</details>
<label class="check"><input type="checkbox" name="confirm" value="1" required> I approve design {h(version['label'])} (version {version['version_no']}) exactly as shown, with the name, number, size and quantity above.</label>
<button class="btn btn-big btn-block">{'Update approval' if approved_now else 'Approve final proof'}</button></form>{quote}</div></div>"""
    return layout("Approve your proof", body, st, private=True, wide=True)


def test_checkout_page(st, req, token, order, session_id):
    body = f"""<section class="narrow"><div class="alert alert-demo"><strong>TEST MODE.</strong> This is a simulated checkout for
development. No card is charged and no real payment is taken.</div>
<h1 class="page-title">Test checkout</h1><p class="sub">Order {h(req['ref'])} · {money(order['total_cents'], order['currency'])}</p>
<form method="post" action="/p/{h(token)}/test-checkout" class="details"><input type="hidden" name="session" value="{h(session_id)}">
<h3>Shipping address (test)</h3><div class="row2"><div><label>First name</label><input name="first_name" value="Test" required></div><div><label>Last name</label><input name="last_name" value="Customer" required></div></div>
<label>Address</label><input name="address1" value="123 Test Street" required>
<div class="row2"><div><label>City</label><input name="city" value="Testville" required></div><div><label>ZIP</label><input name="zip" value="10001" required></div></div>
<div class="row2"><div><label>State</label><input name="region" value="NY"></div><div><label>Country</label><input name="country" value="US"></div></div>
<button class="btn btn-big btn-block" name="outcome" value="pay">Pay {money(order['total_cents'], order['currency'])} (test)</button>
<a class="btn btn-ghost btn-block" href="/p/{h(token)}/approve">Cancel</a></form></section>"""
    return layout("Test checkout", body, st, private=True)


def order_page(st, req, token, order, approval, version, updates, notice=""):
    g = st["garments"][req["garment"]]
    track = ""
    if order and order["tracking_number"]:
        link = f' <a href="{h(order["tracking_url"])}" rel="noopener" target="_blank">Track package</a>' if order["tracking_url"] else ""
        track = f'<div class="callout"><div><h3>On its way</h3><p>{h(order["carrier"])} · {h(order["tracking_number"])}{link}</p></div></div>'
    ups = "".join(f"<li><time>{when(u['created_at'])}</time>{h(u['message'])}</li>" for u in updates)
    summary = ""
    if order and approval:
        paid = order["payment_status"] == "paid"
        summary = f"""<aside class="details"><h3>Order {h(req['ref'])}</h3><dl>
<dt>Design</dt><dd>{h(version['label'])} · version {version['version_no']}</dd><dt>Garment</dt><dd>{h(g['label'])}</dd>
<dt>Back</dt><dd>{h(approval['back_name'] or '—')} {h(approval['back_number'])}</dd><dt>Size</dt><dd>{h(approval['size'])}</dd>
<dt>Quantity</dt><dd>{approval['quantity']}</dd><dt>Total</dt><dd>{money(order['paid_amount_cents'] if paid else order['total_cents'], order['currency'])}</dd>
<dt>Payment</dt><dd>{'Paid ' + when(order['paid_at']) if paid else 'Not paid yet'}</dd></dl></aside>"""
    img = f'<img class="order-img" src="/p/{h(token)}/asset/{version["front_asset_id"]}" alt="Your design">' if version else ""
    body = f"""<section class="private-head"><p class="eyebrow">Your order · {h(req['ref'])}</p>
<h1 class="page-title">{h(STATUS_LABELS.get(req['status'], req['status']))}</h1>{timeline(req['status'])}</section>
{f'<div class="alert alert-ok">{h(notice)}</div>' if notice else ''}{track}
<div class="review-grid"><div>{img}<p><a href="/p/{h(token)}">See all your design versions</a></p></div>
<div>{summary}<aside class="details"><h3>Updates</h3><ul class="updates">{ups}</ul></aside></div></div>"""
    return layout(f"Order {req['ref']}", body, st, private=True, wide=True)


def message_page(st, title, msg, status=200):
    return layout(title, f'<section class="narrow"><h1 class="page-title">{h(title)}</h1><p class="sub">{h(msg)}</p>'
                         f'<p><a class="btn" href="/">Back to home</a></p></section>', st)


# ── Admin ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def admin_login(st, error=""):
    body = f"""<section class="narrow"><h1 class="page-title">Studio admin</h1>{f'<div class="alert">{h(error)}</div>' if error else ''}
<form method="post" action="/admin/login" class="details"><label for="pw">Password</label>
<input id="pw" name="password" type="password" autocomplete="current-password" required autofocus>
<button class="btn btn-block">Log in</button></form></section>"""
    return layout("Admin login", body, st, nav=False)


def admin_list(st, rows, flt):
    filters = "".join(f'<a class="chip {"on" if flt == k else ""}" href="/admin?status={k}">{h(v)}</a>'
                      for k, v in [("", "All"), ("attention", "Needs attention")] + list(STATUS_LABELS.items()))
    trs = "".join(f"""<tr><td><a href="/admin/r/{r['id']}">{h(r['ref'])}</a></td><td>{h(r['idea'])}</td>
<td>{h(st['garments'].get(r['garment'], {}).get('label', r['garment']))}</td><td>{h(r['customer_name'])}</td>
<td><span class="badge s-{h(r['status'])}">{h(STATUS_LABELS.get(r['status'], r['status']))}</span> {''.join(f'<span class="badge badge-warn">{h(f)}</span>' for f in r['flags'])}</td>
<td>{when(r['updated_at'])}</td></tr>""" for r in rows) or '<tr><td colspan="6">No requests yet.</td></tr>'
    body = f"""<h1 class="page-title">Requests</h1><div class="chips">{filters}</div>
<div class="table-wrap"><table class="admin-table"><thead><tr><th>Ref</th><th>Idea</th><th>Garment</th><th>Customer</th><th>Status</th><th>Updated</th></tr></thead><tbody>{trs}</tbody></table></div>"""
    return layout("Requests", body, st, admin=True, wide=True)


def _form(rid, action, inner, multipart=False, cls=""):
    enc = ' enctype="multipart/form-data"' if multipart else ""
    return f'<form method="post" action="/admin/r/{rid}/{action}"{enc} class="{cls}">{CSRF_SLOT}{inner}</form>'


def admin_detail(st, d, notice="", error=""):
    req, rid = d["req"], d["req"]["id"]
    g = st["garments"].get(req["garment"], {})
    img = lambda a: f"/admin/asset/{a}"
    refs = "".join(f'<a href="{img(a["id"])}" target="_blank"><img src="{img(a["id"])}" alt="{h(a["original_name"])}"></a>' for a in d["refs"]) or "<p class='fine'>None</p>"
    brief = d["brief"]
    jobs = "".join(f"<tr><td>#{j['id']}</td><td>{h(j['kind'])}</td><td>{h(j['status'])}</td><td>{j['attempts']}/{j['max_attempts']}</td>"
                   f"<td>{j['progress']}% · {h(j['stage'])}</td><td><pre class='err-pre'>{h(j['error'][:600])}</pre></td></tr>" for j in d["jobs"])

    def vcard(v):
        checks = "".join(f'<li class="ck-{h(c["status"])}"><b>{h(c["status"])}</b> {h(c["check"])}: {h(c["detail"])}</li>' for c in v["checks"].get("items", []))
        acts = ""
        if v["state"] in ("draft", "held"):
            acts = (_form(rid, "send", f'<input type="hidden" name="version_ids" value="{v["id"]}"><button class="btn btn-small">Send to customer</button>', cls="inline")
                    + (_form(rid, "hold", f'<input type="hidden" name="version_id" value="{v["id"]}"><button class="btn btn-small btn-ghost">Hold</button>', cls="inline") if v["state"] == "draft" else ""))
        return f"""<article class="aconcept state-{h(v['state'])}"><header><b>{h(v['label'])}</b> v{v['version_no']} · round {v['round']} · {h(v['provider'])}
<span class="badge">{h(v['state'])}</span>{'<span class="badge badge-demo">DEMO</span>' if v['is_demo'] else ''}{'<span class="badge badge-chosen">chosen</span>' if req['chosen_version_id'] == v['id'] else ''}</header>
<div class="pair"><a href="{img(v['front_asset_id'])}" target="_blank"><img src="{img(v['front_asset_id'])}" alt="front"></a><a href="{img(v['back_asset_id'])}" target="_blank"><img src="{img(v['back_asset_id'])}" alt="back"></a></div>
<p class="fine">Checks: <b>{h(v['checks'].get('overall', '—'))}</b></p><ul class="checks">{checks}</ul>
<details><summary>Prompt</summary><pre>{h(v['prompt'])}</pre></details>{acts}</article>"""
    drafts = [v for v in d["versions"] if v["state"] in ("draft", "held")]
    send_all = _form(rid, "send", "".join(f'<input type="hidden" name="version_ids" value="{v["id"]}">' for v in drafts)
                     + f'<button class="btn">Send all {len(drafts)} drafts to customer</button>', cls="inline") if len(drafts) > 1 else ""
    versions = "".join(vcard(v) for v in reversed(d["versions"])) or "<p class='fine'>No concepts yet.</p>"
    revs = "".join(f"<li>{when(r['created_at'])} on v#{r['version_id']}: “{h(r['note'])}”</li>" for r in d["revisions"]) or "<li>None</li>"
    o, a, proof = d["order"], d["approval"], d["proof"]
    proof_html = (f'<div class="pair"><img src="{img(proof["front_asset_id"])}" alt=""><img src="{img(proof["back_asset_id"])}" alt=""></div>'
                  f'<p class="fine">Proof #{proof["id"]} ({h(proof["source"])}) for v#{proof["version_id"]} · back “{h(proof["back_name"])} {h(proof["back_number"])}”</p>') if proof else "<p class='fine'>No current proof.</p>"
    appr = (f"<p>Valid approval #{a['id']}: v#{a['version_id']}, proof #{a['proof_id']}, {h(a['back_name'])} {h(a['back_number'])}, size {h(a['size'])} × {a['quantity']} · {when(a['created_at'])}</p>"
            if a else "<p class='fine'>No valid approval.</p>")
    old_appr = "".join(f"<li>#{x['id']} v#{x['version_id']} {h(x['size'])}×{x['quantity']} — {h(x['invalidated_reason'])}</li>" for x in d["old_approvals"])
    art_rows = "".join(f'<li class="ck-{h(s)}"><b>{h(s)}</b> {h(p)}: {h(det)}</li>' for p, s, det in d["artwork_report"])
    panel_opts = "".join(f'<option>{h(p["name"])}</option>' for p in g.get("panels", []))
    blockers = "".join(f"<li>{h(b)}</li>" for b in d["blockers"])
    templates = "".join(f'<li><a href="{img(t["id"])}" target="_blank">{h(t["original_name"] or "template")}</a></li>' for t in d["templates"])
    order_html = "<p class='fine'>No order yet (created when the customer approves a proof).</p>"
    if o:
        order_html = f"""<dl class="kv"><dt>Quote</dt><dd>{money(o['total_cents'], o['currency'])} ({money(o['unit_price_cents'])} × {o['quantity']} + {money(o['shipping_cents'])} ship{', tax ' + money(o['tax_cents']) if o['tax_cents'] is not None else ''}){' · DEMO PRICES' if o['prices_are_demo'] else ''}</dd>
<dt>Payment</dt><dd>{h(o['payment_status'])}{' · ' + money(o['paid_amount_cents']) + ' on ' + when(o['paid_at']) if o['paid_at'] else ''} · checkout {h(o['checkout_ref'] or '—')}</dd>
<dt>Ship to</dt><dd>{h(', '.join(x for x in [o['shipping'].get('first_name', ''), o['shipping'].get('address1', ''), o['shipping'].get('city', ''), o['shipping'].get('zip', ''), o['shipping'].get('country', '')] if x) or '—')}</dd>
<dt>Artwork</dt><dd>{h(o['artwork_status'])} {h(o['artwork_notes'])}</dd><dt>Production</dt><dd>{h(o['production_status'])} {h(o['fulfillment_provider'] or '')} {h(o['fulfillment_ref'] or '')}</dd>
<dt>Margin</dt><dd>{'cost ' + money(o['manufacturing_cost_cents']) + ' · margin ' + money((o['paid_amount_cents'] or o['total_cents']) - o['manufacturing_cost_cents']) if o['manufacturing_cost_cents'] is not None else 'cost not recorded'}</dd></dl>
<h4>Production artwork (print panels — separate from concepts and proofs)</h4><ul class="checks">{art_rows}</ul>
{_form(rid, 'artwork', f'<select name="panel">{panel_opts}</select><input type="file" name="file" accept="image/png,image/jpeg,image/webp" required><button class="btn btn-small">Upload panel</button>', multipart=True, cls="row-form")}
<h4>Manufacturer templates</h4><ul>{templates or '<li class="fine">None</li>'}</ul>
{_form(rid, 'template', '<input type="file" name="file" accept="application/pdf,image/png,image/jpeg" required><button class="btn btn-small btn-ghost">Upload template</button>', multipart=True, cls="row-form")}
{_form(rid, 'artwork-review', '<input name="notes" placeholder="Review notes"><button class="btn btn-small" name="passed" value="1">Mark artwork passed</button><button class="btn btn-small btn-ghost" name="passed" value="0">Mark failed</button>', cls="row-form")}
<p><a href="/admin/r/{rid}/pack.zip">Download production pack (.zip)</a></p>
<h4>Submit to production</h4>{f'<ul class="blockers">{blockers}</ul>' if blockers else '<p class="ok">All gates pass.</p>'}
{_form(rid, 'submit', '<input name="external_ref" placeholder="Manufacturer order ref (manual)"><button class="btn" ' + ('disabled' if blockers else '') + '>Submit to production</button>', cls="row-form")}
{_form(rid, 'cost', f'<input name="amount" inputmode="decimal" placeholder="Actual manufacturing cost" value="{(o["manufacturing_cost_cents"] or 0) / 100 if o["manufacturing_cost_cents"] is not None else ""}"><button class="btn btn-small btn-ghost">Save cost</button>', cls="row-form")}
{_form(rid, 'tracking', f'<input name="carrier" placeholder="Carrier" value="{h(o["carrier"])}"><input name="number" placeholder="Tracking number" value="{h(o["tracking_number"])}"><input name="url" placeholder="https://tracking link" value="{h(o["tracking_url"])}"><button class="btn btn-small">Save tracking &amp; email customer</button>', cls="row-form")}"""
    emails = "".join(f'<li><a href="/admin/emails/{e["id"]}" target="_blank">{h(e["subject"])}</a> · {h(e["status"])} · {when(e["created_at"])}</li>' for e in d["emails"])
    body = f"""<p><a href="/admin">← Requests</a></p><h1 class="page-title">{h(req['ref'])} · {h(req['idea'])}</h1>
<p><span class="badge s-{h(req['status'])}">{h(STATUS_LABELS.get(req['status'], req['status']))}</span> · {h(g.get('label', req['garment']))} · {h(req['customer_name'])} &lt;{h(req['email'])}&gt; · created {when(req['created_at'])}</p>
{f'<div class="alert alert-ok">{h(notice)}</div>' if notice else ''}{f'<div class="alert">{h(error)}</div>' if error else ''}
<div class="admin-grid"><div>
<section class="panel"><h2>Concepts</h2>{send_all}<div class="aconcepts">{versions}</div>
<h3>Upload a replacement concept</h3>{_form(rid, 'upload-concept', '<input name="label" placeholder="Label e.g. A or C" maxlength="6"><label>Front <input type="file" name="front" accept="image/*" required></label><label>Back <input type="file" name="back" accept="image/*" required></label><input name="note" placeholder="Note (optional)"><button class="btn btn-small">Upload as draft</button>', multipart=True, cls="row-form")}</section>
<section class="panel"><h2>Design jobs</h2><div class="table-wrap"><table class="admin-table"><tr><th>Job</th><th>Kind</th><th>Status</th><th>Tries</th><th>Progress</th><th>Error</th></tr>{jobs}</table></div>
<p class="fine">Generations used {req['generations_used']} / {st['max_generations_per_request']} · provider: {h(d['provider'])}</p>
{_form(rid, 'regenerate', f'<label>Design instructions (added to the brief)</label><textarea name="admin_instructions" rows="3">{h(req["admin_instructions"])}</textarea><button class="btn btn-small">Save &amp; generate two new concepts</button>')}
<details><summary>Current brief (v{brief['version'] if brief else '—'})</summary><pre>{h(json.dumps(brief['data'] if brief else {}, indent=2))}</pre></details></section>
<section class="panel"><h2>Final proof &amp; approval</h2>{proof_html}
{_form(rid, 'upload-proof', '<label>Proof front <input type="file" name="front" accept="image/*" required></label><label>Proof back <input type="file" name="back" accept="image/*" required></label><button class="btn btn-small">Upload proof (requires fresh approval)</button>', multipart=True, cls="row-form")}
{appr}{f'<details><summary>Earlier approvals</summary><ul>{old_appr}</ul></details>' if old_appr else ''}</section>
<section class="panel"><h2>Order &amp; production</h2>{order_html}</section></div>
<div><section class="panel"><h2>Request</h2><dl class="kv"><dt>Idea</dt><dd>{h(req['idea'])}</dd><dt>Style</dt><dd>{h(req['style'])}</dd><dt>Colours</dt><dd>{h(req['colors'] or '—')}</dd>
<dt>Own brand</dt><dd>{'yes' if req['own_brand'] else 'no'}</dd><dt>Notes</dt><dd>{h(req['instructions'] or '—')}</dd></dl>
<h4>References</h4><div class="thumbs">{refs}</div>
{_form(rid, 'personalisation', f'<label>Back name</label><input name="back_name" value="{h(req["back_name"])}"><label>Number</label><input name="back_number" value="{h(req["back_number"])}"><button class="btn btn-small btn-ghost">Save personalisation (invalidates approval)</button>')}</section>
<section class="panel"><h2>Branding permission</h2><p class="fine">Recognised: {h(', '.join(brief['data'].get('recognised', [])) if brief else '') or 'none'}</p>
{_form(rid, 'branding', f'<select name="status">' + ''.join(f'<option value="{k}" {"selected" if req["branding_status"] == k else ""}>{lbl}</option>' for k, lbl in [("not_needed", "Not needed"), ("pending", "Pending review"), ("cleared", "Cleared (original art only / permission on file)"), ("declined", "Declined")]) + f'</select><textarea name="notes" rows="2" placeholder="Notes / evidence">{h(req["branding_notes"])}</textarea><button class="btn btn-small">Save</button>')}</section>
<section class="panel"><h2>Revisions</h2><p>{req['revisions_used']} used of {req['revision_limit']}</p><ul>{revs}</ul>
{_form(rid, 'grant-revision', '<button class="btn btn-small btn-ghost">Grant one more revision</button>')}</section>
<section class="panel"><h2>Customer updates</h2>{_form(rid, 'update', '<textarea name="message" rows="2" placeholder="Shown on the customer’s order page"></textarea><button class="btn btn-small">Post update</button>')}
<ul class="updates">{''.join(f"<li><time>{when(u['created_at'])}</time>{'' if u['customer_visible'] else '<b>internal</b> '}{h(u['message'])}</li>" for u in d['updates'][:15])}</ul></section>
<section class="panel"><h2>Private link</h2><p class="fine">Links are emailed only. Issue a new one if the customer lost theirs (old link stops working).</p>
{_form(rid, 'rotate-link', '<button class="btn btn-small btn-ghost">Email a new private link</button>')}<h4>Emails</h4><ul>{emails or '<li>None</li>'}</ul></section>
</div></div>"""
    return layout(req["ref"], body, st, admin=True, wide=True)


def admin_settings(st, notice="", error=""):
    simple = [("business_name", "Business name"), ("support_email", "Support email"), ("currency", "Currency (ISO, lowercase)"),
              ("revision_limit", "Included revisions (new requests)"), ("max_generations_per_request", "Generation jobs per request"),
              ("generation_max_attempts", "Attempts per generation job"), ("tax_rate_percent", "Tax rate % (blank = not configured)"),
              ("tax_note", "Tax note shown when no rate"), ("turnaround_design", "Design turnaround text"),
              ("turnaround_production", "Production/shipping text")]
    fields = "".join(f'<label>{h(lbl)}</label><input name="{k}" value="{h("" if st.get(k) is None else st.get(k))}">' for k, lbl in simple)
    checks = "".join(f'<label class="check"><input type="checkbox" name="{k}" value="1" {"checked" if st.get(k) else ""}> {h(lbl)}</label>'
                     for k, lbl in [("prices_are_demo", "Prices are demonstration values (shows a badge)"),
                                    ("require_admin_review", "Review concepts before they're sent to customers"),
                                    ("require_admin_proof", "Upload a dedicated final proof (otherwise the chosen concept is the proof)")])
    js = lambda k: h(json.dumps(st[k], indent=2, ensure_ascii=False))
    body = f"""<h1 class="page-title">Settings</h1>{f'<div class="alert alert-ok">{h(notice)}</div>' if notice else ''}{f'<div class="alert">{h(error)}</div>' if error else ''}
<form method="post" action="/admin/settings" class="panel settings">{CSRF_SLOT}{fields}{checks}
<label>Garments (JSON) — price, shipping, sizes, size_chart (rows, first is header), panels (name + optional verified width/height/min_width/min_height in px), optional printify mapping</label>
<textarea name="garments" rows="22" class="mono">{js('garments')}</textarea>
<label>Shipping countries (JSON list of ISO codes)</label><textarea name="ship_countries" rows="2" class="mono">{js('ship_countries')}</textarea>
<label>Shop links (JSON)</label><textarea name="shop_links" rows="8" class="mono">{js('shop_links')}</textarea>
<label>FAQ (JSON [question, answer] pairs; {{revisions}} = included revisions)</label><textarea name="faq" rows="10" class="mono">{js('faq')}</textarea>
<button class="btn">Save settings</button></form>"""
    return layout("Settings", body, st, admin=True, wide=True)


def admin_emails(st, rows, provider):
    lis = "".join(f'<tr><td>{when(e["created_at"])}</td><td>{h(e["to_addr"])}</td><td><a href="/admin/emails/{e["id"]}" target="_blank">{h(e["subject"])}</a></td><td>{h(e["kind"])}</td><td>{h(e["status"])} {h(e["error"])}</td></tr>' for e in rows)
    body = f"""<h1 class="page-title">Emails</h1><p class="fine">Provider: <b>{h(provider)}</b>. {'Preview mode: nothing is actually sent; open a message to see exactly what the customer would get.' if provider == 'preview' else ''}</p>
<div class="table-wrap"><table class="admin-table"><tr><th>When</th><th>To</th><th>Subject</th><th>Kind</th><th>Status</th></tr>{lis}</table></div>"""
    return layout("Emails", body, st, admin=True, wide=True)
