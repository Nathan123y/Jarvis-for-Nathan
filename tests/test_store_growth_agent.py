import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from plugins import store_growth_agent as agent


class StoreGrowthAgentTests(unittest.TestCase):
    def test_cycles_until_verified_revenue_then_focuses(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(agent, "STATE_FILE", Path(folder) / "state.json"):
            agent.run({"action": "start", "platform": "etsy", "products": "Coach Planner, Pet Planner"})
            self.assertIn("Coach Planner", agent.run({"action": "next"}))
            no_sale = agent.run({"action": "record", "product": "Coach Planner", "views": 20, "orders": 0, "revenue_cents": 0})
            self.assertIn("No verified revenue", no_sale)
            agent.run({"action": "record", "product": "Pet Planner", "views": 12, "orders": 1, "revenue_cents": 900})
            self.assertIn("Focus mode: Pet Planner", agent.run({"action": "next"}))

    def test_rejects_unbounded_or_unverified_inputs(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(agent, "STATE_FILE", Path(folder) / "state.json"):
            with self.assertRaisesRegex(ValueError, "Etsy or Gumroad"):
                agent.run({"action": "start", "platform": "email", "products": "Thing"})
            agent.run({"action": "start", "platform": "gumroad", "products": "Thing"})
            with self.assertRaisesRegex(ValueError, "non-negative"):
                agent.run({"action": "record", "product": "Thing", "revenue_cents": -1})


if __name__ == "__main__":
    unittest.main()
