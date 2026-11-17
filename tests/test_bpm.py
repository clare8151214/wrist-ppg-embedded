"""Pulse-rate software regressions, not ECG or clinical validation."""
import math
from pathlib import Path
import random
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from bpm_estimator import estimate_bpm, summarize_bpm
from spo2_estimator import estimate_spo2
from ppg_viewer import State


def signal(bpm=75, seconds=30, kind='pulse', seed=42):
    rng = random.Random(seed)
    points = []
    walk = 0
    for i in range(seconds * 100 + 1):
        t = i / 100
        wave = math.sin(2 * math.pi * bpm / 60 * t) + .15 * math.sin(4 * math.pi * bpm / 60 * t)
        red, ir = 100000 + 600 * wave, 120000 + 1200 * wave + rng.gauss(0, 3)
        if kind == 'red_flat': red = 100000
        elif kind == 'red_bad': red = rng.choice((0, 262143, 100000))
        elif kind == 'low_ratio': red = 100000 + 100 * wave
        elif kind == 'flat': ir = 120000
        elif kind == 'no_contact': ir = 2000 + 30 * wave
        elif kind == 'clipped': ir = 262143
        elif kind == 'noise': ir = 120000 + rng.gauss(0, 500)
        elif kind == 'walk':
            walk += rng.gauss(0, 10)
            ir = 120000 + walk
        elif kind == 'motion': ir += 15000 * math.sin(2 * math.pi * .2 * t)
        elif kind == 'ir_flat_tail' and t >= seconds - 4: ir = 120000
        elif kind == 'varying_rate':
            phase = (t // 4) * 7 + min(t % 4, 2) + max(0, t % 4 - 2) * 2.5
            ir = 120000 + 1200 * math.sin(2 * math.pi * phase)
        points.append((1, i, round(t * 1e6), round(red), round(ir), 100 + t))
    return points


class BpmTests(unittest.TestCase):
    def test_known_rates(self):
        for rate in (40, 45, 60, 75, 90, 120, 150, 180):
            with self.subTest(rate=rate):
                result = estimate_bpm(signal(rate))
                self.assertTrue(result['valid'], result)
                self.assertAlmostEqual(result['bpm'], rate, delta=1.5)

    def test_independent_of_red_and_spo2_domain(self):
        for kind in ('red_flat', 'red_bad', 'low_ratio'):
            with self.subTest(kind=kind):
                points = signal(kind=kind)
                self.assertFalse(estimate_spo2(points)['valid'])
                result = estimate_bpm(points)
                self.assertTrue(result['valid'], result)
                self.assertAlmostEqual(result['bpm'], 75, delta=1)
                self.assertEqual(result['source'], 'ir')

    def test_bad_ir_has_no_number(self):
        for kind in ('flat', 'no_contact', 'clipped', 'noise', 'walk', 'motion', 'ir_flat_tail', 'varying_rate'):
            with self.subTest(kind=kind):
                result = estimate_bpm(signal(kind=kind))
                self.assertFalse(result['valid'], result)
                self.assertIsNone(result['bpm'])
                self.assertTrue(result['reasons'])

    def test_noise_across_seeds(self):
        for seed in range(5):
            for kind in ('noise', 'walk'):
                self.assertFalse(estimate_bpm(signal(kind=kind, seed=seed))['valid'])

    def test_outside_rate_not_reported_at_half_rate(self):
        for rate in (30, 200, 220):
            with self.subTest(rate=rate):
                self.assertFalse(estimate_bpm(signal(rate))['valid'])

    def test_delivery_short_gap_and_reset(self):
        points = signal()
        for data, kwargs in ((points, {'delivery_ok': False}), (points, {'ambient_events': 1}),
                             (points[:500], {}), (points[:2500]+points[2501:], {}),
                             (points[:-1]+[(2, 0, points[-1][2], *points[-1][3:])], {})):
            result = estimate_bpm(data, **kwargs)
            self.assertFalse(result['valid'], result)
            self.assertIsNone(result['bpm'])

    def test_capture_summary_independent_and_requires_enough_windows(self):
        points = signal(kind='red_flat')
        self.assertFalse(estimate_spo2(points)['valid'])
        result = summarize_bpm(points)
        self.assertTrue(result['valid'], result)
        self.assertEqual(result['valid_windows'], 3)
        for data, args in ((points[:900], {}), (points, {'delivery_ok': False}),
                           (signal(kind='flat'), {}), (points[:1000] + points[1001:], {})):
            self.assertIsNone(summarize_bpm(data, **args)['bpm'])


class LiveBpmTests(unittest.TestCase):
    def make_state(self, prepare_spo2=False):
        state = State(); state.connected = True
        for p in signal(kind='red_flat'):
            state.feed(f'D,1,{p[1]},{p[2]+1},{p[3]},{p[4]}', p[5])
            if p[1] % 100 == 0:
                state.feed(f'M,1,{p[1]+1},{p[2]+1},0,0,0,1,0', p[5])
                if p[1] == 0 and prepare_spo2:
                    state.begin_measurement('spo2', p[5], site='finger')
                state.finish_due(p[5])
                state.update_bpm(p[5])
        self.assertTrue(state.bpm_snapshot(130)['valid'], state.bpm)
        return state

    def test_live_bpm_survives_spo2_preparation_failure(self):
        state = self.make_state(prepare_spo2=True)
        self.assertEqual(state.result['measurement_status'], 'stability_timeout')
        self.assertFalse(state.result['spo2']['valid'])
        self.assertAlmostEqual(state.snapshot(130)['bpm']['bpm'], 75, delta=1)

    def test_stop_and_restart_clear_value_and_history(self):
        state = self.make_state()
        state.set_streaming(False, 130)
        self.assertIsNone(state.bpm_snapshot(130)['bpm'])
        self.assertIsNone(state.update_bpm(130)['bpm'])
        state.set_streaming(True, 130)
        self.assertEqual(len(state.points), 0)
        self.assertIsNone(state.update_bpm(130)['bpm'])

    def test_stale_stream_or_analysis_cannot_keep_number(self):
        state = self.make_state()
        self.assertIsNone(state.bpm_snapshot(131.1)['bpm'])
        state.feed('D,1,3001,30010001,100000,120000', 132.5)
        state.feed('M,1,3002,30010001,0,0,0,1,0', 132.5)
        self.assertEqual(state.bpm_snapshot(132.5)['reasons'], ['stale_bpm_result'])
        self.assertIsNone(state.bpm_snapshot(132.5)['bpm'])

    def test_gap_reset_clipping_and_ambient_invalidate_immediately(self):
        cases = [('D,1,3002,30020001,100000,120000', 'discontinuous_samples'),
                 ('D,2,0,1,100000,120000', 'insufficient_bpm_samples'),
                 ('D,1,3001,30010001,100000,262143', 'adc_clipping'),
                 ('M,1,3001,30000001,0,0,0,1,1', 'recent_stream_fault')]
        for line, reason in cases:
            with self.subTest(reason=reason):
                state = self.make_state()
                state.feed(line, 130.01)
                self.assertEqual(state.bpm_snapshot(130.01)['reasons'], [reason])
                self.assertIsNone(state.bpm_snapshot(130.01)['bpm'])

    def test_check_frequency_is_limited(self):
        state = self.make_state()
        self.assertIsNone(state.update_bpm(130.2))
        self.assertIsNotNone(state.update_bpm(131.1))


if __name__ == '__main__':
    unittest.main()
