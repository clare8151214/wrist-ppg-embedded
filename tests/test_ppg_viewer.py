"""Protocol/measurement checks. Synthetic frames here are never lab evidence."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from ppg_viewer import State, sample_rates


class MeasurementTests(unittest.TestCase):
    def make_run(self, hz=100, gap=False, overflow=0):
        state = State()
        state.connected = True
        state.feed('D,1,0,10000,12000,18000', 100.0)
        state.feed('M,1,1,10000,0,0,0,1,0', 100.0)
        state.begin_measurement('bench', 100.0, seconds=30)
        for i in range(1, 30 * hz + 1):
            seq = i + (1 if gap and i > hz else 0)
            state.feed(f'D,1,{seq},{10000 + round(i*1e6/hz)},12000,18000', 100+i/hz)
        state.feed(f'M,1,{30*hz+1},30010000,0,{overflow},{overflow},1,0', 130.0)
        state.finish_due(130.0)
        return state

    def test_live_100_hz_and_metrics_frame(self):
        state = self.make_run()
        self.assertEqual(state.invalid_frames, 0)
        self.assertTrue(state.result['rate_and_delivery_pass'])
        self.assertAlmostEqual(state.result['device_delivery_hz_per_channel'], 100)
        self.assertFalse(state.result['wrist_contact_reported'])

    def test_nominal_setting_does_not_pass_slow_delivery(self):
        state = self.make_run(hz=25)
        self.assertFalse(state.result['rate_and_delivery_pass'])
        self.assertAlmostEqual(state.result['host_delivery_hz_per_channel'], 25)

    def test_gap_and_fifo_loss_fail_acceptance(self):
        self.assertFalse(self.make_run(gap=True).result['rate_and_delivery_pass'])
        self.assertFalse(self.make_run(overflow=1).result['rate_and_delivery_pass'])

    def test_stale_metrics_and_restarts_fail(self):
        state = State()
        state.connected = True
        state.feed('D,1,0,10000,12,18', 100)
        state.begin_measurement('bench', 100, seconds=30)
        state.feed('D,2,0,10000,12,18', 129)
        state.finish_due(130)
        self.assertFalse(state.result['rate_and_delivery_pass'])
        self.assertFalse(state.result['checks']['no_restart'])

    def test_bad_samples_not_logged(self):
        state = State()
        for line in ['D,1,0,10000,999999,1', 'D,1,0', 'M,1,nan']:
            self.assertIsNone(state.feed(line, 100))
        self.assertEqual(state.invalid_frames, 3)
        self.assertEqual(state.received, 0)

    def test_single_sample_cannot_claim_rate(self):
        self.assertEqual(sample_rates([(1, 0, 10000, 12, 18, 100)]), (None, None))


if __name__ == '__main__':
    unittest.main()
