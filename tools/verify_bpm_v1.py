"""Replay frozen captures for independent BPM and unchanged SpO2 v3 results.

Writes only to logs/bpm-v1-20261117; never changes original measurements.
No reference heart rate is available, so this is not accuracy validation.
"""
import csv
import hashlib
import json
from pathlib import Path
import time

from bpm_estimator import METHOD, estimate_bpm, summarize_bpm
from spo2_estimator import estimate_spo2

ROOT = Path(__file__).resolve().parents[1]


def main():
    assert METHOD == 'ir_interval_bpm_v1', 'Use the matching verifier'
    manifest = json.loads((ROOT / 'logs/spo2-repeatability-20261117/comparison.json').read_text())
    source = ROOT / 'logs/ppg-20261117-164801-910636'
    output = ROOT / 'logs/bpm-v1-20261117'
    output.mkdir(exist_ok=True)
    rows = []
    for row in manifest['runs']:
        path = source / row['file']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row['source_json_sha256']
        saved = json.loads(path.read_text())
        frozen = json.loads((ROOT / 'logs/spo2-v3-20261117' / (path.stem + '-replay.json')).read_text())
        csv_hash = hashlib.sha256(path.with_suffix('.csv').read_bytes()).hexdigest()
        assert csv_hash == frozen['csv_sha256']
        with path.with_suffix('.csv').open(newline='') as stream:
            points = [(int(r['session']), int(r['seq']), int(r['read_elapsed_us']),
                       int(r['red_raw']), int(r['ir_raw']), float(r['host_elapsed_s']))
                      for r in csv.DictReader(stream)]
        arguments = dict(delivery_ok=saved['rate_and_delivery_pass'],
                         ambient_events=saved['counter_deltas']['alc_events'])
        assert estimate_spo2(points, **arguments) == frozen['replay_v3'], path.name
        started = time.perf_counter()
        live = estimate_bpm(points, **arguments)
        live_seconds = time.perf_counter() - started
        bpm = summarize_bpm(points, **arguments)
        item = dict(file=path.name, source_csv_sha256=csv_hash,
                    spo2_unchanged=True, spo2_valid=frozen['replay_v3']['valid'],
                    bpm_valid=bpm['valid'], bpm=bpm['bpm'],
                    bpm_valid_windows=bpm['valid_windows'], bpm_reasons=bpm['reasons'],
                    last_10_seconds_valid=live['valid'], last_10_seconds_bpm=live['bpm'],
                    live_analysis_seconds=live_seconds)
        rows.append(item)
        report = dict(scope='OFFLINE REPLAY. Existing capture; no reference pulse rate or clinical validation.',
                      original=str(path.relative_to(ROOT)), source_csv_sha256=csv_hash,
                      spo2_v3_exactly_unchanged=True, bpm_summary=bpm, last_10_seconds=live)
        (output / (path.stem + '-bpm-replay.json')).write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    summary = dict(method=METHOD, records=len(rows),
                   spo2_v3_exactly_unchanged=all(r['spo2_unchanged'] for r in rows),
                   bpm_accepted=sum(r['bpm_valid'] for r in rows),
                   bpm_accepted_while_spo2_refused=sum(r['bpm_valid'] and not r['spo2_valid'] for r in rows),
                   maximum_live_analysis_seconds=max(r['live_analysis_seconds'] for r in rows),
                   clinical_accuracy_verified=False, production_changed_by_replay=False, runs=rows)
    (output / 'replay-verification.json').write_text(json.dumps(summary, indent=2, allow_nan=False) + '\n')
    print(json.dumps({k: v for k, v in summary.items() if k != 'runs'}, indent=2))


if __name__ == '__main__':
    main()
