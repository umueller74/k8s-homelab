---
name: k8s-check-helmreleases
description: >
  This skill should be used when the user asks to "check helm releases",
  "verify helmreleases", "are helm charts deployed", "check helm status",
  "flux helmrelease health", or wants to inspect HelmRelease readiness
  across all namespaces. Standalone check for Flux-managed HelmReleases.
---

# k8s-check-helmreleases — HelmRelease Health

Verify that all Flux-managed HelmReleases are reconciled and healthy. This
skill is standalone and can be invoked independently or called by `k8s-check`.

## Checks

### 1. HelmRelease status overview

```bash
flux get helmreleases --all-namespaces
```

Inspect the `READY`, `STATUS`, and `AGE` columns:
- `READY: True` → **OK** — include the applied chart version
- `READY: False` with error → **CRIT** — include the status message
- `READY: False` with "reconciling" → **WARN** — upgrade or install in progress

### 2. Detailed HelmRelease status

```bash
flux get helmreleases --all-namespaces -o json
```

For each HelmRelease, check:
- `status.conditions` — any condition with `status: False` and type `Ready` → **CRIT**
- `status.lastAppliedRevision` vs `spec.chart.spec.version` — version mismatch
  after a failed upgrade → **WARN**
- `status.upgrade.failures` — count of recent failures → **WARN** if > 0

### 3. HelmRepository sources

```bash
flux get sources helm --all-namespaces
```

Note: this is a supplementary check; the primary source check is in
`k8s-check-flux`. Include HelmRepository findings here only if they
directly affect a HelmRelease (e.g. a HelmRelease referencing a failed
HelmRepository).

## Discoveries output

Write `/tmp/k8s-check/k8s-check-helmreleases/discoveries.md`:

```bash
mkdir -p /tmp/k8s-check/k8s-check-helmreleases
```

Format:

```
## k8s-check-helmreleases
**Overall:** 🟢 HEALTHY | 🟡 DEGRADED | 🔴 CRITICAL
**Summary:** <N> HelmRelease(s), <M> Ready, <K> failed
**Findings:**
- [OK] traefik/metallb: Ready (v0.15.3)
- [OK] traefik/traefik: Ready (v41.4.0)
- [CRIT] traefik/traefik-old: Failed — Helm install failed: namespace mismatch
- [INFO] 6 HelmRelease(s) checked across 3 namespaces
```

Derive **Overall**:
- Any `[CRIT]` → 🔴 CRITICAL
- Any `[WARN]` → 🟡 DEGRADED
- All `[OK]` or `[INFO]` → 🟢 HEALTHY

Print a concise summary to the user, e.g.:

> ✅ **k8s-check-helmreleases**: 6 HelmReleases Ready — 🟢 HEALTHY
