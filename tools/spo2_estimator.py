"""Experimental 30-second ratio-of-ratios estimator. No clinical calibration.

Independent Python signal processing, using the example quadratic from Maxim
MAXREFDES117 / SparkFun spo2_algorithm.cpp. This is NOT an official port or
validated Maxim algorithm. Quality limits below are engineering heuristics.
Sources, coefficients and limitations are documented in docs/SPO2.md.
"""
from __future__ import annotations

import math
from statistics import mean, median, pstdev
from ppg_signal import (quantile, detrend, smooth, local_ac, noise_sigma, spectral_quality, local_troughs, beat_peak, correlation, periodicity)

METHOD = "continuous_acdc_reference_curve_v3"
COEFFICIENTS = (-45.060, 30.354, 94.845)
CONVERSION_LIMITS = {"ratio_min": 0.3, "ratio_max": 1.84,
                     "demo_percent_min": 70.0, "demo_percent_max": 100.0}
CURVE_TURNING_POINT = -COEFFICIENTS[1] / (2 * COEFFICIENTS[0])
QUALITY_LIMITS = {
    "baseline_seconds": 1.5,
    "pulse_to_raw_noise_min": 3.0,
    "pulse_to_high_frequency_energy_min_db": 6.0,
    "slow_to_pulse_energy_max": 4.0,
    "correlation_min": 0.8,
    "periodicity_min": 0.4,
}


def reference_curve(ratio):
    """Example curve, not calibrated for this breakout, site or user."""
    a, b, c = COEFFICIENTS
    return a * ratio * ratio + b * ratio + c


def convert_ratio(ratio):
    """Experimental acceptance domain, independent of waveform quality."""
    result = {"valid": False, "spo2_percent": None, "reasons": [], "warnings": []}
    estimate = reference_curve(ratio)
    if not (math.isfinite(ratio) and
            CONVERSION_LIMITS["ratio_min"] <= ratio <= CONVERSION_LIMITS["ratio_max"] and
            CONVERSION_LIMITS["demo_percent_min"] <= estimate <= CONVERSION_LIMITS["demo_percent_max"]):
        result["reasons"].append("outside_demo_curve_range")
        return result
    if ratio <= CURVE_TURNING_POINT:
        result["warnings"].append("reference_curve_turning_region")
    result.update(valid=True, spo2_percent=estimate)
    return result


def prepare_signal(points):
    """Filter the continuous capture once; six-second slices share context."""
    groups = [points[i:i + 4] for i in range(0, len(points) - 3, 4)]
    rb = [mean(p[3] for p in g) for g in groups]
    ib = [mean(p[4] for p in g) for g in groups]
    times = [mean(p[2] for p in g) / 1e6 for g in groups]
    fs = (len(times) - 1) / (times[-1] - times[0])
    red, ir = smooth(rb), smooth(ib)
    # Removing a straight slope first prevents reflection from bending a ramp
    # at the two capture endpoints. Raw DC is retained separately.
    ra, ia = local_ac(detrend(red), fs, QUALITY_LIMITS["baseline_seconds"]), local_ac(detrend(ir), fs, QUALITY_LIMITS["baseline_seconds"])
    return {"fs": fs, "rows": list(zip(times, rb, ib, red, ir, ra, ia))}


def window_estimate(points, number, prepared=None):
    result = _window_estimate(points, number, prepared)
    result.setdefault("signal_valid", False)
    result["conversion_valid"] = result["valid"]
    result["signal_reasons"] = [] if result["signal_valid"] else list(result["reasons"])
    result["conversion_reasons"] = list(result["reasons"]) if result["signal_valid"] else []
    result.setdefault("warnings", [])
    return result


