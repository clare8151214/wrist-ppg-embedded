"""Pure PPG signal helpers, shared without any SpO2 conversion rules."""
import math
from statistics import mean, median, pstdev


def quantile(values, q):
    values = sorted(values)
    index = (len(values) - 1) * q
    lo, hi = math.floor(index), math.ceil(index)
    return values[lo] + (values[hi] - values[lo]) * (index - lo)


def detrend(values):
    center = (len(values) - 1) / 2
    average = mean(values)
    slope = sum((i - center) * (x - average) for i, x in enumerate(values)) / sum(
        (i - center) ** 2 for i in range(len(values)))
    return [x - average - slope * (i - center) for i, x in enumerate(values)]


def smooth(values):
    # Symmetric five-point moving average after four-sample block averaging.
    return [mean(values[max(0, i - 2):min(len(values), i + 3)])
            for i in range(len(values))]


def local_ac(values, fs, baseline_seconds=1.5):
    """Same symmetric, reflected 1.5 s baseline removal on both channels.

    This finite window filter is an engineering choice, not a clinical filter.
    No forward phase lag is introduced between Red and IR.
    """
    radius = max(1, round(baseline_seconds * fs / 2))
    padded = values[1:radius + 1][::-1] + values + values[-radius - 1:-1][::-1]
    cumulative = [0.0]
    for value in padded:
        cumulative.append(cumulative[-1] + value)
    width = 2 * radius + 1
    return [value - (cumulative[i + width] - cumulative[i]) / width
            for i, value in enumerate(values)]


def noise_sigma(raw):
    """Robust high-frequency noise proxy from raw second differences.

    sqrt(6) gives the white-noise-equivalent scale. This does not measure all
    motion/physiological noise; coherence, spectra and stability are also used.
    A 0.5 count floor prevents quantized flat/near-flat data claiming infinite SNR.
    """
    differences = [c - 2 * b + a for a, b, c in zip(raw, raw[1:], raw[2:])]
    center = median(differences)
    return max(0.5, median(abs(x - center) for x in differences) / (0.67449 * math.sqrt(6)))


def spectral_quality(values, fs):
    """Hann DFT energies BEFORE smoothing/baseline removal.

    Prevent smoothing from concealing high-frequency noise or slow movement.
    Ratios compare band energy, not calibrated physiological SNR.
    """
    values = detrend(values)
    n = len(values)
    tapered = [v * (0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)))
               for i, v in enumerate(values)]
    slow = pulse = high = 0.0
    for k in range(1, min(n // 2, math.floor(10 * n / fs)) + 1):
        frequency = k * fs / n
        if not (frequency < 0.5 or 0.65 <= frequency <= 3.5 or 5 <= frequency <= 10):
            continue
        angle = 2 * math.pi * k / n
        real = sum(v * math.cos(angle * i) for i, v in enumerate(tapered))
        imag = sum(v * math.sin(angle * i) for i, v in enumerate(tapered))
        energy = real * real + imag * imag
        if frequency < 0.5:
            slow += energy
        elif frequency <= 3.5:
            pulse += energy
        else:
            high += energy
    # Finite floors permit strict JSON even for flat inputs.
    return (10 * math.log10(max(pulse, 1e-12) / max(high, 1e-12)),
            slow / max(pulse, 1e-12))


def local_troughs(values, lag, noise):
    """Select by LOCAL prominence rather than an absolute zero/height cutoff."""
    radius = max(2, round(0.55 * lag))
    minimum = max(3 * noise, 0.4 * pstdev(values))
    candidates = []
    for i in range(1, len(values) - 1):
        if values[i] < values[i - 1] and values[i] <= values[i + 1]:
            left = max(values[max(0, i - radius):i])
            right = max(values[i + 1:min(len(values), i + radius + 1)])
            prominence = min(left, right) - values[i]
            if prominence >= minimum:
                candidates.append((prominence, i))
    troughs = []
    for _, i in sorted(candidates, reverse=True):
        if all(abs(i - j) >= 0.6 * lag for j in troughs):
            troughs.append(i)
    return sorted(troughs)


def beat_peak(values, a, b):
    """Remove the endpoint baseline BEFORE locating the pulse peak."""
    residual = [(values[i] - values[a] - (values[b] - values[a]) * (i - a) / (b - a), i)
                for i in range(a + 1, b)]
    return max(residual)


def correlation(a, b):
    am, bm = mean(a), mean(b)
    numerator = sum((x - am) * (y - bm) for x, y in zip(a, b))
    denominator = math.sqrt(sum((x - am) ** 2 for x in a) *
                            sum((y - bm) ** 2 for y in b))
    return numerator / denominator if denominator else 0.0


def periodicity(values, fs):
    # Seek a local autocorrelation maximum corresponding to 40..180 bpm.
    lo, hi = max(2, round(fs / 3)), min(len(values) // 2, round(fs * 1.5))
    energy = sum(x * x for x in values)
    scores = {lag: sum(values[i] * values[i + lag]
                       for i in range(len(values) - lag)) / energy
              for lag in range(lo - 1, hi + 2)} if energy else {}
    candidates = [lag for lag in range(lo, hi + 1)
                  if scores.get(lag, 0) > scores.get(lag - 1, 0)
                  and scores.get(lag, 0) >= scores.get(lag + 1, 0)]
    if not candidates:
        return None, 0.0
    lag = max(candidates, key=lambda x: scores[x])
    return lag, scores[lag]
