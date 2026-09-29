#!/usr/bin/env python3
"""Configurable Petclinic smoke tests with optional writes and cluster checks."""
import argparse
import json
import subprocess
import sys
import urllib.request
import urllib.error
import time
import uuid
from pathlib import Path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8080')
    parser.add_argument('--timeout', type=int, default=15)
    parser.add_argument('--read-retries', type=int, default=1, help='Retries for transient GET failures only')
    parser.add_argument('--write', action='store_true', help='Create owner, pet and visit; verify update and persistence')
    parser.add_argument('--cleanup', choices=['mysql', 'retain'], help='Required with --write; mysql uses kubectl exec')
    parser.add_argument('--report', type=Path, help='Write a JSON report for CI')
    parser.add_argument('--use-proxy', action='store_true', help='Honor environment proxy settings')
    parser.add_argument('--cluster', action='store_true', help='Also check Argo CD, workloads, storage, F5 and Prometheus')
    parser.add_argument('--context', help='kubectl context; otherwise use the current context')
    parser.add_argument('--namespace', default='team-a-poc')
    parser.add_argument('--release', default='petclinic')
    parser.add_argument('--argocd-namespace', default='team-a-poc', help='Namespace of the team-owned Argo CD Application')
    parser.add_argument('--prometheus-namespace', default='monitoring')
    parser.add_argument('--prometheus-service', default='kube-prometheus-stack-prometheus')
    args = parser.parse_args()
    if args.timeout <= 0 or args.read_retries < 0:
        parser.error('timeout must be positive and read-retries nonnegative')
    if args.write and not args.cleanup:
        parser.error('--write requires --cleanup mysql or --cleanup retain')
    if args.cleanup and not args.write:
        parser.error('--cleanup requires --write')
    opener = urllib.request.build_opener() if args.use_proxy else urllib.request.build_opener(urllib.request.ProxyHandler({}))
    results = []
    records = []
    run_id = uuid.uuid4().hex[:20]
    started = time.time()
    command = ['kubectl'] + (['--context', args.context] if args.context else [])
    command += [f'--request-timeout={args.timeout}s']

    def check(name, action):
        start = time.monotonic()
        detail = None
        try:
            detail = action()
            results.append(True)
            print(f'PASS {name}' + (f': {detail}' if detail else ''), flush=True)
        except Exception as error:
            detail = str(error)
            results.append(False)
            print(f'FAIL {name}: {error}', flush=True)
        records.append({'name': name, 'passed': results[-1], 'detail': detail, 'duration_seconds': round(time.monotonic() - start, 3)})

    def get(path, decode=True):
        for attempt in range(args.read_retries + 1):
            try:
                with opener.open(args.base_url.rstrip('/') + path, timeout=args.timeout) as response:
                    require(response.status == 200, f'HTTP {response.status}')
                    data = response.read()
                return json.loads(data) if decode else data.decode('utf-8')
            except (urllib.error.URLError, TimeoutError) as error:
                if isinstance(error, urllib.error.HTTPError) and error.code not in (429, 502, 503, 504):
                    raise
                if attempt == args.read_retries:
                    raise
                time.sleep(min(2 ** attempt, 5))

    def write(method, path, payload, expected):
        request = urllib.request.Request(args.base_url.rstrip('/') + path, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'}, method=method)
        # Never retry writes: a timeout can occur after the server commits.
        try:
            with opener.open(request, timeout=args.timeout) as response:
                require(response.status == expected, f'expected HTTP {expected}, got {response.status}')
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            raise ValueError(f'{method} {path}: HTTP {error.code}: {error.read(2048).decode(errors="replace")}') from error

    marker = 'Smoke' + run_id

    def lifecycle():
        payload = {'firstName': 'SmokeTest', 'lastName': marker, 'address': marker, 'city': 'Vienna', 'telephone': '0123456789'}
        owner = write('POST', '/api/customer/owners', payload, 201)
        owner_id = int(owner['id'])
        path = f'/api/customer/owners/{owner_id}'
        require(get(path)['lastName'] == marker, 'created owner not persisted')
        payload['city'] = 'Graz'
        write('PUT', path, payload, 204)
        require(get(path)['city'] == 'Graz', 'owner update not persisted')
        types = get('/api/customer/petTypes')
        require(types, 'no pet types available')
        pet = write('POST', path + '/pets', {'id': 0, 'name': marker, 'birthDate': '2020-01-01', 'typeId': types[0]['id']}, 201)
        pet_id = int(pet['id'])
        require(get(path + f'/pets/{pet_id}')['name'] == marker, 'created pet not persisted')
        visit_path = f'/api/visit/owners/{owner_id}/pets/{pet_id}/visits'
        visit = write('POST', visit_path, {'date': '2026-01-01', 'description': marker}, 201)
        require(any(item['id'] == visit['id'] and item['description'] == marker for item in get(visit_path)), 'created visit not persisted')
        return {'owner_id': owner_id, 'pet_id': pet_id, 'visit_id': visit['id']}

    def cleanup():
        if args.cleanup == 'retain':
            return f'retained test data with marker {marker}'
        # Marker consists solely of a fixed prefix plus hex UUID; never accept SQL from CLI.
        sql = f"""START TRANSACTION;
DELETE v FROM visits v JOIN pets p ON v.pet_id=p.id JOIN owners o ON p.owner_id=o.id WHERE o.last_name='{marker}' AND o.address='{marker}' AND o.first_name='SmokeTest';
DELETE p FROM pets p JOIN owners o ON p.owner_id=o.id WHERE o.last_name='{marker}' AND o.address='{marker}' AND o.first_name='SmokeTest';
DELETE FROM owners WHERE last_name='{marker}' AND address='{marker}' AND first_name='SmokeTest';
COMMIT;
SELECT COUNT(*) FROM owners WHERE last_name='{marker}' AND address='{marker}';
"""
        result = subprocess.run(command + ['exec', '-i', '-n', args.namespace, args.release + '-mysql-0', '--', 'sh', '-c', 'MYSQL_PWD="$MYSQL_PASSWORD" exec mysql -h 127.0.0.1 -u "$MYSQL_USER" -D "$MYSQL_DATABASE" -N -B'], input=sql, capture_output=True, text=True, timeout=args.timeout + 5)
        require(result.returncode == 0, result.stderr.strip() or 'cleanup command failed')
        require(result.stdout.strip() == '0', 'cleanup verification failed')
        require(not any(owner.get('lastName') == marker for owner in get('/api/customer/owners')), 'test owner remains visible through API')
        return f'removed run {run_id}; database and API verified'

    def health(path):
        require(get(path).get('status') == 'UP', 'health status is not UP')

    def collection(path, fields):
        items = get(path)
        require(isinstance(items, list), 'expected a JSON array')
        for item in items:
            require(isinstance(item, dict) and all(field in item for field in fields), 'invalid record shape')
        return f'{len(items)} records'

    def owner_and_visits():
        owners = get('/api/customer/owners')
        require(isinstance(owners, list), 'expected owners array')
        if not owners:
            return 'empty database; record lookup not applicable'
        owner = get(f'/api/customer/owners/{int(owners[0]["id"])}')
        require(owner['id'] == owners[0]['id'], 'owner lookup returned a different ID')
        pets = owner.get('pets', [])
        require(isinstance(pets, list), 'expected pets array')
        if pets:
            visits = get(f'/api/visit/owners/{int(owner["id"])}/pets/{int(pets[0]["id"])}/visits')
            require(isinstance(visits, list), 'expected visits array')
        return 'owner lookup and available pet visits verified'

    check('homepage', lambda: require('<html' in get('/', False).lower(), 'expected HTML'))
    check('gateway readiness', lambda: health('/actuator/health/readiness'))
    check('customers API', lambda: collection('/api/customer/owners', ['id', 'firstName', 'lastName']))
    check('vets API', lambda: collection('/api/vet/vets', ['id', 'firstName', 'lastName']))
    check('visits API', lambda: collection('/api/visit/owners/1/pets/1/visits', ['id', 'petId']))
    check('owner and pet lookup', owner_and_visits)
    check('GenAI readiness (no provider calls)', lambda: health('/api/genai/actuator/health/readiness'))

    if args.write:
        print(f'Test data marker: {marker}', flush=True)
        try:
            check('create/read/update owner, create/read pet and visit', lifecycle)
        finally:
            check('test data cleanup', cleanup)

    if args.cluster:
        command = ['kubectl']
        if args.context:
            command += ['--context', args.context]
        command += [f'--request-timeout={args.timeout}s']

        def kube(*parts):
            result = subprocess.run(command + list(parts), capture_output=True, text=True, timeout=args.timeout + 5)
            require(result.returncode == 0, result.stderr.strip() or 'kubectl failed')
            return json.loads(result.stdout)

        def resource(kind, name, namespace=None):
            return kube('get', kind, name, '-n', namespace or args.namespace, '-o', 'json')

        def argo():
            status = resource('application', args.release, args.argocd_namespace)['status']
            require(status.get('sync', {}).get('status') == 'Synced', 'application is not Synced')
            require(status.get('health', {}).get('status') == 'Healthy', 'application is not Healthy')

        services = ['api-gateway', 'customers-service', 'visits-service', 'vets-service', 'genai-service']

        def workload(kind, name):
            obj = resource(kind, name)
            desired = obj['spec'].get('replicas', 1)
            status = obj.get('status', {})
            require(status.get('observedGeneration', 0) >= obj['metadata']['generation'], 'controller has not observed current generation')
            require(status.get('readyReplicas', 0) == desired and status.get('updatedReplicas', 0) == desired, 'replicas not ready or rollout incomplete')
            return f'{desired} replicas ready'

        def storage():
            pvc = resource('pvc', f'data-{args.release}-mysql-0')
            require(pvc.get('status', {}).get('phase') == 'Bound', 'PVC is not Bound')
            pv = kube('get', 'pv', pvc['spec']['volumeName'], '-o', 'json')
            require(pv['spec'].get('csi', {}).get('driver') == 'csi.vsphere.vmware.com', 'volume does not use vSphere CSI')

        def f5():
            status = resource('virtualserver.cis.f5.com', args.release).get('status', {})
            require(status.get('status') == 'OK', 'F5 VirtualServer is not OK')
            return status.get('vsAddress')

        def metrics():
            path = f'/api/v1/namespaces/{args.prometheus_namespace}/services/{args.prometheus_service}:9090/proxy/api/v1/targets'
            targets = kube('get', '--raw', path)['data']['activeTargets']
            pool = f'serviceMonitor/{args.namespace}/{args.release}/0'
            targets = [target for target in targets if target.get('scrapePool') == pool]
            expected = set()
            for service in services:
                pods = kube('get', 'pods', '-n', args.namespace, '-l', f'app.kubernetes.io/instance={args.release},app.kubernetes.io/component={service}', '-o', 'json')['items']
                expected.update(pod['metadata']['name'] for pod in pods if not pod['metadata'].get('deletionTimestamp'))
            actual = {target['labels'].get('pod') for target in targets}
            require(expected and actual == expected and len(targets) == len(expected), f'expected {len(expected)} pod targets, got {len(targets)}; missing={sorted(expected - actual)}')
            failed = [f'{target["labels"].get("pod")}: {target.get("lastError") or target["health"]}' for target in targets if target['health'] != 'up']
            require(not failed, '; '.join(failed))
            return f'{len(targets)}/{len(expected)} targets UP'

        check('Argo CD', argo)
        for service in services:
            check(service + ' deployment', lambda service=service: workload('deployment', args.release + '-' + service))
        check('MySQL StatefulSet', lambda: workload('statefulset', args.release + '-mysql'))
        check('vSphere persistent storage', storage)
        check('F5 VirtualServer', f5)
        check('Prometheus scrape targets', metrics)

    report = {'run_id': run_id, 'marker': marker if args.write else None, 'base_url': args.base_url, 'started_unix': started, 'duration_seconds': round(time.time() - started, 3), 'configuration': {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}, 'passed': all(results), 'checks': records}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(f'\n{sum(results)}/{len(results)} checks passed. Run: {run_id}')
    return 0 if all(results) else 1


if __name__ == '__main__':
    sys.exit(main())
