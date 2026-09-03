---
name: k8s-check-flux
description: >
  This skill should be used when the user asks to "check flux status",
  "verify flux cd health", "are flux kustomizations ready", "check flux
  reconciliation", "flux health check", or wants to inspect the Flux CD
  deployment health. Standalone check for Flux controllers, Kustomizations,
  and Sources.
---

# k8s-check-flux — Flux CD Health

Verify that Flux CD controllers are installed and healthy, and that all
Kustomizations and Sources are reconciled without errors. This skill is
standalone and can be invoked independently or called by `k8s-check`.

## Checks

### 1. Flux controllers

```bash
flux check
```

Verify all Flux controllers are present and ready. If any controller is
missing or not ready → **CRIT** and stop (subsequent checks require Flux
controllers).

### 2. Kustomizations

```bash
flux get kustomizations
```

Inspect the `READY` column and `MESSAGE` column for each Kustomization:
- `READY: True` → **OK**
- `READY: False` with error message → **CRIT** (include the error in findings)
- `READY: False` with "applying" or progress message → **WARN** (reconciliation in progress)

```bash
flux get kustomizations -o json
```

Use JSON output to extract detailed status when the table output is ambiguous.

### 3. Sources — GitRepositories and HelmRepositories

```bash
flux get sources all
```

Check each source's `READY` status and `AGE`:
- `READY: True` → **OK**
- `READY: False` → **CRIT** (include the error message)
- Source age > expected interval (check `spec.interval`) → **WARN** (stale source)

```bash
flux get sources git -o json
flux get sources helm -o json
```

Use JSON output to inspect `status.artifact.lastUpdateTime` for staleness detection.

## Discoveries output

Write `/tmp/k8s-check/k8s-check-flux/discoveries.md`:

```bash
mkdir -p /tmp/k8s-check/k8s-check-flux
```

Format:

```
## k8s-check-flux
**Overall:** 🟢 HEALTHY | 🟡 DEGRADED | 🔴 CRITICAL
**Summary:** <N> Kustomizations, <M> Sources — <K> issue(s)
**Findings:**
- [OK] kustomization/flux-system: Ready
- [CRIT] kustomization/apps: Failed — Traefik HelmRelease namespace mismatch
- [OK] gitrepository/flux-system: Ready
- [OK] helmrepository/jetstack: Ready
- [INFO] kustomization/flux-system: Reconciliation in progress
```

Derive **Overall**:
- Any `[CRIT]` → 🔴 CRITICAL
- Any `[WARN]` → 🟡 DEGRADED
- All `[OK]` or `[INFO]` → 🟢 HEALTHY

Print a concise summary to the user, e.g.:

> ✅ **k8s-check-flux**: 11 Kustomizations Ready, 11 Sources Ready — 🟢 HEALTHY
