---
name: k8s-check-apps
description: >
  This skill should be used when the user asks to "check apps",
  "verify applications", "check deployed apps", "are apps healthy",
  "check traefik", "check pihole", "check homepage", "check headlamp",
  "check monitoring", or wants to verify per-application deployment health
  for the known homelab apps (Traefik, Pi-hole, Homepage, Headlamp,
  monitoring/Prometheus-Grafana). Standalone check for application
  deployment readiness.
---

# k8s-check-apps — Application Health

Verify that all deployed homelab applications have healthy Deployments,
StatefulSets, and DaemonSets with desired replicas matching ready replicas.
This skill is standalone and can be invoked independently or called by
`k8s-check`.

## Application targets

Check each of these applications in their respective namespaces:

| Application | Namespace | Deployment/StatefulSet |
|-------------|-----------|----------------------|
| Traefik | `traefik` | `traefik` (Deployment) |
| Pi-hole | `pihole` | `pihole` (Deployment) |
| Homepage | `homepage` | `homepage` (Deployment) |
| Headlamp | `headlamp` | `headlamp` (Deployment) |
| Monitoring | `monitoring` | `prometheus-*` (StatefulSet), `grafana` (Deployment) |

## Checks

### 1. Per-application deployment status

For each application namespace, run:

```bash
kubectl get deploy,sts,ds -n <namespace> -o wide
```

Check each workload:
- `READY` matches `DESIRED` (e.g. `1/1`, `3/3`) → **OK**
- `READY` < `DESIRED` → **CRIT** (not enough replicas serving traffic)
- `READY: 0/N` → **CRIT** (application is down)
- `UP-TO-DATE` < `DESIRED` → **WARN** (rollout in progress or stuck)

### 2. ReplicaSet detail

```bash
kubectl get rs -n <namespace> -o wide
```

If a deployment shows fewer ready replicas, check the ReplicaSet for:
- Pods in Pending/CrashLoopBackOff → include pod status in finding
- Old ReplicaSet still active → **WARN** (failed rollout)

### 3. Deep pod check (optional delegation)

If any application shows unhealthy replicas, invoke the `k8s-check-pods`
sub-skill via the Skill tool for detailed pod-level diagnostics in that
namespace. Record any additional findings under this check's discoveries.

### 4. Application-specific health endpoints (optional)

For Traefik:
```bash
kubectl -n traefik get svc traefik -o jsonpath='{.status.loadBalancer.ingress[0].ip}'; echo
```
Verify the load balancer IP is assigned. The service being reachable implies
the dashboard is accessible.

For Monitoring (Prometheus/Grafana):
```bash
kubectl -n monitoring get svc -o wide
```
Verify Prometheus (9090) and Grafana (3000) services are ClusterIP or NodePort
with endpoints.

## Discoveries output

Write `/tmp/k8s-check/k8s-check-apps/discoveries.md`:

```bash
mkdir -p /tmp/k8s-check/k8s-check-apps
```

Format:

```
## k8s-check-apps
**Overall:** 🟢 HEALTHY | 🟡 DEGRADED | 🔴 CRITICAL
**Summary:** <N> application(s) checked, <M> issue(s)
**Findings:**
- [OK] traefik: Deployment 1/1 Ready, LoadBalancer 10.98.0.200
- [OK] pihole: Deployment 1/1 Ready
- [OK] homepage: Deployment 1/1 Ready
- [OK] headlamp: Deployment 1/1 Ready
- [OK] monitoring/prometheus: StatefulSet 1/1 Ready
- [OK] monitoring/grafana: Deployment 1/1 Ready
- [CRIT] traefik/traefik: Deployment 0/1 Ready — CrashLoopBackOff
- [INFO] 6 workloads checked across 5 namespaces
```

Derive **Overall**:
- Any `[CRIT]` → 🔴 CRITICAL
- Any `[WARN]` → 🟡 DEGRADED
- All `[OK]` or `[INFO]` → 🟢 HEALTHY

Print a concise summary to the user, e.g.:

> ✅ **k8s-check-apps**: 6 workloads Ready across 5 namespaces — 🟢 HEALTHY
