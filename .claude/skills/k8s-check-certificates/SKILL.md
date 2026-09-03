---
name: k8s-check-certificates
description: >
  This skill should be used when the user asks to "check certificates",
  "verify tls certs", "are certificates valid", "check cert expiry",
  "cert-manager health", "check https certificates", or wants to inspect
  cert-manager certificate status, expiry, and issuer health. Standalone
  check for cert-manager managed certificates.
---

# k8s-check-certificates — Certificate Health

Verify that all cert-manager managed certificates are issued, valid, and not
near expiry. This skill is standalone and can be invoked independently or
called by `k8s-check`.

## Checks

### 1. Certificate status

```bash
kubectl get certificates -A -o wide
```

For each certificate:
- `READY: True` → **OK** — certificate is issued and valid
- `READY: False` → **CRIT** — certificate failed to issue or renew

### 2. Certificate details and expiry

```bash
kubectl get certificates -A -o json
```

For each certificate, check:
- `status.conditions` — Ready condition status and last transition time
- `status.notAfter` — expiry timestamp. Compute time-to-expiry:
  - < 7 days → **CRIT** (imminent expiry)
  - < 30 days → **WARN** (renewal should happen soon)
  - > 30 days → **OK**
- `status.secretName` — referenced Secret should exist

### 3. Secret existence

```bash
kubectl get certificates -A -o jsonpath='{range .items[*]}{.status.secretName}{" "}{.metadata.namespace}{"\n"}{end}'
```

For each secret name and namespace, verify the Secret exists:

```bash
kubectl get secret <name> -n <namespace> -o name 2>/dev/null || echo "MISSING"
```

Missing secret for a Ready certificate → **CRIT**.

### 4. Issuers and ClusterIssuers

```bash
kubectl get clusterissuers -o wide 2>/dev/null || echo "No ClusterIssuers"
kubectl get issuers -A -o wide 2>/dev/null || echo "No Issuers"
```

Check each issuer:
- `READY: True` → **OK**
- `READY: False` → **CRIT** (no certificates can be issued via this issuer)

## Discoveries output

Write `/tmp/k8s-check/k8s-check-certificates/discoveries.md`:

```bash
mkdir -p /tmp/k8s-check/k8s-check-certificates
```

Format:

```
## k8s-check-certificates
**Overall:** 🟢 HEALTHY | 🟡 DEGRADED | 🔴 CRITICAL
**Summary:** <N> certificate(s), <M> issue(s)
**Findings:**
- [OK] traefik/wildcard-homelab: Ready, expires in 82 days
- [CRIT] traefik/old-cert: NotReady — challenges failing: DNS-01 timeout
- [WARN] traefik/wildcard-homelab: Secret exists but last renewal was 60 days ago
- [OK] clusterissuer/letsencrypt-production: Ready
- [INFO] 2 Certificate(s), 1 ClusterIssuer checked
```

Derive **Overall**:
- Any `[CRIT]` → 🔴 CRITICAL
- Any `[WARN]` → 🟡 DEGRADED
- All `[OK]` or `[INFO]` → 🟢 HEALTHY

Print a concise summary to the user, e.g.:

> ✅ **k8s-check-certificates**: 1 Certificate Ready, expires in 82 days — 🟢 HEALTHY
