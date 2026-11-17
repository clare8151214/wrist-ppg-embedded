"""Compare frozen v2 captures with v3; no new measurement or calibration."""
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import time

from spo2_estimator import METHOD, estimate_spo2, preview_quality

ROOT = Path(__file__).resolve().parents[1]


def main():
    assert METHOD == 'continuous_acdc_reference_curve_v3', 'Use the matching version verifier'
    frozen = ROOT / 'logs/spo2-v2-20261117/source/tools/spo2_estimator.py'
    spec = importlib.util.spec_from_file_location('frozen_v2', frozen)
    v2 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v2)
    manifest = json.loads((ROOT / 'logs/spo2-repeatability-20261117/comparison.json').read_text())
    source = ROOT / 'logs/ppg-20261117-164801-910636'
    output = ROOT / 'logs/spo2-v3-20261117'
    output.mkdir(exist_ok=True)
    rows = []
    for row in manifest['runs']:
        path = source / row['file']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row['source_json_sha256']
        saved = json.loads(path.read_text())
        with path.with_suffix('.csv').open(newline='') as stream:
            points = [(int(r['session']), int(r['seq']), int(r['read_elapsed_us']),
                       int(r['red_raw']), int(r['ir_raw']), float(r['host_elapsed_s']))
                      for r in csv.DictReader(stream)]
        arguments = dict(delivery_ok=saved['rate_and_delivery_pass'],
                         ambient_events=saved['counter_deltas']['alc_events'])
        assert v2.estimate_spo2(points, **arguments) == saved['spo2']
        started = time.perf_counter()
        current = estimate_spo2(points, **arguments)
        elapsed = time.perf_counter() - started
        consecutive = 0
        ready_at = None
        # This previews readiness only; the original 30 s file is not relabeled
        # as a new post-stabilization capture.
        for second in range(6, 30):
            window = [p for p in points if second - 6 <= (p[2] - points[0][2]) / 1e6 <= second]
            quality = preview_quality(window)
            consecutive = consecutive + 1 if quality['signal_valid'] else 0
            if consecutive >= 3:
                ready_at = second
                break
        item = dict(file=path.name, v2_valid=saved['spo2']['valid'], v2_windows=saved['spo2']['valid_windows'],
                    v3_valid=current['valid'], v3_signal_windows=current['signal_valid_windows'],
                    v3_conversion_windows=current['conversion_valid_windows'],
                    reasons=current['reasons'], elapsed_seconds=elapsed,
                    hypothetical_readiness_at_seconds=ready_at)
        rows.append(item)
        report = dict(scope='OFFLINE REPLAY. Original data, no fresh post-stabilization capture; not clinical validation.',
                      original=str(path.relative_to(ROOT)),
                      csv_sha256=hashlib.sha256(path.with_suffix('.csv').read_bytes()).hexdigest(),
                      original_v2=saved['spo2'], replay_v3=current)
        (output / (path.stem + '-replay.json')).write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    summary = dict(method=METHOD, records=len(rows), v2_accepted=sum(r['v2_valid'] for r in rows),
                   v3_accepted=sum(r['v3_valid'] for r in rows), original_v2_reproduced=True,
                   clinical_accuracy_verified=False, production_changed_by_replay=False, runs=rows)
    (output / 'replay-verification.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
