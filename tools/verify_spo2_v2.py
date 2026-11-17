"""Replay frozen captures against v2 without rewriting any original result.

Usage: python tools/verify_spo2_v2.py
This is a software regression, not SpO2 ground-truth validation.
"""
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import time

from spo2_estimator import METHOD, estimate_spo2

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / 'logs/spo2-20261117'


def replay():
    if METHOD != 'local_baseline_acdc_reference_curve_v2':
        raise RuntimeError('This verifier targets archived v2; use verify_spo2_v3.py for current code. Historical v2 evidence is unchanged.')
    manifest = json.loads((FROZEN / 'verification.json').read_text(encoding='utf-8'))
    for name, expected in manifest['sha256'].items():
        actual = hashlib.sha256((FROZEN / name).read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError(f'Frozen evidence hash mismatch: {name}')
    spec = importlib.util.spec_from_file_location('frozen_spo2_v1', FROZEN / 'source/tools/spo2_estimator.py')
    v1 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v1)
    output = ROOT / 'logs/spo2-v2-20261117'
    output.mkdir(exist_ok=True)
    report = dict(method=METHOD, scope='Offline reanalysis of old captures; no new human measurement or accuracy validation',
                  original_evidence_hashes_verified=True, trials=[])
    for trial in (1, 2, 3):
        source = FROZEN / f'trial-{trial}.csv'
        with source.open(newline='', encoding='utf-8') as stream:
            points = [(int(r['session']), int(r['seq']), int(r['read_elapsed_us']),
                       int(r['red_raw']), int(r['ir_raw']), float(r['host_elapsed_s']))
                      for r in csv.DictReader(stream)]
        saved = json.loads(source.with_suffix('.json').read_text(encoding='utf-8'))
        arguments = dict(delivery_ok=saved['rate_and_delivery_pass'],
                         ambient_events=saved['counter_deltas']['alc_events'])
        old = v1.estimate_spo2(points, **arguments)
        if old != saved['spo2']:
            raise RuntimeError(f'Trial {trial}: archived v1 does not reproduce saved result')
        started = time.perf_counter()
        new = estimate_spo2(points, **arguments)
        elapsed = time.perf_counter() - started
        if trial == 2:
            assert not new['valid'] and new['spo2_percent'] is None and new['valid_windows'] == 0
        else:
            assert new['valid'] and new['valid_windows'] >= 3
        result = dict(trial=trial, source_csv=str(source.relative_to(ROOT)),
                      source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                      scope=report['scope'], original_v1=old, reanalyzed_v2=new,
                      processing_seconds=elapsed)
        (output / f'trial-{trial}-reanalysis.json').write_text(
            json.dumps(result, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        report['trials'].append(dict(trial=trial, v1_windows=old['valid_windows'],
                                     v2_windows=new['valid_windows'], v2_valid=new['valid'],
                                     reasons=new['reasons'], processing_seconds=elapsed))
    report['source_sha256'] = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in ('tools/spo2_estimator.py', 'tools/ppg_viewer.py', 'tools/ppg_viewer.html',
                     'tests/test_spo2.py', 'tests/test_ppg_viewer.py', 'tools/verify_spo2_v2.py')}
    (output / 'replay-verification.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    replay()
