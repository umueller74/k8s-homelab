---
name: k8s-check-namespaces
description: >
  This skill should be used when the user asks to "check namespaces",
  "list kubernetes namespaces", "are namespaces healthy", "stuck
  namespaces", or wants to verify namespace status. Standalone check
  for Kubernetes namespace health.
---

# k8s-check-namespaces — Namespace Health

Verify that all Kubernetes namespaces are in a healthy state. This skill is
standalone and can be invoked independently or called by `k8s-check`.

## Checks

### 1. Namespace status

```bash
kubectl get namespaces
```

Inspect each namespace:
- `Active` → **OK**
- `Terminating` → flag as **WARN** if the namespace has been Terminating for
  more than 5 minutes (compare AGE or check deletion timestamp)

### 2. Stuck Terminating namespaces

```bash
kubectl get namespaces --field-selector=status.phase=Active -o name | wc -l
kubectl get namespaces --field-selector=status.phase!=Active -o name
```

Any namespace not in `Active` phase → **WARN** (stuck Terminating usually
indicates finalizers or resources that cannot be deleted).

### 3. Resource quotas (optional)

```bash
kubectl get resourcequota -A -o wide 2>/dev/null || echo "No resource quotas configured"
```

If resource quotas exist, check if any are hitting hard limits:
- `USED` near `HARD` (> 90%) → **WARN**
- At hard limit → **CRIT**

If no quotas are configured, note as INFO and continue.

## Discoveries output

Write `/tmp/k8s-check/k8s-check-namespaces/discoveries.md`:

```bash
mkdir -p /tmp/k8s-check/k8s-check-namespaces
```

Format:

```
## k8s-check-namespaces
**Overall:** 🟢 HEALTHY | 🟡 DEGRADED | 🔴 CRITICAL
**Summary:** <N> namespace(s), <M> issue(s)
**Findings:**
- [OK] namespace/default: Active
- [OK] namespace/traefik: Active
- [WARN] namespace/old-app: Terminating for 12 minutes — possible stuck finalizer
- [INFO] resource-quotas: No resource quotas configured
```

Derive **Overall**:
- Any `[CRIT]` → 🔴 CRITICAL
- Any `[WARN]` → 🟡 DEGRADED
- All `[OK]` or `[INFO]` → 🟢 HEALTHY

Print a concise summary to the user, e.g.:

> ✅ **k8s-check-namespaces**: 9 namespaces Active — 🟢 HEALTHY
