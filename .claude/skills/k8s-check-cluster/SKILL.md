---
name: k8s-check-cluster
description: >
  This skill should be used when the user asks to "check cluster nodes",
  "verify cluster health", "check node status", "is the cluster reachable",
  "check kubernetes nodes", or wants to inspect cluster connectivity and
  node-level health. Standalone health check for Kubernetes cluster basics.
---

# k8s-check-cluster — Cluster Connectivity & Node Health

Check that the Kubernetes cluster is reachable and that all nodes and core
system components are healthy. This skill is standalone and can be invoked
independently or called by the `k8s-check` orchestrator.

## Checks

### 1. Cluster reachability

```bash
kubectl cluster-info
```

Verify the API server is reachable and responding. If this fails, report
CRITICAL and stop — no further checks can proceed.

### 2. Node status

```bash
kubectl get nodes -o wide
```

For each node, verify:
- **Ready** condition is `True`.
- No pressure conditions (MemoryPressure, DiskPressure, PIDPressure) are `True`.
- The node is not `Schedulable: false` (cordoned) unless expected.

```bash
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .status.conditions[*]}{.type}={.status}{"\t"}{end}{"\n"}{end}'
```

Flag any node where:
- `Ready` is not `True` → **CRIT**
- `MemoryPressure`, `DiskPressure`, or `PIDPressure` is `True` → **CRIT**
- Node is marked `unschedulable` → **WARN** (may be intentional during maintenance)

### 3. Node capacity (optional, if metrics available)

```bash
kubectl top nodes 2>/dev/null || echo "Metrics server not available"
```

If metrics are available, flag nodes where CPU or memory usage exceeds 90% → **WARN**.
If metrics are not available, note this as INFO and continue.

### 4. Core system components

```bash
kubectl get pods -n kube-system -o wide
```

Check that all pods in `kube-system` are `Running` and `Ready`. Flag:
- Any pod not in `Running` or `Succeeded` phase → **CRIT**
- Any pod with restart count > 5 → **WARN**
- Pods in `Pending` state → **CRIT**

## Discoveries output

After completing all checks, write the discoveries file:

```bash
mkdir -p /tmp/k8s-check/k8s-check-cluster
```

Write `/tmp/k8s-check/k8s-check-cluster/discoveries.md` using this format:

```
## k8s-check-cluster
**Overall:** 🟢 HEALTHY | 🟡 DEGRADED | 🔴 CRITICAL
**Summary:** <N> node(s), <M> core pods, <K> issue(s)
**Findings:**
- [OK] cluster-reachability: API server reachable
- [OK] node/<name>: Ready, no pressure
- [CRIT] node/<name>: NotReady — <reason>
- [WARN] pod/<name>: restart count 12 (>5)
- [INFO] metrics: Metrics server not available — capacity checks skipped
```

Derive **Overall** status:
- Any `[CRIT]` finding → 🔴 CRITICAL
- Any `[WARN]` finding → 🟡 DEGRADED
- All `[OK]` or `[INFO]` → 🟢 HEALTHY

Also print a concise summary to the user, e.g.:

> ✅ **k8s-check-cluster**: 3 nodes Ready, 12 core pods healthy — 🟢 HEALTHY

or

> 🔴 **k8s-check-cluster**: 1 node NotReady — 🔴 CRITICAL
