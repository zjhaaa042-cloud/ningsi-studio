"""HTTP HEAD and ephemeral port behavior using a real local server."""
import http.client
from contextlib import closing
import queue
import threading
import unittest

from ningsi_studio.http.router import Response, Router
from ningsi_studio.http.server import AppServer, sse_response
from ningsi_studio.settings import Settings


class TransportRegressionTests(unittest.TestCase):
    def setUp(self):
        self.router = Router()
        self.closed = threading.Event()
        self.router.get('/api/data')(lambda request: Response.json({'hello': 'world'}))
        self.router.get('/api/watch')(lambda request: sse_response(
            0, queue.Queue(), heartbeat_sec=0.1, keep_alive=lambda: False,
            on_close=self.closed.set))
        self.server = AppServer(self.router, Settings(port=0))
        self.reported_port = self.server.bind()
        self.actual_port = self.server.httpd.server_address[1]
        self.server.start_background()

    def tearDown(self):
        self.server.stop()

    def request(self, method, path):
        with closing(http.client.HTTPConnection('127.0.0.1', self.actual_port, timeout=2)) as conn:
            conn.request(method, path)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()

    def test_port_zero_reports_actual_bound_port(self):
        self.assertEqual(self.reported_port, self.actual_port)
        self.assertIn(':' + str(self.actual_port) + '/', self.server.url)

    def test_head_preserves_get_content_length(self):
        _, get_headers, body = self.request('GET', '/api/data')
        status, head_headers, head_body = self.request('HEAD', '/api/data')
        self.assertEqual(status, 200)
        self.assertEqual(head_headers['Content-Length'], get_headers['Content-Length'])
        self.assertGreater(len(body), 0)
        self.assertEqual(head_body, b'')

    def test_head_stream_closes_subscription_without_streaming(self):
        status, headers, body = self.request('HEAD', '/api/watch')
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('Content-Length'), '0')
        self.assertEqual(body, b'')
        self.assertTrue(self.closed.wait(1))
