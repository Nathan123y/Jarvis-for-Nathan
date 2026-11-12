"""The Etsy print-on-demand shop, run by Jarvis from the background worker.

Once a day (and on request) it:
  1. invents one design for a gift niche (Gemini, free): an illustration prompt, a short slogan, a title,
     a description and 13 Etsy tags. Brands, characters, celebrities, teams and lyrics are refused twice:
     in the prompt and by BLOCKED below, because copyright complaints are how print-on-demand shops get closed;
  2. draws it (Cloudflare Workers AI, flux-1-schnell, free tier), has the drawing looked over and redrawn if it
     has a mistake a shopper would notice, cuts away the white background and sets the slogan with it in big
     capitals (worker/lettering.py), in the shape each product's print area wants;
  3. puts it on a T-shirt, a mug and a sticker in Printify, priced so each sale clears `profit` after
     Printify's cost and Etsy's fees, and publishes them to the Etsy shop connected in Printify.

Printify prints and ships each order; Etsy pays out; Printify charges its cost to the card in Printify.
Nothing is published until an Etsy store is connected in Printify: designs wait in the queue meanwhile.
Every listing says the design was made with AI and printed on demand, as Etsy asks.

State: memory/etsy_shop.json (designs, products, orders seen); artwork: memory/etsy_shop/.
"""
from __future__ import annotations

import base64
import io
import json
import math
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "memory" / "etsy_shop.json"
ART = ROOT / "memory" / "etsy_shop"
NS = "etsy_shop"
API = "https://api.printify.com/v1/"
FLUX = "@cf/black-forest-labs/flux-1-schnell"
FONT = "/System/Library/Fonts/Supplemental/Arial Black.ttf"
DESIGN_W = 3000                      # the artwork is set this wide: a shirt's print area is 2767 px

PRODUCTS = {
    "tee": {"name": "T-Shirt", "blueprint": 12, "provider": 99, "profit": 8.0,      # Bella+Canvas 3001
            "colors": ("White", "Natural", "Ash", "Athletic Heather", "Soft Cream", "Heather Peach"),
            "sizes": ("S", "M", "L", "XL", "2XL"), "fill": 0.9},
    # a hoodie's print area stops above the pocket (about 3:2), so it takes the design's wide layout: a tall one
    # was shrunk to half the width to fit
    "hoodie": {"name": "Hoodie", "blueprint": 77, "provider": 99, "profit": 10.0,  # Gildan 18500
               "colors": ("White", "Sport Grey", "Ash", "Sand", "Light Pink", "Light Blue"),
               "sizes": ("S", "M", "L", "XL", "2XL"), "fill": 0.95, "layout": "wide"},
    "tank": {"name": "Tank Top", "blueprint": 39, "provider": 99, "profit": 7.0,    # Bella+Canvas 3480
             "colors": ("White", "Athletic Heather"), "sizes": ("S", "M", "L", "XL", "2XL"), "fill": 0.9},
    "shorts": {"name": "Sweat Shorts", "blueprint": 1574, "provider": 39, "profit": 7.0,   # Comfort Colors, printed on the left leg
               "position": "front_left_leg", "colors": ("Ivory", "Peachy", "Hydrangea", "Blue Jean"),
               "sizes": ("S", "M", "L", "XL", "2XL"), "fill": 0.85},
    "pants": {"name": "Sweatpants", "blueprint": 1588, "provider": 217, "profit": 7.0,     # Comfort Colors, the left thigh
              "position": "left_leg_front", "colors": ("Ivory", "Peachy", "Hydrangea", "Blue Jean"),
              "sizes": ("S", "M", "L", "XL", "2XL"), "fill": 0.95, "y": 0.3},
    "hat": {"name": "Trucker Hat", "blueprint": 1128, "provider": 41, "profit": 7.0,       # printed (not embroidered) front panel
            "art": "slogan", "colors": ("Black", "Dark Navy", "Dark Green", "Royal"), "sizes": ("One size",), "fill": 0.95},
    "mug": {"name": "Mug", "blueprint": 478, "provider": 99, "profit": 6.0, "sizes": ("11oz",), "fill": 0.9},
    "sticker": {"name": "Sticker", "blueprint": 400, "provider": 99, "profit": 2.5, "fill": 1.0},   # no longer made (2026-10-24)
    # added 2026-10-26
    "crewneck": {"name": "Crewneck Sweatshirt", "blueprint": 49, "provider": 99, "profit": 10.0,   # Gildan 18000
                 "colors": ("White", "Sport Grey", "Ash", "Sand", "Light Pink", "Light Blue"),
                 "sizes": ("S", "M", "L", "XL", "2XL"), "fill": 0.9},
    "poster": {"name": "Poster", "blueprint": 282, "provider": 99, "profit": 8.0,             # matte, vertical
               "sizes": ("11\u2033 x 14\u2033", "12\u2033 x 18\u2033", "16\u2033 x 20\u2033", "18\u2033 x 24\u2033"), "fill": 0.85},
    "phone": {"name": "Phone Case", "blueprint": 421, "provider": 99, "profit": 7.0,          # Tough Case, glossy
              "sizes": ("iPhone 13", "iPhone 14", "iPhone 15", "iPhone 15 Pro", "iPhone 15 Pro Max", "iPhone 16",
                        "iPhone 16 Pro", "iPhone 16 Pro Max", "iPhone 17"), "fill": 0.8},
    "tote": {"name": "Tote Bag", "blueprint": 1313, "provider": 99, "profit": 6.0, "fill": 0.85},  # Liberty Bags canvas
    # ceramic. Sold round and oval only: the star and the heart cut the slogan off (the star was the listing's
    # main photo), and the design is sized to sit inside the curve, a little low to clear the hole for the cord.
    # The round one's face is smaller than its print area says (measured on Printify's photo: about 84% of it).
    "ornament": {"name": "Christmas Ornament", "blueprint": 1599, "provider": 99, "profit": 6.0,
                 "fill": 0.9, "fills": {1.0: 0.76}, "y": 0.53, "shape": "ellipse",
                 "options": {"shape": ("Circle", "Oval")}},
    "tumbler": {"name": "Tumbler", "blueprint": 353, "provider": 1, "profit": 7.0, "fill": 0.8},   # 20oz
    # the ugly-sweater line (2026-10-26, from the user's "GET LIT" photo): its art is light on dark, so dark garments
    "sweater_tee": {"name": "T-Shirt", "blueprint": 12, "provider": 99, "profit": 8.0, "ground": "dark",
                    "colors": ("Black", "Canvas Red", "Forest"), "sizes": ("S", "M", "L", "XL", "2XL"), "fill": 0.92},
    "sweater_hoodie": {"name": "Hoodie", "blueprint": 77, "provider": 99, "profit": 10.0, "ground": "dark",
                       "colors": ("Black", "Red", "Navy", "Forest Green"), "sizes": ("S", "M", "L", "XL", "2XL"),
                       "fill": 0.95, "layout": "wide"},
    # printed all over (sleeves, cuffs, waistband); Printify photographs it on a person, unlike the knit sweater
    # (blueprint 1748), whose only photo is the flat printing template
    "ugly_sweater": {"name": "Ugly Christmas Sweater", "blueprint": 1023, "provider": 90, "profit": 12.0,
                     "art": "sweater", "ground": "dark", "sizes": ("S", "M", "L", "XL", "2XL")},
    # added 2026-10-30, for the family, pet and couple lines (all Printify Choice)
    "youth_tee": {"name": "Youth T-Shirt", "blueprint": 420, "provider": 99, "profit": 7.0,    # Bella+Canvas 3001Y
                  "colors": ("White", "Natural", "Ash", "Athletic Heather"), "sizes": ("S", "M", "L", "XL"), "fill": 0.9},
    "toddler_tee": {"name": "Toddler T-Shirt", "blueprint": 580, "provider": 99, "profit": 7.0,  # Bella+Canvas 3001T
                    "colors": ("White", "Natural", "Athletic Heather"), "sizes": ("2T", "3T", "4T", "5T"), "fill": 1.0},
    "baby_bodysuit": {"name": "Baby Bodysuit", "blueprint": 33, "provider": 99, "profit": 7.0,   # Rabbit Skins 4424
                      "colors": ("White", "Ash", "Heather"), "sizes": ("NB (0-3M)", "6M", "12M", "18M", "24M"), "fill": 1.0},
    # a triangle, point down, its top edge rolled at the collar: words on top, where it's widest, the pet below
    "pet_bandana": {"name": "Dog Bandana", "blueprint": 562, "provider": 99, "profit": 5.0, "fill": 0.97,
                    "shape": "triangle", "layout": "over"},
    # not in the pet line: about $54 at our margin, against ~$30 dog hoodies elsewhere
    "pet_hoodie": {"name": "Dog Hoodie", "blueprint": 10741, "provider": 99, "profit": 8.0, "position": "back_dtg",
                   "colors": ("Grey", "Pink"), "sizes": ("XS", "S", "M", "L", "XL", "2XL"), "fill": 0.85},
    # embroidered: the slogan alone in cream thread on dark caps (the drawing's gradients don't stitch)
    "emb_hat": {"name": "Embroidered Dad Hat", "blueprint": 1729, "provider": 99, "profit": 9.0, "art": "slogan",
                "ink": (245, 238, 222, 255), "colors": ("Black", "Dark Navy", "Charcoal Grey"), "sizes": ("One size",),
                "fill": 0.9},
    "emb_beanie": {"name": "Embroidered Beanie", "blueprint": 1691, "provider": 99, "profit": 8.0, "art": "slogan",
                   "ink": (245, 238, 222, 255), "colors": ("Black", "Navy", "Red", "Spruce", "Dark Grey"),
                   "sizes": ("One size",), "fill": 0.9},
    # the print area is the whole boot's bounding box: the design sits on the leg (left of centre, the foot sticks
    # out to the right), below the cuff, and no wider than the leg
    "stocking": {"name": "Christmas Stocking", "blueprint": 734, "provider": 99, "profit": 7.0, "fill": 0.55,
                 "x": 0.4, "y": 0.56},
    "blanket": {"name": "Sherpa Fleece Blanket", "blueprint": 238, "provider": 99, "profit": 12.0,
                "sizes": ('50" \u00d7 60"', '60" \u00d7 80"'), "fill": 0.75},
    "wrap": {"name": "Wrapping Paper", "blueprint": 1367, "provider": 99, "profit": 5.0, "art": "tile", "fill": 1.0,
             "sizes": ('30" x 36"', '30" x 72"')},     # one sheet's art repeats along a roll; the 15 ft roll leads with a strip photo
    # added 2026-10-30 (user: "do all of those", but Etsy listings cost: none of these are in etsy_products).
    # Left out: the paw stocking (printed in China) and the holiday cards from Australia: too slow for Christmas.
    "pajamas": {"name": "Christmas Pajama Pants", "blueprint": 1254, "provider": 99, "profit": 6.0, "art": "tile",
                "position": "left_leg", "positions": ("left_leg", "right_leg"), "sizes": ("S", "M", "L", "XL", "2XL"),
                "fill": 1.0},
    "puzzle": {"name": "Jigsaw Puzzle", "blueprint": 1149, "provider": 99, "profit": 7.0, "art": "backdrop",  # a third of
               "sizes": ("252 pcs (Vertical)", "520 pcs (Vertical)"), "fill": 1.0},                   # blueprint 596's cost
    "card": {"name": "Christmas Card", "blueprint": 10717, "provider": 28, "profit": 3.0,       # District Photo, US
             "sizes": ('5" x 7" (square corner) / Eggshell / 1 pc', '5" x 7" (square corner) / Eggshell / 10 pcs'),
             "fill": 0.85},
    "doormat": {"name": "Doormat", "blueprint": 1346, "provider": 99, "profit": 6.0, "art": "slogan", "fill": 0.85},
    "desk_mat": {"name": "Desk Mat", "blueprint": 975, "provider": 99, "profit": 7.0, "art": "tile", "fill": 1.0},
    "can_cooler": {"name": "Can Cooler", "blueprint": 636, "provider": 99, "profit": 5.0,
                   "positions": ("front", "back"), "fill": 0.9},
}
SWEATER_PRODUCTS = ["ugly_sweater", "sweater_tee", "sweater_hoodie"]
SWEATER_NOTE = """This one is an UGLY CHRISTMAS SWEATER design: the words are knitted in chunky sweater letters over a
knitted picture we draw for you. Pick the picture that fits your pun and add it to your JSON as "motif", one of:
tree (a Christmas tree strung with glowing lights and tinsel), reindeer, snowman, gingerbread (a gingerbread man),
ornament (a bauble), lights (a string of coloured bulbs), candy_canes, santa_hat, snowflake, present, elf, wreath.
The slogan is a short, punchy pun of 2-4 words (never a sentence) that plays on that picture or on lights, trees, being
lit, ornaments, tinsel, Santa or the season, in the spirit of "GET LIT" but your own. Keep every word to 11 letters or
fewer: a long word has to be knitted small. Put "ugly christmas sweater" in the title and tags; image_prompt can just
name the motif."""
# Holiday lines: while a season runs, one extra design a day is made for it, on the products that sell for it.
# (start, end) are (month, day); the end leaves time to ship before the holiday. Two lines running at once
# take turns, a day each. Halloween is left out this
# year: added on Oct 26, too late to arrive by the 31st.
SEASONS = {
    "christmas": {"start": (10, 26), "end": (12, 14),
                  "products": ["tee", "crewneck", "hoodie", "mug", "ornament", "tumbler", "poster", "tote",
                               "emb_beanie", "stocking", "wrap", "emb_hat", "puzzle", "card", "doormat",
                               "desk_mat", "can_cooler"],
                  "note": "a Christmas / holiday-season gift (ugly-sweater energy, holiday stress, family chaos, "
                          "Santa, elves, reindeer, snowmen, gingerbread, gift-giving, winter break). Aim most designs "
                          "at what people search for in December: a white elephant / Secret Santa gag gift, a gift for "
                          "a coworker or boss, an ugly Christmas sweater party, a matching family or friend-group "
                          "shirt; say which one in the title and tags"},
    "thanksgiving": {"start": (11, 1), "end": (11, 18),
                     "products": ["tee", "crewneck", "hoodie", "mug", "tote"],
                     "note": "Thanksgiving (turkey, pie, food coma, family dinner chaos, Friendsgiving, being thankful "
                             "ironically)"},
    "halloween": {"start": (9, 15), "end": (10, 20),
                  "products": ["tee", "crewneck", "hoodie", "mug", "tote"],
                  "note": "Halloween (spooky season, ghosts, skeletons, pumpkins, witches, costumes, candy)"},
}
SEASON_NOTE = """This design is for the shop's {name} line: make it about {note}. Same voice: funny, edgy,
meme-literate and giftable, not a generic greeting card. Put the holiday in the title and tags (e.g. "funny
christmas shirt", "christmas gift for coworker", "ugly christmas sweater")."""
# One job each (2026-11-03): the job Christmas line, and the jobs among the plain designs' audiences
JOBS = ["nurses", "ER nurses", "ICU nurses", "teachers", "kindergarten teachers", "accountants", "software engineers",
        "construction workers", "electricians", "plumbers", "truck drivers", "hairstylists", "dental hygienists",
        "pharmacists", "paramedics", "firefighters", "police dispatchers", "lawyers", "librarians", "retail workers",
        "servers and bartenders", "warehouse workers", "mechanics", "vet techs", "social workers", "therapists",
        "real estate agents", "HR managers", "school bus drivers", "mail carriers", "flight attendants",
        "physical therapists", "radiology techs", "respiratory therapists", "speech therapists", "CNAs",
        "lunch ladies", "school counselors", "bookkeepers", "baristas"]
