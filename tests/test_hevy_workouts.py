import unittest
from unittest.mock import patch

from plugins import hevy_workouts as hevy


class HevyTests(unittest.TestCase):
    def test_latest_workout_gives_recorded_comparison(self):
        workouts = [
            {"title": "Upper", "start_time": "2026-09-22T10:00:00Z", "exercises": [
                {"title": "Bench Press", "sets": [{"type": "normal", "weight_kg": 60, "reps": 8}]}]},
            {"title": "Upper", "start_time": "2026-09-15T10:00:00Z", "exercises": [
                {"title": "Bench Press", "sets": [{"type": "normal", "weight_kg": 55, "reps": 8}]}]},
        ]
        with patch.object(hevy, "get_plugin_config", return_value={"api_key": "SECRET"}), \
             patch.object(hevy, "_fetch", return_value=workouts):
            result = hevy.run({"action": "workout"})
        self.assertIn("Bench Press: 60 kg × 8 reps (previous: 55 kg × 8 reps)", result)

    def test_comparison_only_reports_observed_sets(self):
        workouts = [
            {"title": "Upper", "start_time": "2026-09-22T10:00:00Z", "exercises": [
                {"title": "Bench Press", "sets": [
                    {"type": "warmup", "weight_kg": 30, "reps": 10},
                    {"type": "normal", "weight_kg": 60, "reps": 8}]}]},
            {"title": "Upper", "start_time": "2026-09-15T10:00:00Z", "exercises": [
                {"title": "Bench Press", "sets": [
                    {"type": "normal", "weight_kg": 55, "reps": 8}]}]},
        ]
        with patch.object(hevy, "get_plugin_config", return_value={"api_key": "SECRET"}), \
             patch.object(hevy, "_fetch", return_value=workouts) as fetch:
            result = hevy.run({"action": "exercise", "exercise": "bench"})
        self.assertIn("60 kg × 8 reps versus 55 kg × 8 reps", result)
        self.assertNotIn("SECRET", result)
        self.assertEqual(fetch.call_args.kwargs["max_pages"], 8)

    def test_missing_key_and_api_errors_do_not_expose_secrets(self):
        with patch.object(hevy, "get_plugin_config", return_value={}):
            self.assertIn("isn't connected", hevy.run({"action": "recent"}))
        with patch.object(hevy, "get_plugin_config", return_value={"api_key": "SECRET"}), \
             patch.object(hevy, "_fetch", side_effect=RuntimeError("SECRET")):
            self.assertNotIn("SECRET", hevy.run({"action": "recent"}))

    def test_fetch_does_not_redirect_api_key(self):
        class Response:
            status_code = 200
            def json(self):
                return {"page_count": 1, "workouts": []}
        import sys
        import types
        stub = types.ModuleType("requests")
        stub.get = lambda *args, **kwargs: Response()
        with patch.dict(sys.modules, {"requests": stub}), \
             patch.object(stub, "get", return_value=Response()) as get:
            self.assertEqual(hevy._fetch("SECRET"), [])
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertEqual(get.call_args.kwargs["headers"]["api-key"], "SECRET")


if __name__ == "__main__":
    unittest.main()
