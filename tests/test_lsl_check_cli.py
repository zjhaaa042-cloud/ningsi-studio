"""Device diagnostics must test the requested stream and report failure."""
import io
from argparse import Namespace
from contextlib import redirect_stdout
import unittest
from unittest.mock import patch

from ningsi_studio.__main__ import _lsl_check


class LslCheckCliTests(unittest.TestCase):
    def test_selected_device_is_pinned_and_timeout_fails(self):
        args = Namespace(device='lsl:Requested EEG', timeout=0.1, seconds=0)
        with patch('ningsi_studio.acquisition.lsl.probe_streams', return_value=[{
                'name': 'Other EEG', 'supported': True}]), patch(
                'ningsi_studio.acquisition.lsl.LiveStreamManager') as factory:
            factory.return_value.wait_for.return_value = False
            with redirect_stdout(io.StringIO()):
                result = _lsl_check(args)
            self.assertEqual(factory.call_args.kwargs.get('wanted_name'), 'Requested EEG')
            self.assertEqual(result, 1)
            factory.return_value.stop.assert_called_once()

    def test_requested_device_with_no_discovered_streams_fails(self):
        args = Namespace(device='lsl:Requested EEG', timeout=0.1, seconds=0)
        with patch('ningsi_studio.acquisition.lsl.probe_streams', return_value=[]):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(_lsl_check(args), 1)
