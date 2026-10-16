"""Additive API metadata and stable SSE event timestamps for the frontend bridge."""
import time
import unittest
from ningsi import config
from ningsi_studio.http.sse import EventBus
from .helpers import StudioTestCase

class NeuroFocusContractTests(StudioTestCase):
    def test_declared_focus_range_and_algorithm_match_engine(self):
        response=self.get('/api/config')
        data=response.json()
        if 'data' in data:
            data=data['data']
        self.assertEqual(data['focus_score']['range'],[config.SCORE_MIN,config.SCORE_MAX])
        self.assertEqual(data['focus_score']['algorithm_id'],config.INDICATOR_SPEC)

class EmissionTimestampTests(unittest.TestCase):
    def test_replay_keeps_original_emission_timestamp(self):
        from ningsi_studio.settings import Settings
        bus=EventBus('a'*32)
        before=int(time.time()*1000)
        event=bus.publish('window',{'scores':{'focus':.5}})
        self.assertGreaterEqual(event['emitted_at_ms'],before)
        subscriber,_=bus.subscribe(last_event_id=0)
        replay=subscriber.events.get_nowait()
        self.assertEqual(replay['emitted_at_ms'],event['emitted_at_ms'])
        bus.unsubscribe(subscriber)
