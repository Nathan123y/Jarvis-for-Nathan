"""Phone line plumbing: Twilio signatures, one-time tokens, TwiML, the tunnel,
Twilio's REST calls, the setup check, the Jarvis plugin and the call alert."""
import io
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from worker.phone import server, service, tunnel, twilio_api


class SignatureAndTokenTests(unittest.TestCase):
    def test_signature_round_trip_and_tampering(self):
        url, params = "https://x.trycloudflare.com/twilio/voice", {"From": "+14085550100", "CallSid": "CA1"}
        sig = server.twilio_signature("secret", url, params)
        self.assertTrue(server.valid_signature("secret", url, params, sig))
        self.assertFalse(server.valid_signature("secret", url, dict(params, From="+1999"), sig))
        self.assertFalse(server.valid_signature("other", url, params, sig))
        self.assertFalse(server.valid_signature("secret", url + "x", params, sig))
        self.assertFalse(server.valid_signature("", url, params, sig))
        self.assertFalse(server.valid_signature("secret", url, params, ""))

    def test_tokens_are_single_use_and_expire(self):
        now = [1000.0]
        tokens = server.Tokens(clock=lambda: now[0])
        t = tokens.issue()
        self.assertTrue(tokens.redeem(t))
        self.assertFalse(tokens.redeem(t))
        late = tokens.issue()
        now[0] += server.TOKEN_SECONDS + 1
        self.assertFalse(tokens.redeem(late))
        self.assertFalse(tokens.redeem("made-up"))

    def test_twiml_streams_to_the_tunnel_with_escaped_parameters(self):
        xml = server.twiml_stream("https://a-b.trycloudflare.com", "tok", '+1"<x>', "CA1")
        self.assertIn('<Stream url="wss://a-b.trycloudflare.com/twilio/media">', xml)
        self.assertIn('<Parameter name="token" value="tok"/>', xml)
        self.assertIn("&quot;&lt;x&gt;", xml)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        self.calls = []

        async def handle(receive_text, send_text, close, token_ok):
            self.calls.append(token_ok)
        self.app = server.build_app(public_base=lambda: "https://a-b.trycloudflare.com",
                                    auth_token=lambda: "secret", handle_call=handle, log=lambda m: None)
        self.client = TestClient(self.app)

    def post(self, form, signature=None):
        url = "https://a-b.trycloudflare.com/twilio/voice"
        sig = signature if signature is not None else server.twilio_signature("secret", url, form)
        return self.client.post("/twilio/voice", data=form, headers={"X-Twilio-Signature": sig})

    def test_signed_call_gets_a_stream_and_forged_one_is_refused(self):
        ok = self.post({"From": "+14085550100", "CallSid": "CA1"})
        self.assertEqual(ok.status_code, 200)
        self.assertIn("<Connect><Stream", ok.text)
        self.assertEqual(self.post({"From": "+1"}, signature="forged").status_code, 403)
        self.assertEqual(self.client.post("/twilio/voice", data={"From": "+1"}).status_code, 403)

    def test_media_socket_hands_the_call_its_token_check(self):
        with self.client.websocket_connect("/twilio/media"):
            pass
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.calls[0]("never-issued"))


class TunnelTests(unittest.TestCase):
    def test_reports_the_tunnel_address_and_skips_the_api_host(self):
        urls = []
        lines = ["INF Requesting new quick Tunnel on https://api.trycloudflare.com...\n",
                 "INF |  https://lucky-fox-bright-sun.trycloudflare.com  |\n",
                 "INF Registered tunnel connection\n"]
        proc = NS(stdout=io.StringIO("".join(lines)), wait=lambda: 0, poll=lambda: 0, terminate=lambda: None)
        t = tunnel.Tunnel(8770, urls.append, log=lambda m: None, popen=lambda *a, **k: proc)
        t._run_once("/bin/cloudflared")
        self.assertEqual(urls, ["https://lucky-fox-bright-sun.trycloudflare.com"])

    def test_missing_cloudflared_is_reported_not_fatal(self):
        logs = []
        with patch.object(tunnel, "binary", return_value=None):
            tunnel.Tunnel(8770, lambda u: None, log=logs.append)._keep_running()
        self.assertIn("cloudflared isn't installed", logs[0])


