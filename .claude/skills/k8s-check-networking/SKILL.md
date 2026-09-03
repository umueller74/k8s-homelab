---
name: k8s-check-networking
description: >
  This skill should be used when the user asks to "check networking",
  "verify ingress", "check load balancer", "check services", "metalb
  status", "check endpoints", "ingress health", or wants to inspect
  Kubernetes networking including Ingress/IngressRoute, LoadBalancer
  services, and Endpoints. Standalone check for network connectivity.
---

# k8s-check-networking — Network Health

Verify Ingress resources, LoadBalancer services (MetalLB), and service
Endpoints are healthy. This skill is standalone and can be invoked
independently or called by `k8s-check`.

## Checks

### 1. Ingress resources

```bash
kubectl get ingress -A -o wide
```

For each Ingress:
- `CLASS` is set and correct (traefik) → **OK**
- Missing class or unknown class → **WARN**
- `ADDRESS` populated → **OK**
- `ADDRESS` empty or `<none>` → **WARN** (no external IP assigned)
- `READY` conditions (if available) → check for errors

### 2. Traefik IngressRoutes (CRD)

```bash
kubectl get ingressroute -A -o wide 2>/dev/null || echo "No IngressRoute CRDs found"
```

For each IngressRoute:
- Route has matching service and is bound → **OK**
- Route references missing service → **CRIT**
- Route present but not configured → **WARN**

### 3. LoadBalancer services (MetalLB)

```bash
kubectl get svc -A --field-selector spec.type=LoadBalancer -o wide
```

For each LoadBalancer service:
- `EXTERNAL-IP` is a valid IP (not `<pending>` or `<none>`) → **OK**
- `EXTERNAL-IP` is `<pending>` → **CRIT** (MetalLB not assigning; may indicate
  IP pool exhaustion or MetalLB not running)
- `EXTERNAL-IP` is `<none>` → **WARN**

```bash
kubectl get svc -A --field-selector spec.type=LoadBalancer -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\t"}{.spec.clusterIP}{"\t"}{.status.loadBalancer.ingress[0].ip}{"\n"}{end}'
```

### 4. Service Endpoints

```bash
kubectl get endpoints -A
```

For each Service:
- Endpoints populated (at least one address) → **OK**
- Endpoints empty (no addresses) → **WARN** (service exists but no backing pods)
- Headless services with endpoints → **OK**

Focus on services in user-facing namespaces (traefik, pihole, monitoring,
homepage, headlamp) rather than kube-system internals.

### 5. DNS resolution (optional quick check)

```bash
kubectl run dns-check --rm -i --restart=Never --image=busybox:1.36 -- nslookup kubernetes.default.svc.cluster.local 2>/dev/null || echo "DNS check skipped"
```

If this works → **OK**. If it fails → **WARN** (coreDNS may have issues).

## Discoveries output

Write `/tmp/k8s-check/k8s-check-networking/discoveries.md`:

```bash
mkdir -p /tmp/k8s-check/k8s-check-networking
```

Format:

```
## k8s-check-networking
**Overall:** 🟢 HEALTHY | 🟡 DEGRADED | 🔴 CRITICAL
**Summary:** <N> ingress(es), <M> LoadBalancer(s), <K> issue(s)
**Findings:**
- [OK] ingress/traefik-dashboard: traefik class, address 10.98.0.200
- [WARN] ingress/old-app: ADDRESS empty — no external IP
- [OK] svc/traefik/traefik: LoadBalancer, external IP 10.98.0.200
- [CRIT] svc/legacy/app: LoadBalancer PENDING — no external IP assigned
- [WARN] endpoints/legacy/app: No endpoints — no backing pods
- [INFO] DNS resolution: kubernetes.default.svc.cluster.local → OK
```

Derive **Overall**:
- Any `[CRIT]` → 🔴 CRITICAL
- Any `[WARN]` → 🟡 DEGRADED
- All `[OK]` or `[INFO]` → 🟢 HEALTHY

Print a concise summary to the user, e.g.:

> ✅ **k8s-check-networking**: 5 ingresses, 3 LoadBalancers with IPs — 🟢 HEALTHY
