"""Testes para o ProcessMonitor."""

import os
import sys
import time
import unittest
from unittest.mock import patch

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

    def test_request_refresh_burst_does_not_collect_faster_than_minimum(self):
        """request_refresh() em rajada não pode fazer duas coletas reais
        colarem muito perto uma da outra - senão cpu_percent() calcula com
        um denominador de tempo perto de zero e infla o valor para ~100%
        em processos que não têm nada de errado (bug real que já vimos
        acontecer)."""
        monitor = ProcessMonitor(update_interval=5.0, max_processes=1)
        monitor.MIN_COLLECT_INTERVAL = 0.2  # acelera o teste

        collect_times = []
        with patch.object(monitor, '_collect_all_processes', side_effect=lambda: collect_times.append(time.monotonic()) or []):
            monitor.start(lambda snapshots: None)
            # dispara uma rajada de refreshes bem rápida
            for _ in range(5):
                monitor.request_refresh()
                time.sleep(0.02)
            time.sleep(0.5)
            monitor.stop()

        self.assertGreaterEqual(len(collect_times), 2)
        gaps = [b - a for a, b in zip(collect_times, collect_times[1:])]
        for gap in gaps:
            self.assertGreaterEqual(gap, monitor.MIN_COLLECT_INTERVAL - 0.05)

    def test_pinned_pid_survives_top_n_cutoff(self):
        """Um processo "fixado" (o selecionado pelo usuário) não pode sumir
        da coleta só porque a CPU dele caiu no ranking - senão o painel
        "Processo Selecionado" fica sem dado novo pra mostrar (bug real
        relatado: o painel travava, sem nunca mais atualizar)."""
        monitor = ProcessMonitor(max_processes=2)
        monitor.set_pinned_pid(999)

        class FakeProc:
            def __init__(self, pid, cpu):
                self.pid = pid
                self.info = {
                    'pid': pid, 'name': f'proc{pid}.exe', 'cpu_percent': cpu,
                    'memory_percent': 0.0, 'memory_info': None,
                    'status': 'running', 'num_threads': 1,
                }

        fake_processes = [FakeProc(1, 90.0), FakeProc(2, 80.0), FakeProc(999, 0.1)]

        with patch('backend.process_monitor.psutil.process_iter', return_value=fake_processes), \
             patch('backend.process_monitor.get_pids_with_visible_window', return_value=set()):
            snapshots = monitor._collect_all_processes()

        pids = {s.pid for s in snapshots}
        self.assertIn(999, pids, "processo fixado não pode sumir mesmo fora do TOP N por CPU")
        self.assertEqual(len(snapshots), 3)


if __name__ == "__main__":
    unittest.main()
