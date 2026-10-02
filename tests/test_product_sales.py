"""Promotion workflow tests; all email, web, and Gumroad access is replaced."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from plugins import gmail, product_sales as sales


class ProductSalesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state_path = Path(self.directory.name) / "private/state.json"
        self.path_patch = patch.object(sales, "_STATE_FILE", self.state_path)
        self.path_patch.start()
        self.settings = {"gmail_account": "personal", "sender_name": "Example Seller",
                         "postal_address": "123 Example Street, Example City, CA 00000"}
        self.config_patch = patch.object(sales, "get_plugin_config", return_value=self.settings)
        self.enabled_patch = patch.object(sales, "get_plugin_enabled", return_value=True)
        self.config_patch.start()
        self.enabled_patch.start()
        self.player = MagicMock()
        self.service = MagicMock()
        self.service.users.return_value.getProfile.return_value.execute.return_value = {"emailAddress": "seller@example.com"}

    def tearDown(self):
        self.enabled_patch.stop()
        self.config_patch.stop()
        self.path_patch.stop()
        self.directory.cleanup()

    def add_lead(self, email="coach@example.com"):
        return sales.run({"action": "add_lead", "email": email, "name": "Example Soccer Club",
                          "source_url": "https://example.com/club/contact", "context": "Youth soccer coaching"})

    def draft(self):
        self.add_lead()
        sales.run({"action": "draft", "email": "coach@example.com"}, player=self.player)
        return next(iter(sales._load()["drafts"]))

    def queue(self, draft_id, **extra):
        captured = {}

        def request(**kwargs):
            captured.update(kwargs)
            return "[CONFIRMATION_PENDING]"

        with patch.object(gmail, "_service", return_value=self.service), \
                patch.object(sales.confirm, "pending_title", return_value=""), \
                patch.object(sales.confirm, "request", side_effect=request):
            result = sales.run({"action": "send", "draft_id": draft_id, **extra}, player=self.player)
        return result, captured

    def test_brief_works_without_account_setup_or_runtime_writes(self):
        self.settings.clear()
        with patch.object(gmail, "_service") as service, patch.object(sales, "_cli_json") as cli:
            result = sales.run({"action": "brief"})
        self.assertIn("30 players", result)
        self.assertIn("Desktop Excel", result)
        self.assertIn("current price", result)
        self.assertNotIn("$12", result)
        self.assertFalse(self.state_path.exists())
        service.assert_not_called()
        cli.assert_not_called()

    def test_registry_exposes_the_plugin_and_existing_settings_contract(self):
        from core import plugin_loader
        record = plugin_loader._validate(sales, "product_sales.py")
        self.assertTrue(record.valid, record.error)
        registry = plugin_loader.PluginRegistry({record.name: record}, lambda _message: None)
        with patch.object(plugin_loader, "get_plugin_enabled", return_value=True), patch.object(plugin_loader, "get_plugin_config", return_value={}):
            declaration = registry.get_tool_declarations()[0]
            self.assertEqual(declaration["name"], "product_sales")
            self.assertEqual(declaration["behavior"], "NON_BLOCKING")
            self.assertEqual(registry.settings_schemas()[0]["namespace"], "product_sales")
            self.assertIn("Soccer Coach Organizer", registry.run("product_sales", {"action": "brief"}))
        self.assertFalse(self.state_path.exists())

    def test_campaign_creates_six_distinct_local_drafts_and_tagged_links(self):
        with patch.object(sales, "_cli_json") as cli, patch.object(gmail, "_service") as service:
            result = sales.run({"action": "campaign", "channel": "facebook"}, player=self.player)
        self.assertIn("None posted", result)
        drafts = sales._load()["drafts"]
        self.assertEqual(len(drafts), 6)
        for key, draft in drafts.items():
            url = draft["body"].split("current price: ", 1)[1]
            self.assertEqual(urlsplit(url).netloc, "attontios.gumroad.com")
            self.assertEqual(parse_qs(urlsplit(url).query)["utm_content"], [key])
            self.assertEqual(draft["status"], "draft")
        if os.name == "posix":
            self.assertEqual(self.state_path.stat().st_mode & 0o777, 0o600)
        cli.assert_not_called()
        service.assert_not_called()

    def test_contact_requires_evidence_and_exact_email(self):
        for email in ["Coach Name", "coach@example.com, other@example.com", "coach@example.com\nBcc: victim@example.com"]:
            result = sales.run({"action": "add_lead", "email": email, "name": "Coach", "relationship": "Known coach", "context": "Soccer"})
            self.assertIn("exact email", result)
        self.assertIn("verified contact", sales.run({"action": "add_lead", "email": "coach@example.com"}))
        self.assertIn("public http", sales.run({"action": "add_lead", "email": "coach@example.com", "source_url": "javascript:alert(1)"}))
        self.assertFalse(self.state_path.exists())

    def test_opt_out_survives_rediscovery_case_changes_and_record_updates(self):
        self.add_lead()
        sales.run({"action": "record", "email": "coach@example.com", "outcome": "do_not_contact"})
        self.add_lead("COACH@EXAMPLE.COM")
        self.assertEqual(len(sales._load()["leads"]), 1)
        self.assertIn("already pitched", sales.run({"action": "draft", "email": "coach@example.com"}))
        self.assertIn("remains opted out", sales.run({"action": "record", "email": "coach@example.com", "outcome": "bought"}))
        self.assertEqual(sales._load()["leads"]["coach@example.com"]["status"], "do_not_contact")

    def test_corrupt_history_is_preserved_and_blocks_promotion_writes(self):
        self.state_path.parent.mkdir()
        self.state_path.write_text("damaged opt-out history", encoding="utf-8")
        result = self.add_lead()
        self.assertIn("opt-outs must not be lost", result)
        self.assertEqual(self.state_path.read_text(), "damaged opt-out history")

    def test_send_requires_setup_full_ui_and_an_enabled_gmail_plugin(self):
        draft_id = self.draft()
        with patch.object(gmail, "_service") as service:
            self.assertIn("Jarvis window", sales.run({"action": "send", "draft_id": draft_id}))
            with patch.object(sales, "get_plugin_enabled", return_value=False):
                self.assertIn("disabled", sales.run({"action": "send", "draft_id": draft_id}, player=self.player))
            self.settings.clear()
            self.assertIn("Plugin Settings", sales.run({"action": "send", "draft_id": draft_id}, player=self.player))
        service.assert_not_called()

    def test_an_existing_confirmation_is_not_replaced(self):
        draft_id = self.draft()
        with patch.object(sales.confirm, "pending_title", return_value="Another action"), patch.object(gmail, "_service") as service, patch.object(sales.confirm, "request") as request:
            result = sales.run({"action": "send", "draft_id": draft_id}, player=self.player)
        self.assertIn("already on screen", result)
        service.assert_not_called()
        request.assert_not_called()

    def test_only_confirmed_callback_sends_the_exact_reviewed_email_once(self):
        draft_id = self.draft()
        with patch.object(gmail, "_send", return_value="Email sent.") as send:
            result, captured = self.queue(draft_id)
            self.assertEqual(result, "[CONFIRMATION_PENDING]")
            send.assert_not_called()
            self.assertEqual(sales._load()["leads"]["coach@example.com"]["status"], "new")
            review = self.player.show_content.call_args.args[1]
            self.assertIn(self.settings["postal_address"], review)
            self.assertIn("Advertisement", review)
            self.assertIn("reply STOP", review)
            self.settings["sender_name"] = "Changed after review"
            self.assertEqual(captured["run"](), "Email sent.")
            body = send.call_args.args[3]
            self.assertTrue(review.endswith(body))
            self.assertNotIn("Changed after review", body)
            self.assertIn("status changed", captured["run"]())
        send.assert_called_once()
        self.assertEqual(sales._load()["leads"]["coach@example.com"]["status"], "sent")

    def test_opt_out_between_preview_and_confirmation_cancels_send(self):
        draft_id = self.draft()
        result, captured = self.queue(draft_id)
        self.assertEqual(result, "[CONFIRMATION_PENDING]")
        sales.run({"action": "record", "email": "coach@example.com", "outcome": "do_not_contact"})
        with patch.object(gmail, "_send") as send:
            self.assertIn("cancelled", captured["run"]())
        send.assert_not_called()

    def test_spam_sender_setting_uses_its_real_address_and_requires_confirmation(self):
        self.settings["gmail_account"] = "spam"
        self.service.users.return_value.getProfile.return_value.execute.return_value = {
            "emailAddress": "promotion@example.com"
        }
        draft_id, captured = self.draft(), {}

        def request(**kwargs):
            captured.update(kwargs)
            return "[CONFIRMATION_PENDING]"

        with patch.object(gmail, "_service", return_value=self.service) as connect, \
                patch.object(gmail, "_send", return_value="Email sent.") as send, \
                patch.object(sales.confirm, "pending_title", return_value=""), \
                patch.object(sales.confirm, "request", side_effect=request):
            result = sales.run({"action": "send", "draft_id": draft_id}, player=self.player)
            connect.assert_called_once_with("spam")
            self.assertEqual(result, "[CONFIRMATION_PENDING]")
            review = self.player.show_content.call_args.args[1]
            self.assertIn("Account: spam\nFrom: promotion@example.com", review)
            self.assertIn("From spam Gmail: promotion@example.com", captured["detail"])
            send.assert_not_called()
            self.settings["gmail_account"] = "personal"
            self.assertEqual(captured["run"](), "Email sent.")
            send.assert_called_once()
            self.assertIs(send.call_args.args[0], self.service)
            self.assertEqual(send.call_args.args[4], "promotion@example.com")
            self.assertTrue(review.endswith(send.call_args.args[3]))

    def test_explicit_spam_account_overrides_the_personal_default(self):
        draft_id = self.draft()
        with patch.object(gmail, "_service", return_value=self.service) as connect, \
                patch.object(sales.confirm, "pending_title", return_value=""), \
                patch.object(sales.confirm, "request", return_value="[CONFIRMATION_PENDING]"):
            result = sales.run({"action": "send", "draft_id": draft_id, "account": "spam"}, player=self.player)
        connect.assert_called_once_with("spam")
        self.assertEqual(result, "[CONFIRMATION_PENDING]")
        self.service.users().messages().send.assert_not_called()

    def test_uncertain_send_is_not_retried_and_can_be_resolved_explicitly(self):
        draft_id = self.draft()
        _, captured = self.queue(draft_id)
        with patch.object(gmail, "_send", side_effect=TimeoutError("secret response")) as send:
            self.assertIn("Check Sent Mail", captured["run"]())
            self.assertIn("cancelled", captured["run"]())
        send.assert_called_once()
        self.assertEqual(sales._load()["leads"]["coach@example.com"]["status"], "uncertain")
        result, captured = self.queue(draft_id)
        self.assertIn("no longer eligible", result)
        self.assertEqual(captured, {})
        sales.run({"action": "record", "email": "coach@example.com", "outcome": "not_sent"})
        self.assertEqual(sales._load()["leads"]["coach@example.com"]["status"], "new")

    def test_oversized_draft_cannot_be_hidden_by_preview_truncation(self):
        self.add_lead()
        result = sales.run({"action": "draft", "email": "coach@example.com", "body": "a" * 2001})
        self.assertIn("2,000", result)
        self.assertEqual(sales._load()["drafts"], {})

    def test_sales_uses_canonical_product_id_deduplicates_pages_and_hides_buyers(self):
        sale = {"id": "order_1", "email": "private-buyer@example.com", "created_at": "2026-10-01", "formatted_total_price": "$12", "refunded": False}
        responses = [{"success": True, "product": {"id": "canonical-id"}},
                     {"success": True, "sales": [sale], "next_page_key": "page-2"},
                     {"success": True, "sales": [sale, {"id": "order_2", "refunded": True}, {"id": "test-order", "is_test_purchase": True}]}]
        with patch.object(sales, "_cli_json", side_effect=responses) as cli:
            result = sales.run({"action": "sales"}, player=self.player)
        self.assertIn("2 reported orders, 1 marked refunded", result)
        self.assertIn("1 explicitly flagged test", result)
        self.assertIn("All returned pages checked", result)
        self.assertNotIn("private-buyer@example.com", result)
        self.assertNotIn("private-buyer@example.com", self.player.show_content.call_args.args[1])
        self.assertEqual(cli.call_args_list[1].args[0][3], "canonical-id")
        self.assertIn("--page-key", cli.call_args_list[2].args[0])
        self.assertFalse(self.state_path.exists())

    def test_page_limit_is_labelled_partial(self):
        responses = [{"success": True, "product": {"id": "canonical-id"}}] + [
            {"success": True, "sales": [{"id": f"order-{i}"}], "next_page_key": f"page-{i}"} for i in range(3)]
        with patch.object(sales, "_cli_json", side_effect=responses):
            result = sales.run({"action": "sales"})
        self.assertIn("Partial report", result)
        self.assertIn("totals are incomplete", result)

    def test_authentication_failure_is_not_reported_as_zero_sales_or_leaked(self):
        result = MagicMock(returncode=1, stdout=json.dumps({"success": False, "error": {"code": "not_authenticated", "message": "sensitive token value"}}))
        with patch.object(sales, "_cli_path", return_value="/example/gumroad"), patch.object(sales.subprocess, "run", return_value=result) as run:
            response = sales.run({"action": "sales"})
        self.assertIn("auth login --web", response)
        self.assertNotIn("0 reported orders", response)
        self.assertNotIn("sensitive token value", response)
        self.assertIn("--non-interactive", run.call_args.args[0])
        self.assertNotIn("shell", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
