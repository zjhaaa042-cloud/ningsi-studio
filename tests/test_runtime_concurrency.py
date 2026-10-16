"""Deterministic regressions for pending input and simultaneous starts."""
import threading
import unittest
from unittest.mock import PropertyMock, patch

from ningsi_studio.core.runtime import SessionManager, SessionRuntime
from ningsi_studio.http.router import Conflict, TooManyRequests
from ningsi_studio.settings import Settings


class RuntimeConcurrencyTests(unittest.TestCase):
    def test_pending_answer_cannot_be_overwritten(self):
        runtime = SessionRuntime('a' * 32)
        first = {'index': 0, 'rt': 0.42}
        self.assertTrue(runtime.provide_input('pvt', first))
        self.assertFalse(runtime.provide_input('pvt', {'index': 0, 'rt': 0.1}))
        self.assertEqual(runtime.wait_for_input('pvt', timeout=0.1), first)
        self.assertTrue(runtime.provide_input('pvt', {'index': 1}))
        self.assertEqual(runtime.wait_for_input('pvt', timeout=0.1), {'index': 1})

    def test_cancel_still_interrupts_pending_answer(self):
        from ningsi_studio.core.runtime import SessionCancelled
        runtime = SessionRuntime('a' * 32)
        runtime.provide_input('pvt', {'index': 0})
        runtime.cancel()
        with self.assertRaises(SessionCancelled):
            runtime.wait_for_input('pvt', timeout=0.1)

    def test_starting_session_reserves_capacity(self):
        manager = SessionManager(Settings(max_active_sessions=1))
        entered, release = threading.Event(), threading.Event()
        failures = []

        def slow_start(runtime):
            if runtime.uuid == 'a' * 32:
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('test synchronization timed out')

        def first_start():
            try:
                manager.start('a' * 32)
            except Exception as exc:
                failures.append(exc)

        with patch.object(SessionRuntime, 'start', slow_start):
            worker = threading.Thread(target=first_start)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(TooManyRequests):
                    manager.start('b' * 32)
                with self.assertRaises(TooManyRequests):
                    manager.ensure_capacity()
                with self.assertRaises(Conflict):
                    manager.start('a' * 32)
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])

    def test_failed_start_releases_registration_and_capacity(self):
        manager = SessionManager(Settings(max_active_sessions=1))
        with patch.object(SessionRuntime, 'start', side_effect=RuntimeError('source unavailable')):
            with self.assertRaises(RuntimeError):
                manager.start('a' * 32)
        self.assertIsNone(manager.get('a' * 32))
        manager.ensure_capacity()

    def test_active_session_cannot_be_started_twice(self):
        manager = SessionManager(Settings(max_active_sessions=2))
        with patch.object(SessionRuntime, 'start'), patch.object(
                SessionRuntime, 'alive', new_callable=PropertyMock,
                return_value=True):
            first = manager.start('a' * 32)
            with self.assertRaises(Conflict):
                manager.start('a' * 32)
            self.assertIs(manager.get('a' * 32), first)
