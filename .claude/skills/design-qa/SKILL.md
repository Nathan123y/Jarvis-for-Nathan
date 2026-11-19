---
name: design-qa
description: Check POD (Printify/Etsy/eBay/TikTok Shop) designs for quality before or after listing - the drawing, the slogan, the per-product art and the listing mockups. Use when asked to check/QA/review designs, "are the designs good", "is this listing OK", or after changing art, lettering or layout code.
---

# Design QA

1. Pick designs and print a summary:
```
python3 .claude/skills/design-qa/check.py            # newest 5
python3 .claude/skills/design-qa/check.py 12         # newest 12
python3 .claude/skills/design-qa/check.py <design-id>
```
It reads `memory/etsy_shop.json` and flags structural problems (missing art file, art not checked, art too small,
slogan too long or not uppercase, products wanted in `only` but not listed, unpublished products, no photos).

2. Then LOOK at the images with the Read tool: each design's `art` file (and the `wide-light` variant if present),
and one or two mockup files from `extra_photos` per product type (tee, hoodie, mug). The script prints the paths.
Judge what a shopper would see:
   - the drawing: one clear subject, no mangled hands/faces/animals, no stray letters or fake text, no brand,
     character, celebrity, team or lyric (copyright is how POD shops get closed)
   - the slogan: spelled right, readable size, not cut off, fits the product's print area, contrast against the
     garment colour (light art on dark shirts and the reverse)
   - sweater/knit look where the design is a sweater motif; layout suits the product (mug wrap vs chest print)
   - mockup photos match the design and the right product

3. Report per design: pass or fail, the specific defect, and the fix (redraw, relayout, delist). Be concrete.

Rules:
- Read-only. Do not republish, redraw, delist or call Printify without the user asking: those cost money or are
  outward-facing. Always flag any cost a fix would incur.
- Printify draft mockups are the source of truth for how it prints; local art can look fine and still print wrong.
- If the worker should pick up a code change afterwards, it must be kickstarted (see the jarvis-ship routine).
