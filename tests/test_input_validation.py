"""Regression tests for malformed numeric and identifier inputs."""
import unittest

from ningsi_studio.api import schemas
from ningsi_studio.http.router import Request, ValidationError
from tests.helpers import StudioTestCase


class InputValidationTests(unittest.TestCase):
    def test_float_rejects_nonfinite_and_boolean(self):
        for value in (float('nan'), float('inf'), -float('inf'), 'NaN', 'Infinity', True):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                schemas.clamp_float(value, 1.0, low=0.01, high=10, name='speed')

    def test_integer_rejects_fraction_boolean_and_infinity(self):
        for value in (1.5, True, float('inf'), float('nan')):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                schemas.clamp_int(value, 1, low=1, high=8, name='channels')

    def test_identifiers_reject_wrong_types_and_extra_prefix(self):
        for value in (12, True, ['p01'], {'id': 'p01'}, 'sub-sub-p01'):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                schemas.normalize_public_id(value)

    def test_uuid_requires_complete_string(self):
        for value in (12, ['a' * 32], 'a' * 32 + '\n'):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                schemas.ensure_uuid(value)

    def test_query_float_rejects_nonfinite(self):
        for value in ('NaN', 'inf', '-inf'):
            request = Request('GET', '/', {'probe': value}, {})
            with self.subTest(value=value), self.assertRaises(ValidationError):
                request.query_float('probe', 1.0)

    def test_valid_values_and_defaults(self):
        self.assertEqual(schemas.normalize_public_id(' sub-p01 '), 'p01')
        self.assertEqual(schemas.ensure_uuid('a' * 32), 'a' * 32)
        self.assertEqual(schemas.clamp_int('2', 1, low=1, high=8, name='channels'), 2)
        self.assertEqual(schemas.clamp_int(2.0, 1, low=1, high=8, name='channels'), 2)
        self.assertEqual(schemas.clamp_float(None, 1.0, low=0, high=2, name='speed'), 1.0)


class InputValidationHttpTests(StudioTestCase):
    port_base = 19090

    def test_invalid_session_inputs_return_422_without_creating_sessions(self):
        for overrides in ({'participant': 12}, {'participant': 'sub-sub-p01'},
                          {'time_scale': 'NaN'}, {'srate': 'Infinity'},
                          {'channels': 1.5}, {'channels': True}):
            with self.subTest(overrides=overrides):
                response = self.post('/api/sessions', {
                    'participant': 'p01', 'device': 'sim', **overrides,
                })
                self.assertEqual(response.status, 422, response.text())
                self.assertEqual(response.error_code(), 'validation_failed')
        overview = self.get('/api/overview').json()
        self.assertEqual(overview['sessions'], 0)

    def test_nonfinite_device_probe_returns_422(self):
        for value in ('NaN', 'inf', '-inf'):
            with self.subTest(value=value):
                self.assertEqual(self.get('/api/devices?probe=' + value).status, 422)
