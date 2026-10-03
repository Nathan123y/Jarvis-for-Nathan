import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from plugins import etsy_growth


class EtsyGrowthTests(unittest.TestCase):
    def test_plugin_is_explicitly_etsy_only(self):
        self.assertIn("Etsy-only", etsy_growth.PLUGIN["description"])
        self.assertIn("Never use this tool for Gumroad", etsy_growth.PLUGIN["description"])
        self.assertTrue(etsy_growth.PLUGIN_SETTINGS["title"].startswith("Etsy shop"))

    def test_status_requires_real_etsy_destination(self):
        with patch.object(etsy_growth, "get_plugin_config", return_value={}):
            result = etsy_growth.run({"action": "status"})
        self.assertIn("incomplete", result)
        self.assertIn("HTTPS Etsy", result)

    def test_campaign_creates_review_only_trackable_drafts(self):
        player = MagicMock()
        with patch.object(etsy_growth, "get_plugin_config", return_value={
            "shop_url": "https://www.etsy.com/shop/ExampleShop"
        }):
            result = etsy_growth.run({
                "action": "campaign",
                "product_name": "Soccer Coach Organizer",
                "audience": "small soccer teams",
                "benefit": "keep attendance and session plans together",
            }, player=player)

        self.assertEqual(result.count("DRAFT "), 3)
        self.assertIn("Nothing was posted or purchased", result)
        urls = [line for line in result.splitlines() if line.startswith("https://")]
        self.assertEqual(len(urls), 3)
        for url in urls:
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(query["utm_source"], ["jarvis"])
            self.assertEqual(query["utm_campaign"], ["etsy_shop_growth"])
        player.show_content.assert_called_once()

    def test_rejects_lookalike_domain(self):
        with patch.object(etsy_growth, "get_plugin_config", return_value={
            "shop_url": "https://etsy.example/shop/fake"
        }):
            with self.assertRaisesRegex(ValueError, "public HTTPS Etsy"):
                etsy_growth.run({"action": "audit"})

    def test_listing_pack_never_claims_to_publish(self):
        with patch.object(etsy_growth, "get_plugin_config", return_value={
            "shop_url": "https://www.etsy.com/shop/ExampleShop"
        }):
            result = etsy_growth.run({
                "action": "listing", "product_name": "Coach Planner",
                "audience": "youth soccer coaches",
                "benefit": "keep session notes together",
                "contents": "One XLSX workbook and one PDF guide",
                "requirements": "Desktop Excel is required and sold separately",
                "keywords": "soccer planner, coach organizer, soccer template",
            })
        self.assertIn("TITLE\nCoach Planner for youth soccer coaches", result)
        self.assertIn("TAGS (3/13)", result)
        self.assertIn("did not sign in, create a listing", result)

    def test_reply_is_for_inbound_questions_and_is_not_sent(self):
        with patch.object(etsy_growth, "get_plugin_config", return_value={
            "shop_url": "https://www.etsy.com/shop/ExampleShop"
        }):
            result = etsy_growth.run({
                "action": "reply", "buyer_message": "Does this work in Numbers?",
                "answer": "Numbers compatibility has not been verified; desktop Excel is required.",
            })
        self.assertIn("Nothing was sent", result)
        self.assertIn("person who contacted your shop", result)
        self.assertIn("desktop Excel is required", result)


if __name__ == "__main__":
    unittest.main()
