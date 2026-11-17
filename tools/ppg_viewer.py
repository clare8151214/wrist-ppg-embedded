"""Local Red/IR viewer and loss-aware recorder. Requires pyserial only.

Run: python tools/ppg_viewer.py --port COM6 --open
All network traffic stays on 127.0.0.1. The serial reader runs independently
of the browser so a slow/closed browser cannot block the sensor FIFO.
"""
from __future__ import annotations

import argparse
from collections import deque
import csv
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import queue
import threading
import time
from urllib.parse import urlsplit
import webbrowser

import serial
from spo2_estimator import CONVERSION_LIMITS, METHOD, QUALITY_LIMITS, estimate_spo2, preview_quality
from bpm_estimator import (METHOD as BPM_METHOD, WINDOW_SECONDS as BPM_WINDOW_SECONDS,
                           UPDATE_SECONDS as BPM_UPDATE_SECONDS, STALE_SECONDS as BPM_STALE_SECONDS,
                           estimate_bpm, invalid_bpm, summarize_bpm)

ROOT = Path(__file__).resolve().parents[1]
METRIC_KEYS = ("session", "samples", "elapsed_us", "i2c_errors", "overflow_events",
               "lost_lower_bound", "max_backlog", "alc_events")
STABILITY_TIMEOUT_S = 30
STABILITY_PASSES = 3


