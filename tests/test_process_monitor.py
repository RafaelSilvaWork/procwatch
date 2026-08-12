"""Testes para o ProcessMonitor."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.models import ProcessSnapshot
from backend.process_monitor import ProcessMonitor


class ProcessMonitorTests(unittest.TestCase):
    def test_collect_all_processes_returns_snapshots(self):
        monitor = ProcessMonitor(max_processes=5)
        snapshots = monitor._collect_all_processes()

        self.assertLessEqual(len(snapshots), 5)
        for snapshot in snapshots:
            self.assertIsInstance(snapshot, ProcessSnapshot)
            # System Idle Process (PID 0) reporta cpu_percent como tempo
            # ocioso, não uso real - não deve aparecer na coleta.
            self.assertNotEqual(snapshot.pid, 0)
            self.assertNotEqual(snapshot.name.lower(), "system idle process")

    def test_get_top_processes_sorts_by_cpu(self):
        monitor = ProcessMonitor()
        monitor._cached_snapshots = [
            ProcessSnapshot(pid=1, name="a", cpu_percent=10.0, memory_mb=0,
                             memory_percent=0, io_read_mb=0, io_write_mb=0, status="running"),
            ProcessSnapshot(pid=2, name="b", cpu_percent=50.0, memory_mb=0,
                             memory_percent=0, io_read_mb=0, io_write_mb=0, status="running"),
        ]

        top = monitor.get_top_processes(by='cpu', limit=1)

        self.assertEqual(top[0].pid, 2)

    def test_get_process_by_name_is_case_insensitive(self):
        monitor = ProcessMonitor()
        monitor._cached_snapshots = [
            ProcessSnapshot(pid=1, name="Chrome.exe", cpu_percent=0, memory_mb=0,
                             memory_percent=0, io_read_mb=0, io_write_mb=0, status="running"),
        ]

        matches = monitor.get_process_by_name("chrome.exe")

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].pid, 1)


if __name__ == "__main__":
    unittest.main()
