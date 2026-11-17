"""Independent IR pulse-rate estimator; no Red or SpO2 conversion dependency.

Engineering quality checks, not ECG validation. Uses device FIFO read timestamps,
so displayed resolution must not be interpreted as clinical accuracy.
"""
from __future__ import annotations

import math
from statistics import mean, median, pstdev

from ppg_signal import detrend, local_ac, local_troughs, noise_sigma, quantile, smooth, spectral_quality

METHOD = 'ir_interval_bpm_v1'
WINDOW_SECONDS = 10
UPDATE_SECONDS = 1
STALE_SECONDS = 2
LIMITS = dict(bpm_min=40, bpm_max=180, periodicity_min=0.4,
              pulse_to_noise_min=3.0, pulse_to_high_frequency_energy_min_db=6.0,
              slow_to_pulse_energy_max=4.0, interval_relative_iqr_max=0.20,
              minimum_intervals=4, interval_acceptance_min=0.8, coverage_min=0.6)


def invalid_bpm(reason):
    return dict(method=METHOD, source='ir', valid=False, bpm=None, reasons=[reason],
                clinically_validated=False, window_seconds=WINDOW_SECONDS,
                quality_limits=dict(LIMITS))


def _subsample_offset(a, b, c):
    denominator = a - 2 * b + c
    return max(-0.5, min(0.5, 0.5 * (a - c) / denominator)) if denominator else 0.0


