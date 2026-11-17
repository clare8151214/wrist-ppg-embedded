"""Synthetic quality checks, never a clinical accuracy validation."""
import math
from pathlib import Path
import random
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from spo2_estimator import METHOD, beat_peak, convert_ratio, estimate_spo2, local_troughs, reference_curve
from ppg_viewer import State


def synthetic(ratio=0.6, kind='pulse'):
    rng = random.Random(42)
    points = []
    for i in range(3001):
        t = i / 100
        pulse = math.sin(2 * math.pi * 1.25 * t)
        red = 100000 + ratio * 1000 * pulse + rng.gauss(0, 3)
        ir = 120000 + 1200 * pulse + rng.gauss(0, 3)
        if kind == 'flat':
            red, ir = 100000, 120000
        elif kind == 'no_contact':
            red, ir = 3000 + pulse * 30, 2000 + pulse * 30
        elif kind == 'noise':
            red, ir = 100000 + rng.gauss(0, 500), 120000 + rng.gauss(0, 600)
        elif kind == 'clipped':
            red = 262143
        elif kind == 'motion':
            red += 15000 * math.sin(2 * math.pi * 0.2 * t)
            ir += 20000 * math.sin(2 * math.pi * 0.2 * t)
        elif kind == 'opposite':
            red = 100000 - ratio * 1000 * pulse
        points.append((1, i, round(t * 1e6), round(red), round(ir), 100 + t))
    return points


def quality_fixture(kind='low_pulse', seed=71, frequency=1.25, ratio=0.6, amplitude=1.0):
    rng = random.Random(seed)
    points = []
    walk = 0
    for i in range(3001):
        t = i / 100
        # Includes a harmonic so this is not only a pure-sine regression.
        pulse = math.sin(2 * math.pi * frequency * t) + 0.15 * math.sin(4 * math.pi * frequency * t)
        red, ir = 100000 + amplitude * 100 * ratio * pulse, 120000 + amplitude * 120 * pulse
        if kind == 'curved_baseline':
            red += 50 * math.sin(2 * math.pi * 0.19 * t)
            ir += 70 * math.sin(2 * math.pi * 0.19 * t + 0.2)
        elif kind == 'noise_buried_pulse':
            red += rng.gauss(0, 80)
            ir += rng.gauss(0, 80)
        elif kind == 'shared_noise':
            noise = rng.gauss(0, 100)
            red, ir = 100000 + noise, 120000 + 2 * noise
        elif kind == 'shared_slow_motion':
            motion = 1000 * math.sin(2 * math.pi * 0.2 * t)
            red, ir = 100000 + motion, 120000 + 2 * motion
        elif kind == 'shared_random_walk':
            walk += rng.gauss(0, 6)
            red, ir = 100000 + walk, 120000 + 2 * walk
        elif kind == 'high_frequency_interference':
            red += 500 * math.sin(2 * math.pi * 8 * t)
            ir += 800 * math.sin(2 * math.pi * 8 * t)
        elif kind == 'near_quantization':
            red, ir = 100000 + 0.8 * pulse, 120000 + 1.2 * pulse
        elif kind == 'changing_ratio':
            current_ratio = (0.5, 0.6, 0.8, 1.0, 1.1)[min(4, int(t / 6))]
            red = 100000 + 100 * current_ratio * pulse
        elif kind == 'unstable_beats':
            red = 100000 + 100 * (0.7 + 0.3 * math.sin(2 * math.pi * 0.28 * t)) * pulse
        elif kind == 'only_two_good_windows' and t >= 12:
            red = 262143
        points.append((1, i, round(t * 1e6), round(red), round(ir), 100 + t))
    return points


