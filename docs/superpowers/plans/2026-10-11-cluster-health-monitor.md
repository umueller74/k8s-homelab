# Cluster Health Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python monitor on raspi1 that checks Flux objects first, drills into workloads/pods only where something is unhealthy, and reports changes to Telegram.

**Architecture:** Pure check functions take plain dicts (parsed Kubernetes JSON) and return `Problem` objects; a thin collector fetches data via the `kubernetes` client; `decide()` turns problems + saved state into the messages to send. Part A (k8s-homelab#187) adds read-only RBAC; Part B (homelab#144) holds the code and the Ansible role.

**Tech Stack:** Python 3.11+, `kubernetes`, `PyYAML`, `requests`, pytest, systemd timer, Ansible, Flux + SOPS.

**Spec:** the agreed design in k8s-homelab#187 (and homelab#144 for the host side).

## Global Constraints

- Runs on raspi1 (Debian 13, aarch64), outside the cluster; systemd timer every 15 min.
- Read-only: ClusterRole has `get/list` only and **no access to Secrets**.
- No container logs are read or sent.
- Telegram message: HTML parse mode, one message per run, max 4096 characters.
- Not Ready counts as a problem only after 10 min (`not_ready_minutes`); certificates warn at < 14 days (`cert_expiry_days`).
- Alert only when the problem set changes (plus "recovered"); heartbeat daily from 08:00; API unreachable -> one alert, repeat hourly.
- Telegram send: 3 attempts, then exit non-zero; state is saved only after a successful send.
- Config: YAML; state: JSON.
- Never commit to `main`; issue -> branch `<issue>-<name>` -> PR (CLAUDE.md of each repo).
- Secrets only as `*.sops.yaml` (k8s-homelab) or in the Ansible vault (homelab). Never print tokens.

## Review Focus

- Flux object with no `status.conditions` yet (just created): must not crash; treat as "not ready" only once older than the threshold (use `metadata.creationTimestamp`). Test in Task 2.
- Kustomization with no `status.inventory` (failed first apply): drill-down must return no children, not raise. Test in Task 3.
- Namespace/resource names containing HTML characters or very long Flux error messages: output must be escaped and the message truncated below 4096 chars, never cut inside an HTML tag. Test in Task 5.
- Corrupt or missing state file: treated as empty state (no crash, first run reports everything). Test in Task 6.
- A resolved problem plus a new one in the same run: one message listing both. Test in Task 6.
- Telegram returns HTTP 429/5xx or the network is down: retried 3 times, then raise, state untouched. Test in Task 7.

---

# Part A - k8s-homelab (issue #187): RBAC

Branch `187-cluster-health-rbac`. Follow the CLAUDE.md workflow (pull main, branch, PR with `Closes #187` only after Part B is deployed; use `Refs #187` for this PR).

### Task A1: ServiceAccount, ClusterRole, token Secret

**Files:**
- Create: `apps/base/cluster-health/kustomization.yaml`
- Create: `apps/base/cluster-health/namespace.yaml`
- Create: `apps/base/cluster-health/rbac.yaml`
- Create: `apps/production/cluster-health/kustomization.yaml`
- Create: `clusters/production/cluster-health.yaml`
- Modify: `apps/production/kustomization.yaml` only if `smtp-relay` is listed there (Step 1 shows which); otherwise leave it alone.

**Interfaces:**
- Produces: ServiceAccount `cluster-health` in namespace `cluster-health`; Secret `cluster-health-token` (type `kubernetes.io/service-account-token`) whose `token` and `ca.crt` the Ansible role later reads.

- [ ] **Step 1: Mirror the smtp-relay wiring**

Run: `cat apps/production/smtp-relay/kustomization.yaml; grep -n smtp-relay apps/production/kustomization.yaml clusters/production/*.yaml`
Expected: see how base, overlay and Flux Kustomization connect; copy that shape.

- [ ] **Step 2: Write the manifests**

`apps/base/cluster-health/namespace.yaml`:
```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: cluster-health
```

`apps/base/cluster-health/rbac.yaml`:
```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: cluster-health
  namespace: cluster-health
---
apiVersion: v1
kind: Secret
metadata:
  name: cluster-health-token
  namespace: cluster-health
  annotations:
    kubernetes.io/service-account.name: cluster-health
type: kubernetes.io/service-account-token
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: cluster-health-read
rules:
  - apiGroups: ["kustomize.toolkit.fluxcd.io", "helm.toolkit.fluxcd.io", "source.toolkit.fluxcd.io"]
    resources: ["*"]
    verbs: ["get", "list"]
  - apiGroups: [""]
    resources: ["nodes", "pods", "events", "persistentvolumeclaims", "namespaces"]
    verbs: ["get", "list"]
  - apiGroups: ["apps"]
    resources: ["deployments", "statefulsets", "daemonsets"]
    verbs: ["get", "list"]
  - apiGroups: ["cert-manager.io"]
    resources: ["certificates"]
    verbs: ["get", "list"]
  - apiGroups: ["longhorn.io"]
    resources: ["volumes"]
    verbs: ["get", "list"]
  - nonResourceURLs: ["/version", "/readyz"]
    verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: cluster-health-read
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-health-read
subjects:
  - kind: ServiceAccount
    name: cluster-health
    namespace: cluster-health
```

Note: the token Secret is filled in by the cluster's token controller, so no secret value is in Git and no SOPS file is needed. The Ansible role fetches the token from the live Secret (Task B9).

- [ ] **Step 3: Flux Kustomization**

`clusters/production/cluster-health.yaml` (same shape as `smtp-relay.yaml`, `path: ./apps/production/cluster-health`, `dependsOn: infrastructure`, no `decryption` block needed).

- [ ] **Step 4: Validate**

Run: `kubectl kustomize apps/production/cluster-health | kubeconform -ignore-missing-schemas -`
Expected: no errors.

- [ ] **Step 5: Commit, push, PR, then merge and verify**

```bash
git checkout -b 187-cluster-health-rbac
git add apps/base/cluster-health apps/production/cluster-health clusters/production/cluster-health.yaml
git commit -m "#187 cluster-health: read-only ServiceAccount and ClusterRole"
git push -u origin 187-cluster-health-rbac
gh pr create --title "Refs #187: cluster-health read-only RBAC" --body "Cluster side of the health monitor. No secret values in Git: the token Secret is minted by the cluster."
git checkout main && git pull origin main
```
After merge: `flux reconcile kustomization cluster-health --with-source`, then
`kubectl auth can-i list secrets --as=system:serviceaccount:cluster-health:cluster-health -A` -> must print `no`;
`... can-i list kustomizations.kustomize.toolkit.fluxcd.io ...` -> `yes`.

---

# Part B - homelab (issue #144)

Branch `144-cluster-health-monitor`. Layout:

```
roles/cluster-health/
  defaults/main.yml
  tasks/main.yml
  handlers/main.yml
  templates/{config.yaml.j2,env.j2,cluster-health.service.j2,cluster-health.timer.j2,kubeconfig.j2}
  files/cluster_health/{__init__,model,config,flux,drill,independent,report,state,telegram,collect,main}.py
  files/requirements.txt
  tests/{conftest.py,test_flux.py,test_drill.py,test_independent.py,test_report.py,test_state.py,test_telegram.py}
playbooks/setup_host_raspi1.yml  (add role)
```
Run tests from `roles/cluster-health/files` with `python -m pytest ../tests -v` (create a venv: `python3 -m venv /tmp/chvenv && /tmp/chvenv/bin/pip install pytest pyyaml requests kubernetes`).

### Task B1: Model and config

**Files:** Create `files/cluster_health/__init__.py` (empty), `model.py`, `config.py`, `tests/conftest.py`, `tests/test_config.py`.

**Interfaces:**
- Produces: `Problem(key: str, severity: str, title: str, detail: str = "", parent: str | None = None)` (frozen dataclass; severity `"error"|"warn"`); `Config` dataclass with defaults `not_ready_minutes=10, pending_minutes=5, restart_threshold=5, cert_expiry_days=14, ignore_suspended=[]`, `Config.load(path) -> Config`; helper `parse_time(s: str) -> datetime` (aware UTC) and `age_minutes(ts: str, now: datetime) -> float` in `model.py`.

- [ ] **Step 1: Write the failing test** (`tests/test_config.py`)

```python
from cluster_health.config import Config
from cluster_health.model import Problem, age_minutes, parse_time
from datetime import datetime, timezone

def test_defaults_when_file_missing(tmp_path):
    c = Config.load(tmp_path / "nope.yaml")
    assert c.not_ready_minutes == 10 and c.cert_expiry_days == 14

def test_yaml_overrides(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("not_ready_minutes: 3\nignore_suspended: [flux-system/foo]\n")
    c = Config.load(p)
    assert c.not_ready_minutes == 3 and c.ignore_suspended == ["flux-system/foo"]

def test_age_minutes():
    now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    assert age_minutes("2026-10-11T11:30:00Z", now) == 30

def test_problem_is_hashable():
    assert {Problem("a", "error", "t")}
```

`tests/conftest.py`:
```python
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "files"))
```

- [ ] **Step 2: Run, expect FAIL** - `python -m pytest roles/cluster-health/tests/test_config.py -v` -> ImportError.

- [ ] **Step 3: Implement**

`model.py`:
```python
from dataclasses import dataclass
from datetime import datetime, timezone

@dataclass(frozen=True)
class Problem:
    key: str
    severity: str
    title: str
    detail: str = ""
    parent: str | None = None

def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)

def age_minutes(ts: str, now: datetime) -> float:
    return (now - parse_time(ts)).total_seconds() / 60
```
`config.py`:
```python
from dataclasses import dataclass, field, fields
from pathlib import Path
import yaml

@dataclass
class Config:
    not_ready_minutes: int = 10
    pending_minutes: int = 5
    restart_threshold: int = 5
    cert_expiry_days: int = 14
    ignore_suspended: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path) -> "Config":
        p = Path(path)
        data = yaml.safe_load(p.read_text()) if p.exists() else {}
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})
```

- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `git commit -m "#144 cluster-health: model and config"`

### Task B2: Flux-level checks

**Files:** Create `files/cluster_health/flux.py`, `tests/test_flux.py`.

**Interfaces:**
- Consumes: `Problem`, `Config`, `age_minutes`.
- Produces: `check_flux(objs: list[dict], kind: str, cfg: Config, now: datetime) -> list[Problem]` where `objs` are items of one Flux kind (`Kustomization`, `HelmRelease`, `GitRepository`, `HelmRepository`, `OCIRepository`); problem keys are `"{kind}/{ns}/{name}"`. `check_stale_revision(kustomizations, gitrepos, cfg, now)` flags a Kustomization whose `status.lastAppliedRevision` differs from its sourceRef GitRepository `status.artifact.revision` for longer than `not_ready_minutes`.

- [ ] **Step 1: Write failing tests**

```python
from datetime import datetime, timezone, timedelta
from cluster_health.config import Config
from cluster_health.flux import check_flux, check_stale_revision

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
def ago(m): return (NOW - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")

def obj(name, ready="True", since=60, suspend=False, msg="", created=None, no_status=False):
    o = {"metadata": {"name": name, "namespace": "flux-system", "creationTimestamp": created or ago(500)},
         "spec": {"suspend": suspend}, "status": {}}
    if not no_status:
        o["status"]["conditions"] = [{"type": "Ready", "status": ready, "message": msg, "lastTransitionTime": ago(since)}]
    return o

def test_healthy_is_silent():
    assert check_flux([obj("a")], "Kustomization", Config(), NOW) == []

def test_not_ready_over_threshold():
    [p] = check_flux([obj("a", ready="False", since=11, msg="boom")], "Kustomization", Config(), NOW)
    assert p.key == "Kustomization/flux-system/a" and "boom" in p.detail and p.severity == "error"

def test_not_ready_under_threshold_is_ignored():
    assert check_flux([obj("a", ready="False", since=3)], "Kustomization", Config(), NOW) == []

def test_suspended_flagged_unless_ignored():
    assert len(check_flux([obj("a", suspend=True)], "HelmRelease", Config(), NOW)) == 1
    cfg = Config(ignore_suspended=["flux-system/a"])
    assert check_flux([obj("a", suspend=True)], "HelmRelease", cfg, NOW) == []

def test_no_conditions_uses_creation_time():
    assert check_flux([obj("a", no_status=True, created=ago(2))], "Kustomization", Config(), NOW) == []
    assert len(check_flux([obj("a", no_status=True, created=ago(30))], "Kustomization", Config(), NOW)) == 1

def test_stale_revision():
    k = obj("apps"); k["spec"]["sourceRef"] = {"kind": "GitRepository", "name": "flux-system"}
    k["status"]["lastAppliedRevision"] = "main@sha1:old"
    g = obj("flux-system"); g["status"]["artifact"] = {"revision": "main@sha1:new", "lastUpdateTime": ago(30)}
    assert len(check_stale_revision([k], [g], Config(), NOW)) == 1
    g["status"]["artifact"]["lastUpdateTime"] = ago(2)
    assert check_stale_revision([k], [g], Config(), NOW) == []
```

- [ ] **Step 2: Run, expect FAIL** (ImportError).

- [ ] **Step 3: Implement**

```python
from datetime import datetime
from .config import Config
from .model import Problem, age_minutes

def _id(o): m = o["metadata"]; return m.get("namespace", ""), m["name"]

def _ready(o):
    for c in o.get("status", {}).get("conditions", []) or []:
        if c.get("type") == "Ready":
            return c
    return None

def check_flux(objs, kind, cfg: Config, now: datetime):
    out = []
    for o in objs:
        ns, name = _id(o)
        key = f"{kind}/{ns}/{name}"
        if o.get("spec", {}).get("suspend"):
            if f"{ns}/{name}" not in cfg.ignore_suspended:
                out.append(Problem(key, "warn", f"{kind} {ns}/{name} is suspended"))
            continue
        c = _ready(o)
        if c is None:
            since = o["metadata"].get("creationTimestamp")
            detail = "no status yet"
            ready = False
        else:
            since = c.get("lastTransitionTime")
            ready = c.get("status") == "True"
            detail = c.get("message", "")
        if not ready and since and age_minutes(since, now) > cfg.not_ready_minutes:
            out.append(Problem(key, "error", f"{kind} {ns}/{name} not ready", detail))
    return out

def check_stale_revision(kustomizations, gitrepos, cfg: Config, now: datetime):
    repos = {_id(g): g for g in gitrepos}
    out = []
    for k in kustomizations:
        ref = k.get("spec", {}).get("sourceRef", {})
        if ref.get("kind") != "GitRepository":
            continue
        ns, name = _id(k)
        g = repos.get((ref.get("namespace", ns), ref["name"]))
        art = (g or {}).get("status", {}).get("artifact")
        applied = k.get("status", {}).get("lastAppliedRevision")
        if art and applied and applied != art["revision"] and age_minutes(art["lastUpdateTime"], now) > cfg.not_ready_minutes:
            out.append(Problem(f"Kustomization/{ns}/{name}/stale", "warn",
                               f"Kustomization {ns}/{name} behind Git", f"applied {applied}, source {art['revision']}"))
    return out
```

- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `git commit -m "#144 cluster-health: Flux checks"`

### Task B3: Drill-down

**Files:** Create `files/cluster_health/drill.py`, `tests/test_drill.py`.

**Interfaces:**
- Consumes: `Problem`, `Config`, `age_minutes`.
- Produces:
  - `inventory_workloads(kustomization: dict) -> list[tuple[str, str, str]]` -> `(kind, namespace, name)` for apps/Deployment|StatefulSet|DaemonSet entries; inventory ids look like `ns_name_apps_Deployment`; empty list if no `status.inventory`.
  - `helm_workloads(helmrelease: dict, workloads: list[dict]) -> list[tuple[str,str,str]]` using labels `helm.toolkit.fluxcd.io/name` and `helm.toolkit.fluxcd.io/namespace`.
  - `drill(parent_key: str, targets: list[tuple[str,str,str]], workloads: list[dict], pods: list[dict], events: list[dict], cfg: Config, now) -> list[Problem]` - children have `parent=parent_key`; workload key `"{kind}/{ns}/{name}"`, pod key `"Pod/{ns}/{pod}"` with parent the workload.
  - Workload `workloads` items must carry a `kind` field (the collector sets it, since list responses omit it).

- [ ] **Step 1: Write failing tests** covering: inventory parsing (`"apps_foo_apps_Deployment"` -> `("Deployment","apps","foo")`, ConfigMap entries ignored, namespace-less ids ignored, missing inventory -> `[]`); a Deployment with `readyReplicas` 0 of 2 -> workload problem; a pod whose container waits with `CrashLoopBackOff` and a Pending pod older than `pending_minutes` -> pod problems under the workload; restart count at threshold -> warn; Warning events for that pod appear in the pod problem detail (last 3, each cut to 200 chars). Build fixtures with small helper functions like in `test_flux.py`; pods match a workload through `spec.selector.matchLabels` being a subset of `pod.metadata.labels` in the same namespace.

Example for the central test:
```python
def test_crashloop_pod_under_deployment():
    wl = {"kind": "Deployment", "metadata": {"name": "foo", "namespace": "apps"},
          "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "foo"}}},
          "status": {"readyReplicas": 0}}
    pod = {"metadata": {"name": "foo-1", "namespace": "apps", "labels": {"app": "foo"}, "creationTimestamp": ago(60)},
           "status": {"phase": "Running", "containerStatuses": [
               {"name": "c", "restartCount": 7, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}
    ev = {"type": "Warning", "reason": "BackOff", "message": "Back-off restarting failed container",
          "involvedObject": {"name": "foo-1", "namespace": "apps"}, "lastTimestamp": ago(1)}
    out = drill("Kustomization/flux-system/apps", [("Deployment", "apps", "foo")], [wl], [pod], [ev], Config(), NOW)
    keys = {p.key: p for p in out}
    assert keys["Deployment/apps/foo"].parent == "Kustomization/flux-system/apps"
    assert keys["Pod/apps/foo-1"].parent == "Deployment/apps/foo"
    assert "CrashLoopBackOff" in keys["Pod/apps/foo-1"].detail and "Back-off" in keys["Pod/apps/foo-1"].detail
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement**

```python
from .config import Config
from .model import Problem, age_minutes

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet"}
BAD_WAITING = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError", "RunContainerError"}

def inventory_workloads(k):
    entries = (k.get("status", {}).get("inventory") or {}).get("entries", [])
    out = []
    for e in entries:
        parts = e.get("id", "").split("_")
        if len(parts) == 4 and parts[2] == "apps" and parts[3] in WORKLOAD_KINDS and parts[0]:
            out.append((parts[3], parts[0], parts[1]))
    return out

def helm_workloads(hr, workloads):
    name, ns = hr["metadata"]["name"], hr["metadata"].get("namespace", "")
    out = []
    for w in workloads:
        lb = w["metadata"].get("labels", {})
        if lb.get("helm.toolkit.fluxcd.io/name") == name and lb.get("helm.toolkit.fluxcd.io/namespace") == ns:
            out.append((w["kind"], w["metadata"]["namespace"], w["metadata"]["name"]))
    return out

def _desired(w):
    if w["kind"] == "DaemonSet":
        return w.get("status", {}).get("desiredNumberScheduled", 0), w.get("status", {}).get("numberReady", 0)
    return w.get("spec", {}).get("replicas", 1), w.get("status", {}).get("readyReplicas", 0) or 0

def _events(pod_name, ns, events):
    ev = [e for e in events if e.get("type") == "Warning"
          and e["involvedObject"].get("name") == pod_name and e["involvedObject"].get("namespace") == ns]
    ev.sort(key=lambda e: e.get("lastTimestamp") or "", reverse=True)
    return [f"{e.get('reason')}: {e.get('message', '')[:200]}" for e in ev[:3]]

def _pod_problem(pod, events, cfg, now, parent):
    ns, name = pod["metadata"]["namespace"], pod["metadata"]["name"]
    reasons = []
    st = pod.get("status", {})
    if st.get("phase") == "Pending" and age_minutes(pod["metadata"]["creationTimestamp"], now) > cfg.pending_minutes:
        reasons.append("Pending")
    for cs in st.get("containerStatuses", []) or []:
        w = (cs.get("state") or {}).get("waiting") or {}
        if w.get("reason") in BAD_WAITING:
            reasons.append(f"{cs['name']}: {w['reason']}")
        if cs.get("restartCount", 0) >= cfg.restart_threshold and not w:
            reasons.append(f"{cs['name']}: {cs['restartCount']} restarts")
    if not reasons:
        return None
    detail = "; ".join(reasons + _events(name, ns, events))
    return Problem(f"Pod/{ns}/{name}", "error", f"Pod {ns}/{name}", detail, parent)

def drill(parent_key, targets, workloads, pods, events, cfg: Config, now):
    index = {(w["kind"], w["metadata"]["namespace"], w["metadata"]["name"]): w for w in workloads}
    out = []
    for kind, ns, name in targets:
        w = index.get((kind, ns, name))
        if w is None:
            out.append(Problem(f"{kind}/{ns}/{name}", "error", f"{kind} {ns}/{name} missing", parent=parent_key))
            continue
        want, ready = _desired(w)
        wkey = f"{kind}/{ns}/{name}"
        pod_problems = []
        sel = (w.get("spec", {}).get("selector") or {}).get("matchLabels", {})
        for pod in pods:
            if pod["metadata"]["namespace"] != ns or not sel:
                continue
            if all(pod["metadata"].get("labels", {}).get(k) == v for k, v in sel.items()):
                p = _pod_problem(pod, events, cfg, now, wkey)
                if p:
                    pod_problems.append(p)
        if ready < want or pod_problems:
            out.append(Problem(wkey, "error", f"{kind} {ns}/{name} {ready}/{want} ready", parent=parent_key))
            out.extend(pod_problems)
    return out
```

- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `git commit -m "#144 cluster-health: drill-down"`

### Task B4: Independent tier

**Files:** Create `files/cluster_health/independent.py`, `tests/test_independent.py`.

**Interfaces:**
- Produces: `check_nodes(nodes, cfg, now)`, `check_flux_controllers(deployments, cfg, now)` (Deployments in namespace `flux-system` not fully ready), `check_certs(certs, cfg, now)` (`status.notAfter` within `cert_expiry_days`, or Ready false), `check_longhorn(volumes, cfg, now)` (`status.robustness` in `degraded|faulted`, or `status.state == "detached"` is NOT a problem). All return `list[Problem]`, keys `Node/<name>`, `Controller/<name>`, `Certificate/<ns>/<name>`, `LonghornVolume/<name>`, no parent.

- [ ] **Step 1: Write failing tests**, one per function: node with `Ready` condition `False` -> problem, node `Ready=True` plus `MemoryPressure=True` -> warn; controller Deployment `readyReplicas 0` -> problem; certificate `notAfter` 5 days ahead -> warn, 40 days ahead -> none, Ready False -> error; Longhorn `robustness: degraded` -> warn, `faulted` -> error, `healthy` -> none.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** with the same style as `flux.py` (plain dict access, `.get` everywhere, time via `parse_time(notAfter) - now`).

```python
def check_nodes(nodes, cfg, now):
    out = []
    for n in nodes:
        name = n["metadata"]["name"]
        conds = {c["type"]: c["status"] for c in n.get("status", {}).get("conditions", [])}
        if conds.get("Ready") != "True":
            out.append(Problem(f"Node/{name}", "error", f"Node {name} not ready"))
        else:
            bad = [t for t in ("MemoryPressure", "DiskPressure", "PIDPressure") if conds.get(t) == "True"]
            if bad:
                out.append(Problem(f"Node/{name}", "warn", f"Node {name}", ", ".join(bad)))
    return out
```
Write the other three analogously; `check_certs` uses `(parse_time(c["status"]["notAfter"]) - now).days < cfg.cert_expiry_days`.

- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `git commit -m "#144 cluster-health: independent checks"`

### Task B5: Report rendering

**Files:** Create `files/cluster_health/report.py`, `tests/test_report.py`.

**Interfaces:**
- Consumes: `Problem`.
- Produces: `render(problems: list[Problem], resolved: list[str], limit: int = 4096) -> str` (HTML, tree by `parent`; orphans whose parent is not in the list are roots; `resolved` is a list of keys shown as a "Resolved:" line). `render_simple(text: str) -> str` escapes plain notices.

- [ ] **Step 1: Write failing tests**

```python
from cluster_health.model import Problem
from cluster_health.report import render

def test_tree_indents_children():
    ps = [Problem("K/a", "error", "Kustomization a"), Problem("D/a", "error", "Deployment a", parent="K/a")]
    out = render(ps, [])
    assert out.index("Kustomization a") < out.index("Deployment a")
    assert "└" in out

def test_escapes_html():
    out = render([Problem("x", "error", "a <b> & c", detail="<script>")], [])
    assert "<script>" not in out and "&lt;script&gt;" in out

def test_truncates_below_limit_without_breaking_tags():
    ps = [Problem(f"k{i}", "error", f"title {i}", detail="x" * 300) for i in range(100)]
    out = render(ps, [], limit=4096)
    assert len(out) <= 4096 and out.count("<b>") == out.count("</b>")
    assert "more" in out.splitlines()[-1]

def test_resolved_line():
    assert "Resolved" in render([], ["K/a"])
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement**

```python
import html
from .model import Problem

ICON = {"error": "🔴", "warn": "🟡"}

def _line(p: Problem, depth: int) -> str:
    pad = "  " * (depth - 1) + "└ " if depth else ""
    detail = f"\n{'  ' * depth}<i>{html.escape(p.detail[:300])}</i>" if p.detail else ""
    return f"{pad}{ICON.get(p.severity, '')} <b>{html.escape(p.title)}</b>{detail}"

def render(problems, resolved, limit=4096) -> str:
    keys = {p.key for p in problems}
    kids = {}
    for p in problems:
        kids.setdefault(p.parent if p.parent in keys else None, []).append(p)
    lines = []
    def walk(parent, depth):
        for p in kids.get(parent, []):
            lines.append(_line(p, depth)); walk(p.key, depth + 1)
    walk(None, 0)
    head = f"<b>Cluster health: {len(problems)} problem(s)</b>" if problems else "<b>Cluster health: all clear</b>"
    tail = f"\n✅ Resolved: {html.escape(', '.join(resolved))}" if resolved else ""
    out, used = [head], len(head) + len(tail)
    for i, line in enumerate(lines):
        more = f"… and {len(lines) - i} more"
        if used + len(line) + 1 + len(more) + 1 > limit:
            out.append(more); break
        out.append(line); used += len(line) + 1
    return "\n".join(out) + tail

def render_simple(text: str) -> str:
    return html.escape(text)
```
Each rendered line is atomic (its tags are closed inside `_line`), so truncation between lines never leaves an open tag. If the `tail` pushes past `limit` in the resolved-only case, slice `resolved` to its first 20 keys.

- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `git commit -m "#144 cluster-health: report rendering"`

### Task B6: State and decision logic

**Files:** Create `files/cluster_health/state.py`, `tests/test_state.py`.

**Interfaces:**
- Produces: `load_state(path) -> dict` (missing/corrupt -> default), `save_state(path, state)` (atomic write via temp file + `os.replace`), and
  `decide(state: dict, problems: list[Problem], api_ok: bool, now: datetime) -> tuple[list[str], dict]` returning `(messages, new_state)` where messages are ready-to-send HTML strings. State keys: `problems` (sorted list of keys), `last_heartbeat` (ISO date), `api_down_notified` (ISO timestamp or null). `main` saves `new_state` only after all messages were sent.

- [ ] **Step 1: Write failing tests**

```python
from datetime import datetime, timezone, timedelta
from cluster_health.model import Problem
from cluster_health.state import decide, load_state

def T(h=9, d=11): return datetime(2026, 10, d, h, 0, tzinfo=timezone.utc)
P = lambda k: Problem(k, "error", k)
S = lambda **kw: {"problems": [], "last_heartbeat": "2026-10-11", "api_down_notified": None, **kw}

def test_no_change_no_message():
    msgs, st = decide(S(problems=["a"]), [P("a")], True, T())
    assert msgs == [] and st["problems"] == ["a"]

def test_new_problem_sends_report():
    msgs, st = decide(S(), [P("a")], True, T())
    assert len(msgs) == 1 and "a" in msgs[0] and st["problems"] == ["a"]

def test_resolved_and_new_in_one_message():
    msgs, st = decide(S(problems=["a"]), [P("b")], True, T())
    assert len(msgs) == 1 and "Resolved" in msgs[0] and "b" in msgs[0]

def test_all_clear():
    msgs, _ = decide(S(problems=["a"]), [], True, T())
    assert "all clear" in msgs[0]

def test_heartbeat_once_per_day_after_8():
    msgs, st = decide(S(last_heartbeat="2026-10-10"), [], True, T(h=9))
    assert len(msgs) == 1 and st["last_heartbeat"] == "2026-10-11"
    assert decide(S(last_heartbeat="2026-10-10"), [], True, T(h=7))[0] == []

def test_api_down_once_then_hourly_then_recovered():
    m1, s1 = decide(S(), [], False, T())
    assert len(m1) == 1 and s1["api_down_notified"]
    m2, s2 = decide(s1, [], False, T() + timedelta(minutes=15))
    assert m2 == []
    m3, _ = decide(s1, [], False, T() + timedelta(minutes=61))
    assert len(m3) == 1
    m4, s4 = decide(s1, [], True, T() + timedelta(minutes=20))
    assert "reachable again" in m4[0] and s4["api_down_notified"] is None

def test_corrupt_state_file(tmp_path):
    p = tmp_path / "s.json"; p.write_text("{not json")
    assert load_state(p)["problems"] == []
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** `load_state` (try/except `(OSError, ValueError)` -> default `{"problems": [], "last_heartbeat": None, "api_down_notified": None}`), `save_state`, and `decide`:

```python
def decide(state, problems, api_ok, now):
    st = {**state}
    msgs = []
    if not api_ok:
        last = st.get("api_down_notified")
        if not last or (now - parse_time(last)).total_seconds() >= 3600:
            msgs.append("<b>🚨 Cluster API unreachable</b>")
            st["api_down_notified"] = now.isoformat()
        return msgs, st
    if st.get("api_down_notified"):
        msgs.append("<b>✅ Cluster API reachable again</b>")
        st["api_down_notified"] = None
    keys = sorted(p.key for p in problems)
    prev = st.get("problems", [])
    if keys != prev:
        resolved = [k for k in prev if k not in keys]
        msgs.append(render(problems, resolved))
        st["problems"] = keys
    today = now.date().isoformat()
    if not msgs and now.hour >= 8 and st.get("last_heartbeat") != today:
        msgs.append(f"<b>💚 Cluster health: heartbeat</b>\n{len(keys)} open problem(s)")
    if now.hour >= 8 and st.get("last_heartbeat") != today:
        st["last_heartbeat"] = today
    return msgs, st
```
(Imports: `from .model import parse_time`, `from .report import render`.) Note the 15-minute API-down case: `s1` has `api_down_notified` at T; at T+15 min the elapsed time is under 3600 s, so no message.

- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `git commit -m "#144 cluster-health: state and decisions"`

### Task B7: Telegram sender

**Files:** Create `files/cluster_health/telegram.py`, `tests/test_telegram.py`.

**Interfaces:**
- Produces: `send(token: str, chat_id: str, text: str, *, post=requests.post, sleep=time.sleep, attempts: int = 3) -> None`; raises `TelegramError` after the last failed attempt. Injected `post`/`sleep` make it testable without network.

- [ ] **Step 1: Write failing tests**

```python
import pytest
from cluster_health.telegram import send, TelegramError

class R:
    def __init__(self, code): self.status_code = code; self.text = "x"

def test_success_first_try():
    calls = []
    send("T", "1", "hi", post=lambda *a, **k: (calls.append(k), R(200))[1], sleep=lambda s: None)
    assert len(calls) == 1 and calls[0]["json"]["parse_mode"] == "HTML"

def test_retries_then_succeeds():
    seq = iter([R(500), R(429), R(200)])
    send("T", "1", "hi", post=lambda *a, **k: next(seq), sleep=lambda s: None)

def test_gives_up_after_three():
    n = []
    def post(*a, **k): n.append(1); return R(500)
    with pytest.raises(TelegramError):
        send("T", "1", "hi", post=post, sleep=lambda s: None)
    assert len(n) == 3

def test_network_error_is_retried():
    def post(*a, **k): raise OSError("down")
    with pytest.raises(TelegramError):
        send("T", "1", "hi", post=post, sleep=lambda s: None)

def test_token_never_in_error():
    with pytest.raises(TelegramError) as e:
        send("SECRETTOKEN", "1", "hi", post=lambda *a, **k: R(500), sleep=lambda s: None)
    assert "SECRETTOKEN" not in str(e.value)
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement**

```python
import time
import requests

class TelegramError(RuntimeError):
    pass

def send(token, chat_id, text, *, post=requests.post, sleep=time.sleep, attempts=3):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    last = "no attempt"
    for i in range(attempts):
        try:
            r = post(url, json=body, timeout=15)
            if r.status_code == 200:
                return
            last = f"HTTP {r.status_code}"
        except OSError as e:
            last = type(e).__name__
        if i < attempts - 1:
            sleep(2 ** i)
    raise TelegramError(f"Telegram send failed after {attempts} attempts: {last}")
```
(`requests` exceptions subclass `OSError`, so network errors are covered.)

- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `git commit -m "#144 cluster-health: Telegram sender"`

### Task B8: Collector and entry point

**Files:** Create `files/cluster_health/collect.py`, `files/cluster_health/main.py`, `files/requirements.txt` (`kubernetes`, `PyYAML`, `requests`).

**Interfaces:**
- Consumes: everything above.
- Produces: `collect.Client(kubeconfig: str)` with `ping() -> bool` (GET `/readyz` or list namespaces limit 1; any exception -> `False`), `flux(kind, version) -> list[dict]` (cluster-wide custom-object list; group by kind: Kustomization `kustomize.toolkit.fluxcd.io/v1`, HelmRelease `helm.toolkit.fluxcd.io/v2`, GitRepository/HelmRepository/OCIRepository `source.toolkit.fluxcd.io/v1`; a kind whose CRD is absent returns `[]`), `nodes()`, `workloads()` (adds `kind` to each Deployment/StatefulSet/DaemonSet item), `pods()`, `events()`, `certificates()`, `longhorn_volumes()`. All return plain dicts via `ApiClient().sanitize_for_serialization`. `main.run(argv) -> int` reads `CH_CONFIG`, `CH_STATE`, `CH_KUBECONFIG`, `CH_TELEGRAM_TOKEN`, `CH_TELEGRAM_CHAT` from the environment; exit codes: 0 ok, 1 Telegram failure, 2 unexpected error.

- [ ] **Step 1: Write `collect.py`** using `kubernetes.config.load_kube_config(config_file=...)`, `CustomObjectsApi.list_cluster_custom_object(group, version, plural)` (a `404` -> `[]`), `AppsV1Api`, `CoreV1Api`. Set `_request_timeout=20` on every call.
- [ ] **Step 2: Write `main.py`**

```python
import os, sys
from datetime import datetime, timezone
from .collect import Client
from .config import Config
from .drill import drill, helm_workloads, inventory_workloads
from .flux import check_flux, check_stale_revision
from .independent import check_certs, check_flux_controllers, check_longhorn, check_nodes
from .state import decide, load_state, save_state
from .telegram import TelegramError, send

def gather(c: Client, cfg: Config, now):
    ks, hr = c.flux("Kustomization"), c.flux("HelmRelease")
    git = c.flux("GitRepository")
    problems = check_flux(ks, "Kustomization", cfg, now) + check_flux(hr, "HelmRelease", cfg, now)
    for kind in ("GitRepository", "HelmRepository", "OCIRepository"):
        problems += check_flux(git if kind == "GitRepository" else c.flux(kind), kind, cfg, now)
    problems += check_stale_revision(ks, git, cfg, now)
    flagged = {p.key for p in problems}
    if any(p.key.startswith(("Kustomization/", "HelmRelease/")) for p in problems):
        workloads, pods, events = c.workloads(), c.pods(), c.events()
        for o, kind, targets in [(k, "Kustomization", inventory_workloads(k)) for k in ks] + \
                                [(h, "HelmRelease", helm_workloads(h, workloads)) for h in hr]:
            key = f"{kind}/{o['metadata'].get('namespace','')}/{o['metadata']['name']}"
            if key in flagged:
                problems += drill(key, targets, workloads, pods, events, cfg, now)
    problems += check_nodes(c.nodes(), cfg, now)
    controllers = [w for w in c.workloads()
                   if w["kind"] == "Deployment" and w["metadata"]["namespace"] == "flux-system"]
    problems += check_flux_controllers(controllers, cfg, now)
    problems += check_certs(c.certificates(), cfg, now) + check_longhorn(c.longhorn_volumes(), cfg, now)
    return list({p.key: p for p in problems}.values())
```
`Client.workloads()` caches its result per run (`functools.cache` on the method's underlying fetch), so the second call for the Flux controller check costs nothing.

```python
def run(argv=None) -> int:
    env = os.environ
    now = datetime.now(timezone.utc)
    cfg = Config.load(env["CH_CONFIG"])
    state = load_state(env["CH_STATE"])
    client = Client(env["CH_KUBECONFIG"])
    ok = client.ping()
    problems = gather(client, cfg, now) if ok else []
    msgs, new_state = decide(state, problems, ok, now)
    try:
        for m in msgs:
            send(env["CH_TELEGRAM_TOKEN"], env["CH_TELEGRAM_CHAT"], m)
    except TelegramError as e:
        print(e, file=sys.stderr)
        return 1
    save_state(env["CH_STATE"], new_state)
    return 0

if __name__ == "__main__":
    sys.exit(run())
```
Wrap `gather` so a mid-run API failure falls back to `ok = False` (re-`ping()` after an exception), and any other exception propagates (exit 2 via systemd).

- [ ] **Step 3: Offline smoke test** - add `tests/test_main.py` with a `FakeClient` (all methods return fixture lists) and monkeypatched `send`; assert: healthy cluster, heartbeat already sent -> no `send` call, state saved; one failing Kustomization with a crashlooping Deployment -> exactly one message containing both titles; `send` raising `TelegramError` -> return code 1 and state file unchanged.
- [ ] **Step 4: Run all tests** - `python -m pytest roles/cluster-health/tests -v` -> all PASS.
- [ ] **Step 5: Live dry run from the workstation** (read-only, no Telegram): `CH_TELEGRAM_TOKEN=x CH_TELEGRAM_CHAT=x` with a `send` stub is not needed; instead add `--dry-run` handling to `run()` that prints messages to stdout and skips `send`/`save_state`. Run with an admin kubeconfig: `python -m cluster_health.main --dry-run`. Expected: either nothing or the current real problems; compare with `flux get all -A | grep -v True`.
- [ ] **Step 6: Commit** `git commit -m "#144 cluster-health: collector and entry point"`

### Task B9: Ansible role and deployment

**Files:** Create `roles/cluster-health/{defaults/main.yml,tasks/main.yml,handlers/main.yml,templates/*}`; Modify `playbooks/setup_host_raspi1.yml`; Modify `inventory/group_vars/all/vault.yaml` (via `ansible-vault edit`, add `vault_cluster_health_telegram_token`, `vault_cluster_health_telegram_chat`); update `CLAUDE.md` of the homelab repo with the new playbook line.

**Interfaces:**
- Consumes: the `cluster-health-token` Secret (Part A), vault variables above, package from Tasks B1-B8.
- Produces: `/opt/cluster-health/` (package + venv), `/etc/cluster-health/{config.yaml,kubeconfig,env}` (mode 0600 root), `/var/lib/cluster-health/state.json`, units `cluster-health.service` (oneshot) and `cluster-health.timer` (`OnCalendar=*:0/15`, `Persistent=true`, `RandomizedDelaySec=30`).

- [ ] **Step 1: Write the role.** Tasks, in order: install `python3-venv`; create directories; `ansible.builtin.copy` the `files/cluster_health` package and `requirements.txt` to `/opt/cluster-health/`; `ansible.builtin.pip` into `/opt/cluster-health/venv` (`virtualenv_command: python3 -m venv`); template the config, env, unit and timer files; kubeconfig built with `delegate_to: localhost`, `run_once: true` reading the live token (`kubectl -n cluster-health get secret cluster-health-token -o jsonpath=...`, `no_log: true`) and the server URL from the admin kubeconfig; enable and start the timer.

  `env.j2`:
  ```
  CH_CONFIG=/etc/cluster-health/config.yaml
  CH_STATE=/var/lib/cluster-health/state.json
  CH_KUBECONFIG=/etc/cluster-health/kubeconfig
  CH_TELEGRAM_TOKEN={{ vault_cluster_health_telegram_token }}
  CH_TELEGRAM_CHAT={{ vault_cluster_health_telegram_chat }}
  ```
  `cluster-health.service.j2`:
  ```
  [Unit]
  Description=Kubernetes cluster health check
  After=network-online.target
  Wants=network-online.target

  [Service]
  Type=oneshot
  EnvironmentFile=/etc/cluster-health/env
  WorkingDirectory=/opt/cluster-health
  ExecStart=/opt/cluster-health/venv/bin/python -m cluster_health.main
  ```
  Every task that touches the token or env file gets `no_log: true`.

- [ ] **Step 2: Lint and check mode** - `ansible-lint roles/cluster-health` (if installed) and `ansible-playbook playbooks/setup_host_raspi1.yml --limit raspi1 --tags cluster-health --check --diff` -> no errors.
- [ ] **Step 3: Deploy** - `ansible-playbook playbooks/setup_host_raspi1.yml --limit raspi1 --tags cluster-health`; then on raspi1: `systemctl start cluster-health.service; journalctl -u cluster-health -n 30 --no-pager`. Expected: exit 0, and a Telegram message only if something is wrong or the heartbeat is due.
- [ ] **Step 4: Fault-injection test** (explicit, reversible): `flux suspend kustomization wishlist` -> within 15 min (or after a manual `systemctl start`) a Telegram message names `Kustomization flux-system/wishlist is suspended`; `flux resume kustomization wishlist` -> next run sends "Resolved". Then stop the timer's network path test: `systemctl start cluster-health` with `CH_KUBECONFIG` pointing to a bad server in a temporary override is NOT needed; unit-tested in Task B6.
- [ ] **Step 5: Commit, PR** `Closes #144`, switch back to main (CLAUDE.md workflow). After merge of both PRs close k8s-homelab#187.

### Task B10: Follow-ups

- [ ] Rotate the recovered n8n bot token via BotFather (create `/revoke`, update vault, redeploy), or create a fresh bot and update `vault_cluster_health_telegram_token`/`_chat`.
- [ ] Delete `~/.n8n-telegram-token` and the scratchpad script once the token is in the vault.
- [ ] Write the second-brain entry (AGENTS.md format) and cache any web pages read as source notes.
- [ ] Add the monitor's own liveness to the daily heartbeat check: if no heartbeat arrives by 09:00, the timer died (manual, no automation needed).