def _window_estimate(points, number, prepared):
    result = {"window": number, "valid": False, "reasons": []}
    reasons = result["reasons"]
    if len(points) < 400 or points[-1][2] - points[0][2] < 4800000:
        reasons.append("short_window")
        return result
    raw_red, raw_ir = [p[3] for p in points], [p[4] for p in points]
    if any(v <= 0 or v >= 262143 for v in raw_red + raw_ir):
        reasons.append("adc_clipping")
        return result
    prepared = prepared or prepare_signal(points)
    fs = prepared["fs"]
    _, red_blocks, ir_blocks, red, ir, red_ac, ir_ac = map(list, zip(*prepared["rows"]))
    red_dc, ir_dc = mean(red), mean(ir)
    result.update(red_dc=red_dc, ir_dc=ir_dc, processing_hz=fs)
    if min(red_dc, ir_dc) < 10000:
        reasons.append("weak_optical_contact")
        return result
    for raw, dc in ((raw_red, red_dc), (raw_ir, ir_dc)):
        if max(abs(b - a) for a, b in zip(raw, raw[1:])) / dc > 0.02:
            reasons.append("abrupt_motion")
            return result
        if (max(raw) - min(raw)) / dc > 0.20:
            reasons.append("large_signal_drift")
            return result
    red_pi, ir_pi = pstdev(red_ac) / red_dc, pstdev(ir_ac) / ir_dc
    red_noise, ir_noise = noise_sigma(raw_red), noise_sigma(raw_ir)
    red_strength, ir_strength = pstdev(red_ac) / red_noise, pstdev(ir_ac) / ir_noise
    red_band_db, red_slow_ratio = spectral_quality(red_blocks, fs)
    ir_band_db, ir_slow_ratio = spectral_quality(ir_blocks, fs)
    corr = correlation(red_ac, ir_ac)
    result.update(red_pi_rms_pct=red_pi * 100, ir_pi_rms_pct=ir_pi * 100,
                  red_ir_correlation=corr,
                  red_noise_sigma_counts=red_noise, ir_noise_sigma_counts=ir_noise,
                  red_pulse_to_raw_noise=red_strength, ir_pulse_to_raw_noise=ir_strength,
                  red_pulse_to_high_frequency_energy_db=red_band_db,
                  ir_pulse_to_high_frequency_energy_db=ir_band_db,
                  red_slow_to_pulse_energy=red_slow_ratio,
                  ir_slow_to_pulse_energy=ir_slow_ratio)
    # PI remains descriptive. A low PI is not alone evidence of a poor signal.
    if min(red_strength, ir_strength) < QUALITY_LIMITS["pulse_to_raw_noise_min"] or min(
            red_band_db, ir_band_db) < QUALITY_LIMITS["pulse_to_high_frequency_energy_min_db"]:
        reasons.append("pulse_below_noise_margin")
    if max(red_slow_ratio, ir_slow_ratio) > QUALITY_LIMITS["slow_to_pulse_energy_max"]:
        reasons.append("baseline_drift_dominates")
    if max(red_pi, ir_pi) > 0.05:
        reasons.append("excessive_variation")
    if corr < QUALITY_LIMITS["correlation_min"]:
        reasons.append("channels_disagree")
    lag, periodic_score = periodicity(ir_ac, fs)
    red_lag, red_periodic_score = periodicity(red_ac, fs)
    result.update(periodicity=periodic_score, red_periodicity=red_periodic_score)
    if lag is None or red_lag is None or min(periodic_score, red_periodic_score) < QUALITY_LIMITS["periodicity_min"]:
        reasons.append("no_consistent_pulse")
    elif abs(lag - red_lag) > 0.15 * lag:
        reasons.append("channel_periods_disagree")
    if reasons:
        return result
    troughs = local_troughs(ir_ac, lag, ir_noise)
    result["trough_count"] = len(troughs)
    ratios, intervals = [], []
    for a, b in zip(troughs, troughs[1:]):
        if not 0.65 * lag <= b - a <= 1.45 * lag:
            continue
        ir_amp, ir_peak = beat_peak(ir_ac, a, b)
        red_amp, red_peak = beat_peak(red_ac, a, b)
        if abs(red_peak - ir_peak) / fs > 0.12:
            continue
        if min(red_amp, ir_amp) <= 0:
            continue
        ratio = (red_amp / mean(red[a:b + 1])) / (ir_amp / mean(ir[a:b + 1]))
        if math.isfinite(ratio) and ratio > 0:
            ratios.append(ratio)
            intervals.append((b - a) / fs)
    result["accepted_beats"] = len(ratios)
    if len(ratios) < 3:
        reasons.append("too_few_consistent_beats")
        return result
    ratio = median(ratios)
    spread = (quantile(ratios, 0.75) - quantile(ratios, 0.25)) / ratio
    result.update(ratio=ratio, ratio_relative_iqr=spread,
                  pulse_bpm=60 / median(intervals))
    if spread > 0.25:
        reasons.append("unstable_beat_ratio")
        return result
    result["signal_valid"] = True
    conversion = convert_ratio(ratio)
    reasons.extend(conversion["reasons"])
    result.update(valid=conversion["valid"], spo2_percent=conversion["spo2_percent"],
                  warnings=conversion["warnings"])
    return result