class TwilioApiTests(unittest.TestCase):
    def test_e164(self):
        self.assertEqual(twilio_api.e164("(408) 555-0100"), "+14085550100")
        self.assertEqual(twilio_api.e164("+1 408 555 0100"), "+14085550100")
        self.assertEqual(twilio_api.e164(""), "")

    def test_point_number_finds_it_then_sets_the_voice_url(self):
        calls = []

        def fake(method, url, **kw):
            calls.append((method, url, kw.get("data"), kw.get("params")))
            body = {"incoming_phone_numbers": [{"sid": "PN1"}]} if method == "GET" else {}
            return NS(status_code=200, json=lambda: body)
        import requests
        with patch.object(requests, "request", side_effect=fake):
            twilio_api.point_number_at("AC1", "tok", "408-555-0100", "https://t/twilio/voice")
        self.assertEqual(calls[0][3], {"PhoneNumber": "+14085550100"})
        self.assertTrue(calls[1][1].endswith("/Accounts/AC1/IncomingPhoneNumbers/PN1.json"))
        self.assertEqual(calls[1][2], {"VoiceUrl": "https://t/twilio/voice", "VoiceMethod": "POST"})

    def test_errors_are_plain_and_never_echo_credentials(self):
        import requests
        with patch.object(requests, "request", return_value=NS(status_code=401, json=lambda: {})):
            with self.assertRaisesRegex(twilio_api.TwilioError, "rejected the Account SID") as err:
                twilio_api.account_status("AC1", "supersecret")
        self.assertNotIn("supersecret", str(err.exception))
        with patch.object(requests, "request", return_value=NS(status_code=200, json=lambda: {"incoming_phone_numbers": []})):
            with self.assertRaisesRegex(twilio_api.TwilioError, "isn't a number on this Twilio account"):
                twilio_api.number_sid("AC1", "t", "4085550100")


class SetupAndPluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        p = patch.object(service, "STATUS_FILE", Path(self.temp.name) / "phone_status.json")
        p.start()
        self.addCleanup(p.stop)

    def config(self, **phone):
        def get(ns):
            return phone if ns == "phone_line" else {}
        return patch("memory.config_manager.get_plugin_config", side_effect=get)

    def test_missing_lists_every_step_left(self):
        with self.config(), patch.object(service, "gemini_key", return_value="k"), \
                patch.object(tunnel, "binary", return_value=None):
            self.assertEqual(service.missing(), ["turn the phone line on", "the Twilio Account SID and Auth Token",
                                                 "the Twilio phone number", "the Stripe payment link",
                                                 "cloudflared (the tunnel program)"])
        with self.config(enabled="on", twilio_sid="AC1", twilio_token="t", twilio_number="4085550100",
                         payment_link="https://buy.stripe.com/x"), \
                patch.object(service, "gemini_key", return_value="k"), \
                patch.object(tunnel, "binary", return_value="/x/cloudflared"):
            self.assertEqual(service.missing(), [])

    def test_start_stays_off_and_says_why(self):
        with self.config(), patch.object(service, "gemini_key", return_value=""), \
                patch.object(tunnel, "binary", return_value=None):
            self.assertIsNone(service.start_if_configured(log=lambda m: None))
        import json
        self.assertEqual(json.loads(service.STATUS_FILE.read_text())["state"], "off")

    def test_plugin_status_and_calls(self):
        from plugins import phone_line
        with patch("worker.phone.service.missing", return_value=["the Twilio phone number"]):
            self.assertIn("Still needed: the Twilio phone number", phone_line.run({"action": "status"}))
        service.write_status(state="answering", number="+14085550100", last_call=time.time(),
                             last_caller="Joe's Tacos")
        with patch("worker.phone.service.missing", return_value=[]):
            self.assertIn("answering on +14085550100. Last call: Joe's Tacos", phone_line.run({"action": "status"}))
        rows = [{"ts": 1_800_000_000, "title": "Joe's Tacos", "status": "wants_site",
                 "detail": {"emailed": ["joe@gmail.com"], "summary": "Wants it Friday"}}]
        with patch("core.events.Store.events", return_value=rows):
            out = phone_line.run({"action": "calls"})
        self.assertIn("Joe's Tacos — wants the site; link emailed to joe@gmail.com. Note: \"Wants it Friday\"", out)


class CallAlertTests(unittest.TestCase):
    def test_a_call_is_announced_with_its_outcome(self):
        from plugins import while_away
        rows = [{"id": 5, "kind": "call_received", "title": "Joe's Tacos", "status": "wants_site",
                 "detail": {"emailed": ["joe@gmail.com"], "summary": "Ready to pay"}}]
        st = NS(get=lambda k, d=None: 0, delivered_through=lambda: (0, 0), events=lambda **kw: rows,
                set=lambda k, v: None)
        said = []
        with patch.object(while_away, "store", return_value=st), \
                patch.object(while_away.away, "idle_seconds", return_value=5), \
                patch.object(while_away.away, "screen_state", return_value="unlocked"):
            n = while_away.announce_replies(lambda text: said.append(text), None, lambda: True)
        self.assertEqual(n, 1)
        self.assertIn("Joe's Tacos called and wants the website; the payment link was emailed", said[0])
        self.assertIn('"Ready to pay"', said[0])


if __name__ == "__main__":
    unittest.main()
