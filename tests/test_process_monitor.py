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

        # max_processes é um teto "flexível": PIDs fixados e processos com
        # caminho suspeito sempre sobrevivem ao corte, então o resultado
        # pode passar um pouco de 5 numa máquina real - o que importa aqui
        # é que o corte está funcionando (não devolvendo todos os processos
        # do sistema) e que os snapshots são válidos.
        self.assertLessEqual(len(snapshots), 20)
        self.assertGreater(len(snapshots), 0)
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
        monitor.pin_pid(999)

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

    def test_suspicious_process_survives_top_n_cutoff_and_is_flagged(self):
        """Um "svchost.exe" falso rodando de local suspeito não pode sumir
        do corte por CPU baixa - ele precisa aparecer e vir marcado."""
        monitor = ProcessMonitor(max_processes=1)

        class FakeProc:
            def __init__(self, pid, cpu, name):
                self.pid = pid
                self.info = {
                    'pid': pid, 'name': name, 'cpu_percent': cpu,
                    'memory_percent': 0.0, 'memory_info': None,
                    'status': 'running', 'num_threads': 1,
                }

        fake_processes = [
            FakeProc(1, 90.0, 'real.exe'),
            FakeProc(666, 0.1, 'svchost.exe'),  # CPU baixa, mas nome de sistema
        ]

        class FakeExeProc:
            def __init__(self, pid):
                self._pid = pid

            def exe(self):
                if self._pid == 666:
                    return r'C:\Users\alguem\AppData\Local\Temp\svchost.exe'
                return r'C:\Windows\System32\real.exe'

        with patch('backend.process_monitor.psutil.process_iter', return_value=fake_processes), \
             patch('backend.process_monitor.psutil.Process', side_effect=lambda pid: FakeExeProc(pid)), \
             patch('backend.process_monitor.get_pids_with_visible_window', return_value=set()):
            snapshots = monitor._collect_all_processes()

        by_pid = {s.pid: s for s in snapshots}
        self.assertIn(666, by_pid, "processo suspeito não pode sumir mesmo fora do TOP N por CPU")
        self.assertTrue(by_pid[666].is_suspicious_path)
        self.assertFalse(by_pid[1].is_suspicious_path)

    def test_multiple_pinned_pids_all_survive_cutoff(self):
        """Vários processos monitorados ao mesmo tempo - todos precisam
        sobreviver ao corte do TOP N, não só um."""
        monitor = ProcessMonitor(max_processes=1)
        monitor.pin_pid(888)
        monitor.pin_pid(999)

        class FakeProc:
            def __init__(self, pid, cpu):
                self.pid = pid
                self.info = {
                    'pid': pid, 'name': f'proc{pid}.exe', 'cpu_percent': cpu,
                    'memory_percent': 0.0, 'memory_info': None,
                    'status': 'running', 'num_threads': 1,
                }

        fake_processes = [FakeProc(1, 90.0), FakeProc(888, 0.2), FakeProc(999, 0.1)]

        with patch('backend.process_monitor.psutil.process_iter', return_value=fake_processes), \
             patch('backend.process_monitor.get_pids_with_visible_window', return_value=set()):
            snapshots = monitor._collect_all_processes()

        pids = {s.pid for s in snapshots}
        self.assertEqual(pids, {1, 888, 999})

    def test_unpin_pid_allows_it_to_be_cut_again(self):
        monitor = ProcessMonitor(max_processes=1)
        monitor.pin_pid(999)
        monitor.unpin_pid(999)

        class FakeProc:
            def __init__(self, pid, cpu):
                self.pid = pid
                self.info = {
                    'pid': pid, 'name': f'proc{pid}.exe', 'cpu_percent': cpu,
                    'memory_percent': 0.0, 'memory_info': None,
                    'status': 'running', 'num_threads': 1,
                }

        fake_processes = [FakeProc(1, 90.0), FakeProc(999, 0.1)]

        with patch('backend.process_monitor.psutil.process_iter', return_value=fake_processes), \
             patch('backend.process_monitor.get_pids_with_visible_window', return_value=set()):
            snapshots = monitor._collect_all_processes()

        pids = {s.pid for s in snapshots}
        self.assertEqual(pids, {1})

    def test_window_scan_is_cached_across_cycles(self):
        """Enumerar as janelas do Windows a cada coleta é desperdício - a
        lista de janelas abertas muda bem menos que CPU/memória. Só deve
        rodar a cada WINDOW_SCAN_EVERY_N_CYCLES coletas."""
        monitor = ProcessMonitor(max_processes=5)
        call_count = 0

        def fake_get_pids():
            nonlocal call_count
            call_count += 1
            return set()

        with patch('backend.process_monitor.get_pids_with_visible_window', side_effect=fake_get_pids):
            for _ in range(7):
                monitor._collect_all_processes()

        expected = len(range(0, 7, monitor.WINDOW_SCAN_EVERY_N_CYCLES))
        self.assertEqual(call_count, expected)


if __name__ == "__main__":
    unittest.main()
