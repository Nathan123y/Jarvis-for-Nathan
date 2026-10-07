"""The AI site designer: whatever the model returns is validated, and the template is always the fallback."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worker.campaign import build, check, design
from test_campaign import BIZ, IDENT, Rig, biz

FACTS = {"hours": "Mo-Fr 08:00-17:00"}
SHOWN = {k: BIZ[k] for k in ("name", "category", "address", "city", "phone", "email")}
MAP = "https://www.google.com/maps/search/?api=1&query=410%20Market%20Street%2C%20San%20Jose%2C%20CA%2095113"

CSS = """:root{--bg:#FFFFFF;--surface:#EAF0F6;--text:#0F1B2A;--muted:#45566A;--primary:#0B4F8A;--on-primary:#FFFFFF;--accent:#B34700}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:1.05rem/1.6 system-ui,sans-serif}
header{background:var(--surface);padding:1rem}nav a{color:var(--text);margin-right:1rem}
.hero{background:var(--primary);color:var(--on-primary);padding:4rem 1.25rem}
.hero a{color:var(--on-primary);font-weight:800;font-size:2rem}
section{padding:2.5rem 1.25rem}h1{font-size:3rem;margin:0 0 .5rem}
a:focus-visible{outline:3px solid var(--accent);outline-offset:3px}
.sample{color:var(--muted)}"""
BODY = f"""<header><nav aria-label="Sections"><a href="#services">Services</a><a href="#find">Find us</a></nav></header>
<main id="main">
<section class="hero"><h1>Bay Leaf Plumbing</h1><p>Plumbing in San Jose</p><a href="tel:+14085550101">(408) 555-0101</a></section>
<section id="services"><h2>What we do</h2><ul><li>Leak repair</li><li>Drain cleaning</li></ul>
<p class="sample">Sample list: typical services the owner will replace.</p></section>
<section id="find"><h2>Find us</h2><p>410 Market Street, San Jose, CA 95113</p>
<p><a href="{MAP}">Get directions</a> <a href="mailto:hello@bayleaf.test">hello@bayleaf.test</a></p></section>
</main>"""
GOOD = f"===CSS===\n{CSS}\n===BODY===\n{BODY}"


def run(reply, tries=2, extra=None):
    prompts = []

    def gen(p):
        prompts.append(p)
        return reply if isinstance(reply, str) or reply is None else reply[min(len(prompts), len(reply)) - 1]
    site, notes = design.design(SHOWN, FACTS, sender="Sam", generate=gen, tries=tries, extra_checks=extra)
    return site, notes, prompts


class ValidationTests(unittest.TestCase):
    def test_harmless_attributes_are_stripped_not_rejected(self):
        messy = GOOD.replace('<a ', '<a target="_blank" rel="noopener" style="color:red" ', 1)
        site, notes, _ = run(messy)
        self.assertIsNotNone(site, notes)
        self.assertNotIn("target=", site.files["index.html"])
        self.assertNotIn('style="color:red"', site.files["index.html"])

    def test_a_reply_without_our_markers_is_still_understood(self):
        css, body = design.split_reply("```html\n<style>:root{--a:#fff}</style>\n<body><main id=\"main\">x</main></body>\n```")
        self.assertEqual(css, ":root{--a:#fff}")
        self.assertIn("<main", body)

    def test_a_bare_hash_link_is_pointed_at_the_page(self):
        self.assertIn('href="#main"', design.strip_harmless('<a href="#">x</a>'))
        self.assertIn('href="#services"', design.strip_harmless('<a href="#services">x</a>'))

    def test_script_tags_are_still_rejected(self):
        self.assertTrue(design.body_problems('<header><nav></nav></header><main id="main"><script>x</script></main>'))

    def test_inline_event_handlers_are_removed(self):
        self.assertNotIn("onclick", design.strip_harmless('<a onclick="x()" href="#a">y</a>'))

    def test_a_good_page_is_accepted_and_wrapped_in_our_own_labels(self):
        site, notes, _ = run(GOOD)
        self.assertIsNotNone(site, notes)
        html = site.files["index.html"]
        self.assertEqual(site.theme, "ai")
        self.assertIn('content="noindex', html)
        self.assertEqual(html.lower().count(build.CONCEPT_NOTE.lower()), 2)          # banner and footer, written by us
        self.assertIn("Contact form disabled", html)
        self.assertIn('class="cc-callbar"', html)
        res = check.static_checks(html, "ai", site.used, "Sam", tokens=site.tokens)
        self.assertEqual([r for r in res if r["required"] and not r["ok"]], [])

    def test_each_kind_of_bad_page_is_rejected(self):
        def with_body(old, new):
            return GOOD.replace(old, new)
        bad = {
            "script": with_body("</main>", "<script>1</script></main>"),
            "image": with_body("<h2>What we do</h2>", "<h2>What we do</h2><img src='x.png'>"),
            "outside link": with_body("Get directions", "Get directions</a><a href='https://evil.example/x'>x"),
            "form": with_body("</main>", "<form><input></form></main>"),
            "hex colour outside tokens": with_body("section{padding", "section{border:1px solid #ff0000;padding"),
            "colour name": with_body("section{padding", "section{border:1px solid red;padding"),
            "rgb": with_body("section{padding", "section{color:rgb(0,0,0);padding"),
            "gradient": with_body("section{padding", "section{background:linear-gradient(var(--bg),var(--surface));padding"),
            "url()": with_body("section{padding", "section{background:url(x.png);padding"),
            "missing token": GOOD.replace("--accent:#B34700", ""),
            "no markers": CSS + BODY,
            "wrong phone": with_body("(408) 555-0101", "(408) 555-9999").replace("tel:+14085550101", "tel:+14085559999"),
            "invented claim": with_body("What we do", "Licensed and insured, award-winning"),
            "invented review": with_body("Find us</h2>", "Find us</h2><p>Five-star reviews</p>"),
            "svg colour": with_body("</main>", '<svg viewBox="0 0 10 10"><path d="M0 0L5 5" fill="#123456"/></svg></main>'),
        }
        for label, reply in bad.items():
            extra = lambda html, tokens, used: [f"{r['check']}" for r in check.static_checks(html, "ai", used, "Sam", tokens=tokens)
                                                if r["required"] and not r["ok"]]
            site, notes, _ = run(reply, tries=1, extra=extra)
            self.assertIsNone(site, label)
            self.assertTrue(notes, label)

    def test_the_model_is_told_what_was_wrong_and_gets_one_more_try(self):
        site, notes, prompts = run([GOOD.replace("</main>", "<script>1</script></main>"), GOOD])
        self.assertIsNotNone(site)
        self.assertEqual(len(prompts), 2)
        self.assertIn("REJECTED", prompts[1])
        self.assertIn("<script>", prompts[1])

    def test_smooth_scrolling_and_plain_phone_links_are_fine(self):
        css = CSS + "\nhtml{scroll-behavior:smooth}"
        for href in ("tel:+14085550101", "tel:4085550101", "tel:(408) 555-0101", "tel:408-555-0101"):
            reply = f"===CSS===\n{css}\n===BODY===\n{BODY.replace('tel:+14085550101', href)}"
            extra = lambda html, tokens, used: [r["check"] for r in check.static_checks(html, "ai", used, "Sam", tokens=tokens)
                                                if r["required"] and not r["ok"]]
            site, notes, _ = run(reply, tries=1, extra=extra)
            self.assertIsNotNone(site, (href, notes))
        wrong = f"===CSS===\n{css}\n===BODY===\n{BODY.replace('tel:+14085550101', 'tel:+14085559999')}"
        extra = lambda html, tokens, used: [r["check"] for r in check.static_checks(html, "ai", used, "Sam", tokens=tokens)
                                            if r["required"] and not r["ok"]]
        self.assertIsNone(run(wrong, tries=1, extra=extra)[0])                       # the digits must still be the verified ones
        self.assertIsNone(run(CSS.replace("*{", "*{behavior:url(x);") and f"===CSS===\n{CSS}\nbody{{behavior:none}}\n===BODY===\n{BODY}", tries=1)[0])

    def test_the_prompt_gives_the_exact_phone_link(self):
        self.assertIn('href="tel:+14085550101"', design.prompt(SHOWN, FACTS, direction="x"))

    def test_an_unavailable_model_is_not_hammered(self):
        site, notes, prompts = run(None)
        self.assertIsNone(site)
        self.assertEqual(len(prompts), 1)

    def test_the_prompt_sends_only_public_business_details(self):
        text = design.prompt(SHOWN, FACTS, direction=design.seed_direction("x"))
        self.assertIn("Bay Leaf Plumbing", text)
        for private in (IDENT["sender_name"], IDENT["postal_address"], IDENT["gmail_account"], "Sam"):
            self.assertNotIn(private, text)

    def test_neighbouring_businesses_get_different_directions(self):
        self.assertGreater(len({design.seed_direction(f"Business {i}") for i in range(30)}), 3)

    def test_fenced_or_chatty_replies_are_tolerated_but_unparseable_ones_are_not(self):
        site, _, _ = run("```\n" + GOOD + "\n```")
        self.assertIsNotNone(site)
        self.assertIsNone(run("Sure! Here is your site: <html></html>", tries=1)[0])


class BrowserTests(unittest.TestCase):
    def test_real_contrast_is_measured_in_a_browser(self):
        with tempfile.TemporaryDirectory() as d:
            good = Path(d, "good.html")
            good.write_text(design.assemble(CSS, BODY, SHOWN, sender="Sam"))
            r = check.contrast_audit(good)
            if r["status"] == "not_run":
                self.skipTest(r["detail"])
            self.assertEqual(r["status"], "passed", r)
            poor = Path(d, "poor.html")
            poor.write_text(design.assemble(CSS.replace("--muted:#45566A", "--muted:#B0BCC8") + ".sample{color:var(--muted);background:var(--bg)}",
                                            BODY, SHOWN, sender="Sam"))
            r = check.contrast_audit(poor)
            self.assertEqual(r["status"], "failed", r)
            self.assertIn("low contrast", r["detail"])


class OutageTests(unittest.TestCase):
    def test_after_every_model_fails_the_ai_is_skipped_for_a_while(self):
        import sys, types, time as _t
        calls = []

        class Models:
            def generate_content(self, **kw):
                calls.append(kw["model"])
                raise RuntimeError("503 UNAVAILABLE")
        fake_client = types.SimpleNamespace(models=Models())
        gem = types.SimpleNamespace(api_key=lambda: "k", client=lambda **kw: fake_client)
        gtypes = types.SimpleNamespace(GenerateContentConfig=lambda **kw: kw)
        with mock.patch.dict(sys.modules, {"core.gemini": gem, "google": types.SimpleNamespace(genai=types.SimpleNamespace(types=gtypes)),
                                           "google.genai": types.SimpleNamespace(types=gtypes), "google.genai.types": gtypes}), \
             mock.patch("core.gemini", gem, create=True), mock.patch.object(_t, "sleep"):
            design._down_until = 0.0
            design._model_skip.clear()
            gen = design.gemini_generate()
            self.assertIsNotNone(gen)
            self.assertIsNone(gen("x"))
            first = len(calls)
            self.assertGreaterEqual(first, len(design.MODEL_LADDER))
            self.assertIsNone(gen("x"))                                   # cooling down: no new requests at all
            self.assertEqual(len(calls), first)
            design._down_until = 0.0
            design._model_skip.clear()

    def test_a_model_that_failed_is_skipped_and_the_working_one_goes_first(self):
        import sys, types, time as _t
        calls = []

        class Models:
            def generate_content(self, **kw):
                calls.append(kw["model"])
                if kw["model"] != "gemini-3.7-flash":
                    raise RuntimeError("404 not found")
                return types.SimpleNamespace(text="hello")
        gem = types.SimpleNamespace(api_key=lambda: "k", client=lambda **kw: types.SimpleNamespace(models=Models()))
        gtypes = types.SimpleNamespace(GenerateContentConfig=lambda **kw: kw)
        with mock.patch.dict(sys.modules, {"core.gemini": gem, "google": types.SimpleNamespace(genai=types.SimpleNamespace(types=gtypes)),
                                           "google.genai": types.SimpleNamespace(types=gtypes), "google.genai.types": gtypes}), \
             mock.patch("core.gemini", gem, create=True), mock.patch.object(_t, "sleep"):
            design._down_until = 0.0
            design._model_skip.clear()
            gen = design.gemini_generate()
            self.assertEqual(design.MODEL_LADDER[0], "gemini-3.5-flash")
            self.assertEqual(gen("x"), "hello")
            self.assertIn("gemini-3.5-flash", calls)
            calls.clear()
            self.assertEqual(gen("x"), "hello")
            self.assertEqual(calls, ["gemini-3.7-flash"])                 # the ones that failed are not asked again
            design._model_skip.clear()


class PipelineTests(unittest.TestCase):
    def build_one(self, designer, **patches):
        r = Rig(self, [biz(1)])
        r.env.designer = designer
        passed = {"status": "passed", "detail": "ok"}
        with mock.patch.object(check, "contrast_audit", return_value=patches.get("audit", passed)), \
             mock.patch.object(check, "visual_check", return_value={**passed, "screens": []}):
            r.discover()
        (b,) = r.businesses()
        return r, b

    def good_for_biz1(self):
        return (GOOD.replace("Bay Leaf Plumbing", "Biz 1 Plumbing").replace("(408) 555-0101", "(408) 555-0101")
                .replace("hello@bayleaf.test", "owner1@biz1.test"))

    def test_an_ai_site_that_passes_is_used_and_recorded(self):
        r, b = self.build_one(lambda p: self.good_for_biz1())
        self.assertEqual(b["data"]["site"]["designer"], "ai", b["data"].get("site"))
        self.assertEqual(b["status"], "ok")
        built = [e for e in r.events.events(kinds=("site_built",))]
        self.assertEqual(built[0]["detail"]["designer"], "ai")

    def test_a_page_that_fails_twice_falls_back_to_the_template(self):
        r, b = self.build_one(lambda p: GOOD.replace("</main>", "<script>1</script></main>"))
        self.assertEqual(b["data"]["site"]["designer"], "template")
        self.assertEqual(b["status"], "ok")                          # the campaign was not held up
        self.assertTrue(b["data"]["site"]["design_notes"])

    def test_unreadable_contrast_or_no_browser_falls_back_to_the_template(self):
        for audit in ({"status": "failed", "detail": "low contrast: p 2.0:1"}, {"status": "not_run", "detail": "no browser"}):
            r, b = self.build_one(lambda p: self.good_for_biz1(), audit=audit)
            self.assertEqual(b["data"]["site"]["designer"], "template", audit)

    def test_no_designer_means_template_designs_as_before(self):
        r, b = self.build_one(None)
        self.assertEqual(b["data"]["site"]["designer"], "template")

    def test_a_model_that_raises_never_stops_the_campaign(self):
        def boom(p):
            raise RuntimeError("quota")
        r, b = self.build_one(boom)
        self.assertEqual(b["data"]["site"]["designer"], "template")


if __name__ == "__main__":
    unittest.main()
