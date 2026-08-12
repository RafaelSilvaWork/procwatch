"""Testes para o AlertEngine."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.alert_engine import AlertEngine
from backend.models import AlertSeverity, ProcessSnapshot


def make_snapshot(pid=1234, name="test.exe", cpu=0.0, mem=0.0, status="running"):
    return ProcessSnapshot(
        pid=pid, name=name, cpu_percent=cpu, memory_mb=100.0,
        memory_percent=mem, io_read_mb=0.0, io_write_mb=0.0, status=status,
    )


class AlertEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = AlertEngine()
        self.received = []
        self.engine.subscribe(self.received.append)

    def test_cpu_critical_triggers_alert(self):
        self.engine.check_processes([make_snapshot(cpu=99.0)])

        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0].severity, AlertSeverity.CRITICAL)

    def test_alert_is_suppressed_on_repeat(self):
        snapshot = make_snapshot(cpu=99.0)
        self.engine.check_processes([snapshot])
        self.engine.check_processes([snapshot])

        self.assertEqual(len(self.received), 1)

    def test_below_threshold_does_not_alert(self):
        self.engine.check_processes([make_snapshot(cpu=10.0, mem=10.0)])

        self.assertEqual(len(self.received), 0)

    def test_prune_dead_processes_clears_history(self):
        self.engine.check_processes([make_snapshot(pid=555)])
        self.assertIn(555, self.engine._process_history)

        self.engine.check_processes([])
        self.assertNotIn(555, self.engine._process_history)

    def test_zombie_status_triggers_error_alert(self):
        self.engine.check_processes([make_snapshot(status="zombie")])

        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0].severity, AlertSeverity.ERROR)

    def test_critical_log_keyword_triggers_alert(self):
        self.engine.check_log_entry("Fatal error: segmentation fault")

        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0].severity, AlertSeverity.CRITICAL)

    def test_warning_log_keyword_triggers_alert(self):
        self.engine.check_log_entry("Connection timeout while contacting server")

        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0].severity, AlertSeverity.WARNING)

    def test_update_thresholds_changes_behavior(self):
        self.engine.update_thresholds({'cpu_critical': 50.0})
        self.engine.check_processes([make_snapshot(cpu=60.0)])

        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0].severity, AlertSeverity.CRITICAL)

    def test_clear_alerts_empties_history(self):
        self.engine.check_processes([make_snapshot(cpu=99.0)])
        self.assertTrue(self.engine.get_recent_alerts())

        self.engine.clear_alerts()
        self.assertEqual(self.engine.get_recent_alerts(), [])


if __name__ == "__main__":
    unittest.main()