def _period(values, fs):
    # Look beyond the reporting range (30..240 bpm) to detect rather than
    # silently halve a pulse above 180 bpm. Prefer the first strong recurrence.
    lo, hi = max(2, math.floor(fs / 4)), min(len(values) // 2, math.ceil(fs * 2))
    energy = sum(x * x for x in values)
    if not energy:
        return None, 0.0
    scores = {lag: sum(values[i] * values[i + lag] for i in range(len(values) - lag)) / energy
              for lag in range(lo - 1, hi + 2)}
    peaks = [lag for lag in range(lo, hi + 1)
             if scores[lag] > scores[lag - 1] and scores[lag] >= scores[lag + 1]]
    if not peaks:
        return None, 0.0
    best = max(scores[lag] for lag in peaks)
    strong = [lag for lag in peaks if scores[lag] >= max(LIMITS['periodicity_min'], 0.85 * best)]
    if not strong:
        return None, best
    lag = min(strong)
    fractional = lag + _subsample_offset(scores[lag - 1], scores[lag], scores[lag + 1])
    return fractional, scores[lag]


def estimate_bpm(points, delivery_ok=True, ambient_events=0):
    """Estimate the most recent 10 s. Return None BPM whenever checks fail."""
    result = invalid_bpm('insufficient_bpm_samples')
    if not delivery_ok:
        result['reasons'] = ['delivery_check_failed']
        return result
    if ambient_events:
        result['reasons'] = ['ambient_light_overflow']
        return result
    if len(points) < 2:
        return result
    points = [p for p in points if p[2] >= points[-1][2] - WINDOW_SECONDS * 1000000]
    if len(points) < 2:
        return result
    if any(p[0] != points[0][0] for p in points) or any(
            b[1] != a[1] + 1 or b[2] <= a[2] for a, b in zip(points, points[1:])):
        result['reasons'] = ['discontinuous_samples']
        return result
    span = (points[-1][2] - points[0][2]) / 1e6
    hz = (len(points) - 1) / span
    result.update(sample_span_s=span, sample_count=len(points), device_hz=hz,
                  first_session_seq_read_us=list(points[0][:3]),
                  last_session_seq_read_us=list(points[-1][:3]))
    if span < 9.5 or not 80 <= hz <= 120:
        return result
    reasons = []
    result['reasons'] = reasons
    raw = [p[4] for p in points]  # Red is intentionally not a gate for BPM.
    if any(x <= 0 or x >= 262143 for x in raw):
        reasons.append('adc_clipping')
        return result
    dc = mean(raw)
    result['ir_dc'] = dc
    if dc < 10000:
        reasons.append('weak_optical_contact')
        return result
    if max(abs(b - a) for a, b in zip(raw, raw[1:])) / dc > 0.02:
        reasons.append('abrupt_motion')
        return result
    if (max(raw) - min(raw)) / dc > 0.20:
        reasons.append('large_signal_drift')
        return result
    groups = [points[i:i + 4] for i in range(0, len(points) - 3, 4)]
    values = [mean(p[4] for p in g) for g in groups]
    times = [mean(p[2] for p in g) / 1e6 for g in groups]
    fs = (len(times) - 1) / (times[-1] - times[0])
    ac = local_ac(detrend(smooth(values)), fs, baseline_seconds=1.5)
    noise = noise_sigma(raw)
    strength = pstdev(ac) / noise
    band_db, slow_ratio = spectral_quality(values, fs)
    lag, periodicity = _period(ac, fs)
    result.update(processing_hz=fs, ir_noise_sigma_counts=noise, ir_pulse_to_raw_noise=strength,
                  ir_pulse_to_high_frequency_energy_db=band_db,
                  ir_slow_to_pulse_energy=slow_ratio, periodicity=periodicity)
    if strength < LIMITS['pulse_to_noise_min'] or band_db < LIMITS['pulse_to_high_frequency_energy_min_db']:
        reasons.append('pulse_below_noise_margin')
    if slow_ratio > LIMITS['slow_to_pulse_energy_max']:
        reasons.append('baseline_drift_dominates')
    if pstdev(ac) / dc > .05:
        reasons.append('excessive_variation')
    if lag is None or periodicity < LIMITS['periodicity_min']:
        reasons.append('no_consistent_pulse')
    if reasons:
        return result
    troughs = local_troughs(ac, lag, noise)
    # Subsample trough timing reduces 25 Hz quantization of beat intervals.
    beat_times = [times[i] + _subsample_offset(ac[i - 1], ac[i], ac[i + 1]) / fs for i in troughs]
    all_intervals = [b - a for a, b in zip(beat_times, beat_times[1:])]
    intervals = [dt for dt in all_intervals if .65 * lag / fs <= dt <= 1.45 * lag / fs]
    result.update(detected_intervals=len(all_intervals), accepted_intervals=len(intervals),
                  covered_seconds=sum(intervals))
    if len(intervals) < LIMITS['minimum_intervals']:
        reasons.append('too_few_bpm_intervals')
        return result
    if len(intervals) / len(all_intervals) < LIMITS['interval_acceptance_min'] or sum(intervals) / span < LIMITS['coverage_min']:
        reasons.append('incomplete_pulse_coverage')
        return result
    typical = median(intervals)
    relative_iqr = (quantile(intervals, .75) - quantile(intervals, .25)) / typical
    result.update(interval_median_s=typical, interval_relative_iqr=relative_iqr)
    if relative_iqr > LIMITS['interval_relative_iqr_max']:
        reasons.append('unstable_bpm_intervals')
        return result
    # Three-interval averages reduce repeating sample-phase timing bias while
    # the unaveraged interval IQR above still detects unstable beats.
    period = median(mean(intervals[i:i + 3]) for i in range(len(intervals) - 2))
    result['period_seconds'] = period
    if abs(period - lag / fs) / (lag / fs) > .15:
        reasons.append('bpm_period_disagreement')
        return result
    bpm = round(60 / period, 1)
    if not LIMITS['bpm_min'] <= bpm <= LIMITS['bpm_max']:
        reasons.append('outside_bpm_range')
        return result
    result.update(valid=True, bpm=bpm)
    return result


def summarize_bpm(points, delivery_ok=True, ambient_events=0):
    """Independent summary of three non-overlapping 10 s capture windows."""
    result = invalid_bpm('insufficient_bpm_capture')
    result.update(scope='capture_summary', windows=[], valid_windows=0, total_windows=3)
    if not delivery_ok:
        result['reasons'] = ['delivery_check_failed']
        return result
    if len(points) < 2 or points[-1][2] - points[0][2] < 29000000:
        return result
    if any(p[0] != points[0][0] for p in points) or any(
            b[1] != a[1] + 1 or b[2] <= a[2] for a, b in zip(points, points[1:])):
        result['reasons'] = ['discontinuous_samples']
        return result
    origin = points[0][2]
    for i in range(3):
        data = [p for p in points if i * 10000000 <= p[2] - origin < (i + 1) * 10000000]
        item = estimate_bpm(data, delivery_ok=delivery_ok, ambient_events=ambient_events)
        item['window'] = i + 1
        result['windows'].append(item)
    values = [w['bpm'] for w in result['windows'] if w['valid']]
    result['valid_windows'] = len(values)
    if len(values) < 2:
        result['reasons'] = ['too_few_bpm_windows'] + sorted({r for w in result['windows'] for r in w['reasons']})
        return result
    spread = (quantile(values, .75) - quantile(values, .25)) / median(values)
    result['window_bpm_relative_iqr'] = spread
    if spread > .20:
        result['reasons'] = ['unstable_bpm_intervals']
        return result
    result.update(valid=True, bpm=round(median(values), 1), reasons=[])
    return result