def sample_rates(points):
    """Measured paired-sample rate, never the nominal sensor configuration."""
    if len(points) < 2:
        return None, None
    a, b = points[0], points[-1]
    if a[0] != b[0] or b[1] <= a[1] or b[2] <= a[2] or b[5] <= a[5]:
        return None, None
    # Count received rows rather than sequence differences: gaps must not
    # inflate the measured delivery rate.
    n = len(points) - 1
    return n * 1e6 / (b[2] - a[2]), n / (b[5] - a[5])


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.points = deque(maxlen=2500)  # session, seq, read_us, red, ir, host_mono
        self.metrics = {}
        self.metric_time = 0.0
        self.messages = deque(maxlen=8)
        self.received = 0
        self.seq_gaps = 0
        self.invalid_frames = 0
        self.error = ""
        self.connected = False
        self.last_sample_at = 0.0
        self.measurement = None
        self.result = None
        self.pending_results = []
        self.bpm = invalid_bpm('insufficient_bpm_samples')
        self.bpm_updated_at = 0.0
        self.next_bpm_check = 0.0
        self.last_bpm_fault_at = float('-inf')
        self.bpm_paused = False

    def invalidate_bpm(self, reason, now, fault=False):
        self.bpm = invalid_bpm(reason)
        self.bpm_updated_at = now
        if fault:
            self.last_bpm_fault_at = now

    def set_streaming(self, enabled, now):
        with self.lock:
            self.bpm_paused = not enabled
            self.invalidate_bpm('insufficient_bpm_samples' if enabled else 'stream_stopped', now)
            if enabled:
                self.points.clear()
            self.next_bpm_check = 0.0

    def update_bpm(self, now):
        """Serial-thread analysis, once per second; never triggered by a browser."""
        with self.lock:
            if now < self.next_bpm_check:
                return None
            self.next_bpm_check = now + BPM_UPDATE_SECONDS
            if self.bpm_paused:
                result = invalid_bpm('stream_stopped')
            elif not self.connected or self.error or now - self.last_sample_at >= 1 or now - self.metric_time >= 2:
                result = invalid_bpm('no_fresh_stream')
            elif now - self.last_bpm_fault_at <= BPM_WINDOW_SECONDS:
                result = invalid_bpm('recent_stream_fault')
            else:
                result = estimate_bpm(list(self.points))
            self.bpm, self.bpm_updated_at = result, now
            return dict(result)

    def bpm_snapshot(self, now):
        if self.bpm_paused:
            result = invalid_bpm('stream_stopped')
        elif not self.connected or self.error or now - self.last_sample_at >= 1 or now - self.metric_time >= 2:
            result = invalid_bpm('no_fresh_stream')
        elif now - self.bpm_updated_at > BPM_STALE_SECONDS:
            result = invalid_bpm('stale_bpm_result')
        else:
            result = dict(self.bpm)
        result.update(scope='live', age_seconds=max(0, now - self.bpm_updated_at),
                      update_seconds=BPM_UPDATE_SECONDS)
        return result

    def feed(self, line, now):
        """Return a validated sample for CSV, or None for status lines."""
        with self.lock:
            if line.startswith("# ERROR"):
                self.error = line
                self.invalidate_bpm('recent_stream_fault', now, fault=True)
            if line.startswith("# PPG_STOP"):
                self.bpm_paused = True
                self.invalidate_bpm('stream_stopped', now)
            if line.startswith("# PPG_START"):
                self.bpm_paused = False
                self.invalidate_bpm('insufficient_bpm_samples', now)
            if line.startswith("#"):
                self.messages.append(line)
                return None
            fields = line.split(",")
            if fields[0] not in ("D", "M"):
                return None
            try:
                if fields[0] == "M":
                    if len(fields) != 9:
                        raise ValueError("metric field count")
                    values = [int(x) for x in fields[1:]]
                    if any(x < 0 for x in values):
                        raise ValueError("negative metric")
                    incoming = dict(zip(METRIC_KEYS, values))
                    if self.metrics.get('session') == incoming['session'] and any(
                            incoming[k] > self.metrics.get(k, 0)
                            for k in ('i2c_errors', 'overflow_events', 'lost_lower_bound', 'alc_events')):
                        self.invalidate_bpm('recent_stream_fault', now, fault=True)
                    self.metrics = incoming
                    self.metric_time = now
                    return None
                if len(fields) != 6:
                    raise ValueError("sample field count")
                session, seq, read_us, red, ir = [int(x) for x in fields[1:]]
                if min(session, seq, read_us, red, ir) < 0 or max(red, ir) > 262143:
                    raise ValueError("sample out of range")
                point = (session, seq, read_us, red, ir, now)
                if self.points:
                    previous = self.points[-1]
                    if session != previous[0] or seq <= previous[1] or read_us <= previous[2]:
                        if self.measurement:
                            self.measurement["interrupted"] = True
                        self.points.clear()
                        self.metrics = {}
                        self.error = ""
                        self.invalidate_bpm('insufficient_bpm_samples', now)
                    elif seq != previous[1] + 1:
                        self.seq_gaps += seq - previous[1] - 1
                        self.invalidate_bpm('discontinuous_samples', now, fault=True)
                self.points.append(point)
                self.received += 1
                self.last_sample_at = now
                if ir <= 0 or ir >= 262143 or ir < 10000:
                    self.invalidate_bpm('adc_clipping' if ir in (0, 262143) else 'weak_optical_contact', now)
                if self.measurement:
                    if self.measurement["phase"] == "stabilizing":
                        self.measurement["preview_points"].append(point)
                    else:
                        self.measurement["points"].append(point)
                return point
            except (ValueError, IndexError):
                self.invalid_frames += 1
                self.invalidate_bpm('recent_stream_fault', now, fault=True)
                return None

    def begin_measurement(self, label, now, seconds=30, site=None):
        with self.lock:
            if self.measurement:
                raise ValueError("A measurement is already running")
            if not self.connected or now - self.last_sample_at > 1:
                raise ValueError("No fresh samples; start capture first")
            if label == "spo2" and site not in ("finger", "wrist"):
                raise ValueError("Select finger or wrist for SpO2 capture")
            self.result = None
            self.measurement = dict(label=label, site=site, started=now, seconds=seconds,
                                    points=[], interrupted=False,
                                    phase="stabilizing" if label == "spo2" else "recording",
                                    armed_at=now, preview_points=deque(maxlen=1000),
                                    next_stability_check=now + 6, stability_passes=0,
                                    stability_duration_s=0, cancel_reason=None,
                                    last_quality={"signal_valid": False,
                                                  "signal_reasons": ["collecting_stability_samples"]},
                                    start_gaps=self.seq_gaps,
                                    start_invalid=self.invalid_frames,
                                    start_metrics=dict(self.metrics))

    def cancel_measurement(self, now):
        with self.lock:
            if self.measurement:
                self.measurement["cancel_reason"] = "measurement_cancelled"
                self.finish_due(now)

    def check_stability(self, run, now):
        if now - run["armed_at"] >= STABILITY_TIMEOUT_S:
            run["cancel_reason"] = "stability_timeout"
            return
        if now < run["next_stability_check"]:
            return
        run["next_stability_check"] = now + 1
        points = list(run["preview_points"])
        if points:
            points = [p for p in points if p[2] >= points[-1][2] - 6000000]
        if not self.connected or self.error or now - self.last_sample_at > 1 or now - self.metric_time >= 2:
            quality = {"signal_valid": False, "signal_reasons": ["no_fresh_stream"]}
        else:
            quality = preview_quality(points)
        run["last_quality"] = quality
        run["stability_passes"] = run["stability_passes"] + 1 if quality["signal_valid"] else 0
        if run["stability_passes"] >= STABILITY_PASSES:
            # Warm-up samples never enter the formal 30-second capture.
            run.update(phase="recording", started=now, points=[], interrupted=False,
                       stability_duration_s=now - run["armed_at"],
                       start_gaps=self.seq_gaps, start_invalid=self.invalid_frames,
                       start_metrics=dict(self.metrics))
            run["preview_points"].clear()

    def finish_due(self, now):
        with self.lock:
            run = self.measurement
            if not run:
                return
            if run["phase"] == "stabilizing" and not run["cancel_reason"]:
                self.check_stability(run, now)
            if not run["cancel_reason"] and (run["phase"] == "stabilizing" or now - run["started"] < run["seconds"]):
                return
            points = run["points"]
            device_hz, host_hz = sample_rates(points)
            host_span = points[-1][5] - points[0][5] if len(points) > 1 else 0
            counters = {}
            for key in ("i2c_errors", "overflow_events", "lost_lower_bound", "alc_events"):
                counters[key] = self.metrics.get(key, 0) - run["start_metrics"].get(key, 0)
            gaps = self.seq_gaps - run["start_gaps"]
            invalid = self.invalid_frames - run["start_invalid"]
            fresh = now - self.metric_time < 2
            checks = {
                "not_cancelled": not run["cancel_reason"],
                "recording_started": run["phase"] == "recording",
                "host_span_sufficient": host_span >= run["seconds"] - 1,
                "device_delivery_ge_50_hz": device_hz is not None and device_hz >= 50,
                "host_delivery_ge_50_hz": host_hz is not None and host_hz >= 50,
                "no_sequence_gaps": gaps == 0,
                "no_invalid_frames": invalid == 0,
                "no_i2c_errors": counters["i2c_errors"] == 0,
                "no_fifo_loss": counters["overflow_events"] == counters["lost_lower_bound"] == 0,
                "no_restart": not run["interrupted"],
                "fresh_device_metrics": fresh,
                "no_stream_error": not self.error,
                "connected": self.connected,
            }
            channels = {}
            for name, col in (("red", 3), ("ir", 4)):
                values = [p[col] for p in points]
                channels[name] = {"min": min(values) if values else None,
                                  "max": max(values) if values else None,
                                  "rail_samples": sum(x in (0, 262143) for x in values)}
            self.result = {
                "label": run["label"],
                "wrist_contact_reported": run["label"] == "wrist" or run["site"] == "wrist",
                "contact_site_reported": run["site"] or run["label"],
                "requested_duration_s": run["seconds"],
                "measurement_status": run["cancel_reason"] or "complete",
                "recording_started": run["phase"] == "recording",
                "stabilization": {"required": run["label"] == "spo2",
                                  "duration_s": run["stability_duration_s"] if run["phase"] == "recording" else now - run["armed_at"],
                                  "last_quality": run["last_quality"] if run["label"] == "spo2" else None},
                "received_pairs": len(points), "host_sample_span_s": host_span,
                "device_delivery_hz_per_channel": device_hz,
                "host_delivery_hz_per_channel": host_hz,
                "sequence_gaps": gaps, "invalid_frames": invalid,
                "counter_deltas": counters, "channels": channels,
                "first_session_seq_read_us": list(points[0][:3]) if points else None,
                "last_session_seq_read_us": list(points[-1][:3]) if points else None,
                "checks": checks,
                "rate_and_delivery_pass": all(checks.values()),
                "note": "Raw data only. Wrist contact is operator-reported; pulse quality is not certified. read_us is FIFO read completion time, not an exact ADC conversion timestamp.",
            }
            if run["label"] == "spo2":
                self.result["spo2"] = estimate_spo2(
                    points, delivery_ok=self.result["rate_and_delivery_pass"],
                    ambient_events=counters["alc_events"])
                if run["cancel_reason"]:
                    self.result["spo2"]["reasons"] = [run["cancel_reason"]]
                    self.result["spo2"]["signal_reasons"] = [run["cancel_reason"]]
            self.result['bpm'] = summarize_bpm(points,
                delivery_ok=self.result['rate_and_delivery_pass'], ambient_events=counters['alc_events'])
            self.pending_results.append((self.result, points))
            self.measurement = None

    def snapshot(self, now):
        with self.lock:
            recent = [p for p in self.points if now - p[5] <= 5]
            device_hz, host_hz = sample_rates(recent)
            fresh = self.connected and now - self.last_sample_at < 1
            run = self.measurement
            return {"connected": self.connected, "fresh": fresh,
                    "estimator_method": METHOD,
                    "bpm": self.bpm_snapshot(now),
                    "conversion_limits": dict(CONVERSION_LIMITS),
                    "quality_limits": dict(QUALITY_LIMITS),
                    "device_hz": device_hz if fresh else None,
                    "host_hz": host_hz if fresh else None,
                    "window_seconds": recent[-1][5] - recent[0][5] if len(recent) > 1 else 0,
                    "received": self.received, "seq_gaps": self.seq_gaps,
                    "invalid_frames": self.invalid_frames, "metrics": self.metrics,
                    "error": self.error, "messages": list(self.messages),
                    "points": [list(p[:5]) for p in self.points],
                    "measurement_remaining": max(0, run["seconds"] - (now - run["started"])) if run and run["phase"] == "recording" else None,
                    "measurement_phase": run["phase"] if run else None,
                    "stabilization": {"elapsed_s": now - run["armed_at"],
                                      "timeout_s": STABILITY_TIMEOUT_S,
                                      "passes": run["stability_passes"], "required_passes": STABILITY_PASSES,
                                      "quality": run["last_quality"]} if run and run["phase"] == "stabilizing" else None,
                    "measurement_label": run["label"] if run else None,
                    "measurement_site": run["site"] if run else None,
                    "result": self.result}


