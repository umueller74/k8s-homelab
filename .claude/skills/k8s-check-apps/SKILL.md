---
name: k8s-check-apps
description: >
  This skill should be used when the user asks to "check apps",
  "verify applications", "are apps healthy", "check <app name>"
  (e.g. traefik, pihole, paperless, homeassistant, gitea), or wants to verify
  that every Deployment, StatefulSet and DaemonSet in the cluster runs its
  desired replicas. Standalone check for application deployment readiness.
---

# k8s-check-apps — Application Health

Verify that every workload in the cluster (Deployments, StatefulSets,
DaemonSets) runs its desired replicas and has finished its rollout. This skill
is standalone and can be invoked independently or called by `k8s-check`.

## Targets

The cluster is the inventory: check every workload in every namespace. Each
app has its own Flux Kustomization in `clusters/production/<app>.yaml`, so
`flux get kustomizations` lists the apps, but the workloads themselves are
what this check grades.

Placements a hard-coded list gets wrong:

| App | Where it actually runs |
|-----|------------------------|
| Traefik | Deployment `traefik` in `flux-system` (its HelmRelease lives there); LoadBalancer Service `flux-system/traefik` |
| Pi-hole | StatefulSet `pihole` in `pihole` (1 replica, one PVC per replica) |
| Grafana | Deployment `prometheus-grafana` in `monitoring` |
| Hermes Agent | No workload: runs on raspi2. `hermes-agent/hermes-agent` is a selectorless Service backed by the manual EndpointSlice `apps/base/hermes-agent/endpointslice.yaml` |

## Checks

### 1. Workload readiness

```bash
kubectl get deploy -A --no-headers
kubectl get sts -A --no-headers
kubectl get ds -A --no-headers
```

Grade each workload:
- `READY` equals desired (e.g. `1/1`, `3/3`) and `UP-TO-DATE` equals desired → **OK**
- `READY: 0/N` → **CRIT** (application is down)
- `READY` below desired → **CRIT** (not enough replicas serving traffic)
- `UP-TO-DATE` below desired → **WARN** (rollout in progress or stuck)
- DaemonSet `READY` or `UP-TO-DATE` below `DESIRED` → **WARN**; `READY: 0` → **CRIT**

A workload scaled to `0/0` on purpose (e.g. a suspended app) is **INFO**;
check whether its Flux Kustomization is suspended.

### 2. Stuck rollouts

Count active ReplicaSets per owning Deployment (several Deployments share a
namespace, so group by owner, never by namespace):

```bash
kubectl get rs -A -o json | jq -r '[.items[] | select(.status.replicas>0)
  | "\(.metadata.namespace)/\(.metadata.ownerReferences[0].name)"]
  | group_by(.) | map(select(length>1)) | .[] | "\(.[0]) x\(length)"'
```

Any Deployment with more than one active ReplicaSet → **WARN** (failed or
stuck rollout). For a Deployment with missing replicas, list its pods and
include their status (Pending, CrashLoopBackOff, ...) in the finding.

### 3. Deep pod check (optional delegation)

If any workload shows unhealthy replicas, invoke the `k8s-check-pods`
sub-skill via the Skill tool for pod-level diagnostics in that namespace.
Record its additional findings under this check's discoveries.

### 4. Externally hosted apps

```bash
kubectl get endpointslices -n hermes-agent -o wide
```

The EndpointSlice must list an address with `ready: true` → **OK**; missing →
**WARN** (the IngressRoute `hermes.homelab.cs-ol.de` has no backend). The
legacy `Endpoints` object of a selectorless Service stays empty; that is
expected.

### 5. Entry points

```bash
kubectl -n flux-system get svc traefik -o jsonpath='{.status.loadBalancer.ingress[0].ip}'; echo
kubectl -n monitoring get endpointslices -l 'kubernetes.io/service-name in (prometheus-kube-prometheus-prometheus,prometheus-grafana)'
```

Traefik needs a LoadBalancer IP (10.98.0.200) → else **CRIT**. Prometheus
(9090) and Grafana must each have a ready endpoint → else **WARN**.

## Discoveries output

Write `/tmp/k8s-check/k8s-check-apps/discoveries.md`:

```bash
mkdir -p /tmp/k8s-check/k8s-check-apps
```

Format: one finding per unhealthy workload; group healthy ones per app or
layer so the file stays scannable.

```
## k8s-check-apps
**Overall:** 🟢 HEALTHY | 🟡 DEGRADED | 🔴 CRITICAL
**Summary:** <N> workloads (<D> Deployments, <S> StatefulSets, <DS> DaemonSets) across <M> namespaces, <K> issue(s)
**Findings:**
- [OK] traefik: Deployment flux-system/traefik 1/1 Ready, LoadBalancer 10.98.0.200
- [OK] pihole: StatefulSet pihole 1/1 Ready
- [OK] monitoring: prometheus 1/1, alertmanager 1/1, grafana 1/1, node-exporter 3/3
- [OK] paperless: app, ingest, db, redis, tika, gotenberg all Ready
- [OK] hermes-agent: EndpointSlice → 192.168.0.54:9119 ready
- [CRIT] gitea/gitea: Deployment 0/1 Ready — CrashLoopBackOff
- [WARN] homepage/homepage: 2 active ReplicaSets — rollout stuck
- [INFO] 63 workloads checked across 27 namespaces
```

Derive **Overall**:
- Any `[CRIT]` → 🔴 CRITICAL
- Any `[WARN]` → 🟡 DEGRADED
- All `[OK]` or `[INFO]` → 🟢 HEALTHY

Print a concise summary to the user, e.g.:

> ✅ **k8s-check-apps**: 63 workloads Ready across 27 namespaces — 🟢 HEALTHY
