# Controlled Chaos Monkey experiments

The Helm chart supports Chaos Monkey for customers, visits and vets. It is
disabled by default. Enable instrumentation through the GitOps Application in
the platform repository by adding these Helm value files:

```yaml
spec:
  source:
    helm:
      releaseName: petclinic
      valueFiles:
        - values.yaml
        - values-chaos.yaml
```

Commit and push the change, then wait for Argo CD to report Synced/Healthy.
The example instruments visits-service only. Its replicas restart with the
`chaos-monkey` profile, but attacks remain disabled until explicitly enabled.
`chaos.services` can select customers-service, visits-service and vets-service.
Do not enable attacks in startup configuration. Application rest controllers
are instrumented; health probes are excluded.

Actuator runs on a separate management port (9090) on instrumented services.
The gateway routes application ports only, so Chaos Monkey control endpoints
are not reachable through F5. Prometheus retains metrics access through the
Service's named metrics port. The network policy permits the management port
only from configured Prometheus pods; operators use Kubernetes pod port-forward
access. Network policies are port-based and do not distinguish HTTP methods:
the trusted Prometheus pods can reach control paths as well as metrics.

## Run a single-pod experiment

Run the smoke suite first:

```sh
python3 scripts/smoke_test.py --cluster
kubectl get pods -n team-a-poc -l app.kubernetes.io/component=visits-service
```

Select ONE ready pod and forward both ports from that same pod in a separate
terminal (replace POD_NAME):

```sh
kubectl port-forward -n team-a-poc pod/POD_NAME 19090:9090 18082:8082
```

Run either bounded experiment:

```sh
python3 scripts/chaos/experiment.py --fault latency --latency-ms 500 --samples 5
python3 scripts/chaos/experiment.py --fault exception --samples 5
python3 scripts/smoke_test.py --cluster
```

Use `--target-url` for a different service's GET endpoint; `--management-url`,
`--samples`, `--interval`, `--timeout`, and `--report` are configurable. Both URLs
must use localhost port-forwards. The runner refuses a target whose Chaos Monkey
is already enabled, snapshots assault settings, measures baseline/fault/recovery,
then disables faults and restores the settings in a finally block. SIGTERM and
Ctrl-C trigger cleanup. No requests are retried. It never writes application data.

Pass criteria: baseline and recovery return HTTP 200; exception injection yields
HTTP 5xx on every sampled request; latency injection keeps HTTP 200 and raises
median latency by at least 70% of the configured delay. Reports contain the
configuration, timestamps, individual samples, outcome and cleanup status. These
are functional injection checks, not statistical evidence of production resilience.
Observe gateway availability separately through F5 and Grafana while faults run.

Kill and memory-pressure helpers from the original repository remain available,
but this runner intentionally supports only bounded latency and exceptions.
Faults affect other concurrent requests to the selected pod during the experiment.
For process termination with SIGKILL, machine failure, or a lost port-forward,
automatic cleanup cannot be guaranteed. Reconnect to the same pod and disable:

```sh
curl --fail -X POST http://127.0.0.1:19090/actuator/chaosmonkey/disable
```

If the pod restarts, faults default to disabled. Remove `values-chaos.yaml` from
the Argo CD Application and push to remove instrumentation after experiments.

Reference: https://codecentric.github.io/chaos-monkey-spring-boot/latest/