# Narrow audiences (2026-10-30): "funny christmas shirt" is up against millions of listings, "christmas gift for
# an ER nurse" against a few hundred, and its buyer already knows what they want. Every design is aimed at one,
# the one used least lately.
AUDIENCES = ["nurses", "ER nurses", "teachers", "kindergarten teachers", "accountants", "software engineers",
             "construction workers", "electricians", "truck drivers", "hairstylists", "dental hygienists",
             "pharmacists", "paramedics", "firefighters", "lawyers", "librarians", "retail workers", "servers and bartenders",
             "warehouse workers", "remote workers", "gym lovers", "runners", "golfers", "fishermen", "hunters",
             "gamers", "book lovers", "coffee addicts", "plant lovers", "bakers", "home cooks", "gardeners",
             "introverts", "new dads", "new moms", "grandmas", "grandpas", "college students", "grad students",
             "dog dads", "dog moms", "cat moms", "cat dads", "chicken keepers", "horse girls", "camping lovers"]
AUDIENCE_NOTE = """Aim this design at ONE narrow audience: {}. The joke is one only they'd fully get (their daily
life, in-jokes, pet peeves, jargon), and the title and at least 6 of the tags say who it's for, the way gift shoppers
search (e.g. "funny nurse christmas shirt", "gift for nurse", "er nurse humor", "nurse gift idea")."""
# Christmas lines beside the plain and ugly-sweater designs (2026-10-30): matching family shirts (bought 4-6 at a
# time), pet gear, and couples' "first Christmas" keepsakes. Each has its own audiences and products.
LINES = {
    "family": {"products": ["tee", "youth_tee", "toddler_tee", "baby_bodysuit", "crewneck", "hoodie", "pajamas"],
               "audiences": ["the whole family, parents and kids", "a family with a new baby (baby's first Christmas)",
                             "a friend group (Friendsmas)", "cousins at Grandma's house", "grandparents and grandkids",
                             "a loud, chaotic family with too many kids", "siblings who fight over everything",
                             "a family that's competitive at board games", "a family whose dog runs the house",
                             "a family that's always late"],
               "note": """This is a MATCHING FAMILY / GROUP shirt: everyone (adults, kids, toddlers, the baby) wears
the same design in a photo, so the slogan must work on all of them and stay kid-appropriate (no swearing, nothing
crude). Think "CERTIFIED CHRISTMAS CHAOS CREW" energy, but your own. Title and tags say "matching family christmas
shirts", "family christmas pajama shirt", "christmas group shirts" and the audience."""},
    "pet": {"products": ["pet_bandana", "ornament", "mug", "tee", "stocking", "doormat"],
            "audiences": ["golden retrievers", "french bulldogs", "labradors", "german shepherds", "dachshunds",
                          "corgis", "pit bulls", "huskies", "chihuahuas", "australian shepherds", "beagles",
                          "doodles (goldendoodles, labradoodles)", "border collies", "shih tzus", "boxers",
                          "black cats", "orange cats", "tuxedo cats", "rescue dogs", "senior dogs",
                          # added 2026-11-03: every breed is its own search, so the batch goes wide
                          "great danes", "rottweilers", "pugs", "yorkies", "cavaliers (king charles spaniels)",
                          "bernese mountain dogs", "boston terriers", "poodles", "miniature schnauzers",
                          "cocker spaniels", "shiba inus", "greyhounds", "bulldogs", "pomeranians",
                          "basset hounds", "weimaraners", "maltese", "havanese", "siamese cats", "maine coons",
                          "calico cats", "grey tabby cats", "ragdoll cats", "sphynx cats"],
            # each pet design brings this many more breeds the same day (free channels only, never Etsy)
            "series": 2,
            "note": """This is a PET line design, for {audience} and the people who own them: it's sold as a dog
bandana (worn by the pet) and as an ornament, mug, shirt and stocking for the owner. Draw that breed
or kind of animal (cute, funny, a little unhinged), with a short slogan that works both on the pet and on the owner's
mug. No swearing. A pun on the breed's name is best when one works ("MERRY CORGMAS", "DACHSHUND THROUGH THE SNOW"
energy, but your own). Title and tags name the breed (e.g. "corgi christmas", "corgi mom gift", "dog christmas
bandana")."""},
    "couple": {"products": ["ornament", "mug", "blanket", "stocking", "tee", "puzzle", "doormat", "card"],
               "audiences": ["couples' first Christmas together", "first Christmas married", "first Christmas engaged",
                             "first Christmas as parents", "first Christmas in our new home",
                             "a long-distance couple's Christmas", "first Christmas with our rescue pet"],
               "note": """This is a COUPLES / MILESTONE keepsake for {audience}, mostly bought as an ornament. Sweet
with a wink (cute, funny, never crude): a cosy illustration and a slogan with the year {year}, e.g. "OUR FIRST
CHRISTMAS {year}" but your own twist. Title and tags say "our first christmas ornament {year}" (or the milestone),
"couples christmas gift", "keepsake ornament"."""},
    # 2026-11-03: Christmas shirts for one job, bought as coworker gifts and for work ugly-sweater days
    "job": {"products": ["tee", "crewneck", "hoodie", "mug", "ornament", "emb_beanie", "tumbler"],
            "audiences": JOBS,
            "look": "retro",
            "series": 1,
            "note": """This is a JOB CHRISTMAS line design for {audience}: the shirt they wear to the work holiday
party or that a coworker buys them. Mix Christmas with their job's jargon, tools and daily pain (e.g. a nurse:
"MERRY CHRISTMAS, YOUR VITALS ARE FESTIVE" energy, but your own). Draw one funny character or object from that job
in a Christmas way (a stethoscope in a Santa hat, a toolbox with tinsel). Mild swearing at most: a boss or coworker
has to be able to give it. Title and tags lead with the job, the way people search: "{audience} christmas shirt",
"christmas gift for {audience}", "{audience} gift", "funny {audience} christmas"."""},
}
# Shirts from our viral "what if" videos (2026-11-03, user: "for each different topic could we make a shirt"):
# one design per posted video topic, on top of the rest, free channels only (Etsy charges per listing).
# Never the real people in a video (no names, faces or likenesses): only the idea, the non-human side, the matchup.
VIRAL_STATE = ROOT / "memory" / "viral.json"
VIDEO_PRODUCTS = ["tee", "hoodie", "crewneck", "mug", "poster", "tote"]
VIDEO_NOTE = """This design comes from one of our viral "what if" videos on TikTok: "{topic}". Make the shirt
someone who loved that video would buy: the video's question, matchup or wild idea turned into a funny, quotable
slogan and one bold original illustration (e.g. a video "could a gorilla beat 100 men" becomes a smug cartoon gorilla
and "100 MEN? WARM-UP."). Put the topic in the title and tags the way people search for it.

HARD RULE: no real person in any way. If the topic names a real person (athlete, celebrity, streamer, politician),
leave them out completely: no name, nickname, initials, jersey number, face, likeness or "you know who" hint. Use the
other side of the matchup (a devil, an animal, a planet), the sport or setting, or the viewer ("1V1 ME, SATAN" style).
Also add a key "real_people" to your JSON: a list of every real person named in the topic (empty if none)."""
# Look per line: "retro" sets the slogan arched over a 70s sunset with a worn print texture (worker/retro.py);
# lines without one alternate between it and the plain caption, so both keep getting tried
RETRO_LINES = {"job", "family", "pet", "plain", "video"}
# a Christmas day's holiday designs, by slot: the first is the one Etsy gets, so it's always a line buyers search
# for by name (dog breed or job), taking turns; the ugly sweater (the user's favourite) is always second; the third
# takes turns between the gift lines. 2026-11-03 (13 Etsy visits in 30 days, zero sales from 52 general jokes):
# Etsy only gets the niche lines, where a new shop isn't up against thousands of sellers.
NICHE_ROTATION = ["pet", "job"]
GIFT_ROTATION = ["couple", "family"]
CHRISTMAS_ROTATION = NICHE_ROTATION + GIFT_ROTATION
# Sales channels connected in Printify that designs are listed on. eBay titles stop at 80 characters. The
# Printify Pop-Up Store charges nothing to list, so it gets every design and comes first on the site's buy links.
CHANNELS = {"popup": {"name": "our shop", "title_max": 140}, "etsy": {"name": "Etsy", "title_max": 140},
            "ebay": {"name": "eBay", "title_max": 80}, "tiktok": {"name": "TikTok Shop", "title_max": 140}}
# TikTok Shop rejects listings that swear (2026-11-08), so designs whose words do stay off it
PROFANE = re.compile(r"(?<![a-z])(f+u+c+k|sh[i1]t|b[i1]tch|ass(hole)?|damn|dick|cunt|piss|wtf|stfu|bastard)", re.I)
# Printify renders dozens of product photos but sends Etsy one or two, and only says what they are when the
# product is created; these are saved then, for the listing (once Jarvis can reach Etsy) and the storefront.
# Left out: the folded shirt (it hides the design) and the mug scenes (they look at the side of the mug,
# where a centred design wraps out of sight). Every listing also gets a close-up of the design itself.
EXTRA_PHOTOS = {"tee": ("person-1", "person-2", "lifestyle", "hanging-1", "duo"),
                "hoodie": ("person-1", "person-2", "lifestyle", "duo"), "tank": ("person-1", "person-2", "lifestyle"),
                "mug": (),
                "sticker": ("context-1", "context-2")}
MAX_MOCKUPS = 7                     # kinds not listed above get every other angle, up to this many
DEFAULTS = {"enabled": True, "designs_per_day": 1, "time": "06:00",  # live before the 7 AM text
            "products": ["tee", "hoodie", "tank", "shorts", "pants", "hat", "mug",    # the user's lineup (2026-10-24): no stickers
                         "crewneck", "poster", "phone", "tote"],                    # added 2026-10-26
            "holiday_per_day": 1,                # extra designs a day for the running holiday line (0 = none)
            # Etsy charges $0.20 a listing (2026-10-27, user: keep it to ~$0.80 a day): one holiday design a day,
            # on the products that sell for the holidays. 0 a day = nothing new on Etsy.
            "etsy_per_day": 1, "etsy_holiday_only": True,
            # and only designs from these lines (2026-11-03, user agreed: niche only on Etsy); [] = any line
            "etsy_lines": ["pet", "job"],
            # in this order of preference, and at most etsy_per_design of them (3 = $0.60 a day)
            "etsy_products": ["ugly_sweater", "sweater_tee", "sweater_hoodie", "pet_bandana", "ornament", "tee",
                              "youth_tee", "toddler_tee", "hoodie", "mug"],
            "etsy_per_design": 3,
            # eBay lists 250 a month free, and its listings renew monthly against that, so live listings stay under it
            "ebay_max": 250,
            # TikTok Shop (2026-11-08, user: "lets do tik tok shop"): free to list, ~6-8% a sale. New shops get
            # reviewed, so at most this many new listings a day, newest designs first
            "tiktok_per_day": 25,
            # and only these kinds: TikTok takes products printed in the US from Printify's TikTok catalog. The all-over
            # sweater (a provider abroad) was refused 2026-11-11; the T-shirt and hoodie went live
            "tiktok_products": ["sweater_tee", "sweater_hoodie", "tee", "hoodie"],
            # sell-what-sold mix (2026-11-10, user: "whatever is gonna get me more sales"): daily designs alternate a close
            # variant of the winner (same core slogan, slight twist) and a new joke in its style; the extra breed/job
            # designs and the shirts for viral videos are off (never sold)
            "winner_variants": True, "line_series": False, "video_shirts": False,
            # eBay full: end the oldest unsold listing to make room for a winner's spin-off. Off until it's confirmed that
            # deleting the Printify product really ends the eBay listing (else each new one costs ~$0.35)
            "ebay_rotate": False,
            # every daily design riffs on a design that sold, not one in two (2026-11-06, user: focus on what sold)
            "winner_focus": True,
            "video_times": ""}                 # e.g. "11:00,15:00,19:00", one video at each; empty = no videos (off: user prefers the photos)

