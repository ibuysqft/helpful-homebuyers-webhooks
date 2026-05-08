"""Tests for appointment calendar routing."""
import importlib
import os
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestCalendarRouting(unittest.TestCase):
    def test_fixed_owner_defaults_to_jeffrey(self):
        with patch.dict(os.environ, {}, clear=False):
            import calendar_routing

            importlib.reload(calendar_routing)
            route = calendar_routing.resolve_calendar_route(agent_name="shelby")

            self.assertEqual(route.mode, "fixed_owner")
            self.assertEqual(route.owner_key, "jeffrey_bord")
            self.assertEqual(route.owner_name, "Jeffrey Bord")
            self.assertEqual(route.calendar_id, "2xJXutj4eTskFPYx8AeL")

    def test_round_robin_uses_rep_pool_when_configured(self):
        env = {
            "CALENDAR_ROUTING_MODE": "round_robin",
            "CALENDAR_REP_CONFIG_JSON": """
            [
              {"owner_key": "harper", "owner_name": "Harper", "calendar_id": "cal-1", "location_id": "loc-1"},
              {"owner_key": "riley", "owner_name": "Riley", "calendar_id": "cal-2", "location_id": "loc-2"}
            ]
            """,
        }
        with patch.dict(os.environ, env, clear=False):
            import calendar_routing

            importlib.reload(calendar_routing)
            route = calendar_routing.resolve_calendar_route(agent_name="shelby", contact_id="contact-123")

            self.assertEqual(route.mode, "round_robin")
            self.assertIn(route.owner_key, {"harper", "riley"})
            self.assertIn(route.calendar_id, {"cal-1", "cal-2"})

    def test_requested_owner_key_pins_route_for_booking(self):
        env = {
            "CALENDAR_ROUTING_MODE": "round_robin",
            "CALENDAR_REP_CONFIG_JSON": """
            [
              {"owner_key": "harper", "owner_name": "Harper", "calendar_id": "cal-1", "location_id": "loc-1"},
              {"owner_key": "riley", "owner_name": "Riley", "calendar_id": "cal-2", "location_id": "loc-2"}
            ]
            """,
        }
        with patch.dict(os.environ, env, clear=False):
            import calendar_routing

            importlib.reload(calendar_routing)
            route = calendar_routing.resolve_calendar_route(
                agent_name="shelby",
                requested_owner_key="riley",
            )

            self.assertEqual(route.owner_key, "riley")
            self.assertEqual(route.calendar_id, "cal-2")


if __name__ == "__main__":
    unittest.main()
