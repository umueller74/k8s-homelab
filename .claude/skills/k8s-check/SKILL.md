---
name: k8s-check
description: >
  This skill should be used when the user asks to "check cluster health",
  "run k8s health check", "verify cluster status", "check everything",
  "full health check", "audit the cluster", or wants an overview of
  Kubernetes cluster and Flux CD health. Only orchestrates sub-skills;
  runs no direct cluster commands itself.
---

# k8s-check — Cluster Health Orchestrator

Run a comprehensive health check of the Kubernetes cluster and Flux CD deployment.
This skill does **not** execute cluster commands directly — it delegates to
individual `k8s-check-*` sub-skills and aggregates their discoveries into a single
consolidated report.

## Prerequisites

- `kubectl` and `flux` must be configured with access to the target cluster.
- The cluster context should be active (verify with `kubectl config current-context`).

## Execution flow

### 1. Prepare the work directory

```bash
mkdir -p /tmp/k8s-check
```

This shared directory is where every sub-skill writes its discoveries. Previous run
data is overwritten on each execution.

### 2. Determine the current date for the report filename

```bash
date -u +%Y-%m-%d
```

### 3. Invoke each sub-skill

Invoke the following sub-skills via the Skill tool, **in order**. Each one performs
its checks and writes a discovery file to `/tmp/k8s-check/<skill-name>/discoveries.md`.

1. `k8s-check-cluster` — cluster connectivity, nodes, core components
2. `k8s-check-flux` — Flux controllers, Kustomizations, Sources
3. `k8s-check-namespaces` — namespace status
4. `k8s-check-pods` — pod health anomalies
5. `k8s-check-helmreleases` — HelmRelease readiness
6. `k8s-check-certificates` — cert-manager certificates and issuers
7. `k8s-check-storage` — Longhorn volumes, PVCs, StorageClasses
8. `k8s-check-networking` — Ingress, LoadBalancer services, Endpoints
9. `k8s-check-apps` — per-application replica health

Wait for each sub-skill to complete before invoking the next. Record the summary each
one prints.

### 4. Read all discovery files

```bash
cat /tmp/k8s-check/k8s-check-cluster/discoveries.md
cat /tmp/k8s-check/k8s-check-flux/discoveries.md
cat /tmp/k8s-check/k8s-check-namespaces/discoveries.md
cat /tmp/k8s-check/k8s-check-pods/discoveries.md
cat /tmp/k8s-check/k8s-check-helmreleases/discoveries.md
cat /tmp/k8s-check/k8s-check-certificates/discoveries.md
cat /tmp/k8s-check/k8s-check-storage/discoveries.md
cat /tmp/k8s-check/k8s-check-networking/discoveries.md
cat /tmp/k8s-check/k8s-check-apps/discoveries.md
```

### 5. Derive overall status

Compute the **overall cluster status** as the worst status across all sub-skills:

- Any sub-skill at **CRITICAL** → overall = 🔴 CRITICAL
- Any sub-skill at **DEGRADED** → overall = 🟡 DEGRADED
- All sub-skills **HEALTHY** → overall = 🟢 HEALTHY

### 6. Assemble and write the final report

Read `references/report-template.md` for the canonical report layout. Populate every
section from the discovery file data. Write the report to:

```
report/k8s-check-<YYYY-MM-DD>.md
```

Create the `report/` directory if it does not exist. If a report with the same date
already exists, overwrite it.

### 7. Print the report

Render the full report content to the user in the conversation, followed by the
file path where it was saved.

## Discovery file format

Every sub-skill writes a single file `discoveries.md` in the following format:

```
## k8s-check-<part>
**Overall:** 🟢 HEALTHY | 🟡 DEGRADED | 🔴 CRITICAL
**Summary:** <one-line counts>
**Findings:**
- [OK] <component>: <detail>
- [WARN] <component>: <detail>
- [CRIT] <component>: <detail>
- [INFO] <component>: <detail>
```

## Reporting

Follow the report structure in `references/report-template.md`. The report includes:
header (cluster context, timestamp, overall status), summary counts, issues grouped
by severity (CRITICAL → WARNING → INFO), healthy components, detailed per-check
findings, additional observations, and next steps or recommendations.

---

*For the canonical report layout, see `references/report-template.md`.*
