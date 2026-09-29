#!/usr/bin/env python3
"""Bounded single-pod Chaos Monkey experiment through localhost port forwards."""
import argparse
import json
import signal
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--management-url', default='http://127.0.0.1:19090')
    parser.add_argument('--target-url', default='http://127.0.0.1:18082/owners/1/pets/1/visits')
    parser.add_argument('--fault', choices=['latency', 'exception'], default='latency')
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--latency-ms', type=int, default=500)
    parser.add_argument('--timeout', type=float, default=5)
    parser.add_argument('--interval', type=float, default=0.2)
    parser.add_argument('--report', type=Path, default=Path('dist/chaos-report.json'))
    args = parser.parse_args()
    if not 3 <= args.samples <= 100 or not 1 <= args.latency_ms <= 5000 or not 0 < args.timeout <= 30 or not 0 <= args.interval <= 5:
        parser.error('samples: 3–100; latency-ms: 1–5000; timeout: (0,30]; interval: 0–5')
    if args.timeout * 1000 <= args.latency_ms + 1000:
        parser.error('timeout must exceed injected latency by at least 1 second')
    for url in (args.management_url, args.target_url):
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname not in ('localhost', '127.0.0.1', '::1') or parsed.scheme != 'http':
            parser.error('use HTTP localhost pod port-forwards for both URLs')
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    control = args.management_url.rstrip('/') + '/actuator/chaosmonkey'
    report = {'configuration': {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()}, 'started_unix': time.time(), 'phases': {}, 'passed': False, 'cleanup': 'not required'}
    original = None
    armed = False

    def api(path, payload=None):
        request = urllib.request.Request(control + path, data=json.dumps(payload).encode() if payload is not None else None, headers={'Content-Type': 'application/json'})
        with client.open(request, timeout=args.timeout) as response:
            body = response.read().decode()
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return body

    def sample(phase):
        points = []
        report['phases'][phase] = points
        for _ in range(args.samples):
            start = time.monotonic()
            try:
                with client.open(args.target_url, timeout=args.timeout) as response:
                    status = response.status
                    json.loads(response.read())
            except urllib.error.HTTPError as error:
                status = error.code
            points.append({'status': status, 'duration_ms': round((time.monotonic() - start) * 1000, 2)})
            time.sleep(args.interval)
        print(f'{phase}: HTTP statuses {[p["status"] for p in points]}, median {statistics.median(p["duration_ms"] for p in points):.1f} ms', flush=True)
        return points

    def stopped(signum, frame):
        raise KeyboardInterrupt(f'signal {signum}')

    signal.signal(signal.SIGTERM, stopped)
    try:
        status = api('/status')
        if not isinstance(status, dict) or status.get('enabled') is not False:
            raise RuntimeError('target must report enabled=false before an experiment')
        original = api('/assaults')
        baseline = sample('baseline')
        if any(point['status'] != 200 for point in baseline):
            raise RuntimeError('baseline failed; no fault was enabled')
        assault = dict(original)
        assault.update(level=1, deterministic=True, latencyActive=args.fault == 'latency', exceptionsActive=args.fault == 'exception', killApplicationActive=False, memoryActive=False, cpuActive=False, latencyRangeStart=args.latency_ms, latencyRangeEnd=args.latency_ms)
        armed = True
        api('/assaults', assault)
        api('/enable', {})
        fault = sample('fault')
        api('/disable', {})
        api('/assaults', original)
        recovery = sample('recovery')
        if any(point['status'] != 200 for point in recovery):
            raise RuntimeError('recovery failed')
        if args.fault == 'exception':
            if not all(point['status'] >= 500 for point in fault):
                raise RuntimeError('expected injected exceptions were not observed on every request')
        else:
            delta = statistics.median(point['duration_ms'] for point in fault) - statistics.median(point['duration_ms'] for point in baseline)
            report['latency_delta_ms'] = delta
            if any(point['status'] != 200 for point in fault) or delta < args.latency_ms * 0.7:
                raise RuntimeError('expected injected latency was not observed')
        report['passed'] = True
    except (Exception, KeyboardInterrupt) as error:
        report['error'] = str(error)
        print(f'FAIL: {error}', flush=True)
    finally:
        if armed:
            try:
                api('/disable', {})
                api('/assaults', original)
                if api('/status').get('enabled') is not False:
                    raise RuntimeError('Chaos Monkey remains enabled')
                report['cleanup'] = 'disabled; original assault configuration restored'
            except Exception as error:
                report['cleanup'] = f'FAILED: {error}; disable target manually'
                report['passed'] = False
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(('PASS' if report['passed'] else 'FAIL') + f': report {args.report}; cleanup: {report["cleanup"]}')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