class Recorder:
    def __init__(self, port, directory):
        self.port = port
        self.directory = directory
        self.state = State()
        self.commands = queue.Queue()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        started = time.monotonic()
        try:
            with (self.directory / "serial.log").open("wb") as raw, \
                    (self.directory / "raw.csv").open("w", newline="", encoding="utf-8") as csv_file, \
                    (self.directory / "bpm-live.jsonl").open("w", encoding="utf-8") as bpm_file:
                writer = csv.writer(csv_file)
                writer.writerow(("host_elapsed_s", "session", "seq", "read_elapsed_us", "red_raw", "ir_raw"))
                with serial.Serial(port=None, baudrate=115200, timeout=0.1, write_timeout=1) as device:
                    device.port = self.port
                    device.dtr = False
                    device.rts = False
                    device.open()
                    device.reset_input_buffer()
                    self.state.connected = True
                    device.write(b"p")
                    flushed = time.monotonic()
                    try:
                        while not self.stop_event.is_set():
                            while not self.commands.empty():
                                command = self.commands.get_nowait()
                                if command == b"p":
                                    with self.state.lock:
                                        self.state.error = ""
                                device.write(command)
                            line = device.read_until(b"\n", 512)
                            now = time.monotonic()
                            if line:
                                raw.write(line)
                                try:
                                    decoded = line.decode("ascii").strip()
                                except UnicodeDecodeError:
                                    decoded = ""
                                    self.state.invalid_frames += 1
                                    with self.state.lock:
                                        self.state.invalidate_bpm('recent_stream_fault', now, fault=True)
                                point = self.state.feed(decoded, now)
                                if point:
                                    writer.writerow((f"{now - started:.9f}", *point[:5]))
                            self.state.finish_due(now)
                            bpm = self.state.update_bpm(now)
                            if bpm is not None:
                                bpm_file.write(json.dumps(dict(host_elapsed_s=now-started, **bpm), allow_nan=False) + '\n')
                            with self.state.lock:
                                while self.state.pending_results:
                                    result, captured = self.state.pending_results.pop(0)
                                    tag = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
                                    path = self.directory / f"{result['label']}-{tag}.json"
                                    csv_path = path.with_suffix(".csv")
                                    with csv_path.open("w", newline="", encoding="utf-8") as capture_file:
                                        capture_writer = csv.writer(capture_file)
                                        capture_writer.writerow(("host_elapsed_s", "session", "seq", "read_elapsed_us", "red_raw", "ir_raw"))
                                        for p in captured:
                                            capture_writer.writerow((f"{p[5] - started:.9f}", *p[:5]))
                                    result["files"] = {"json": path.name, "csv": csv_path.name}
                                    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
                                    (self.directory / "latest-measurement.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
                                    print(json.dumps({"measurement_file": str(path), "pass": result["rate_and_delivery_pass"]}), flush=True)
                            if now - flushed >= 1:
                                raw.flush()
                                csv_file.flush()
                                bpm_file.flush()
                                flushed = now
                    finally:
                        try:
                            device.write(b"x")
                            device.flush()
                        except serial.SerialException:
                            pass
        except (OSError, serial.SerialException) as exc:
            with self.state.lock:
                self.state.error = str(exc)
            print(f"Serial error: {exc}", flush=True)
        finally:
            with self.state.lock:
                self.state.connected = False
                summary = self.state.snapshot(time.monotonic())
                summary.pop("points")
            (self.directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM6")
    parser.add_argument("--http-port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open the local viewer in your browser")
    args = parser.parse_args()
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    directory = ROOT / "logs" / f"ppg-{timestamp}"
    directory.mkdir(parents=True, exist_ok=False)
    recorder = Recorder(args.port, directory)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, status, body, content_type="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/":
                return self.send(200, (ROOT / "tools/ppg_viewer.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/state":
                payload = recorder.state.snapshot(time.monotonic())
                payload.update(port=args.port, directory=str(directory))
                return self.send(200, json.dumps(payload).encode("utf-8"))
            if path == "/api/bpm":
                with recorder.state.lock:
                    return self.send(200, json.dumps(recorder.state.bpm_snapshot(time.monotonic()), allow_nan=False).encode('utf-8'))
            if path == "/api/latest-result":
                with recorder.state.lock:
                    result = recorder.state.result
                    return self.send(200 if result else 404, json.dumps(result).encode("utf-8"))
            if path == "/api/latest.csv":
                with recorder.state.lock:
                    result = recorder.state.result
                    filename = result.get("files", {}).get("csv") if result else None
                    if filename:
                        return self.send(200, (directory / filename).read_bytes(), "text/csv; charset=utf-8")
                return self.send(404, b'{}')
            self.send(404, b'{}')

        def do_POST(self):
            # Same-origin local UI only; reject cross-site control requests.
            origin = self.headers.get("Origin")
            if origin and origin != f"http://127.0.0.1:{args.http_port}":
                return self.send(403, b'{"error":"origin"}')
            path = urlsplit(self.path).path
            if path in ("/api/start", "/api/stop"):
                if path.endswith("start") and recorder.state.measurement:
                    return self.send(409, b'{"error":"Measurement is active; stop it first"}')
                if path.endswith("stop"):
                    recorder.state.cancel_measurement(time.monotonic())
                recorder.state.set_streaming(path.endswith('start'), time.monotonic())
                recorder.commands.put(b"p" if path.endswith("start") else b"x")
            elif path in ("/api/measure/wrist", "/api/measure/bench", "/api/measure/spo2/finger", "/api/measure/spo2/wrist"):
                try:
                    parts = path.strip("/").split("/")
                    recorder.state.begin_measurement(parts[2], time.monotonic(),
                                                     site=parts[3] if len(parts) == 4 else None)
                except ValueError as exc:
                    return self.send(409, json.dumps({"error": str(exc)}).encode())
            elif path == "/api/shutdown":
                recorder.stop_event.set()
                self.send(200, b'{"ok":true}')
                threading.Thread(target=server.shutdown, daemon=True).start()
                return
            else:
                return self.send(404, b'{}')
            self.send(200, b'{"ok":true}')

    # Bind before opening serial: a busy HTTP port must not leave a reader behind.
    server = ThreadingHTTPServer(("127.0.0.1", args.http_port), Handler)
    (directory / "session.json").write_text(json.dumps({
        "started_at": datetime.now().astimezone().isoformat(), "port": args.port,
        "baud": 115200, "url": f"http://127.0.0.1:{args.http_port}",
        "configuration": "100 paired samples/s, averaging=1, 18-bit, 8192 nA, LED_PA=0x40",
        "estimator_method": METHOD,
        "bpm_method": BPM_METHOD,
        "bpm": {"source": "ir", "window_seconds": BPM_WINDOW_SECONDS,
                "update_seconds": BPM_UPDATE_SECONDS, "stale_seconds": BPM_STALE_SECONDS},
        "conversion_limits": dict(CONVERSION_LIMITS),
        "quality_limits": dict(QUALITY_LIMITS),
        "stabilization": {"timeout_s": STABILITY_TIMEOUT_S, "consecutive_checks": STABILITY_PASSES,
                          "window_seconds": 6, "check_interval_seconds": 1},
        "timestamp_note": "read_elapsed_us is device FIFO read completion; host_elapsed_s is Python monotonic receive time",
    }, indent=2) + "\n", encoding="utf-8")
    recorder.thread.start()
    print(f"Viewer: http://127.0.0.1:{args.http_port}", flush=True)
    print(f"Recording: {directory}", flush=True)
    if args.open:
        webbrowser.open(f"http://127.0.0.1:{args.http_port}")
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        recorder.stop_event.set()
        recorder.thread.join(timeout=3)
        server.server_close()


if __name__ == "__main__":
    main()
