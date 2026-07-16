from __future__ import annotations

import unittest

from app.entity_control import EntityControlDenied, resolve_entity_control


def entity(entity_id: str, **attributes: object) -> dict[str, object]:
    return {"entity_id": entity_id, "state": "on", "attributes": attributes}


class EntityControlBoundaryTests(unittest.TestCase):
    def test_common_physical_actions_resolve_to_exact_entity_target(self) -> None:
        light = resolve_entity_control(
            entity("light.kitchen"), "set_brightness", 42, None
        )
        self.assertEqual(light["domain"], "light")
        self.assertEqual(light["service"], "turn_on")
        self.assertEqual(light["service_data"], {"brightness_pct": 42.0})
        self.assertEqual(light["target"], {"entity_id": "light.kitchen"})

        climate = resolve_entity_control(
            entity("climate.office", hvac_modes=["off", "heat"]),
            "set_hvac_mode",
            None,
            "heat",
        )
        self.assertEqual(climate["service_data"], {"hvac_mode": "heat"})

    def test_numeric_values_are_finite_and_use_entity_bounds(self) -> None:
        thermostat = entity("climate.office", min_temp=7, max_temp=30)
        with self.assertRaisesRegex(EntityControlDenied, "höchstens 30"):
            resolve_entity_control(thermostat, "set_temperature", 45, None)
        with self.assertRaisesRegex(EntityControlDenied, "endlich"):
            resolve_entity_control(
                entity("number.charge_limit", min=0, max=32),
                "set_value",
                float("nan"),
                None,
            )

    def test_unsupported_domains_and_cross_domain_actions_are_blocked(self) -> None:
        for entity_id in (
            "automation.open_door",
            "script.restart_server",
            "scene.away",
            "button.reboot_router",
            "input_boolean.admin_mode",
            "update.home_assistant_core",
        ):
            with self.subTest(entity_id=entity_id):
                with self.assertRaises(EntityControlDenied):
                    resolve_entity_control(entity(entity_id), "turn_on", None, None)
        with self.assertRaises(EntityControlDenied):
            resolve_entity_control(entity("light.kitchen"), "unlock", None, None)

    def test_mode_must_be_currently_advertised_by_entity(self) -> None:
        with self.assertRaisesRegex(EntityControlDenied, "nicht angeboten"):
            resolve_entity_control(
                entity("select.ev_mode", options=["eco", "fast"]),
                "select_option",
                None,
                "dangerous-hidden-mode",
            )


if __name__ == "__main__":
    unittest.main()