class SpO2Tests(unittest.TestCase):
    def test_user_requested_ratio_domain_and_turning_region(self):
        self.assertFalse(convert_ratio(0.2999)['valid'])
        self.assertTrue(convert_ratio(0.3)['valid'])
        self.assertIn('reference_curve_turning_region', convert_ratio(0.3)['warnings'])
        self.assertEqual(convert_ratio(0.35)['warnings'], [])
        self.assertFalse(convert_ratio(1.6)['valid'])
        self.assertFalse(convert_ratio(float('nan'))['valid'])

    def test_good_waveform_is_distinct_from_conversion_refusal(self):
        result = estimate_spo2(synthetic(ratio=0.2))
        self.assertTrue(result['signal_valid'], result)
        self.assertEqual(result['signal_valid_windows'], 5)
        self.assertEqual(result['conversion_valid_windows'], 0)
        self.assertIn('outside_demo_curve_range', result['conversion_reasons'])
        self.assertEqual(result['signal_reasons'], [])
        self.assertIsNone(result['spo2_percent'])

    def test_low_ratio_is_experimental_with_warning(self):
        result = estimate_spo2(synthetic(ratio=0.31))
        self.assertTrue(result['valid'], result)
        self.assertFalse(result['calibrated'])
        self.assertIn('reference_curve_turning_region', result['warnings'])

    def test_continuous_baseline_preserves_pulses_on_a_ramp(self):
        points = [(*p[:3], round(p[3] + 100 * (p[5] - 100)),
                   round(p[4] + 120 * (p[5] - 100)), p[5]) for p in quality_fixture()]
        result = estimate_spo2(points)
        self.assertTrue(result['valid'], result)
        self.assertAlmostEqual(result['ratio_median'], 0.6, delta=0.025)

    def test_low_pi_pulse_preserves_ratio_across_rates(self):
        for frequency in (0.8, 1.0, 1.25, 1.8, 2.5):
            for ratio in (0.5, 0.7, 0.95):
                with self.subTest(frequency=frequency, ratio=ratio):
                    result = estimate_spo2(quality_fixture(frequency=frequency, ratio=ratio, amplitude=0.4))
                    self.assertTrue(result['valid'], result)
                    self.assertAlmostEqual(result['ratio_median'], ratio, delta=0.025)
                    self.assertEqual(result['method'], METHOD)
                    self.assertFalse(result['calibrated'])
                    self.assertLess(min(w['red_pi_rms_pct'] for w in result['windows']), 0.05)

    def test_curved_baseline_does_not_hide_local_pulses(self):
        result = estimate_spo2(quality_fixture('curved_baseline'))
        self.assertTrue(result['valid'], result)
        self.assertAlmostEqual(result['ratio_median'], 0.6, delta=0.04)
        self.assertGreaterEqual(result['valid_windows'], 4)

    def test_local_prominence_does_not_require_negative_troughs(self):
        values = [20 + math.sin(2 * math.pi * i / 25) for i in range(150)]
        self.assertGreaterEqual(len(local_troughs(values, 25, 0.01)), 5)

    def test_peak_is_selected_after_removing_endpoint_slope(self):
        values = [100 + 10 * i + (8 if i == 3 else 0) for i in range(9)]
        amplitude, peak = beat_peak(values, 0, 8)
        self.assertEqual(peak, 3)
        self.assertEqual(amplitude, 8)

    def test_noise_motion_and_unstable_ratios_still_refuse(self):
        for kind in ('noise_buried_pulse', 'shared_noise', 'shared_slow_motion',
                     'shared_random_walk', 'high_frequency_interference',
                     'near_quantization', 'changing_ratio', 'unstable_beats',
                     'only_two_good_windows'):
            with self.subTest(kind=kind):
                result = estimate_spo2(quality_fixture(kind))
                self.assertFalse(result['valid'], result)
                self.assertIsNone(result['spo2_percent'])
                self.assertTrue(result['reasons'])

    def test_correlated_random_noise_rejected_across_seeds(self):
        for seed in range(5):
            for kind in ('shared_noise', 'shared_random_walk'):
                with self.subTest(seed=seed, kind=kind):
                    result = estimate_spo2(quality_fixture(kind, seed))
                    self.assertFalse(result['valid'], result)
                    self.assertIsNone(result['spo2_percent'])

    def test_known_pulse_ratio_uses_reference_curve(self):
        result = estimate_spo2(synthetic())
        self.assertTrue(result['valid'], result)
        self.assertAlmostEqual(result['spo2_percent'], reference_curve(0.6), delta=0.5)
        self.assertFalse(result['calibrated'])
        self.assertGreaterEqual(result['valid_windows'], 3)

    def test_bad_signals_cannot_become_a_percentage(self):
        for kind in ('flat', 'no_contact', 'noise', 'clipped', 'motion', 'opposite'):
            with self.subTest(kind=kind):
                result = estimate_spo2(synthetic(kind=kind))
                self.assertFalse(result['valid'])
                self.assertIsNone(result['spo2_percent'])
                self.assertTrue(result['reasons'])

    def test_loss_interference_and_short_capture_rejected(self):
        points = synthetic()
        for data, kwargs in ((points, {'delivery_ok': False}),
                             (points, {'ambient_events': 1}),
                             (points[:1000], {}),
                             (points[:1000] + points[1001:], {})):
            result = estimate_spo2(data, **kwargs)
            self.assertFalse(result['valid'])
            self.assertIsNone(result['spo2_percent'])

    def test_out_of_range_curve_not_clamped_to_normal(self):
        result = estimate_spo2(synthetic(ratio=1.6))
        self.assertFalse(result['valid'])
        self.assertIsNone(result['spo2_percent'])
        self.assertIn('outside_demo_curve_range', result['reasons'])

    def test_result_uses_new_samples_after_button_press(self):
        state = State()
        state.connected = True
        state.feed('D,1,0,1,100000,120000', 99.99)
        state.feed('M,1,1,1,0,0,0,1,0', 99.99)
        state.begin_measurement('spo2', 100, site='finger')
        for i in range(1, 4001):
            t = i / 100
            pulse = math.sin(2 * math.pi * 1.25 * t)
            state.feed(f'D,1,{i},{round(t*1e6)+1},{round(100000+600*pulse)},{round(120000+1200*pulse)}', 100+t)
            if i % 100 == 0:
                state.feed(f'M,1,{i+1},{round(t*1e6)+1},0,0,0,1,0', 100+t)
                state.finish_due(100+t)
        self.assertEqual(state.result['received_pairs'], 3000)
        self.assertEqual(state.result['contact_site_reported'], 'finger')
        self.assertTrue(state.result['spo2']['valid'], state.result)
        self.assertEqual(state.result['stabilization']['duration_s'], 8)
        self.assertEqual(state.result['first_session_seq_read_us'][1], 801)

    def test_preparation_times_out_without_claiming_30_second_capture(self):
        state = State(); state.connected = True
        state.feed('D,1,0,1,100000,120000', 100)
        state.begin_measurement('spo2', 100, site='finger')
        for i in range(1, 3001):
            state.feed(f'D,1,{i},{i*10000+1},100000,120000', 100+i/100)
            if i % 100 == 0:
                state.feed(f'M,1,{i+1},{i*10000+1},0,0,0,1,0', 100+i/100)
                state.finish_due(100+i/100)
        self.assertEqual(state.result['measurement_status'], 'stability_timeout')
        self.assertFalse(state.result['recording_started'])
        self.assertEqual(state.result['received_pairs'], 0)
        self.assertIsNone(state.result['spo2']['spo2_percent'])
        self.assertIsNone(state.measurement)

    def test_stopping_preparation_cannot_start_later(self):
        state = State(); state.connected = True
        state.feed('D,1,0,1,100000,120000', 100)
        state.begin_measurement('spo2', 100, site='finger')
        self.assertEqual(state.snapshot(101)['measurement_phase'], 'stabilizing')
        self.assertIsNone(state.snapshot(101)['measurement_remaining'])
        state.cancel_measurement(101)
        self.assertEqual(state.result['measurement_status'], 'measurement_cancelled')
        state.finish_due(140)
        self.assertIsNone(state.measurement)
        self.assertIsNone(state.result['spo2']['spo2_percent'])

    def test_preparation_does_not_gate_on_conversion_domain(self):
        state = State(); state.connected = True
        state.feed('D,1,0,1,100000,120000', 100)
        state.begin_measurement('spo2', 100, site='finger')
        for p in synthetic(ratio=0.2)[1:801]:
            state.feed(f'D,1,{p[1]},{p[2]+1},{p[3]},{p[4]}', p[5])
            if p[1] % 100 == 0:
                state.feed(f'M,1,{p[1]+1},{p[2]+1},0,0,0,1,0', p[5])
                state.finish_due(p[5])
        self.assertEqual(state.measurement['phase'], 'recording')
        self.assertTrue(state.measurement['last_quality']['signal_valid'])
        self.assertFalse(state.measurement['last_quality']['conversion_valid'])

    def test_stopping_recording_discards_percentage(self):
        state = State(); state.connected = True
        state.feed('D,1,0,1,100000,120000', 100)
        state.begin_measurement('spo2', 100, site='finger')
        for p in synthetic()[1:1001]:
            state.feed(f'D,1,{p[1]},{p[2]+1},{p[3]},{p[4]}', p[5])
            if p[1] % 100 == 0:
                state.feed(f'M,1,{p[1]+1},{p[2]+1},0,0,0,1,0', p[5])
                state.finish_due(p[5])
        self.assertEqual(state.measurement['phase'], 'recording')
        state.cancel_measurement(110)
        self.assertTrue(state.result['recording_started'])
        self.assertGreater(state.result['received_pairs'], 0)
        self.assertFalse(state.result['rate_and_delivery_pass'])
        self.assertIsNone(state.result['spo2']['spo2_percent'])


if __name__ == '__main__':
    unittest.main()