BLOCKED = re.compile(r"(?<![a-z])(" + "|".join([
    "disney", "pixar", "marvel", "dc comics", "batman", "superman", "spider-?man", "avengers", "star wars", "jedi",
    "harry potter", "hogwarts", "pokemon", "pikachu", "nintendo", "mario", "zelda", "sonic", "minecraft", "fortnite",
    "barbie", "hello kitty", "sanrio", "snoopy", "peanuts", "looney", "mickey", "minnie", "simpsons", "sesame",
    "nike", "adidas", "gucci", "chanel", "louis vuitton", "supreme", "coca-?cola", "pepsi", "starbucks", "mcdonald",
    "nfl", "nba", "mlb", "nhl", "fifa", "ncaa", "olympic", "taylor swift", "swiftie", "beyonce", "elvis", "beatles",
    "friends tv", "the office", "stranger things", "game of thrones", "lord of the rings", "hobbit", "grinch",
    "dr\\.? seuss", "winnie", "bluey", "paw patrol", "peppa", "baby shark", "yoda", "grogu", "trump", "biden",
    "rudolph", "frosty", "elf on the shelf", "charlie brown", "home alone", "christmas story", "lampoon",
    "griswold", "mariah", "polar express", "nightmare before christmas", "jack skellington", "hocus pocus",
    "harley", "jeep", "jordans?", "air jordan", "skibidi", "windows xp", "microsoft", "apple", "iphone", "google", "chatgpt", "openai", "tiktok", "instagram", "netflix", "amazon", "ford", "chevy", "john deere", "yeti", "stanley cup", "lego", "crocs",
]) + r")(?![a-z])", re.I)

IDEA_PROMPT = """You design products for an Etsy print-on-demand shop that sells T-shirts, mugs and stickers to
people aged roughly 16-35 who live online. The shop's voice is edgy, sarcastic and meme-literate: the kind of
shirt that makes a stranger laugh, take a photo or ask where you got it. Not family friendly, not wholesome,
not "Live Laugh Love". Invent ONE new design.

Good directions: internet and meme humour (original jokes in the style of current memes and slang), unhinged or
chaotic energy, brutal honesty about work, school, money, dating, sleep, anxiety, gym, gaming and adulting,
dark-but-harmless humour, absurd deadpan statements, ironic fake motivation, cursed-looking cute animals.
Swearing is fine, uncensored (shit, damn, f*ck, hell, ass, etc.) when it makes the joke land; crude is fine
too, but it still has to be funny and quotable.

{trends}

{notes}

Rules, all required:
- Never: slurs, hate or jokes punching down at a group, sexual acts or nudity, drugs, violence against
  real people, self-harm encouragement, politics or religion.
- Never name or depict a real person (celebrities, streamers, athletes, politicians, people from viral
  clips), and never use song lyrics, song titles or artist names: those get listings taken down.
- No brands, trademarks, logos, characters, celebrities, sports teams, movies, TV, games, songs, quotes or
  slogans someone else owns, and no famous meme images or meme characters (Pepe, Doge, This Is Fine dog,
  Wojak, etc.) - the joke and the drawing must be original.
- The illustration: ONE funny, weird or deadpan character or object (e.g. a raccoon in a hoodie, a burnt-out
  coffee cup, a smug cat, a skeleton at a laptop), whole and centred, bold streetwear-style vector art, thick dark
  outlines, a few punchy colours, isolated on a plain pure white background: no scene, no ground or shadow under it,
  and no text, letters, numbers, signs or speech bubbles anywhere in it (an AI draws it and spells badly). Start
  image_prompt with exactly what it is; if the design is for an animal breed or a job, that breed or job's own
  character IS the subject, drawn so its owner or holder recognises it at a glance. Cute and funny, never creepy.
- The slogan is printed with the picture by us, in big capitals: 2 to 7 words (never more), ALL CAPS, plain ASCII,
  punchy and quotable. Shorter prints bigger and sells better: cut every word the joke doesn't need.
- Title: the slogan first, in Title Case, then what buyers type into Etsy search (e.g. "funny sarcastic shirt",
  "meme shirt", "gen z humor"); if the notes above say what the title must lead with, lead with that and put the
  slogan right after. 60-120 characters, no product type (we add "T-Shirt" etc.).
- Exactly 13 tags, each 20 characters or fewer, lowercase, what buyers search for.
- Don't repeat the theme or joke of these recent designs; pick a different topic: {recent}
- "niche" names the meme or trend you riffed on, or the topic if none fit.

Reply with JSON only:
{{"niche": "...", "image_prompt": "...", "slogan": "...", "title": "...", "description": "2-3 sentences in the shop's voice for the listing", "tags": ["...", ...]}}"""

TRENDS_NOTE = """What the internet is talking about TODAY (from Know Your Meme and Reddit):
{}

Pick the funniest meme, catchphrase, slang term or running joke from that list that works on a shirt (skip
news, sports and anything about a real person) and riff on it: use its phrase or format so people who are
online get it instantly, but write your own joke and draw your own original picture. If nothing there works,
use a meme or slang phrase that's been big online in the last few months instead."""


# ── small pieces ─────────────────────────────────────────────────────────────
# Printify's catalog answers "Too Many Attempts" when every pass asks again; a product's variants don't change
_VARIANTS: dict = {}
_AREAS: dict = {}                    # kind -> {print position: (w, h)}, for the all-over-print sweater
_GROUPS: dict = {}                   # kind -> [(variant ids, (w, h))], one per shape of print area (a round ornament, an oval one)
_FACTS: dict = {}                    # blueprint -> the maker's own facts about it (fabric, fit, care), for the listing


def season_for(day) -> Optional[str]:
    """The holiday line a day's extra design is for, or None outside every season."""
    md = (day.month, day.day)
    running = [n for n, x in SEASONS.items() if x["start"] <= md <= x["end"]]
    return running[day.toordinal() % len(running)] if running else None


def pick_audience(pool: list, designs: list, line: str) -> str:
    """The audience in `pool` used least by the recent designs of the same line (ties go round in order)."""
    recent = [d.get("audience") for d in designs[-80:] if (d.get("style") or "plain") == line]
    return min(pool, key=lambda a: (recent.count(a), (pool.index(a) - len(designs)) % len(pool)))


NOT_NAMES = {"the", "a", "an", "of", "and", "prime", "young", "old", "peak", "could", "would", "what", "if", "vs",
             "versus", "in", "on", "jr", "sr", "mr", "mrs", "dr", "saint", "st", "king", "lord", "big", "little"}


def real_names(topic: str, listed=None) -> list:
    """The words of real people's names to keep off a video shirt: the ones the idea listed, plus any run of two or
    more capitalised words in the topic ("LeBron James"); each name's own words too, so "James" alone is caught."""
    names = [str(n) for n in (listed or []) if str(n).strip()]
    names += [m.group(0) for m in re.finditer(r"\b[A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*)+", topic)]
    out = []
    for n in names:
        for part in [n, *n.split()]:
            part = part.strip(" .,'?!")
            if len(part) >= 3 and part.lower() not in NOT_NAMES and part.lower() not in (o.lower() for o in out):
                out.append(part)
    return out


def video_topics(path: Path = VIRAL_STATE) -> list:
    """The topics of the viral videos that have been posted, oldest first, each once."""
    try:
        videos = json.loads(Path(path).read_text()).get("videos") or []
    except (OSError, ValueError):
        return []
    out = []
    for v in sorted(videos, key=lambda v: v.get("made") or 0):
        topic = " ".join(str(v.get("topic") or v.get("title") or "").split())
        if v.get("posted") and topic and topic.lower() not in (t.lower() for t in out):
            out.append(topic)
    return out


def christmas_style(day, slot: int, per_day: int) -> str:
    """The line a Christmas day's holiday design `slot` is for: first a rotating line (Etsy's pick), then the ugly
    sweater, then the next lines along."""
    if slot == 1 or per_day == 1 and day.toordinal() % 3 == 0:
        return "sweater"
    turn = day.toordinal()
    if slot == 0:
        return NICHE_ROTATION[turn % len(NICHE_ROTATION)]
    return GIFT_ROTATION[(turn // 2 + slot) % len(GIFT_ROTATION)]


def season_running(name: Optional[str], day) -> bool:
    """Whether a season's selling window covers this day (unlike season_for, which takes turns when two overlap)."""
    s = SEASONS.get(name or "")
    return bool(s) and s["start"] <= (day.month, day.day) <= s["end"]


def settings() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        stored = get_plugin_config(NS)
    except Exception:
        stored = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in stored.items() if k in DEFAULTS and v not in (None, "")})
    try:
        out["designs_per_day"] = max(0, min(5, int(out["designs_per_day"])))
    except (TypeError, ValueError):
        out["designs_per_day"] = DEFAULTS["designs_per_day"]
    try:
        out["holiday_per_day"] = max(0, min(3, int(out["holiday_per_day"])))
    except (TypeError, ValueError):
        out["holiday_per_day"] = DEFAULTS["holiday_per_day"]
    for key in ("etsy_per_day", "etsy_per_design", "ebay_max", "tiktok_per_day"):
        try:
            out[key] = max(0, int(out[key]))
        except (TypeError, ValueError):
            out[key] = DEFAULTS[key]
    return out


def load_state(path: Path = STATE) -> dict:
    try:
        st = json.loads(path.read_text())
    except (OSError, ValueError):
        st = {}
    st.setdefault("designs", [])
    st.setdefault("orders_seen", [])
    return st


def save_state(st: dict, path: Path = STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(path)


def overnight(since: float, path: Path = STATE) -> dict:
    """For the morning text: sales since `since` and the designs made since then."""
    st = load_state(path)
    sales = [x for x in st.get("sales_log") or [] if x.get("at", 0) >= since]
    return {"sales": len(sales), "revenue": round(sum(x.get("price") or 0 for x in sales), 2),
            "new": [d.get("slogan") or d.get("title", "") for d in st["designs"] if d.get("made", 0) >= since
                    and any(p.get("published") for p in d.get("products", {}).values())],
            "listed": sum(1 for d in st["designs"] if any(p.get("published") for p in d.get("products", {}).values()))}


def price_for(cost_cents: int, profit: float) -> int:
    """Cents, ending in .99, so `profit` is left after Printify's cost and Etsy's ~15% (fees, payment, ads)."""
    dollars = (cost_cents / 100 + profit) / 0.85
    return int(math.ceil(dollars)) * 100 - 1


def clean_idea(idea: dict) -> dict:
    """Etsy's limits (140-character title, 13 tags of 20 characters), and refuse anything off-limits."""
    if not isinstance(idea, dict) or not idea.get("image_prompt") or not idea.get("title"):
        raise ValueError("the idea came back incomplete")
    text = " ".join(str(idea.get(k, "")) for k in ("niche", "image_prompt", "slogan", "title", "description")) + \
        " " + " ".join(map(str, idea.get("tags") or []))
    hit = BLOCKED.search(text)
    if hit:
        raise ValueError(f"the idea used a protected name ({hit.group(0)})")
    tags, seen = [], set()
    for t in idea.get("tags") or []:
        t = " ".join(re.sub(r"[^a-z0-9 '&-]", "", str(t).lower()).split())
        while len(t) > 20 and " " in t:                    # too long for Etsy: drop whole words, never half of one
            t = t.rsplit(" ", 1)[0]                        # ("funny christmas shir" was a wasted tag)
        if t and len(t) <= 20 and t not in seen:
            seen.add(t)
            tags.append(t)
    slogan = re.sub(r"[^A-Z0-9 '!?&.,*-]", "", str(idea.get("slogan") or "").upper()).strip()
    if len(slogan.split()) > 8:
        raise ValueError("the slogan is too long to print")
    out = {"niche": str(idea.get("niche") or "")[:80], "image_prompt": str(idea["image_prompt"])[:900],
           "slogan": " ".join(slogan.split()), "title": " ".join(str(idea["title"]).split())[:110],
           "description": " ".join(str(idea.get("description") or "").split())[:600], "tags": tags[:13]}
    from worker import sweater
    motif = re.sub(r"[^a-z]+", "_", str(idea.get("motif") or "").lower()).strip("_")
    if motif in sweater.MOTIFS:                            # the sweater line's knitted picture
        out["motif"] = motif
    return out


# ── artwork ──────────────────────────────────────────────────────────────────
def cut_background(img, tolerance: int = 38):
    """The white around the illustration becomes transparent (flood-filled from the edges, so white
    inside the drawing stays)."""
    from PIL import Image, ImageDraw
    img = img.convert("RGBA")
    w, h = img.size
    marker = (255, 0, 255, 0)
    for x, y in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]:
        r, g, b, a = img.getpixel((x, y))
        if r > 225 and g > 225 and b > 225:
            ImageDraw.floodfill(img, (x, y), marker, thresh=tolerance)
    px = img.load()
    for yy in range(h):
        for xx in range(w):
            if px[xx, yy] == marker:
                px[xx, yy] = (255, 255, 255, 0)
    img = drop_specks(img)
    bbox = img.getbbox()
    return img.crop(bbox) if bbox else img


