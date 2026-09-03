---
name: k8s-check-pods
description: >
  This skill should be used when the user asks to "check pod health",
  "find failing pods", "are all pods running", "check for crashloops",
  "pod status", "find pending pods", or wants to inspect pod-level
  health across all namespaces. Standalone check for Kubernetes pod
  anomalies including CrashLoopBackOff, ImagePullBackOff, Pending, and
  high restart counts.
---

# k8s-check-pods — Pod Health

Inspect all pods across every namespace and identify anomalies. This skill
is standalone and can be invoked independently or called by `k8s-check`.

## Checks

### 1. All pods overview

```bash
kubectl get pods -A -o wide
```

Capture the full list. Count total pods, pods in each phase, and pods with
issues.

### 2. Non-running pods

```bash
kubectl get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded -o wide
```

Any pod not in `Running` or `Succeeded` phase is a candidate finding.

### 3. Pod status detail

For each pod, inspect the `STATUS` and `RESTARTS` columns:

- **CrashLoopBackOff** → **CRIT** — include the pod name, namespace, restart count,
  and last few log lines if available (`kubectl logs -n <ns> <pod> --tail=20`).
- **ImagePullBackOff** or **ErrImagePull** → **CRIT** — include image name and error.
- **CreateContainerConfigError** → **CRIT** — include error detail.
- **OOMKilled** (visible in restart reason) → **CRIT** — pod is hitting memory limits.
- **Pending** → **CRIT** — include scheduling reason from `kubectl describe pod`.
- **Terminating** (stuck, age > 5 min) → **WARN** — may have finalizer issues.
- **Running but restart count > 5** → **WARN** — indicates instability.
- **Running but not all containers ready** → **WARN**.

### 4. Scheduling failures (for Pending pods)

```bash
kubectl get pods -A --field-selector=status.phase=Pending -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}' | while read pod; do
  ns=$(echo "$pod" | cut -d/ -f1)
  name=$(echo "$pod" | cut -d/ -f2)
  echo "--- $ns/$name ---"
  kubectl describe pod "$name" -n "$ns" | grep -A5 "Events:"
done
```

Report scheduling failure reasons (insufficient resources, node affinity, PVC
binding, etc.) as part of the finding detail.

### 5. High-restart pods

```bash
kubectl get pods -A -o jsonpath='{range .items[*]}{range .status.containerStatuses[*]}{.restartCount}{" "}{.name}{" "}{.state}{"\n"}{end}{end}' 2>/dev/null | sort -rn | head -20
```

Any container with restart count > 10 → **WARN**; > 50 → **CRIT**.

## Discoveries output

Write `/tmp/k8s-check/k8s-check-pods/discoveries.md`:

```bash
mkdir -p /tmp/k8s-check/k8s-check-pods
```

Format:

```
## k8s-check-pods
**Overall:** 🟢 HEALTHY | 🟡 DEGRADED | 🔴 CRITICAL
**Summary:** <N> total pods, <M> issues (<K> critical, <L> warnings)
**Findings:**
- [OK] All pods Running and Ready
- [CRIT] traefik/traefik-xyz: CrashLoopBackOff — 12 restarts, exit code 137
- [WARN] monitoring/prometheus-xyz: restart count 8 (>5)
- [CRIT] pihole/pihole-xyz: Pending — Insufficient cpu
- [INFO] Metrics for 45 containers collected
```

Derive **Overall**:
- Any `[CRIT]` → 🔴 CRITICAL
- Any `[WARN]` → 🟡 DEGRADED
- All `[OK]` or `[INFO]` → 🟢 HEALTHY

Print a concise summary to the user, e.g.:

> ✅ **k8s-check-pods**: 45 pods Running, 0 issues — 🟢 HEALTHY