def preview_quality(points):
    """Readiness uses waveform quality only, never the reference-curve domain."""
    if len(points) < 2 or (points[-1][2] - points[0][2]) / 1e6 < 5.8:
        return {"signal_valid": False, "signal_reasons": ["collecting_stability_samples"]}
    if any(p[0] != points[0][0] for p in points) or any(
            b[1] != a[1] + 1 or b[2] <= a[2] for a, b in zip(points, points[1:])):
        return {"signal_valid": False, "signal_reasons": ["discontinuous_samples"]}
    hz = (len(points) - 1) * 1e6 / (points[-1][2] - points[0][2])
    if not 80 <= hz <= 120:
        return {"signal_valid": False, "signal_reasons": ["requires_30_seconds_at_100_hz"]}
    return window_estimate(points, 0)


def estimate_spo2(points, delivery_ok=True, ambient_events=0):
    result = {"method": METHOD, "calibrated": False, "valid": False,
              "spo2_percent": None, "reasons": [], "windows": [],
              "valid_windows": 0, "total_windows": 5,
              "signal_valid_windows": 0, "conversion_valid_windows": 0,
              "signal_valid": False, "signal_reasons": [], "conversion_reasons": [],
              "warnings": [],
              "coefficients": list(COEFFICIENTS),
              "quality_limits": dict(QUALITY_LIMITS),
              "conversion_limits": dict(CONVERSION_LIMITS),
              "display_label": "Experimental estimate - not clinically validated"}
    reasons = result["reasons"]
    if not delivery_ok:
        reasons.append("delivery_check_failed")
        return result
    if ambient_events:
        reasons.append("ambient_light_overflow")
        return result
    if len(points) < 2:
        reasons.append("insufficient_samples")
        return result
    if any(p[0] != points[0][0] for p in points) or any(
            b[1] != a[1] + 1 or b[2] <= a[2] for a, b in zip(points, points[1:])):
        reasons.append("discontinuous_samples")
        return result
    span = (points[-1][2] - points[0][2]) / 1e6
    hz = (len(points) - 1) / span
    if span < 29 or not 80 <= hz <= 120:
        reasons.append("requires_30_seconds_at_100_hz")
        return result
    origin = points[0][2]
    prepared = prepare_signal(points)
    for i in range(5):
        window = [p for p in points if i * 6000000 <= p[2] - origin < (i + 1) * 6000000]
        context = {"fs": prepared["fs"], "rows": [row for row in prepared["rows"]
                   if i * 6 <= row[0] - origin / 1e6 < (i + 1) * 6]}
        result["windows"].append(window_estimate(window, i + 1, context))
    signal = [w for w in result["windows"] if w["signal_valid"]]
    valid = [w for w in result["windows"] if w["valid"]]
    result["valid_windows"] = len(valid)
    result.update(signal_valid_windows=len(signal), conversion_valid_windows=len(valid))
    if signal:
        signal_ratios = [w["ratio"] for w in signal]
        signal_iqr = (quantile(signal_ratios, .75) - quantile(signal_ratios, .25)) / median(signal_ratios)
        result.update(signal_ratio_median=median(signal_ratios), signal_ratio_relative_iqr=signal_iqr)
    if len(signal) < 3:
        result["signal_reasons"] = ["too_few_signal_windows"] + sorted(
            {r for w in result["windows"] for r in w["signal_reasons"]})
    elif signal_iqr > 0.15:
        result["signal_reasons"] = ["unstable_between_windows"]
    else:
        result["signal_valid"] = True
    if len(valid) < 3 and len(signal) >= 3:
        result["conversion_reasons"] = ["too_few_conversion_windows"] + sorted(
            {r for w in signal for r in w["conversion_reasons"]})
    # Preserve conversion refusals even if a signal issue also blocks output.
    reasons.extend(result["signal_reasons"] + result["conversion_reasons"])
    if reasons:
        return result
    ratios = [w["ratio"] for w in valid]
    estimates = [w["spo2_percent"] for w in valid]
    relative_iqr = (quantile(ratios, 0.75) - quantile(ratios, 0.25)) / median(ratios)
    spo2_iqr = quantile(estimates, 0.75) - quantile(estimates, 0.25)
    result.update(ratio_median=median(ratios), ratio_relative_iqr=relative_iqr,
                  window_estimate_iqr_percentage_points=spo2_iqr,
                  pulse_bpm=median(w["pulse_bpm"] for w in valid))
    if relative_iqr > 0.15 or spo2_iqr > 5:
        reasons.append("unstable_between_windows")
        result["conversion_reasons"] = list(reasons)
        return result
    result["warnings"] = sorted({r for w in valid for r in w["warnings"]})
    result.update(valid=True, spo2_percent=round(reference_curve(median(ratios)), 1))
    return result