def drop_specks(img, share: float = 0.004):
    """Small loose bits floating away from the drawing (the AI art sometimes leaves a faint signature or scribble of
    text off to the side) are cleared: pieces under `share` of the ink that sit outside the main shape's box."""
    try:
        import numpy as np
        from scipy import ndimage
    except ImportError:
        return img
    alpha = np.array(img.getchannel("A")) > 0
    labels, n = ndimage.label(alpha)
    if n < 2:
        return img
    sizes = ndimage.sum(alpha, labels, range(1, n + 1))
    main = int(np.argmax(sizes)) + 1
    ys, xs = np.nonzero(labels == main)
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
    centers = ndimage.center_of_mass(alpha, labels, range(1, n + 1))
    clear = np.zeros_like(alpha)
    for i, (size, (cy, cx)) in enumerate(zip(sizes, centers), start=1):
        if size < share * alpha.sum() and not (y0 <= cy <= y1 and x0 <= cx <= x1):
            clear |= labels == i
    if not clear.any():
        return img
    out = np.array(img)
    out[clear] = (255, 255, 255, 0)
    from PIL import Image
    return Image.fromarray(out, "RGBA")


def compose(art, slogan: str, width: int = DESIGN_W, layout: str = "tall", look: str = "plain", seed: int = 0):
    """The print: the drawing with the slogan set big beside or under it (worker/lettering.py); a transparent PNG."""
    from worker import lettering
    return lettering.compose(art, slogan, layout=layout, look=look, seed=seed, width=width)


# What a drawing is checked for before it's printed (2026-11-10). The AI that draws can't spell, sometimes draws the
# wrong animal for a breed's owner, and now and then a creature with red eyes or a spare leg: each of those went
# live before this.
ART_STYLE = (" Bold flat vector sticker illustration, thick clean dark outlines, a few flat punchy colours, one centred "
             "subject shown whole, friendly eyes, plain pure white background, no shadow, no ground, no scenery, "
             "no text, no letters, no numbers, no watermark.")
ART_CHECK = """You check a drawing before it is printed on T-shirts and gifts and sold. An AI image model drew it for
this design:
- the slogan (we print it ourselves beside the picture; it must NOT be in the picture): "{slogan}"
- what the picture should show: {prompt}
- who buys it: {audience}

Look at the attached picture. Report only what a shopper would notice in a listing photo:
- text: any letters, words, numbers, signature or watermark drawn in the picture
- subject: it doesn't show what it should (the wrong animal, or a breed its owner wouldn't recognise as theirs; a
  person where an animal was asked for; the main thing missing)
- glitch: an extra, missing or deformed limb, eye or face; melted or doubled parts; red, blank or creepy eyes on
  something meant to be cute; gibberish symbols
- cropped: the subject is cut off by the edge of the picture
- background: a scene, box or coloured backdrop behind it instead of plain white, or a large shadow or puddle
  under it

Severity "major" = a shopper would notice and it makes the product look cheap, wrong or broken. "minor" = only a
careful designer would notice. Reply with JSON only:
{{"ok": true/false, "problems": [{{"kind": "text|subject|glitch|cropped|background", "severity": "major|minor",
"what": "short description"}}]}}
"ok" is false when there is at least one major problem."""


def art_problems(look: Callable, raw: bytes, idea: dict, audience: Optional[str]) -> Optional[list]:
    """The major problems one look finds in a drawing ([] = it's fine); None when nobody could look."""
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im.thumbnail((768, 768))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        out = look(buf.getvalue(), ART_CHECK.format(slogan=idea.get("slogan") or "(none)", prompt=idea["image_prompt"][:500],
                                                    audience=audience or "anyone who finds it funny"))
    except Exception:
        return None
    if not isinstance(out, dict) or not ("ok" in out or "problems" in out):
        return None
    return [f"{p.get('kind', '?')}: {str(p.get('what', ''))[:100]}" for p in out.get("problems") or []
            if isinstance(p, dict) and p.get("severity") == "major"]


def slogan_case(slogan: str) -> str:
    """I'M LOSING MY TINSEL -> I'm Losing My Tinsel (str.title() makes it "I'M")."""
    return " ".join(w.capitalize() for w in slogan.split())


FACT_WORDS = re.compile(r"material|fabric|cotton|polyester|ceramic|\d ?%|\boz\b|\bfit\b|dishwasher|microwave|"
                        r"lead|bpa|label|one-sided|hanging|machine wash|sherpa|fleece|canvas|rubber|neoprene", re.I)
NOT_FACTS = re.compile(r"available in|shapes|sourced|customi[sz]|personali[sz]|your (design|print)|automatically|"
                       r"disclaimer|providers?\b|assembled|blank product", re.I)


def product_facts(description: str, limit: int = 5) -> list:
    """The maker's facts worth telling a buyer (what it's made of, how it fits, how to wash it), from the catalog's
    description of a blank: its ".:" bullet lines, minus the ones written for sellers or about sizes we don't stock."""
    text = re.sub(r"<br\s*/?>|</div>|</p>|</li>", "\n", description or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text).replace("&nbsp;", " ").replace("&amp;", "&").replace("\u00a0", " ")
    out = []
    for line in text.split("\n"):
        line = " ".join(line.split())
        if not line.startswith(".:"):
            continue
        line = line[2:].strip()
        if 8 <= len(line) <= 260 and FACT_WORDS.search(line) and not NOT_FACTS.search(line) and line not in out:
            out.append(line if line.endswith((".", "!")) else line + ".")
    return out[:limit]


AI_NOTE = ("Designed in our studio with the help of AI image tools, then printed on demand and shipped by our "
           "production partner. Each item is made for you after you order.")


def listing_text(rec: dict, facts: list, closing: str = AI_NOTE) -> str:
    """A listing's description: the pitch, what the design says, what the product is made of, how it's made."""
    parts = [rec["description"]]
    if rec.get("slogan"):
        parts.append(f"It reads: \"{slogan_case(rec['slogan'])}\".")
    if facts:
        parts.append("Details:\n" + "\n".join(f"- {f}" for f in facts))
    return "\n\n".join(parts + [closing])


def slogan_art(slogan: str, size: tuple = (3300, 1500), ink: tuple = (34, 30, 38, 255)):
    """The slogan alone, for a trucker hat's wide front panel (white foam on every colour): bold dark letters.
    Embroidered caps get light thread (`ink`) on dark colours."""
    from PIL import Image, ImageDraw, ImageFont
    w, h = size
    draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    pt = 520
    while pt > 100:
        font = ImageFont.truetype(FONT, pt)
        lines = _wrap(slogan, font, draw, w * 0.94)
        if len(lines) <= 3 and len(lines) * pt * 1.15 <= h * 0.92:
            break
        pt -= 20
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    line_h = round(pt * 1.15)
    y = (h - line_h * len(lines)) / 2
    for line in lines:
        tw = d.textlength(line, font=font)
        d.text(((w - tw) / 2, y), line, font=font, fill=ink)
        y += line_h
    return canvas


