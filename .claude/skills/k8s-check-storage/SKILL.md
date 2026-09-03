---
name: k8s-check-storage
description: >
  This skill should be used when the user asks to "check storage",
  "verify longhorn", "check pvcs", "check persistent volumes",
  "storage health", "longhorn status", "check disk usage", or wants to
  inspect Longhorn storage health, PVC/PV status, and StorageClasses.
  Standalone check for Kubernetes storage layer.
---

# k8s-check-storage — Storage Health

Verify Longhorn distributed storage, PersistentVolumeClaims, and
StorageClasses are healthy. This skill is standalone and can be invoked
independently or called by `k8s-check`.

## Checks

### 1. Longhorn nodes

```bash
kubectl -n longhorn-system get nodes -o wide 2>/dev/null || echo "Longhorn not found in longhorn-system namespace"
```

Check each Longhorn node:
- `READY: True` and `SCHEDULABLE: True` → **OK**
- Not ready or not schedulable → **CRIT**

If Longhorn is not installed, report as INFO and skip remaining storage checks.

### 2. Longhorn volumes

```bash
kubectl -n longhorn-system get volumes -o wide 2>/dev/null
```

For each volume:
- `STATE: healthy` and `ROBUSTNESS: healthy` → **OK**
- `STATE: degraded` → **WARN** (fewer replicas than desired)
- `STATE: faulted` → **CRIT** (volume data at risk)
- `REPLICAS` count less than expected → **WARN**

```bash
kubectl -n longhorn-system get volumes -o json 2>/dev/null
```

Use JSON for detailed replica status per volume.

### 3. StorageClasses

```bash
kubectl get storageclass
```

Verify:
- At least one default StorageClass exists → **CRIT** if missing
- Longhorn is the default → **OK**; other default → **INFO** (note which)
- Check reclaim policy and volume binding mode

### 4. PersistentVolumeClaims

```bash
kubectl get pvc -A -o wide
```

For each PVC:
- `STATUS: Bound` → **OK**
- `STATUS: Pending` → **WARN** (may be waiting for Longhorn or storage provisioning)
- `STATUS: Lost` → **CRIT** (underlying PV is gone)

### 5. PersistentVolumes

```bash
kubectl get pv
```

For each PV:
- `STATUS: Bound` → **OK**
- `STATUS: Released` → **WARN** (PV not reclaimed)
- `STATUS: Failed` → **CRIT**

## Discoveries output

Write `/tmp/k8s-check/k8s-check-storage/discoveries.md`:

```bash
mkdir -p /tmp/k8s-check/k8s-check-storage
```

Format:

```
## k8s-check-storage
**Overall:** 🟢 HEALTHY | 🟡 DEGRADED | 🔴 CRITICAL
**Summary:** <N> volume(s), <M> PVC(s), <K> issue(s)
**Findings:**
- [OK] longhorn-node/node1: Ready, Schedulable
- [OK] longhorn-volume/pvc-abc: healthy, 3/3 replicas
- [WARN] longhorn-volume/pvc-xyz: degraded, 2/3 replicas
- [OK] storageclass/longhorn: Default, Delete reclaim
- [WARN] pihole/pihole-data: PVC Pending — waiting for volume
- [CRIT] old-app/data: PVC Lost
- [INFO] 8 PVC(s), 6 PV(s), 3 Longhorn volume(s) checked
```

Derive **Overall**:
- Any `[CRIT]` → 🔴 CRITICAL
- Any `[WARN]` → 🟡 DEGRADED
- All `[OK]` or `[INFO]` → 🟢 HEALTHY

Print a concise summary to the user, e.g.:

> ✅ **k8s-check-storage**: 3 Longhorn volumes healthy, 8 PVCs Bound — 🟢 HEALTHY
