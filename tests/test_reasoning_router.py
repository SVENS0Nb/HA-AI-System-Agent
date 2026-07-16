from __future__ import annotations

import unittest

from app.reasoning import AdaptiveReasoningRouter


class AdaptiveReasoningRouterTests(unittest.TestCase):
    def test_simple_status_and_greetings_use_no_reasoning(self) -> None:
        self.assertEqual(AdaptiveReasoningRouter.select("Hallo"), "none")
        self.assertEqual(
            AdaptiveReasoningRouter.select("Welchen Zustand hat sensor.kueche?"),
            "none",
        )

    def test_monitor_work_uses_medium_reasoning(self) -> None:
        self.assertEqual(
            AdaptiveReasoningRouter.select(
                "Überwache sensor.heizung und benachrichtige mich bei unavailable"
            ),
            "medium",
        )

    def test_device_control_uses_medium_reasoning(self) -> None:
        self.assertEqual(
            AdaptiveReasoningRouter.select("Schalte light.wohnzimmer ein"),
            "medium",
        )

    def test_cross_source_root_cause_work_uses_high_reasoning(self) -> None:
        self.assertEqual(
            AdaptiveReasoningRouter.select(
                "Führe eine Ursachenanalyse durch, vergleiche Logs und mehrere Config-Dateien auf Fehler."
            ),
            "high",
        )

    def test_proactive_runs_have_a_medium_floor(self) -> None:
        self.assertEqual(
            AdaptiveReasoningRouter.select("Status melden", proactive=True), "medium"
        )

    def test_structural_escalation_is_bounded(self) -> None:
        self.assertEqual(AdaptiveReasoningRouter.escalate("none", "medium"), "medium")
        self.assertEqual(AdaptiveReasoningRouter.escalate("high", "medium"), "high")
        self.assertEqual(AdaptiveReasoningRouter.at_least("low", "medium"), "medium")

    def test_effort_is_normalized_for_known_model_capabilities(self) -> None:
        self.assertEqual(
            AdaptiveReasoningRouter.for_model("gpt-5-pro", "none"), "high"
        )
        self.assertEqual(
            AdaptiveReasoningRouter.for_model("gpt-5.4-pro", "low"), "medium"
        )
        self.assertEqual(
            AdaptiveReasoningRouter.for_model("gpt-5.4", "max"), "xhigh"
        )
        self.assertEqual(
            AdaptiveReasoningRouter.for_model("custom-model", "xhigh"), "xhigh"
        )