def tile_art(design, size: tuple, across: int = 4):
    """Wrapping paper: the design repeated in staggered rows over the whole sheet, on white."""
    from PIL import Image
    w, h = size
    cell = w // across
    art = design.copy()
    art.thumbnail((int(cell * 0.8), int(cell * 0.8)), Image.LANCZOS)
    canvas = Image.new("RGB", size, (255, 255, 255))
    row_h = int(art.height * 1.15) or 1
    for row in range(-1, h // row_h + 2):
        shift = cell // 2 if row % 2 else 0
        for col in range(-1, across + 1):
            x = col * cell + shift + (cell - art.width) // 2
            canvas.paste(art, (x, row * row_h), art)
    return canvas


def backdrop_art(design, size: tuple, color: tuple = (244, 239, 230)):
    """A puzzle's picture: the design in the middle of a soft cream sheet the shape of the print area."""
    from PIL import Image
    art = design.copy()
    art.thumbnail((int(size[0] * 0.82), int(size[1] * 0.82)), Image.LANCZOS)
    canvas = Image.new("RGB", size, color)
    canvas.paste(art, ((size[0] - art.width) // 2, (size[1] - art.height) // 2), art)
    return canvas


def _wrap(text: str, font, draw, width: int) -> list:
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) <= width * 0.96 or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    return lines + ([cur] if cur else [])


def design_closeup(design: Path, dest: Path, size: int = 2000, detail: bool = False, dark: bool = False) -> None:
    """The design on a soft square backdrop: a clear look at the artwork for the listing photos.
    `detail` zooms in on the middle of the illustration, from the original drawing. `dark`: a near-black backdrop,
    for designs lettered in cream for dark garments (on the cream one their words vanished)."""
    from PIL import Image
    art = Image.open(design).convert("RGBA")
    raw = design.with_name(design.stem + "-raw.jpg")
    if detail and raw.exists():
        art = cut_background(Image.open(raw))                    # the drawing alone, no slogan
        w, h = art.size                                          # then the middle 70 %, filling the frame
        art = art.crop((int(w * 0.15), int(h * 0.12), int(w * 0.85), int(h * 0.82)))
        art = art.resize((size, round(art.height * size / art.width)), Image.LANCZOS)
        backdrop = (226, 236, 228, 255)
    else:
        art.thumbnail((int(size * 0.8), int(size * 0.8)), Image.LANCZOS)
        backdrop = (26, 26, 30, 255) if dark else (244, 239, 230, 255)
    canvas = Image.new("RGBA", (size, size), backdrop)
    canvas.paste(art, ((size - art.width) // 2, (size - art.height) // 2), art)
    canvas.convert("RGB").save(dest, "JPEG", quality=90)


def png_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


ORDER_STATUS = {
    "sending-to-production": "is being sent to the printer",
    "in-production": "is being printed",
    "fulfilled": "has shipped",
    "partially-fulfilled": "has partly shipped",
    "canceled": "was canceled",
    "has-issues": "has a problem: check it in Printify",
    "on-hold": "is on hold in Printify (check your payment card)",
}


def order_update(order: dict, items: str, channel: str) -> Optional[str]:
    """The text for an order that changed status, with tracking once it ships."""
    what = ORDER_STATUS.get(str(order.get("status")))
    if not what:
        return None
    line = f"📦 Your {channel} order ({items}) {what}"
    track = next((sh for sh in order.get("shipments") or [] if sh.get("url") or sh.get("number")), None)
    if track and order.get("status") in ("fulfilled", "partially-fulfilled"):
        line += f": {track.get('carrier', '').upper()} {track.get('url') or track.get('number')}"
    return line + "."


GARMENT_WORDS = re.compile(r"\b(?:t-?shirts?|tees?|shirts?|tops?)\b", re.I)


def sweater_title(title: str) -> str:
    """The sweater line's search words up front ("Ugly Christmas Sweater" is what people type), in title case."""
    rest = re.sub(r"\bugly\s+christmas\s+sweat(?:er|\s*shirt)s?\b|\bsweat\s*shirts?\b|\bsweaters?\b", " ", title, flags=re.I)
    words = [w if any(c.isupper() for c in w[1:]) else w[:1].upper() + w[1:] for w in rest.split()]
    return " ".join(["Ugly Christmas Sweater"] + words)[:110]


def listing_title(base: str, product: str, limit: int) -> str:
    """'<title> <product>' within the channel's limit, cut at a word so the product name always fits. The idea's
    title often says "Shirt" or "Tee" anyway: dropped, so a mug isn't sold as a "Christmas Shirt ... Mug"."""
    base = re.sub(rf"\b{re.escape(product)}\b", " ", GARMENT_WORDS.sub(" ", base), flags=re.I)   # said once, at the end
    base = " ".join(base.split()).strip(" ,-|&")
    room = limit - len(product) - 1
    if len(base) > room:
        base = base[:room].rsplit(" ", 1)[0].rstrip(" ,-|&")
    return f"{base} {product}"


def products_in(rec: dict, channel: str) -> dict:
    """A design's products in one channel. Etsy's live in rec["products"] (the shop started there)."""
    if channel == "etsy":
        return rec.setdefault("products", {})
    return rec.setdefault("channels", {}).setdefault(channel, {})


def placements(kind: str, image_id: str, size: tuple, area: tuple) -> list:
    """Where the design goes in the print area. Printify's scale is relative to the area's width.
    Centred, even on a mug: the photo Printify sends to Etsy looks straight at the middle of the wrap,
    and a copy on each side of the handle (tried 2026-10-24) puts a seam right there.
    A product that isn't the rectangle its print area claims gets the design sized to what's really there: an
    "ellipse" (a round or oval ornament: the biggest box of the design's proportions inside the curve) or a
    "triangle" (a bandana, point down). Measured on Printify's photos, a bandana's cloth shows from 15% of the
    print area's height (above is hem) to its point at 90%, and on a dog the top quarter of that rolls under the
    collar: the design hangs from 32%, its sides inside the slanting edges for most of its height (only the
    picture's bottom corners, which are empty, pass them)."""
    (w, h), (pw, ph) = size, area
    p = PRODUCTS[kind]
    y = p.get("y", 0.5)
    fill = (p.get("fills") or {}).get(round(pw / ph, 2), p["fill"])     # a shape of area with its own margin
    if p.get("shape") == "ellipse":
        scale = 2 / math.hypot(2 / pw, 2 * h / (w * ph)) / pw
    elif p.get("shape") == "triangle":
        edge, tip, top = 0.15, 0.90, 0.32
        box_w = min(pw * (tip - top) * ph / ((tip - edge) * ph + 0.85 * pw * h / w), (tip - top) * 0.96 * ph * w / h)
        scale = box_w / pw
        y = top + box_w * h / w / ph / 2
    else:
        scale = min(1.0, (ph / pw) * (w / h))
    return [{"id": image_id, "x": p.get("x", 0.5), "y": round(y, 3), "scale": round(scale * fill, 3), "angle": 0}]


def native_ground(rec: dict) -> str:
    """What a design's main artwork was drawn for: the sweater line is lettered in cream for dark garments, the rest
    in dark ink for light ones."""
    return "dark" if rec.get("style") == "sweater" else "light"


def variant_key(rec: dict, kind: str) -> Optional[str]:
    """Which rendering of a design a product kind takes, e.g. "wide-light"; None for the main artwork."""
    p = PRODUCTS[kind]
    layout, ground = p.get("layout", "tall"), p.get("ground", "light")
    return None if (layout == "tall" and ground == native_ground(rec)) else f"{layout}-{ground}"


def design_seed(rec: dict) -> int:
    """The seed a design was drawn with (designs from before seeds were kept: one from its id, always the same)."""
    return int(rec["seed"]) if rec.get("seed") is not None else int(re.sub(r"\D", "", str(rec.get("id"))) or 0) % 100000


def render_variant(rec: dict, layout: str, ground: str):
    """A design drawn again in another layout or for the other ground; None when it can't be (no slogan, the original
    drawing is gone, or a dark-ink design asked for in light ink)."""
    from PIL import Image
    slogan = rec.get("slogan")
    if not slogan:
        return None
    if rec.get("style") == "sweater":
        from worker import sweater
        return sweater.design(slogan, design_seed(rec), motif_name=rec.get("motif") or "tree", ground=ground, layout=layout)
    if ground != "light":
        return None
    base = Path(rec.get("file", ""))
    cut, raw = Path(rec.get("art") or base.with_name(f"{rec.get('id')}-art.png")), base.with_name(f"{rec.get('id')}-raw.jpg")
    if cut.is_file():
        art = Image.open(cut).convert("RGBA")
    elif raw.is_file():
        art = cut_background(Image.open(raw))
    else:
        return None
    return compose(art, slogan, layout=layout, look=rec.get("look") or "plain", seed=design_seed(rec))


# ── the services ─────────────────────────────────────────────────────────────
class Clients:
    """Gemini, Cloudflare and Printify, each replaceable in tests."""

    def __init__(self):
        from memory.config_manager import get_plugin_config
        self.printify_token = str(get_plugin_config("printify").get("token") or "").strip()
        cf = get_plugin_config("cloudflare")
        self.cf_account, self.cf_token = str(cf.get("account_id") or "").strip(), str(cf.get("api_token") or "").strip()

    def ask(self, prompt: str) -> Optional[dict]:
        """The agents' thinking (Trend Scout, Analyst): one Gemini call, JSON back, None if it didn't answer."""
        from core import gemini
        return gemini.as_json(prompt, tier=gemini.SMART)

    def idea(self, recent: list, trending: str = "", notes: str = "") -> dict:
        from core import gemini
        out = gemini.as_json(IDEA_PROMPT.format(recent="; ".join(recent[-40:]) or "none yet",
                                                trends=TRENDS_NOTE.format(trending) if trending else "", notes=notes),
                             tier=gemini.SMART)
        if out is None:
            raise RuntimeError("Gemini didn't answer (out of free quota?)")
        return out

    def draw(self, prompt: str) -> bytes:
        import requests
        r = requests.post(f"https://api.cloudflare.com/client/v4/accounts/{self.cf_account}/ai/run/{FLUX}",
                          headers={"Authorization": f"Bearer {self.cf_token}"},
                          json={"prompt": prompt.rstrip(". ") + "." + ART_STYLE, "steps": 8}, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"Cloudflare image failed ({r.status_code}): {r.text[:160]}")
        return base64.b64decode(r.json()["result"]["image"])

    def look(self, image: bytes, prompt: str) -> Optional[dict]:
        """One look at a picture by the smart model (Claude on the membership, then Gemini): its JSON answer, None if
        nobody answered."""
        from core import gemini
        from google.genai import types
        return gemini.as_json([types.Part.from_bytes(data=image, mime_type="image/jpeg"), prompt], tier=gemini.SMART,
                              timeout_ms=120_000)

    def printify(self, method: str, path: str, body=None):
        import requests
        r = requests.request(method, API + path, headers={"Authorization": f"Bearer {self.printify_token}",
                                                          "User-Agent": "Jarvis"}, json=body, timeout=120)
        if r.status_code >= 400:
            raise RuntimeError(f"Printify {method} {path.split('?')[0]} failed ({r.status_code}): {r.text[:200]}")
        return r.json() if r.content else {}


class Shop:
    def __init__(self, clients=None, *, state_path: Path = STATE, art_dir: Path = ART,
                 cfg: Callable[[], dict] = settings, log: Callable[[str], None] = print,
                 notify: Optional[Callable[[str, str], None]] = None, clock: Callable[[], float] = time.time,
                 trends_fetch: Optional[Callable[[], dict]] = None, notebook_path: Optional[Path] = None):
        from worker import shop_agents
        self.c = clients or Clients()
        self.trends_fetch = trends_fetch
        self.nb_path = notebook_path or shop_agents.NOTEBOOK
        self.state_path, self.art_dir, self.cfg, self.log, self.clock = state_path, art_dir, cfg, log, clock
        self.notify = notify or (lambda kind, text: None)
        self._variants = _VARIANTS if clients is None else {}     # the real catalog is shared across passes

    # which Printify stores are connected to which sales channel
    def stores(self) -> dict:
        """{"etsy": store, "ebay": store, "popup": store} for the channels connected in Printify."""
        out = {}
        for shop in self.c.printify("GET", "shops.json"):
            channel = str(shop.get("sales_channel") or "")
            if channel == "storefront" or "pop" in channel.lower():   # the Pop-Up Store ("storefront" in the API)
                channel = "popup"
            if "tiktok" in channel.lower():
                channel = "tiktok"
            if channel in CHANNELS:
                out.setdefault(channel, shop)
        return out

    def on_etsy(self, rec: dict, st: dict) -> bool:
        """Whether a design goes on Etsy: the first niche-line holiday design (or winner spin-off) of each day, up to
        etsy_per_day. Decided once per design and kept."""
        if "etsy" not in rec:
            cfg = self.cfg()
            day = datetime.fromtimestamp(rec["made"]).date()
            picked = sum(1 for d in st["designs"] if d.get("etsy") and datetime.fromtimestamp(d["made"]).date() == day)
            wanted = (bool(rec.get("season") or rec.get("spinoff_of")) or not cfg.get("etsy_holiday_only")) \
                and not rec.get("series")
            lines = cfg.get("etsy_lines") or []
            if lines and not rec.get("spinoff_of") and rec.get("style") not in lines:
                wanted = False                   # a winner's spin-off still goes on; otherwise niche lines only
            rec["etsy"] = wanted and picked < cfg.get("etsy_per_day", 1)
        return rec["etsy"]

    def etsy_shop(self) -> Optional[dict]:
        return self.stores().get("etsy")

    def any_shop(self) -> dict:
        shop = self.etsy_shop()
        if shop:
            return shop
        shops = self.c.printify("GET", "shops.json")
        if not shops:
            raise RuntimeError("there's no store in Printify")
        return shops[0]

    def variants(self, kind: str) -> tuple:
        """(variant ids to sell, placeholder size) for a product kind."""
        if kind in self._variants:
            return self._variants[kind]
        p = PRODUCTS[kind]
        data = self.c.printify("GET", f"catalog/blueprints/{p['blueprint']}/print_providers/{p['provider']}/variants.json")
        chosen = []
        for v in data["variants"]:
            title = v.get("title", "")
            opts = v.get("options") or {}
            if "colors" in p and opts.get("color") not in p["colors"]:
                continue
            if any(opts.get(k) != want for k, want in (p.get("match") or {}).items()):
                continue
            if any(opts.get(k) not in allowed for k, allowed in (p.get("options") or {}).items()):
                continue
            size = opts.get("size") or title.split(" / ")[-1]
            if "sizes" in p and size not in p["sizes"] and not any(title.startswith(s) for s in p["sizes"]):
                continue
            chosen.append(v)
        if kind == "mug":
            white = [v for v in chosen if "white" in v.get("title", "").lower()]
            chosen = white or chosen
        if not chosen:
            raise RuntimeError(f"no {p['name']} variants matched")
        front = next(ph for ph in chosen[0]["placeholders"] if ph["position"] == p.get("position", "front"))
        _AREAS[kind] = {ph["position"]: (ph["width"], ph["height"]) for ph in chosen[0]["placeholders"]}
        groups: dict = {}                    # variants by the shape of their print area: each gets its own placement
        for v in chosen[:100]:
            ph = next((x for x in v["placeholders"] if x["position"] == front["position"]), front)
            groups.setdefault(round(ph["width"] / ph["height"], 2), ((ph["width"], ph["height"]), []))[1].append(v["id"])
        _GROUPS[kind] = [(ids, area) for area, ids in groups.values()]
        self._variants[kind] = ([v["id"] for v in chosen][:100], (front["width"], front["height"]))
        return self._variants[kind]

    def facts(self, kind: str) -> list:
        """What the product is made of and how it fits, in the maker's words, for the listing (asked once a blank)."""
        bp = PRODUCTS[kind]["blueprint"]
        if bp not in _FACTS:
            try:
                _FACTS[bp] = product_facts((self.c.printify("GET", f"catalog/blueprints/{bp}.json") or {}).get("description", ""))
            except Exception:
                return []                        # the catalog is busy: this listing goes without, the next asks again
        return _FACTS[bp]

    def trending(self, st: dict) -> str:
        """The Trend Scout's picks from today's memes and internet talk (feeds fetched once a day, saved into st, which the
        caller saves; the picks are cached in the notebook)."""
        from worker import shop_agents, trends
        today = datetime.fromtimestamp(self.clock()).date().isoformat()
        cached = st.get("trends") or {}
        if cached.get("day") != today:
            found = self.trends_fetch() if self.trends_fetch else trends.fetch()
            cached = {"day": today, "text": trends.brief(found)}
            if found:
                st["trends"] = cached
        nb = shop_agents.load(self.nb_path)
        text = shop_agents.scout(nb, cached.get("text", ""), getattr(self.c, "ask", None), today, self.clock)
        shop_agents.save(nb, self.nb_path)
        return text

    # one design, start to finish
    def make_design(self, only: Optional[list] = None, spinoff: Optional[str] = None, season: Optional[str] = None,
                    style: Optional[str] = None, series: bool = False, video: Optional[str] = None,
                    variant: bool = False) -> dict:
        """only: the product kinds this design goes on (e.g. ["tee"]); None means the usual set. spinoff: the id of a winning
        design to riff on instead of today's trends. series: one of the extra breeds/jobs a line design brings with it
        (it doesn't bring more, and never goes on Etsy). video: a viral video's topic to make a shirt of (no real
        people, never on Etsy)."""
        from worker import shop_agents
        st = load_state(self.state_path)
        recent = [d.get("niche", "") + ": " + d.get("slogan", "") for d in st["designs"]]
        parent = next((d for d in st["designs"] if d.get("id") == spinoff), None) if spinoff else None
        if parent and parent.get("style") != "video":       # same look, products and season as the design that sold
            style = style or parent.get("style")
            season = season or parent.get("season")
        trending = "" if parent else self.trending(st)
        nb = shop_agents.load(self.nb_path)
        taken = [d.get("slogan", "") for d in st["designs"] if parent and d.get("spinoff_of") == parent["id"]]
        notes = shop_agents.designer_notes(nb, parent, variant=variant and bool(parent), taken=taken)
        if season:
            notes = SEASON_NOTE.format(name=season.title(), note=SEASONS[season]["note"]) + "\n\n" + notes
        if style == "sweater":
            notes = SWEATER_NOTE + "\n\n" + notes
            only = only or SWEATER_PRODUCTS
        if video:
            style = "video"
            notes = VIDEO_NOTE.format(topic=video) + "\n\n" + notes
            only = only or VIDEO_PRODUCTS
        line = LINES.get(style or "")
        audience = parent.get("audience") if parent else None if video else pick_audience(line["audiences"] if line else AUDIENCES, st["designs"], style or "plain")
        if line:
            year = datetime.fromtimestamp(self.clock()).year
            notes = line["note"].format(audience=audience, year=year) + "\n\n" + notes
            only = only or line["products"]
        elif audience:
            notes = AUDIENCE_NOTE.format(audience) + "\n\n" + notes
        last_error = None
        for _ in range(3):                                      # a refused idea gets another try
            try:
                raw_idea = self.c.idea(recent, trending, notes)
                idea = clean_idea(raw_idea)
                if video:
                    named = real_names(video, raw_idea.get("real_people") if isinstance(raw_idea, dict) else None)
                    text = " ".join(str(idea.get(k, "")) for k in ("image_prompt", "slogan", "title", "description")) + \
                        " " + " ".join(idea["tags"])
                    hit = next((n for n in named if re.search(r"(?<![a-z])" + re.escape(n) + r"(?![a-z])", text, re.I)), None)
                    if hit:
                        raise ValueError(f"the idea used a real person's name ({hit})")
                break
            except ValueError as exc:
                last_error = exc
                self.log(f"shop: idea refused: {exc}")
                shop_agents.problem(nb, f"Designer's idea was refused: {exc}", self.clock)
                shop_agents.save(nb, self.nb_path)
        else:
            raise RuntimeError(f"no usable idea in three tries ({last_error})")
        if style == "sweater":
            idea["title"] = sweater_title(idea["title"])
        else:
            idea.pop("motif", None)
        from PIL import Image
        stamp = datetime.fromtimestamp(self.clock()).strftime("%Y%m%d-%H%M%S")
        self.art_dir.mkdir(parents=True, exist_ok=True)
        path = self.art_dir / f"{stamp}.png"
        seed = int(self.clock()) if style == "sweater" else len(st["designs"])
        extra = {}
        if style == "sweater":
            from worker import sweater
            # a close variant of a winner keeps the winner's picture; otherwise the Designer's pick (a lit tree if none)
            idea["motif"] = (parent.get("motif") or "tree") if (variant and parent) else idea.get("motif") or "tree"
            design = sweater.design(idea["slogan"] or idea["title"][:30], seed=seed, motif_name=idea["motif"])
        else:
            raw, extra = self.draw_checked(idea, audience)
            art = cut_background(Image.open(io.BytesIO(raw)))
            look = self.look_for(style, st["designs"]) if idea["slogan"] else "plain"
            design = compose(art, idea["slogan"], look=look, seed=seed)
            (self.art_dir / f"{stamp}-raw.jpg").write_bytes(raw)
            cut = self.art_dir / f"{stamp}-art.png"            # kept: other products get the design in other layouts
            art.save(cut, "PNG", optimize=True)
            extra = dict(extra, art=str(cut))
        design.save(path, "PNG", optimize=True)
        rec = {"id": stamp, **idea, "file": str(path), "size": list(design.size), "made": self.clock(), "products": {},
               "v": 2, "seed": seed, **extra}
        if only:
            rec["only"] = list(only)
        if parent:
            rec["spinoff_of"] = parent["id"]
            if variant:
                rec["variant"] = True
        if season:
            rec["season"] = season
        if style:
            rec["style"] = style
        if audience:
            rec["audience"] = audience
        if style != "sweater":
            rec["look"] = look
        if video:
            rec["video"] = video
            rec["etsy"] = False                               # on top of the rest: free channels only
        if series:
            rec["series"] = True
            rec["etsy"] = False                               # extra breeds/jobs stay on the free channels
        elif line and line.get("series") and not parent and self.cfg().get("line_series", True):
            st["line_queue"] = (st.get("line_queue") or []) + [style] * int(line["series"])
        st["designs"].append(rec)
        save_state(st, self.state_path)
        self.log(f"shop: designed '{idea['title']}' ({idea['slogan']})")
        nb = shop_agents.load(self.nb_path)
        shop_agents.log(nb, "designer", "publisher", f"designed \"{idea['slogan']}\" ({idea['niche']})" +
                        (f", a spin-off of \"{parent.get('slogan')}\"" if parent else ""), self.clock)
        shop_agents.save(nb, self.nb_path)
        return rec

    @staticmethod
    def look_for(style: Optional[str], designs: list) -> str:
        """"retro" or "plain": a line's own look if it has one, else the retro lines take turns with the plain caption."""
        line = LINES.get(style or "") or {}
        if line.get("look"):
            return line["look"]
        if (style or "plain") not in RETRO_LINES:
            return "plain"
        same = [d for d in designs if (d.get("style") or "plain") == (style or "plain") and d.get("look")]
        return "plain" if same and same[-1]["look"] == "retro" else "retro"

    ART_TRIES = 3

    def draw_checked(self, idea: dict, audience: Optional[str]) -> tuple:
        """(the drawing, what to note on the design). Each drawing is looked over (art_problems) and drawn again while
        the look finds something a shopper would notice, up to ART_TRIES; if they're all flawed the design is put off
        rather than sold with letters in the art or the wrong dog. With nobody to look, the drawing is used as it is."""
        from worker import shop_agents
        look = getattr(self.c, "look", None)
        found = []
        for n in range(self.ART_TRIES):
            raw = self.c.draw(idea["image_prompt"])
            problems = art_problems(look, raw, idea, audience) if look else None
            if problems is None:
                return raw, {}
            if not problems:
                return raw, {"art_checked": n + 1}             # which drawing passed
            found.append(problems)
            self.log(f"shop: drawing {n + 1} for \"{idea.get('slogan')}\" has a flaw ({'; '.join(problems)[:160]})")
        nb = shop_agents.load(self.nb_path)
        shop_agents.problem(nb, f"Designer's drawing for \"{idea.get('slogan')}\" failed its check {self.ART_TRIES} times: "
                                f"{'; '.join(found[-1])[:120]}", self.clock)
        shop_agents.save(nb, self.nb_path)
        raise RuntimeError(f"the drawing kept coming out wrong ({'; '.join(found[-1])[:100]})")

    def art_for(self, rec: dict, kind: str, main_id: str) -> tuple:
        """(Printify image id, size) of the artwork a product kind takes: the main design, or the design drawn again
        for it (wide for a hoodie, in dark ink when a cream-lettered sweater design goes on a white shirt), made and
        uploaded once."""
        key = variant_key(rec, kind)
        if not key:
            return main_id, rec["size"]
        saved = rec.setdefault("variants", {}).get(key)
        if not saved:
            path = Path(rec["file"]).with_name(f"{rec['id']}-{key}.png")
            if not path.exists():
                img = render_variant(rec, *key.split("-"))
                if img is None:
                    return main_id, rec["size"]                # nothing to redraw it from: the main design, as before
                path.parent.mkdir(parents=True, exist_ok=True)
                img.save(path, "PNG", optimize=True)
            from PIL import Image
            with Image.open(path) as im:
                size = list(im.size)
            up = self.c.printify("POST", "uploads/images.json",
                                 {"file_name": path.name, "contents": base64.b64encode(path.read_bytes()).decode()})
            saved = rec["variants"][key] = {"upload_id": up["id"], "size": size}
        return saved["upload_id"], saved["size"]

    def upload(self, rec: dict) -> str:
        if rec.get("upload_id"):
            return rec["upload_id"]
        data = Path(rec["file"]).read_bytes()
        up = self.c.printify("POST", "uploads/images.json",
                             {"file_name": Path(rec["file"]).name, "contents": base64.b64encode(data).decode()})
        return up["id"]

    def upload_slogan(self, rec: dict, ink: Optional[tuple] = None) -> tuple:
        """(Printify image id, size) of the slogan-only art, made and uploaded once per design (and ink)."""
        light = ink is not None
        path = Path(rec["file"]).with_name(f"{rec['id']}-slogan{'-light' if light else ''}.png")
        key = "slogan_light_upload_id" if light else "slogan_upload_id"
        if not path.exists():
            slogan_art(rec.get("slogan") or rec["title"][:40], **({"ink": tuple(ink)} if light else {})).save(
                path, "PNG", optimize=True)
        if not rec.get(key):
            up = self.c.printify("POST", "uploads/images.json",
                                 {"file_name": path.name, "contents": base64.b64encode(path.read_bytes()).decode()})
            rec[key] = up["id"]
        from PIL import Image
        with Image.open(path) as im:
            return rec[key], im.size

    def upload_sheet(self, rec: dict, art: str, area: tuple) -> tuple:
        """(Printify image id, size) of a full-sheet picture for a print area: the design tiled ("tile": wrapping
        paper, pajamas, desk mats) or on a cream backdrop ("backdrop": puzzles), at half the print size (plenty
        for these). One per design, kind of art and area shape."""
        from PIL import Image
        size = (area[0] // 2, area[1] // 2)
        key = f"{art}-{size[0]}x{size[1]}"
        path = Path(rec["file"]).with_name(f"{rec['id']}-{key}.jpg")
        if not path.exists():
            with Image.open(rec["file"]) as im:
                im = im.convert("RGBA")
                (tile_art(im, size, across=max(3, round(3 * size[0] / size[1]))) if art == "tile"
                 else backdrop_art(im, size)).save(path, "JPEG", quality=88)
        ups = rec.setdefault("sheet_uploads", {})
        if not ups.get(key):
            up = self.c.printify("POST", "uploads/images.json",
                                 {"file_name": path.name, "contents": base64.b64encode(path.read_bytes()).decode()})
            ups[key] = up["id"]
        return ups[key], size

    def create(self, rec: dict, kind: str, shop_id: int, image_id: str, title_max: int = 140) -> dict:
        p = PRODUCTS[kind]
        ids, (pw, ph) = self.variants(kind)
        art_size = rec["size"]
        if p.get("art") == "slogan":
            image_id, art_size = self.upload_slogan(rec, p.get("ink"))
        if p.get("art") in ("tile", "backdrop"):
            image_id, art_size = self.upload_sheet(rec, p["art"], (pw, ph))
        if p.get("art") == "sweater":
            return self.create_sweater(rec, kind, shop_id, ids, title_max)
        body = {"title": listing_title(rec["title"], p["name"], title_max),
                "description": listing_text(rec, self.facts(kind)), "tags": rec["tags"], "blueprint_id": p["blueprint"],
                "print_provider_id": p["provider"],
                "variants": [{"id": i, "price": 2999, "is_enabled": True} for i in ids],
                "print_areas": self.print_areas(rec, kind, image_id, art_size)}
        product = self.c.printify("POST", f"shops/{shop_id}/products.json", body)
        extra = self.save_photos(rec, kind, product.get("images") or [])
        priced = [{"id": v["id"], "price": price_for(int(v.get("cost") or 0), p["profit"]), "is_enabled": True}
                  for v in product.get("variants") or [] if v["id"] in ids]
        if priced and all(v.get("cost") for v in product["variants"] if v["id"] in ids):
            self.c.printify("PUT", f"shops/{shop_id}/products/{product['id']}.json", {"variants": priced})
        return {"id": product["id"], "shop": shop_id, "price": min((v["price"] for v in priced), default=2999),
                "published": False, "extra_photos": extra, "art": variant_key(rec, kind) or "main"}

    def print_areas(self, rec: dict, kind: str, image_id: str, art_size) -> list:
        """A product's print areas: the design placed in each shape of area its variants have (one for most; a round
        and an oval ornament each get their own). Kinds with made-up art (slogan only, tiled sheets) keep the one
        placement over every variant."""
        p = PRODUCTS[kind]
        ids, area = self.variants(kind)
        groups = [(ids, area)]
        if not p.get("art"):
            image_id, art_size = self.art_for(rec, kind, image_id)
            groups = _GROUPS.get(kind) or groups
        return [{"variant_ids": g_ids, "placeholders": [
            {"position": pos, "images": placements(kind, image_id, art_size, g_area)}
            for pos in p.get("positions") or (p.get("position", "front"),)]} for g_ids, g_area in groups]

    def refresh_product(self, rec: dict, kind: str, prod: dict) -> None:
        """Send one listed product its artwork again, as it would be made today (the rendering and placement for its
        kind), and publish it again if it was live so the listing's photos follow."""
        rec["upload_id"] = self.upload(rec)
        areas = self.print_areas(rec, kind, rec["upload_id"], rec["size"])
        # an update must name every variant the product's print areas already cover, sold or not (Printify fills them
        # in itself when a product is created, and answers 400 "all product variants must be present" otherwise): the
        # rest go with the first area. Not every variant the product lists: an older product also lists ones its
        # printer has since dropped, and naming those is refused too
        path = f"shops/{prod['shop']}/products/{prod['id']}.json"
        named = {i for a in areas for i in a["variant_ids"]}
        covered = [i for a in (self.c.printify("GET", path) or {}).get("print_areas") or [] for i in a.get("variant_ids") or []]
        rest = [i for i in dict.fromkeys(covered) if i not in named]
        areas[0] = dict(areas[0], variant_ids=list(areas[0]["variant_ids"]) + rest)
        self.c.printify("PUT", path, {"print_areas": areas})
        if prod.get("published"):
            self.publish(prod)
        prod["art"] = variant_key(rec, kind) or "main"
        prod.pop("photos", None)                   # the storefront fetches the new ones

    def sweater_core(self, rec: dict):
        """The art for the all-over sweater's chest: the design without the fair-isle bands a T-shirt print carries (the
        sweater has its own, edge to edge). Designs from before the knitted look are used as they are."""
        from PIL import Image
        if rec.get("v", 1) >= 2:
            from worker import sweater
            return sweater.design(rec.get("slogan") or rec["title"][:30], design_seed(rec),
                                  motif_name=rec.get("motif") or "tree", bands=False)
        with Image.open(rec["file"]) as art:
            return art.convert("RGBA")

    def create_sweater(self, rec: dict, kind: str, shop_id: int, ids: list, title_max: int) -> dict:
        """The all-over-print sweater: every panel (front, back, sleeves, cuffs, collar, waistband) is its own image."""
        from PIL import Image
        from worker import sweater
        p = PRODUCTS[kind]
        ups = rec.setdefault("panel_uploads", {})
        todo = {k: v for k, v in _AREAS[kind].items() if k not in ups}
        panels = sweater.panels(self.sweater_core(rec), todo, seed=int(re.sub(r"\D", "", rec["id"]) or 0)) if todo else {}
        for pos, img in panels.items():
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=92)
            up = self.c.printify("POST", "uploads/images.json", {"file_name": f"{rec['id']}-{pos}.jpg",
                                                                 "contents": base64.b64encode(buf.getvalue()).decode()})
            ups[pos] = up["id"]
        holders = [{"position": pos, "images": [{"id": ups[pos], "x": 0.5, "y": 0.5, "scale": 1, "angle": 0}]}
                   for pos in _AREAS[kind]]
        desc = listing_text(rec, self.facts(kind),
                            "A lightweight crew neck sweatshirt printed all over, sleeves and cuffs included: the knit, the "
                            "lights and the stitches are printed, not real. "
                            "Designed in our studio, then printed on demand and shipped by our production partner. "
                            "Made for you after you order, so allow about two weeks.")
        body = {"title": listing_title(rec["title"], p["name"], title_max), "description": desc, "tags": rec["tags"],
                "blueprint_id": p["blueprint"], "print_provider_id": p["provider"],
                "variants": [{"id": i, "price": 5999, "is_enabled": True} for i in ids],
                "print_areas": [{"variant_ids": ids, "placeholders": holders}]}
        product = self.c.printify("POST", f"shops/{shop_id}/products.json", body)
        extra = self.save_photos(rec, kind, product.get("images") or [])
        priced = [{"id": v["id"], "price": price_for(int(v.get("cost") or 0), p["profit"]), "is_enabled": True}
                  for v in product.get("variants") or [] if v["id"] in ids]
        if priced and all(v.get("cost") for v in product["variants"] if v["id"] in ids):
            self.c.printify("PUT", f"shops/{shop_id}/products/{product['id']}.json", {"variants": priced})
        return {"id": product["id"], "shop": shop_id, "price": min((v["price"] for v in priced), default=5999),
                "published": False, "extra_photos": extra, "art": "main"}

    def save_photos(self, rec: dict, kind: str, images: list) -> list:
        """Download the chosen extra photos (of the first variant) to memory/etsy_shop/photos/, plus the
        design close-up."""
        import requests
        first = next((i["variant_ids"][0] for i in images if i.get("variant_ids")), None)
        folder = self.art_dir / "photos"
        saved = []
        for name, detail in (("design", False), ("detail", True)):
            closeup = folder / f"{rec['id']}-{name}.jpg"
            if not closeup.exists() and Path(rec.get("file", "")).is_file():
                folder.mkdir(parents=True, exist_ok=True)
                design_closeup(Path(rec["file"]), closeup, detail=detail, dark=rec.get("style") == "sweater")
            if closeup.exists():
                saved.append(str(closeup))
        mine = [i for i in images if first is None or first in i.get("variant_ids", [])]
        labels = [m.group(1) for i in mine if (m := re.search(r"camera_label=([\w-]+)$", i["src"]))]
        # the chosen shots, or for a kind without a list every other angle Printify rendered (not the main
        # photo, which Etsy already has), up to Etsy's 10 with Printify's own and the two close-ups
        cams = list(EXTRA_PHOTOS[kind]) if kind in EXTRA_PHOTOS else \
            [c for c in dict.fromkeys(labels) if labels and c != labels[0]]
        for cam in cams[:MAX_MOCKUPS]:
            img = next((i for i in mine if i["src"].endswith(f"camera_label={cam}")), None)
            if not img:
                continue
            dest = folder / f"{rec['id']}-{kind}-{cam}.jpg"
            if not dest.exists():
                try:
                    r = requests.get(img["src"], headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
                    if r.status_code != 200 or len(r.content) < 5000:
                        continue
                    folder.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(r.content)
                except Exception:
                    continue
            saved.append(str(dest))
        return saved

    def publish(self, product: dict) -> None:
        self.c.printify("POST", f"shops/{product['shop']}/products/{product['id']}/publish.json",
                        {"title": True, "description": True, "images": True, "variants": True, "tags": True,
                         "keyFeatures": True, "shipping_template": True})
        product["published"] = True

    def end_oldest_ebay(self, st: dict, shop: dict, sold: set) -> bool:
        """Delete the oldest eBay product of a design that never sold and isn't a winner's spin-off (Printify takes
        the listing down with it). True when one was ended."""
        for d in st["designs"]:
            if d.get("id") in sold or d.get("spinoff_of"):
                continue
            ebay = d.get("channels", {}).get("ebay", {})
            for kind, prod in list(ebay.items()):
                try:
                    self.c.printify("DELETE", f"shops/{shop['id']}/products/{prod['id']}.json")
                except Exception as exc:
                    self.log(f"shop: couldn't end eBay {kind} of {d['id']}: {exc}")
                    return False
                del ebay[kind]
                d.setdefault("ebay_ended", []).append(kind)
                self.log(f"shop: ended eBay {kind} of '{d.get('slogan')}' to make room")
                return True
        return False

    def list_pending(self) -> list:
        """Put every queued design on the products it's still missing in each connected channel, and publish
        them. A channel that fails waits an hour without holding up the others."""
        stores = self.stores()
        if not stores:
            return []
        st = load_state(self.state_path)
        cfg = self.cfg()
        done = []
        on_ebay = sum(len(d.get("channels", {}).get("ebay", {})) for d in st["designs"])
        today = datetime.fromtimestamp(self.clock()).date()
        tiktok_today = sum(1 for d in st["designs"] for p in d.get("channels", {}).get("tiktok", {}).values()
                           if p.get("listed_at") and datetime.fromtimestamp(p["listed_at"]).date() == today)
        # TikTok's daily budget goes to the newest clean designs first (the holiday ones that matter now)
        budget, tiktok_ok = cfg.get("tiktok_per_day", 25) - tiktok_today, {}     # list position -> new listings it may make
        sold = {i for x in st.get("sales_log") or [] for i in x.get("designs") or []}
        winning = lambda d: d.get("id") in sold or bool(d.get("spinoff_of"))
        # the winner and its spin-offs/variants first, then newest
        order = sorted(range(len(st["designs"])), key=lambda i: (not winning(st["designs"][i]), -i))
        for pos in order if "tiktok" in stores else []:
            rec = st["designs"][pos]
            if budget <= 0:
                break
            if rec.get("failed") or PROFANE.search(f"{rec.get('slogan', '')} {rec.get('title', '')}"):
                continue
            listed = products_in(rec, "tiktok")
            missing = sum(1 for k in rec.get("only") or cfg["products"]
                          if k in PRODUCTS and k in cfg["tiktok_products"] and not (listed.get(k) or {}).get("published"))
            if missing:
                tiktok_ok[pos] = min(missing, budget)
                budget -= missing
        for pos, rec in enumerate(st["designs"]):
            if rec.get("failed"):
                continue
            for channel, shop in stores.items():
                if channel == "tiktok" and pos not in tiktok_ok:
                    continue
                retry = rec.setdefault("retry_at_by", {})
                if self.clock() < retry.get(channel, rec.get("retry_at", 0) if channel == "etsy" else 0):
                    continue
                listed = products_in(rec, channel)
                kinds = rec.get("only") or cfg["products"]
                if channel == "etsy":
                    live = [k for k in kinds if (listed.get(k) or {}).get("published")]   # unpublished drafts cost nothing yet
                    if "etsy_kinds" not in rec:      # decided once; a design already on Etsy gets nothing more
                        picks = [k for k in (cfg.get("etsy_products") or kinds) if k in kinds]
                        rec["etsy_kinds"] = live or (picks[:cfg.get("etsy_per_design", 3)] if self.on_etsy(rec, st) else [])
                    kinds = [k for k in kinds if k in live or k in rec["etsy_kinds"]]
                if channel == "tiktok":
                    kinds = [k for k in kinds if k in cfg["tiktok_products"]]
                for kind in kinds:
                    if kind not in PRODUCTS:
                        continue
                    prod = listed.get(kind)
                    if channel == "ebay" and prod is None:
                        if kind in rec.get("ebay_ended", []):
                            continue                       # taken down to make room: stays off eBay
                        if on_ebay >= cfg.get("ebay_max", 250) and cfg.get("ebay_rotate") and winning(rec) \
                                and self.end_oldest_ebay(st, shop, sold):
                            on_ebay -= 1                   # a slot freed for the winner's spin-off
                        if on_ebay >= cfg.get("ebay_max", 250):
                            if st.get("ebay_full_texted") != datetime.fromtimestamp(self.clock()).strftime("%Y-%m"):
                                st["ebay_full_texted"] = datetime.fromtimestamp(self.clock()).strftime("%Y-%m")
                                self.notify("shop", f"🛍️ eBay has {on_ebay} listings, its free limit: new designs "
                                                    "now go only on the free Pop-Up Store (and Etsy's daily pick).")
                            continue
                        on_ebay += 1
                    if channel == "tiktok" and not (prod or {}).get("published"):
                        if tiktok_ok.get(pos, 0) <= 0:
                            continue
                        tiktok_ok[pos] -= 1
                    try:
                        if prod is None or prod.get("shop") != shop["id"]:
                            rec["upload_id"] = self.upload(rec)
                            prod = listed[kind] = self.create(rec, kind, shop["id"], rec["upload_id"],
                                                              CHANNELS[channel]["title_max"])
                            save_state(st, self.state_path)
                        if not prod["published"]:
                            self.publish(prod)
                            prod["listed_at"] = self.clock()
                            done.append(f"{rec['title'][:45]} {PRODUCTS[kind]['name']} on {CHANNELS[channel]['name']} "
                                        f"(${prod['price'] / 100:.2f})")
                    except Exception as exc:
                        self.log(f"shop: {channel} {kind} for {rec['id']} failed: {exc}")
                        if channel not in rec.setdefault("failure_texted", []):
                            rec["failure_texted"].append(channel)
                            self.notify("shop", f"⚠️ Couldn't list '{rec['title'][:50]}' on "
                                                f"{CHANNELS[channel]['name']}: {str(exc)[:120]}. I'll retry in an hour.")
                        rec.setdefault("errors", []).append(f"{channel} {kind}: {str(exc)[:200]}")
                        from worker import shop_agents
                        nb = shop_agents.load(self.nb_path)
                        shop_agents.problem(nb, f"Publisher couldn't list \"{rec.get('slogan')}\" {kind} on {channel}: {str(exc)[:120]}",
                                            self.clock)
                        shop_agents.save(nb, self.nb_path)
                        retry[channel] = self.clock() + 3600         # an hour, not every pass
                        if len(rec["errors"]) >= 12:
                            rec["failed"] = True
                        save_state(st, self.state_path)
                        break
                    save_state(st, self.state_path)
        return done

    def new_orders(self) -> list:
        stores = self.stores()
        if not stores:
            return []
        st = load_state(self.state_path)
        started = st.setdefault("orders_started_by", {"etsy": True} if st.get("orders_started") else {})
        lines = []
        retail = {p.get("id"): p.get("price") for rec in st["designs"] for ch in CHANNELS
                  for p in products_in(rec, ch).values() if p.get("id")}
        design_of = {p.get("id"): rec.get("id") for rec in st["designs"] for ch in CHANNELS
                     for p in products_in(rec, ch).values() if p.get("id")}
        for channel, shop in stores.items():
            data = self.c.printify("GET", f"shops/{shop['id']}/orders.json?limit=20")
            status = st.setdefault("order_status", {})
            for o in data.get("data") or []:
                items = ", ".join(str((li.get("metadata") or {}).get("title") or "an item")[:50]
                                  for li in o.get("line_items") or [])
                if o["id"] not in st["orders_seen"]:
                    st["orders_seen"].append(o["id"])
                    status[o["id"]] = o.get("status")
                    if not started.get(channel):                # the first look at a channel is a baseline
                        continue
                    prices = [retail.get(li.get("product_id")) for li in o.get("line_items") or []]
                    total = sum(p * int(li.get("quantity") or 1) for p, li in zip(prices, o.get("line_items") or [])
                                if p) / 100                       # what the buyer paid us (Printify's total is its cost)
                    designs = sorted({design_of[li.get("product_id")] for li in o.get("line_items") or [] if li.get("product_id") in design_of})
                    st.setdefault("sales_log", []).append({"at": self.clock(), "channel": channel, "items": items,
                                                           "price": total, "designs": designs})
                    st["sales_log"] = st["sales_log"][-500:]
                    lines.append(f"💰 SALE on {CHANNELS[channel]['name']}! {items}" +
                                 (f" (${total:.2f})" if total else "") + ". Printify will print and ship it.")
                    from worker import shop_agents
                    nb = shop_agents.load(self.nb_path)
                    for did in designs:
                        win = shop_agents.on_sale(nb, st, did, total / max(1, len(designs)), self.clock)
                        if win:
                            lines.append(win)
                    shop_agents.save(nb, self.nb_path)
                    continue
                if o.get("status") != status.get(o["id"]):
                    status[o["id"]] = o.get("status")
                    line = order_update(o, items, CHANNELS[channel]["name"])
                    if line:
                        lines.append(line)
            started[channel] = True
        st["orders_started"] = True
        st["orders_seen"] = st["orders_seen"][-500:]
        save_state(st, self.state_path)
        return lines


class ShopService:
    """The worker's shop thread: today's designs at `time`, listing whatever is queued, and new orders."""

    PASS_SECONDS = 300
    viral_state = VIRAL_STATE                  # the viral videos' state, for a shirt per posted topic

    def __init__(self, log: Callable[[str], None] = print, notify: Optional[Callable[[str, str], None]] = None):
        self.log, self.notify = log, notify or (lambda kind, text: None)
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="etsy-shop").start()

    def stop(self) -> None:
        self._stop.set()

    def run_once(self, shop: Optional[Shop] = None, now: Optional[datetime] = None) -> None:
        cfg = settings()
        if not cfg["enabled"]:
            return
        shop = shop or Shop(log=self.log, notify=self.notify)
        if not (shop.c.printify_token and shop.c.cf_token):
            return
        now = now or datetime.now()
        st = load_state(shop.state_path)
        today = now.date().isoformat()
        h, m = (int(x) for x in str(cfg["time"]).split(":"))
        made_today = sum(1 for d in st["designs"] if datetime.fromtimestamp(d["made"]).date().isoformat() == today
                         and (d.get("spinoff_of") or not d.get("season")))
        season = season_for(now.date())
        holiday_today = sum(1 for d in st["designs"] if datetime.fromtimestamp(d["made"]).date().isoformat() == today
                            and d.get("season") and not d.get("series") and not d.get("spinoff_of"))
        # the holiday line's design(s), from half an hour after the daily one
        holiday = bool(season) and holiday_today < cfg["holiday_per_day"] and \
            now.hour * 60 + now.minute >= h * 60 + m + 30 + holiday_today * 240
        queued = int(st.get("make_now") or 0)              # "make N new products now", from the plugin; one per pass
        spin_q = list(st.get("make_spinoff") or [])        # spin-offs of a design that just sold, queued by the Analyst
        line_q = list(st.get("line_queue") or []) if cfg["line_series"] else []   # more breeds/jobs for a line design
        done_v = {t.lower() for t in st.get("video_shirts") or []}
        video_q = [t for t in video_topics(self.viral_state) if t.lower() not in done_v] \
            if cfg["video_shirts"] else []                  # posted videos without a shirt
        cooling = time.time() - st.get("last_fail_at", 0) < 3600
        # N designs a day are spread from the start time to midnight (4 a day at 06:00: 06:00, 10:30, 15:00, 19:30)
        n = cfg["designs_per_day"]
        gap = (24 * 60 - (h * 60 + m)) / n if n else 0
        daily = made_today < n and now.hour * 60 + now.minute >= h * 60 + m + made_today * gap
        if not cooling and (spin_q or queued or daily or holiday or line_q or video_q):
            from worker import shop_agents
            try:
                if spin_q:
                    shop.make_design(spinoff=spin_q[0])
                    st = load_state(shop.state_path)
                    st["make_spinoff"] = (st.get("make_spinoff") or [])[1:]
                    st["spinoffs_made"] = int(st.get("spinoffs_made") or 0) + 1
                    save_state(st, shop.state_path)
                    queued = 0
                elif queued:
                    style = st.get("make_style")
                    shop.make_design(only=st.get("make_only"), style=style,
                                     season="christmas" if style else None)    # every line is a Christmas line
                elif line_q and not (holiday or daily):
                    st["line_queue"] = line_q[1:]          # dropped first, so a failing one can't loop forever
                    save_state(st, shop.state_path)
                    shop.make_design(season="christmas", style=line_q[0], series=True)
                elif video_q and not (holiday or daily):
                    st["video_shirts"] = (st.get("video_shirts") or []) + [video_q[0]]   # once, even if it fails
                    save_state(st, shop.state_path)
                    shop.make_design(video=video_q[0])
                elif holiday and not daily:
                    # Christmas days take turns between the lines (family, pet, couple, plain), with an
                    # ugly-sweater design every day (the user's favourite, 2026-10-26)
                    style = christmas_style(now.date(), holiday_today, cfg["holiday_per_day"]) \
                        if season == "christmas" else "plain"
                    if style == "plain":
                        shop.make_design(only=SEASONS[season]["products"], season=season)
                    else:
                        shop.make_design(season=season, style=style)
                else:                                      # the daily design: every other one riffs on a winner, if there is one
                    win = shop_agents.next_spinoff(shop_agents.load(shop.nb_path),
                                                   bool(st.get("last_was_spinoff")) and not cfg.get("winner_focus"),
                                                   int(st.get("spinoffs_made") or 0))
                    parent = next((d for d in st["designs"] if d.get("id") == win), None)
                    if parent and parent.get("season") and not season_running(parent["season"], now.date()):
                        win = None                         # no Christmas spin-offs once Christmas orders have closed
                    made = int(st.get("spinoffs_made") or 0)
                    shop.make_design(spinoff=win, variant=bool(win) and cfg["winner_variants"] and made % 2 == 0)
                    st = load_state(shop.state_path)
                    st["last_was_spinoff"] = bool(win)
                    if win:
                        st["spinoffs_made"] = int(st.get("spinoffs_made") or 0) + 1
                    save_state(st, shop.state_path)
                if queued:
                    st = load_state(shop.state_path)
                    st["make_now"] = queued - 1
                    if queued == 1:
                        st.pop("make_only", None)
                        st.pop("make_style", None)
                    save_state(st, shop.state_path)
            except Exception as exc:
                self.log(f"shop: today's design failed: {exc}")
                busy = "didn't answer" in str(exc)                 # Gemini overloaded for a while: try again in 15 minutes, not an hour
                st["last_fail_at"] = time.time() - (2700 if busy else 0)
                save_state(st, shop.state_path)
                if st.get("last_fail_day") != today:
                    st["last_fail_day"] = today
                    save_state(st, shop.state_path)
                    self.notify("system", f"⚠️ The Etsy shop couldn't make today's design: {str(exc)[:120]}")
                return
        listed = shop.list_pending()
        if listed:
            self.notify("shop", "🛍️ Just listed: " + "; ".join(listed[:6]) +
                        (f"; and {len(listed) - 6} more" if len(listed) > 6 else "") + ".")
        self.repair_art(shop)
        for line in shop.new_orders():
            self.notify("shop", line)
        self.storefront(shop, today, force=bool(listed))
        self.etsy_photos(shop)
        self.video_slot(shop, now)
        self.evening_review(shop, now)

    REPAIR_RETRY = 6 * 3600

    def repair_art(self, shop: "Shop", limit: int = 3) -> int:
        """Listings made before a design came in more than one rendering, whose words can't be read on the product (the
        sweater line is lettered in cream for dark garments, and its first winner was put on white shirts, hoodies and
        mugs as it was), get the rendering made for them, a few a pass. One that fails is tried again in six hours,
        not every pass."""
        st = load_state(shop.state_path)
        n = 0
        for rec in st["designs"]:
            for channel in CHANNELS:
                for kind, prod in products_in(rec, channel).items():
                    p = PRODUCTS.get(kind)
                    if n >= limit or not p or p.get("art") or not prod.get("id") \
                            or prod.get("art") not in (None, "unfixed") \
                            or p.get("ground", "light") == native_ground(rec) \
                            or shop.clock() - prod.get("art_tried", 0) < self.REPAIR_RETRY:
                        continue
                    n += 1
                    prod["art_tried"] = shop.clock()
                    try:
                        shop.refresh_product(rec, kind, prod)
                        self.log(f"shop: redrew '{rec.get('slogan')}' for its {p['name']} on {CHANNELS[channel]['name']}")
                    except Exception as exc:
                        prod["art"] = "unfixed"
                        self.log(f"shop: couldn't redraw {rec.get('id')} {kind} on {channel}: {str(exc)[:400]}")
                    save_state(st, shop.state_path)
        return n

    REVIEW_HOUR = 21

    def evening_review(self, shop: "Shop", now: datetime) -> bool:
        """The Analyst's nightly rewrite of the agents' notebook."""
        from worker import shop_agents
        nb = shop_agents.load(shop.nb_path)
        if now.hour < self.REVIEW_HOUR or nb.get("reviewed_day") == now.date().isoformat() or not hasattr(shop.c, "ask"):
            return False
        nb["reviewed_day"] = now.date().isoformat()          # one try a day, even if Gemini doesn't answer
        shop_agents.save(nb, shop.nb_path)
        ok = shop_agents.review(nb, load_state(shop.state_path), shop.c.ask, shop.clock)
        shop_agents.save(nb, shop.nb_path)
        self.log("shop: the Analyst " + ("rewrote the notebook" if ok else "couldn't review today"))
        return ok

    def video_slot(self, shop: "Shop", now: datetime) -> str:
        """At each of the day's video times: make the next video and post it (Instagram) or text it to you."""
        times = [t.strip() for t in str(settings()["video_times"]).split(",") if t.strip()]
        st = load_state(shop.state_path)
        today = now.date().isoformat()
        done = st.setdefault("video_slots", {}).get(today, [])
        due = [i for i, t in enumerate(times) if i not in done and now.strftime("%H:%M") >= t]
        if not due:
            return ""
        slot = due[0]
        st["video_slots"] = {today: done + due}           # missed slots (Mac asleep) are skipped, not stacked up
        save_state(st, shop.state_path)
        listed = [d for d in st["designs"] if any(p.get("published") for p in d.get("products", {}).values())]
        if not listed:
            return ""
        from worker import videos
        fmt = videos.FORMATS[slot % len(videos.FORMATS)]
        made = {(v["design"], v["format"]) for v in st.setdefault("videos", [])}
        fresh = [d for d in reversed(listed) if (d["id"], fmt) not in made]
        rec = fresh[0] if fresh else min(listed, key=lambda d: sum(v["design"] == d["id"] for v in st["videos"]))
        try:
            from worker import storefront
            video = videos.make(rec, fmt, shop.art_dir / "videos", storefront.settings()["name"], printify=shop.c.printify)
        except Exception as exc:
            self.log(f"videos: couldn't make a {fmt} video: {exc}")
            return "failed"
        video["made"] = time.time()
        st = load_state(shop.state_path)
        for d in st["designs"]:
            if d["id"] == rec["id"] and rec.get("video"):
                d["video"] = rec["video"]                    # keep the copy for the next formats
        st.setdefault("videos", []).append(video)
        save_state(st, shop.state_path)
        return self.deliver(video, shop, slot + 1, len(times))

    def deliver(self, video: dict, shop: "Shop", n: int, of: int) -> str:
        """Texts the video to the user (Jarvis never posts to their social accounts)."""
        from core import phone_alerts as pa
        cfg = pa.settings()
        if not cfg["enabled"] or "videos" in cfg["off"]:
            return "off"
        try:
            pa.send_file(video["file"], f"🎬 Video {n} of {of} for today. Not posted yet: save it and post it "
                                        f"yourself on TikTok, Instagram Reels or YouTube Shorts. "
                                        f"Copy this caption:\n{video['caption']}", cfg)
            self.mark(shop, video, texted=True)
            return "texted"
        except Exception as exc:
            self.log(f"videos: couldn't text the video: {exc}")
            return "failed"

    def mark(self, shop: "Shop", video: dict, **kv) -> None:
        st = load_state(shop.state_path)
        for v in st.get("videos", []):
            if v["file"] == video["file"]:
                v.update(kv)
        save_state(st, shop.state_path)

    def more_photos(self, shop: "Shop", limit: int = 10) -> None:
        """Products listed before every angle was saved (only the two close-ups) get the angles Printify
        still has, once each."""
        st = load_state(shop.state_path)
        n = 0
        for rec in st["designs"]:
            for kind, prod in rec.get("products", {}).items():
                if n >= limit or prod.get("photos_added") or prod.get("photos_checked") or len(prod.get("extra_photos") or []) > 2:
                    continue
                n += 1
                try:
                    p = shop.c.printify("GET", f"shops/{prod['shop']}/products/{prod['id']}.json")
                except Exception:
                    continue
                prod["extra_photos"] = list(dict.fromkeys((prod.get("extra_photos") or []) +
                                                          shop.save_photos(rec, kind, p.get("images") or [])))
                prod["photos_checked"] = True
        if n:
            save_state(st, shop.state_path)

    def etsy_photos(self, shop: "Shop") -> None:
        """Add the saved extra photos to each Etsy listing, once Jarvis is connected to Etsy itself."""
        from worker import etsy_api
        self.more_photos(shop)
        if not etsy_api.connected():
            return
        st = load_state(shop.state_path)
        n = 0
        for rec in st["designs"]:
            for kind, prod in rec.get("products", {}).items():
                if not prod.get("published") or prod.get("photos_added") or not prod.get("extra_photos"):
                    continue
                if not prod.get("url"):
                    try:
                        p = shop.c.printify("GET", f"shops/{prod['shop']}/products/{prod['id']}.json")
                        prod["url"] = (p.get("external") or {}).get("handle") or ""
                    except Exception:
                        continue
                listing = etsy_api.listing_id(prod.get("url"))
                if not listing:
                    continue                                   # Etsy hasn't got it yet; next pass
                try:
                    n += etsy_api.add_photos(listing, prod["extra_photos"])
                    prod["photos_added"] = True
                except Exception as exc:
                    self.log(f"shop: photos for Etsy listing {listing} failed: {exc}")
                    save_state(st, shop.state_path)
                    return                                     # try again next pass
                save_state(st, shop.state_path)
        if n:
            self.log(f"shop: added {n} photos to Etsy listings")

    def storefront(self, shop: "Shop", today: str, force: bool = False) -> None:
        """Rebuild the public storefront (worker/storefront.py) after new listings, else once a day."""
        st = load_state(shop.state_path)
        if not force and st.get("storefront_day") == today:
            return
        st["storefront_day"] = today
        try:
            from worker import storefront
            storefront.publish(st["designs"], shop.c.printify, self.log)    # caches each listing's link and photos
        except Exception as exc:
            self.log(f"storefront: {exc}")
        save_state(st, shop.state_path)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                self.log(f"shop: pass failed: {exc!r}")
            self._stop.wait(self.PASS_SECONDS)
