# Paperless-ngx Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move Paperless-ngx, its FTP/SMB scanner ingest and a NAS backup from lab1 (Docker) into the Talos cluster, with the scanners keeping the address `192.168.1.28`.

**Architecture:** Plain Kustomize manifests in `apps/base/paperless` (namespace `paperless`): Postgres 17, Redis, Gotenberg, Tika, the Paperless webserver, one "ingest" pod (proftpd + samba) on a Multus macvlan LAN address, a nightly exporter/`pg_dump`/rsync CronJob, and a PrometheusRule. Data moves with `document_exporter` / `document_importer`. The ingest pod takes over lab1's IP after lab1 has been given a new one.

**Tech Stack:** Flux CD, Kustomize, SOPS (age), Longhorn, Multus macvlan, Traefik IngressRoute, kube-prometheus-stack, paperless-ngx 3.0.4.

**Spec:** `docs/superpowers/specs/2026-10-09-paperless-k8s-migration-design.md` (corrected in Task 0 for the findings below).

## Global Constraints

- Issue **#180**, branch `180-paperless-migration`. Commits start with `#180 `. Never commit to `main`. After creating the PR, switch back to `main` and pull.
- Commit messages end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`; the PR body ends with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- Paperless image `ghcr.io/paperless-ngx/paperless-ngx:3.0.4` (the version on lab1; the importer needs the same version). Postgres `postgres:17`, Redis `redis:8`, Gotenberg `docker.io/gotenberg/gotenberg:8.20`.
- Longhorn storage only. `longhorn-retain` for Postgres, `data`, `media`, `consume`; default `longhorn` for the rebuildable `export` volume.
- Every container has resource requests and limits (memory is the tight resource; nodes were at 49-64 %). Do not start if any node is above ~70 % memory.
- Secrets only as `*.sops.yaml` (public repo). Never print a secret value; build Secrets from shell variables or files, then `sops --encrypt --in-place`.
- Flux Kustomization has `decryption: {provider: sops, secretRef: {name: sops-age}}`, `dependsOn: infrastructure, multus, monitoring`, `wait: true`.
- Domain `paperless.homelab.cs-ol.de` (wildcard cert, `tls: {}`). `PAPERLESS_URL` is internal only (decided 2026-10-09; the duckdns name is dropped).
- Mail via `smtp-relay.smtp-relay.svc.cluster.local:587`, sender `@cs-ol.de`.
- Ingest address is **192.168.1.28/22** (decided 2026-10-09). lab1 must release it first, and **FTP/SMB credentials are kept unchanged** so scanners and devices need no reconfiguration. All other secrets are newly generated.
- lab1's compose files stay on disk, only disabled, for rollback. User merges PRs in the `homelab` repo.
- Shell commands that stop services on lab1 may be blocked by the permission classifier: prepare them and hand the user `! <command>` lines, then verify read-only.
- Backups on the NAS: history in `/share/MD0_DATA/Backup/paperless/<YYYY-MM-DD>/` (14 kept, hardlinked); a single mirror in `/share/MD0_DATA/Paperless/offsite/` which `mega_io` already syncs (it syncs the whole `Paperless` share to MEGA root; free account, 20 GB total).

## Review Focus

- **lab1's IP changes while HA still writes to InfluxDB on `192.168.1.28:8086`** (#171/#172 not merged on 2026-10-09). Expected: the IP swap is blocked until the InfluxDB cutover is done (Task 0 check, Task 9 gate).
- **Files dropped by FTP/SMB into a new subfolder are not consumable** if the folder is not world-writable (uid 33 writes, uid 1000 consumes). Expected: a test drop into an existing and a new subfolder is consumed (Task 9).
- **Two nodes holding one RWO volume.** Expected: the ingest pod and the backup job are pinned to the webserver's node (required podAffinity) and the webserver prefers the ingest pod's node (soft affinity). After any webserver restart check `kubectl -n paperless get pods -o wide`; if the webserver sits in `ContainerCreating` with a Multi-Attach error, delete the ingest pod (`kubectl -n paperless delete pod -l app=paperless-ingest`) and it reschedules next to the webserver.
- **Backup silently empty or stale.** Expected: the job fails if the manifest is missing or the tree is implausibly small, and the alert fires when the last success is older than 36 h (Task 5, Task 10).
- **MEGA cap.** Expected: total synced size of the `Paperless` share is measured and below ~15 GB; hardlinked history is not under the synced share (Task 0, Task 10).
- **Importer into a non-empty database.** Expected: the import happens before any admin user or document exists, so no `PAPERLESS_ADMIN_*` is set (Task 3, Task 8).

---

### Task 0: Correct the spec and run read-only checks

**Files:**
- Modify: `docs/superpowers/specs/2026-10-09-paperless-k8s-migration-design.md`

Findings from reading lab1 that differ from the approved spec:
1. MetalLB's pool is `10.98.0.200-254` and cannot serve LAN addresses; ingest uses a macvlan address like Mosquitto, in **one** pod with two containers (no MetalLB services, no per-service podAffinity).
2. The scanner IP is lab1's own `192.168.1.28`; lab1 moves to a new IP first.
3. `document_exporter` in 3.0.4 has no `--delta`; it skips unchanged files by default. Use `-d` (delete stale files). It supports `-c` (checksum compare).
4. The webserver also mounts `removepassword.py` (pre-consume script) and `passwords.txt` (PDF unlock passwords). Both move over; the passwords file is a SOPS secret.
5. Document count on lab1 is 602 (not 542).
6. Backup layout: history under `/Backup/paperless/` plus a single mirror under `/Paperless/offsite/` that `mega_io` syncs, so hardlinked history does not multiply into MEGA.

- [ ] **Step 1: Apply the spec corrections**

Edit the spec so that: the Components table has a single `paperless-ingest` row (proftpd + samba containers, macvlan `192.168.1.28/22`, `consume` PVC); the "Access" section drops LoadBalancer/MetalLB wording; the Backup section uses `document_exporter -d` and the two-target layout; the Cutover section gets the IP-swap step (Task 9 order) and the InfluxDB prerequisite; the document count says 602; a `removepassword.py` / `passwords.txt` bullet is added to Components.

- [ ] **Step 2: Verify the NAS side** (read-only; the NAS user/credentials come from the existing php-apache backup Secret)

```bash
cd ~/Workspace/k8s-homelab
export NAS_USER=$(kubectl -n php-apache get secret php-apache-backup-nas -o jsonpath='{.data.username}' | base64 -d)
export SSHPASS=$(kubectl -n php-apache get secret php-apache-backup-nas -o jsonpath='{.data.password}' | base64 -d)
SSH_OPTS="-o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa -o StrictHostKeyChecking=yes"
sshpass -e ssh $SSH_OPTS "$NAS_USER@192.168.1.240" 'which rsync; rsync --version | head -1; ls -ld /share/MD0_DATA/Backup /share/MD0_DATA/Paperless; du -sh /share/MD0_DATA/Paperless 2>/dev/null; touch /share/MD0_DATA/Paperless/.w && rm /share/MD0_DATA/Paperless/.w && echo WRITE_OK'
```
Expected: an `rsync` path and version line, both directories listed, the share size well below 15 GB, `WRITE_OK`. If `rsync` is missing: stop and ask the user (fallback is the NAS rsync daemon on 873).

- [ ] **Step 3: Verify the cluster side**

```bash
kubectl top nodes                                   # every node below ~70 % memory
kubectl -n mosquitto get net-attach-def lan-macvlan -o jsonpath='{.spec.config}' | grep '"master"'
gh pr view 172 --json state -q .state; gh issue view 171 --json state -q .state
ssh root@192.168.1.28 'docker ps --format "{{.Names}}" | grep -i influx || echo "influxdb not on lab1"'
```
Expected: memory OK; the Mosquitto NAD uses `"master": "ens19"` (the same NIC name the new NAD uses, already proven on these nodes); record the InfluxDB state. #172 merged on 2026-10-09 but only deployed an empty instance: the gate in Task 9 needs the data restored, HA repointed and lab1's InfluxDB stopped (`influxdb not on lab1`).

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-10-09-paperless-k8s-migration-design.md docs/superpowers/plans/2026-10-09-paperless-k8s-migration.md
git commit -m "#180 Correct Paperless spec after reading lab1 and add the plan

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 1: Namespace, storage and configuration

**Files:**
- Create: `apps/base/paperless/namespace.yaml`
- Create: `apps/base/paperless/pvc.yaml`
- Create: `apps/base/paperless/config.yaml`
- Create: `apps/base/paperless/kustomization.yaml`

**Interfaces:**
- Produces: namespace `paperless`; PVCs `paperless-data`, `paperless-media`, `paperless-consume`, `paperless-export`; ConfigMap `paperless-env` (env for the webserver and exporter); ConfigMap `paperless-scripts` (key `removepassword.py`).

- [ ] **Step 1: Write `namespace.yaml`**

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: paperless
```

- [ ] **Step 2: Write `pvc.yaml`**

```yaml
# longhorn-retain, not the default class: an accidental Kustomization delete
# must not take the documents with it. `export` is rebuildable (staging for
# the backup and for the one-time import), so it uses the default class.
#
# On lab1 (2026-10-09): data 205 MB, media 457 MB.
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: paperless-data
  namespace: paperless
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: longhorn-retain
  resources:
    requests:
      storage: 2Gi
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: paperless-media
  namespace: paperless
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: longhorn-retain
  resources:
    requests:
      storage: 10Gi
---
# Shared by the webserver and the ingest pod (FTP/SMB), which are scheduled onto
# the same node.
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: paperless-consume
  namespace: paperless
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: longhorn-retain
  resources:
    requests:
      storage: 1Gi
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: paperless-export
  namespace: paperless
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: longhorn
  resources:
    requests:
      storage: 5Gi
```

- [ ] **Step 3: Write `config.yaml`** (the script is copied verbatim from lab1's `/docker/paperless/removepassword.py`)

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: paperless-env
  namespace: paperless
data:
  PAPERLESS_URL: https://paperless.homelab.cs-ol.de
  PAPERLESS_TIME_ZONE: Europe/Berlin
  PAPERLESS_OCR_LANGUAGE: deu
  PAPERLESS_OCR_LANGUAGES: eng
  PAPERLESS_OCR_OUTPUT_TYPE: pdfa
  PAPERLESS_OCR_CLEAN: clean
  PAPERLESS_OCR_DESKEW: 'true'
  PAPERLESS_REDIS: redis://paperless-redis:6379
  PAPERLESS_DBHOST: paperless-db
  PAPERLESS_TIKA_ENABLED: '1'
  PAPERLESS_TIKA_GOTENBERG_ENDPOINT: http://paperless-gotenberg:3000
  PAPERLESS_TIKA_ENDPOINT: http://paperless-tika:9998
  PAPERLESS_CONSUMER_RECURSIVE: 'true'
  PAPERLESS_PRE_CONSUME_SCRIPT: /usr/src/paperless/scripts/removepassword.py
  PAPERLESS_EMAIL_HOST: smtp-relay.smtp-relay.svc.cluster.local
  PAPERLESS_EMAIL_PORT: '587'
  PAPERLESS_EMAIL_FROM: paperless@cs-ol.de
---
# Pre-consume hook: unlocks encrypted PDFs with the passwords in
# passwords.txt (a SOPS Secret, mounted next to it) and extracts PDF
# attachments into the consume folder.
apiVersion: v1
kind: ConfigMap
metadata:
  name: paperless-scripts
  namespace: paperless
data:
  removepassword.py: |
    #!/usr/bin/env python3

    import os
    import pikepdf


    def is_pdf(file_path: str) -> bool:
        return os.path.splitext(file_path.lower())[1] == ".pdf"


    def is_pdf_encrypted(file_path: str) -> bool:
        try:
            with pikepdf.open(file_path) as pdf:
                return pdf.is_encrypted
        except:
            return True


    def pdf_has_attachments(file_path: str) -> bool:
        try:
            with pikepdf.open(file_path) as pdf:
                return len(pdf.attachments) > 0
        except:
            return False


    def unlock_pdf(file_path: str):
        password = None
        print("reading passwords")
        with open(pass_file_path, "r") as f:
            passwords = f.readlines()
        for p in passwords:
            password = p.strip()
            try:
                with pikepdf.open(
                    file_path, password=password, allow_overwriting_input=True
                ) as pdf:
                    # print("password is working")
                    print("unlocked succesfully")
                    pdf.save(file_path, deterministic_id=True)
                    break
            except pikepdf.PasswordError:
                print("password is not working")
                continue
        if password is None:
            print("empty password file")


    def extract_pdf_attachments(file_path: str):
        with pikepdf.open(file_path) as pdf:
            ats = pdf.attachments
            for atm in ats:
                trg_filename = ats.get(atm).filename
                if is_pdf(trg_filename):
                    trg_file_path = os.path.join(consume_path, trg_filename)
                    try:
                        with open(trg_file_path, "wb") as wb:
                            wb.write(ats.get(atm).obj["/EF"]["/F"].read_bytes())
                            print("saved: ", trg_file_path)
                    except:
                        print("error ", trg_file_path)
                        continue
                else:
                    print("skipped: ", trg_filename)

    src_file_path = os.environ.get('DOCUMENT_WORKING_PATH')
    pass_file_path = "/usr/src/paperless/scripts/passwords.txt"
    consume_path = "/usr/src/paperless/consume/"

    if src_file_path is None:
        print("no file path")
        exit(0)

    if not is_pdf(src_file_path):
        print("not pdf")
        exit(0)

    if is_pdf_encrypted(src_file_path):
        print("decrypting pdf")
        unlock_pdf(src_file_path)
    else:
        print("not encrypted")

    if pdf_has_attachments(src_file_path):
        print("getting attachments")
        extract_pdf_attachments(src_file_path)
    else:
        print("no attachments")
```

- [ ] **Step 4: Write `kustomization.yaml`** (extended by later tasks)

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - pvc.yaml
  - config.yaml
```

- [ ] **Step 5: Verify**

Run: `kubectl kustomize apps/base/paperless | grep -c '^kind:'` → Expected: `7` (1 Namespace, 4 PVCs, 2 ConfigMaps).
Run: `kubectl kustomize apps/base/paperless | python3 -c 'import sys,yaml; [d for d in yaml.safe_load_all(sys.stdin)]; print("yaml ok")'` → `yaml ok`. Then confirm the embedded script survived: `kubectl kustomize apps/base/paperless | grep -c pikepdf` → at least `6` (the script mentions pikepdf six times).

- [ ] **Step 6: Commit**

```bash
git add apps/base/paperless
git commit -m "#180 paperless: namespace, volumes and configuration

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Postgres, Redis, Gotenberg, Tika

**Files:**
- Create: `apps/base/paperless/postgres.yaml`
- Create: `apps/base/paperless/redis.yaml`
- Create: `apps/base/paperless/gotenberg.yaml`
- Create: `apps/base/paperless/tika.yaml`
- Modify: `apps/base/paperless/kustomization.yaml`

**Interfaces:**
- Consumes: Secret `paperless-secrets` with key `POSTGRES_PASSWORD` (created in Task 6).
- Produces: Services `paperless-db:5432`, `paperless-redis:6379`, `paperless-gotenberg:3000`, `paperless-tika:9998` (names match `paperless-env`).

- [ ] **Step 1: Write `postgres.yaml`**

```yaml
# Postgres 17, the same major version as lab1, so the move does not include a
# major upgrade. PGDATA is a subdirectory because a fresh Longhorn volume has a
# lost+found directory in its root, which initdb refuses.
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: paperless-db
  namespace: paperless
spec:
  serviceName: paperless-db
  replicas: 1
  selector:
    matchLabels:
      app: paperless-db
  template:
    metadata:
      labels:
        app: paperless-db
    spec:
      securityContext:
        runAsUser: 999
        runAsGroup: 999
        fsGroup: 999
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: postgres
          image: postgres:17
          ports:
            - name: postgres
              containerPort: 5432
          env:
            - name: POSTGRES_DB
              value: paperless
            - name: POSTGRES_USER
              value: paperless
            - name: PGDATA
              value: /var/lib/postgresql/data/pgdata
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: paperless-secrets
                  key: POSTGRES_PASSWORD
          resources:
            requests:
              cpu: 50m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 512Mi
          readinessProbe:
            exec:
              command: [pg_isready, -U, paperless, -d, paperless]
            periodSeconds: 10
          livenessProbe:
            exec:
              command: [pg_isready, -U, paperless, -d, paperless]
            initialDelaySeconds: 30
            periodSeconds: 20
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: [ALL]
          volumeMounts:
            - name: data
              mountPath: /var/lib/postgresql/data
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: [ReadWriteOnce]
        storageClassName: longhorn-retain
        resources:
          requests:
            storage: 5Gi
---
apiVersion: v1
kind: Service
metadata:
  name: paperless-db
  namespace: paperless
spec:
  selector:
    app: paperless-db
  ports:
    - name: postgres
      port: 5432
      targetPort: postgres
```

- [ ] **Step 2: Write `redis.yaml`**

```yaml
# Task queue only: nothing here needs to survive a restart.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: paperless-redis
  namespace: paperless
spec:
  replicas: 1
  selector:
    matchLabels:
      app: paperless-redis
  template:
    metadata:
      labels:
        app: paperless-redis
    spec:
      securityContext:
        runAsUser: 999
        runAsGroup: 999
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: redis
          image: redis:8
          args: [redis-server, --save, '', --appendonly, 'no']
          ports:
            - name: redis
              containerPort: 6379
          resources:
            requests:
              cpu: 10m
              memory: 32Mi
            limits:
              cpu: 200m
              memory: 128Mi
          readinessProbe:
            tcpSocket:
              port: redis
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: [ALL]
---
apiVersion: v1
kind: Service
metadata:
  name: paperless-redis
  namespace: paperless
spec:
  selector:
    app: paperless-redis
  ports:
    - name: redis
      port: 6379
      targetPort: redis
```

- [ ] **Step 3: Write `gotenberg.yaml`**

```yaml
# Converts Office files and .eml to PDF. Same flags as lab1: no JavaScript and
# no external content (tracking pixels) when rendering mail.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: paperless-gotenberg
  namespace: paperless
spec:
  replicas: 1
  selector:
    matchLabels:
      app: paperless-gotenberg
  template:
    metadata:
      labels:
        app: paperless-gotenberg
    spec:
      securityContext:
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: gotenberg
          image: docker.io/gotenberg/gotenberg:8.20
          # args, not command: keeps the image ENTRYPOINT (tini) as PID 1 to reap children.
          args:
            - gotenberg
            - --chromium-disable-javascript=true
            - --chromium-allow-list=file:///tmp/.*
          ports:
            - name: http
              containerPort: 3000
          resources:
            requests:
              cpu: 20m
              memory: 192Mi
            limits:
              cpu: '1'
              memory: 768Mi
          readinessProbe:
            httpGet:
              path: /health
              port: http
            periodSeconds: 15
          securityContext:
            allowPrivilegeEscalation: false
---
apiVersion: v1
kind: Service
metadata:
  name: paperless-gotenberg
  namespace: paperless
spec:
  selector:
    app: paperless-gotenberg
  ports:
    - name: http
      port: 3000
      targetPort: http
```

- [ ] **Step 4: Write `tika.yaml`**

```yaml
# Pinned by digest to the image lab1 runs (Tika 3.3.1); lab1 uses :latest.
# The JVM heap is capped below the container limit, since Tika is the largest
# memory consumer of the stack.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: paperless-tika
  namespace: paperless
spec:
  replicas: 1
  selector:
    matchLabels:
      app: paperless-tika
  template:
    metadata:
      labels:
        app: paperless-tika
    spec:
      securityContext:
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: tika
          image: apache/tika@sha256:90b7fa1dc018434075fce9e1d9b88b1e3d0ea6979d0cf86e116c79a8073ae973
          env:
            - name: JAVA_TOOL_OPTIONS
              value: -Xmx512m
          ports:
            - name: http
              containerPort: 9998
          resources:
            requests:
              cpu: 20m
              memory: 256Mi
            limits:
              cpu: '1'
              memory: 768Mi
          readinessProbe:
            tcpSocket:
              port: http
            periodSeconds: 15
          securityContext:
            allowPrivilegeEscalation: false
---
apiVersion: v1
kind: Service
metadata:
  name: paperless-tika
  namespace: paperless
spec:
  selector:
    app: paperless-tika
  ports:
    - name: http
      port: 9998
      targetPort: http
```

- [ ] **Step 6: Register and verify**

Add to `kustomization.yaml` resources: `postgres.yaml`, `redis.yaml`, `gotenberg.yaml`, `tika.yaml`.
Run: `kubectl apply --dry-run=client -k apps/base/paperless | tail -12`
Expected: every object listed as `created (dry run)`, no errors.
Run: `kubectl kustomize apps/base/paperless | grep -c 'memory:'` → Expected: `8` (a request and a limit for each of the four containers).

- [ ] **Step 7: Commit**

```bash
git add apps/base/paperless
git commit -m "#180 paperless: Postgres, Redis, Gotenberg and Tika

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Webserver, Service and IngressRoute

**Files:**
- Create: `apps/base/paperless/paperless.yaml`
- Create: `apps/base/paperless/ingressroute.yaml`
- Modify: `apps/base/paperless/kustomization.yaml`

**Interfaces:**
- Consumes: ConfigMaps `paperless-env`, `paperless-scripts`; PVCs from Task 1; Secrets `paperless-secrets` (keys `PAPERLESS_DBPASS`, `PAPERLESS_SECRET_KEY`) and `paperless-passwords` (key `passwords.txt`).
- Produces: Deployment label `app: paperless` (podAffinity target for the ingest pod and the backup job); Service `paperless:8000`.

- [ ] **Step 1: Write `paperless.yaml`**

```yaml
# No PAPERLESS_ADMIN_USER/PASSWORD on purpose: document_importer refuses a
# non-empty database, and the exported users (with their password hashes) are
# restored by the import. See the cutover tasks.
#
# The image starts as root (s6) and drops to uid/gid 1000 itself, so no
# runAsNonRoot here. It also chowns the mount roots at start, which is why every
# volume must be verified before the first start (see #107).
apiVersion: apps/v1
kind: Deployment
metadata:
  name: paperless
  namespace: paperless
spec:
  replicas: 1
  # RWO volumes: the old pod must be gone before the new one mounts them.
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: paperless
  template:
    metadata:
      labels:
        app: paperless
    spec:
      securityContext:
        seccompProfile:
          type: RuntimeDefault
      # Soft preference: follow the ingest pod, which shares the RWO consume
      # volume and is pinned to the webserver's node by a required affinity.
      # Without it a restart can land elsewhere (Multi-Attach).
      affinity:
        podAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              podAffinityTerm:
                labelSelector:
                  matchLabels:
                    app: paperless-ingest
                topologyKey: kubernetes.io/hostname
      containers:
        - name: paperless
          image: ghcr.io/paperless-ngx/paperless-ngx:3.0.4
          envFrom:
            - configMapRef:
                name: paperless-env
          env:
            - name: PAPERLESS_DBPASS
              valueFrom:
                secretKeyRef:
                  name: paperless-secrets
                  key: PAPERLESS_DBPASS
            - name: PAPERLESS_SECRET_KEY
              valueFrom:
                secretKeyRef:
                  name: paperless-secrets
                  key: PAPERLESS_SECRET_KEY
          ports:
            - name: http
              containerPort: 8000
          resources:
            requests:
              cpu: 200m
              memory: 768Mi
            limits:
              cpu: '2'
              memory: 2Gi
          # Migrations and the first start take minutes.
          startupProbe:
            tcpSocket:
              port: http
            periodSeconds: 10
            failureThreshold: 60
          readinessProbe:
            httpGet:
              path: /accounts/login/
              port: http
            periodSeconds: 15
          livenessProbe:
            tcpSocket:
              port: http
            periodSeconds: 30
            failureThreshold: 4
          securityContext:
            allowPrivilegeEscalation: false
          volumeMounts:
            - name: data
              mountPath: /usr/src/paperless/data
            - name: media
              mountPath: /usr/src/paperless/media
            - name: consume
              mountPath: /usr/src/paperless/consume
            - name: export
              mountPath: /usr/src/paperless/export
            - name: scripts
              mountPath: /usr/src/paperless/scripts/removepassword.py
              subPath: removepassword.py
              readOnly: true
            - name: passwords
              mountPath: /usr/src/paperless/scripts/passwords.txt
              subPath: passwords.txt
              readOnly: true
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: paperless-data
        - name: media
          persistentVolumeClaim:
            claimName: paperless-media
        - name: consume
          persistentVolumeClaim:
            claimName: paperless-consume
        - name: export
          persistentVolumeClaim:
            claimName: paperless-export
        - name: scripts
          configMap:
            name: paperless-scripts
            defaultMode: 0755
        - name: passwords
          secret:
            secretName: paperless-passwords
---
apiVersion: v1
kind: Service
metadata:
  name: paperless
  namespace: paperless
  labels:
    app: paperless
spec:
  selector:
    app: paperless
  ports:
    - name: http
      port: 8000
      targetPort: http
```

- [ ] **Step 2: Write `ingressroute.yaml`**

```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: paperless
  namespace: paperless
spec:
  entryPoints:
    - websecure
  routes:
    - kind: Rule
      match: Host(`paperless.homelab.cs-ol.de`)
      services:
        - kind: Service
          name: paperless
          port: 8000
  tls: {}
```

- [ ] **Step 3: Register and verify**

Add `paperless.yaml` and `ingressroute.yaml` to `kustomization.yaml`.
Run: `kubectl apply --dry-run=client -k apps/base/paperless | grep -E 'paperless |ingressroute' `
Expected: `deployment.apps/paperless created (dry run)`, `ingressroute.traefik.io/paperless created (dry run)`.
Run: `kubectl kustomize apps/base/paperless | grep -c PAPERLESS_ADMIN` → Expected: `0`.

- [ ] **Step 4: Commit**

```bash
git add apps/base/paperless
git commit -m "#180 paperless: webserver, service and IngressRoute

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: FTP + SMB ingest pod on the LAN address

**Files:**
- Create: `apps/base/paperless/networkattachment.yaml`
- Create: `apps/base/paperless/ingest.yaml`
- Modify: `apps/base/paperless/kustomization.yaml`

**Interfaces:**
- Consumes: PVC `paperless-consume`; Deployment label `app: paperless`; Secret `paperless-ingest` with keys `FTP_LIST` (`user:password`) and `SMB_PASSWORD` (created in Task 6).
- Produces: Deployment `paperless-ingest`, shipped with **`replicas: 0`** (lab1 still owns `192.168.1.28`; Task 9 scales it up).

- [ ] **Step 1: Write `networkattachment.yaml`**

```yaml
# Gives the ingest pod the address lab1 has today, so scanners and devices need
# no reconfiguration. MetalLB cannot do this: its pool (10.98.0.200-254) lives
# on the node subnet. Same design as mosquitto/lan-macvlan: no "gateway", so
# cluster DNS and pod traffic keep using eth0.
#
# Only one host may hold .28: lab1 must be moved to its new address and the pod
# scaled up only afterwards (two ARP answerers break both).
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: lan-macvlan
  namespace: paperless
spec:
  config: |
    {
      "cniVersion": "0.3.1",
      "name": "lan-macvlan",
      "type": "macvlan",
      "master": "ens19",
      "mode": "bridge",
      "ipam": {
        "type": "static",
        "addresses": [
          { "address": "192.168.1.28/22" }
        ]
      }
    }
```

- [ ] **Step 2: Write `ingest.yaml`** (images pinned to the digests lab1 runs; environment mirrors lab1's compose files, minus the unused sample user `alice` and the host Avahi directory)

```yaml
# Scanner ingest: proftpd (FTP, passive 50000-50100) and samba (SMB, the
# "Public" share) write into the consume folder that Paperless watches. One pod
# with one LAN address, so both protocols share 192.168.1.28.
#
# replicas: 0 until lab1 has released 192.168.1.28 (cutover runbook).
#
# Both containers write as uid/gid 33 (as on lab1); Paperless consumes as 1000,
# so the consume tree must stay world-writable, as it is on lab1.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: paperless-ingest
  namespace: paperless
spec:
  replicas: 0
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: paperless-ingest
  template:
    metadata:
      labels:
        app: paperless-ingest
      annotations:
        k8s.v1.cni.cncf.io/networks: paperless/lan-macvlan
    spec:
      # consume is RWO: run on the node where the webserver has it attached.
      affinity:
        podAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector:
                matchLabels:
                  app: paperless
              topologyKey: kubernetes.io/hostname
      initContainers:
        # lab1's consume tree is world-writable; uid 33 (ftp/smb) writes,
        # uid 1000 (Paperless) consumes. A fresh volume root is 0755.
        - name: consume-perms
          image: alpine:3.22
          command: ['sh', '-c', 'chmod 0777 /consume']
          resources:
            requests:
              cpu: 5m
              memory: 8Mi
            limits:
              cpu: 50m
              memory: 16Mi
          securityContext:
            allowPrivilegeEscalation: false
          volumeMounts:
            - name: consume
              mountPath: /consume
      containers:
        - name: proftpd
          image: kibatic/proftpd@sha256:6f3d8dc449720be2c098bdf4a9b5f07ce6ec3a7df9cd80aff9a74607b23aee65
          env:
            - name: FTP_LIST
              valueFrom:
                secretKeyRef:
                  name: paperless-ingest
                  key: FTP_LIST
            - name: USERADD_OPTIONS
              value: -o --gid 33 --uid 33
            - name: PASSIVE_MIN_PORT
              value: '50000'
            - name: PASSIVE_MAX_PORT
              value: '50100'
            - name: MASQUERADE_ADDRESS
              value: 192.168.1.28
          ports:
            - name: ftp
              containerPort: 21
          resources:
            requests:
              cpu: 10m
              memory: 32Mi
            limits:
              cpu: 200m
              memory: 128Mi
          readinessProbe:
            tcpSocket:
              port: ftp
            periodSeconds: 15
          volumeMounts:
            - name: consume
              mountPath: /home/paperless
        - name: samba
          image: ghcr.io/servercontainers/samba@sha256:155d384c4e3948f84ab8126647877bb409d7882a04173952bddf78fa547e1f9b
          env:
            - name: MODEL
              value: TimeCapsule
            - name: AVAHI_NAME
              value: paperless-scan
            - name: SAMBA_CONF_LOG_LEVEL
              value: '3'
            - name: GROUP_family
              value: '33'
            - name: UID_paperless
              value: '33'
            - name: ACCOUNT_paperless
              valueFrom:
                secretKeyRef:
                  name: paperless-ingest
                  key: SMB_PASSWORD
            - name: SAMBA_VOLUME_CONFIG_public
              value: '[Public]; path=/shares/public; guest ok = yes; read only = no; browseable = yes; force group = family;'
          ports:
            - name: smb
              containerPort: 445
          resources:
            requests:
              cpu: 20m
              memory: 64Mi
            limits:
              cpu: 500m
              memory: 256Mi
          readinessProbe:
            tcpSocket:
              port: smb
            periodSeconds: 15
          volumeMounts:
            - name: consume
              mountPath: /shares/public
      volumes:
        - name: consume
          persistentVolumeClaim:
            claimName: paperless-consume
```

- [ ] **Step 3: Register and verify**

Add `networkattachment.yaml` and `ingest.yaml` to `kustomization.yaml`.
Run: `kubectl apply --dry-run=client -k apps/base/paperless | grep -E 'ingest|macvlan'` → Expected: both listed as `created (dry run)`.
Run: `kubectl kustomize apps/base/paperless | grep -A3 'name: paperless-ingest' | grep replicas` → Expected: `replicas: 0`.

- [ ] **Step 4: Commit**

```bash
git add apps/base/paperless
git commit -m "#180 paperless: FTP and SMB ingest on the LAN address (scaled to 0)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Backup CronJob and alerts

**Files:**
- Create: `apps/base/paperless/known-hosts.yaml` (generated from the php-apache one)
- Create: `apps/base/paperless/backup.yaml`
- Create: `apps/base/paperless/prometheusrule.yaml`
- Modify: `apps/base/paperless/kustomization.yaml`

**Interfaces:**
- Consumes: Secret `paperless-backup-nas` (keys `username`, `password`); ConfigMaps `paperless-env`; PVCs `paperless-data`, `paperless-media`, `paperless-export`; Secret `paperless-secrets`.
- Produces: CronJob `paperless-backup` (used by `kubectl create job --from=cronjob/paperless-backup`).

- [ ] **Step 1: Generate `known-hosts.yaml`** (pinned NAS host key, public material, copied rather than retyped)

```bash
cd ~/Workspace/k8s-homelab
awk '/^---$/{exit} {print}' apps/base/php-apache/backup.yaml | sed 's/php-apache/paperless/g' > apps/base/paperless/known-hosts.yaml
grep -c 'ssh-rsa AAAA' apps/base/paperless/known-hosts.yaml; grep -E '^  (name|namespace):' apps/base/paperless/known-hosts.yaml
```
Expected: `1`; `name: paperless-backup-known-hosts`, `namespace: paperless`.

- [ ] **Step 2: Write `backup.yaml`**

```yaml
# Nightly: (1) document_exporter into a staging tree on the export PVC,
# (2) pg_dump next to it, (3) rsync to the NAS twice:
#   - history: /Backup/paperless/<date>/, hardlinked to the previous snapshot,
#     newest 14 kept;
#   - mirror:  /Paperless/offsite/, a single copy. mega_io syncs the whole
#     Paperless share to MEGA, so the hardlinked history stays outside it
#     (rclone/megacmd would upload every hardlink as a separate file and blow
#     the 20 GB cap).
# The exporter skips unchanged files by itself; -d removes files of deleted
# documents. The job runs on the webserver's node (RWO volumes).
# The exporter takes a lock file in media and writes its logs under data, so
# both are mounted read-write (RWO, same node as the webserver).
apiVersion: batch/v1
kind: CronJob
metadata:
  name: paperless-backup
  namespace: paperless
spec:
  schedule: '30 3 * * *'
  timeZone: Europe/Berlin
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 2
      # A Pending job (webserver absent) must not block later runs.
      activeDeadlineSeconds: 7200
      template:
        spec:
          restartPolicy: OnFailure
          affinity:
            podAffinity:
              requiredDuringSchedulingIgnoredDuringExecution:
                - labelSelector:
                    matchLabels:
                      app: paperless
                  topologyKey: kubernetes.io/hostname
          securityContext:
            seccompProfile:
              type: RuntimeDefault
          initContainers:
            - name: export
              image: ghcr.io/paperless-ngx/paperless-ngx:3.0.4
              command:
                - sh
                - -c
                - |
                  set -eu
                  mkdir -p /usr/src/paperless/export/backup/tree /usr/src/paperless/export/backup/db
                  chown -R 1000:1000 /usr/src/paperless/export/backup
                  document_exporter /usr/src/paperless/export/backup/tree -d --no-progress-bar
              envFrom:
                - configMapRef:
                    name: paperless-env
              env:
                # The command replaces the image's /init (s6), so the
                # document_exporter wrapper's with-contenv would look for
                # /run/s6/container_environment and wipe the environment.
                # S6_KEEP_ENV makes it pass the environment through.
                - name: S6_KEEP_ENV
                  value: '1'
                - name: PAPERLESS_DBPASS
                  valueFrom:
                    secretKeyRef:
                      name: paperless-secrets
                      key: PAPERLESS_DBPASS
                - name: PAPERLESS_SECRET_KEY
                  valueFrom:
                    secretKeyRef:
                      name: paperless-secrets
                      key: PAPERLESS_SECRET_KEY
              resources:
                requests:
                  cpu: 100m
                  memory: 256Mi
                limits:
                  cpu: '1'
                  memory: 1Gi
              volumeMounts:
                - name: data
                  mountPath: /usr/src/paperless/data
                - name: media
                  mountPath: /usr/src/paperless/media
                - name: export
                  mountPath: /usr/src/paperless/export
            - name: dump
              image: postgres:17
              command:
                - sh
                - -c
                - |
                  set -eu
                  rm -f /export/backup/db/*.tmp
                  OUT=/export/backup/db/paperless-$(date -u +%Y-%m-%d).dump
                  pg_dump -h paperless-db -U paperless -Fc -f "$OUT.tmp" paperless
                  pg_restore -l "$OUT.tmp" >/dev/null
                  [ "$(wc -c < "$OUT.tmp")" -gt 1000 ] || { echo "FATAL: dump implausibly small" >&2; exit 1; }
                  mv "$OUT.tmp" "$OUT"
                  # Keep the newest three dumps in the staging area.
                  ls -1t /export/backup/db/paperless-*.dump | sed -n '4,$p' | xargs -r rm -f
              env:
                - name: PGPASSWORD
                  valueFrom:
                    secretKeyRef:
                      name: paperless-secrets
                      key: POSTGRES_PASSWORD
              resources:
                requests:
                  cpu: 20m
                  memory: 64Mi
                limits:
                  cpu: 500m
                  memory: 256Mi
              volumeMounts:
                - name: export
                  mountPath: /export
          containers:
            - name: rsync
              image: alpine:3.22
              env:
                - name: KEEP
                  value: '14'
                - name: NAS_HOST
                  value: '192.168.1.240'
                - name: HIST
                  value: /share/MD0_DATA/Backup/paperless
                - name: OFFSITE
                  value: /share/MD0_DATA/Paperless/offsite
                - name: NAS_USER
                  valueFrom:
                    secretKeyRef:
                      name: paperless-backup-nas
                      key: username
                # sshpass -e reads the password from SSHPASS, keeping it off
                # the command line and out of the process list.
                - name: SSHPASS
                  valueFrom:
                    secretKeyRef:
                      name: paperless-backup-nas
                      key: password
              command:
                - sh
                - -c
                - |
                  set -eu
                  apk add --no-cache openssh-client sshpass rsync >/dev/null

                  SRC=/export/backup
                  [ -s "$SRC/tree/manifest.json" ] || { echo "FATAL: no manifest.json in the export" >&2; exit 1; }
                  KB=$(du -sk "$SRC/tree" | cut -f1)
                  [ "$KB" -gt 1024 ] || { echo "FATAL: export implausibly small (${KB} KB)" >&2; exit 1; }
                  ls "$SRC"/db/paperless-*.dump >/dev/null

                  # The NAS runs OpenSSH 7.6 with an ssh-rsa host key only.
                  SSH_OPTS="-o UserKnownHostsFile=/etc/nas/known_hosts -o StrictHostKeyChecking=yes -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa"
                  RSH="sshpass -e ssh $SSH_OPTS"
                  NAS="${NAS_USER}@${NAS_HOST}"
                  STAMP=$(date -u +%Y-%m-%d)

                  $RSH "$NAS" "mkdir -p '$HIST' '$OFFSITE'"

                  # Newest earlier snapshot, for --link-dest.
                  PREV=$($RSH "$NAS" "ls -1d '$HIST'/20* 2>/dev/null | grep -v '/$STAMP\$' | sort | tail -1" || true)
                  LINK=""
                  [ -z "$PREV" ] || LINK="--link-dest=$PREV"

                  rsync -a --no-owner --no-group $LINK -e "$RSH" "$SRC/" "$NAS:$HIST/$STAMP/"
                  rsync -a --no-owner --no-group --delete -e "$RSH" "$SRC/" "$NAS:$OFFSITE/"

                  # Keep the newest $KEEP snapshots.
                  $RSH "$NAS" "cd '$HIST' && ls -1d 20* | sort -r | sed -n '$((KEEP + 1)),\$p' | while read -r d; do rm -rf \"\$d\"; done"

                  echo "Backed up ${KB} KB to ${NAS_HOST}:${HIST}/${STAMP} and ${OFFSITE}"
              resources:
                requests:
                  cpu: 10m
                  memory: 32Mi
                limits:
                  cpu: 500m
                  memory: 256Mi
              securityContext:
                allowPrivilegeEscalation: false
              volumeMounts:
                - name: export
                  mountPath: /export
                  readOnly: true
                - name: known-hosts
                  mountPath: /etc/nas
                  readOnly: true
          volumes:
            - name: data
              persistentVolumeClaim:
                claimName: paperless-data
            - name: media
              persistentVolumeClaim:
                claimName: paperless-media
            - name: export
              persistentVolumeClaim:
                claimName: paperless-export
            - name: known-hosts
              configMap:
                name: paperless-backup-known-hosts
```

- [ ] **Step 3: Write `prometheusrule.yaml`**

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: paperless
  namespace: paperless
  labels:
    release: prometheus
spec:
  groups:
    - name: paperless
      rules:
        - alert: PaperlessDown
          expr: |
            kube_deployment_status_replicas_available{namespace="paperless", deployment="paperless"} < 1
          for: 10m
          labels:
            severity: warning
          annotations:
            summary: Paperless has no available pod
            description: paperless.homelab.cs-ol.de is down; scans keep piling up in the consume folder.
        - alert: PaperlessBackupStale
          # The metric is absent until the first successful run, so this also
          # fires if the CronJob never worked.
          expr: |
            time() - kube_cronjob_status_last_successful_time{namespace="paperless", cronjob="paperless-backup"} > 129600
              or absent(kube_cronjob_status_last_successful_time{namespace="paperless", cronjob="paperless-backup"})
          for: 30m
          labels:
            severity: warning
          annotations:
            summary: No successful Paperless backup for 36 hours
            description: >-
              The nightly export to the NAS (and from there to MEGA) has not
              succeeded. Check `kubectl -n paperless get jobs` and the job logs.
```

- [ ] **Step 4: Register and verify**

Add `known-hosts.yaml`, `backup.yaml`, `prometheusrule.yaml` to `kustomization.yaml`.
Run: `kubectl apply --dry-run=client -k apps/base/paperless | grep -E 'backup|prometheusrule'` → Expected: three objects `created (dry run)`.
Run the shell logic of the rsync script syntax-only: `kubectl kustomize apps/base/paperless | python3 -c 'import sys,yaml; d=[x for x in yaml.safe_load_all(sys.stdin) if x["kind"]=="CronJob"][0]; open("/tmp/claude-1000/-home-umueller-Workspace-k8s-homelab/1e0c7c91-7767-46d5-b0bb-4a67529663ca/scratchpad/rsync.sh","w").write(d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"][2])' && sh -n /tmp/claude-1000/-home-umueller-Workspace-k8s-homelab/1e0c7c91-7767-46d5-b0bb-4a67529663ca/scratchpad/rsync.sh && echo syntax-ok`
Expected: `syntax-ok`.

- [ ] **Step 5: Commit**

```bash
git add apps/base/paperless
git commit -m "#180 paperless: nightly export and rsync backup to the NAS, alerts

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Secrets, production overlay and Flux Kustomization

**Files:**
- Create: `apps/production/paperless/kustomization.yaml`
- Create: `apps/production/paperless/paperless-secrets.sops.yaml`
- Create: `apps/production/paperless/paperless-passwords.sops.yaml`
- Create: `apps/production/paperless/paperless-ingest.sops.yaml`
- Create: `apps/production/paperless/paperless-backup-nas.sops.yaml`
- Create: `clusters/production/paperless.yaml`

**Interfaces:**
- Produces the Secrets referenced above: `paperless-secrets` (`POSTGRES_PASSWORD`, `PAPERLESS_DBPASS`, `PAPERLESS_SECRET_KEY`), `paperless-passwords` (`passwords.txt`), `paperless-ingest` (`FTP_LIST`, `SMB_PASSWORD`), `paperless-backup-nas` (`username`, `password`).

Nothing is printed in this task. If `.sops.yaml` creation rules need `data`/`stringData` for these file names, they already match `*.sops.yaml`.

- [ ] **Step 1: Write the overlay `kustomization.yaml`**

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - ../../base/paperless
  - paperless-secrets.sops.yaml
  - paperless-passwords.sops.yaml
  - paperless-ingest.sops.yaml
  - paperless-backup-nas.sops.yaml
```

- [ ] **Step 2: Generate and encrypt the new secrets** (values from `openssl`; the Postgres password is stored under both keys)

```bash
cd ~/Workspace/k8s-homelab/apps/production/paperless
DBPW=$(openssl rand -hex 24)
kubectl create secret generic paperless-secrets -n paperless \
  --from-literal=POSTGRES_PASSWORD="$DBPW" \
  --from-literal=PAPERLESS_DBPASS="$DBPW" \
  --from-literal=PAPERLESS_SECRET_KEY="$(openssl rand -hex 32)" \
  --dry-run=client -o yaml > paperless-secrets.sops.yaml
sops --encrypt --in-place paperless-secrets.sops.yaml
unset DBPW
```

- [ ] **Step 3: Carry over the PDF passwords file** (2 lines on lab1; copied, not displayed)

```bash
P=/tmp/claude-1000/-home-umueller-Workspace-k8s-homelab/1e0c7c91-7767-46d5-b0bb-4a67529663ca/scratchpad/passwords.txt
ssh root@192.168.1.28 'cat /docker/paperless/passwords.txt' > "$P"
wc -l "$P"
kubectl create secret generic paperless-passwords -n paperless --from-file=passwords.txt="$P" --dry-run=client -o yaml > paperless-passwords.sops.yaml
sops --encrypt --in-place paperless-passwords.sops.yaml
shred -u "$P"
```
Expected: `2` lines.

- [ ] **Step 4: Carry over the FTP and SMB credentials unchanged** (so no device needs reconfiguring)

```bash
FTP=$(ssh root@192.168.1.28 "sed -nE 's/^ *FTP_LIST: *\"?([^\"]*)\"?\$/\1/p' /docker/proftpd/docker-compose.yml")
SMB=$(ssh root@192.168.1.28 "sed -nE 's/^ *ACCOUNT_paperless: *\"?([^\"]*)\"?\$/\1/p' /docker/samba/docker-compose.yml")
[ -n "$FTP" ] && [ -n "$SMB" ] && echo "both found"
case "$FTP" in paperless:*) echo "ftp user ok";; *) echo "UNEXPECTED FTP_LIST format"; esac
kubectl create secret generic paperless-ingest -n paperless \
  --from-literal=FTP_LIST="$FTP" --from-literal=SMB_PASSWORD="$SMB" \
  --dry-run=client -o yaml > paperless-ingest.sops.yaml
sops --encrypt --in-place paperless-ingest.sops.yaml
unset FTP SMB
```
Expected: `both found`, `ftp user ok`. (If `FTP_LIST` holds several `user:pass` entries separated by commas, that is fine; the whole value is copied.)

- [ ] **Step 5: Copy the NAS credentials from the php-apache Secret** (same QNAP user)

```bash
kubectl -n php-apache get secret php-apache-backup-nas -o json \
  | python3 -c 'import sys,json; s=json.load(sys.stdin); s["metadata"]={"name":"paperless-backup-nas","namespace":"paperless"}; s.pop("type",None); print(json.dumps(s))' \
  | kubectl create --dry-run=client -o yaml -f - > paperless-backup-nas.sops.yaml
sops --encrypt --in-place paperless-backup-nas.sops.yaml
```

- [ ] **Step 6: Write `clusters/production/paperless.yaml`**

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization

metadata:
  name: paperless
  namespace: flux-system

spec:
  interval: 10m

  sourceRef:
    kind: GitRepository
    name: flux-system

  path: ./apps/production/paperless

  prune: true
  wait: true

  # Decrypt *.sops.yaml Secrets with the age key in flux-system/sops-age
  decryption:
    provider: sops
    secretRef:
      name: sops-age

  dependsOn:
    - name: infrastructure
    # Provides the NetworkAttachmentDefinition CRD for the ingest LAN address.
    - name: multus
    # Provides the PrometheusRule CRD.
    - name: monitoring
```

- [ ] **Step 7: Verify nothing readable was committed**

```bash
cd ~/Workspace/k8s-homelab
for f in apps/production/paperless/*.sops.yaml; do printf '%s: ' "$f"; grep -c 'ENC\[AES256_GCM' "$f"; done
sops --decrypt apps/production/paperless/paperless-secrets.sops.yaml | grep -c 'kind: Secret'
kubectl kustomize apps/production/paperless | grep -c '^kind: Secret'
```
Expected: every file shows a count above `0`; `1`; `4`. Also `git diff --cached | grep -ci 'password: [^E]'` must be `0` after staging.

- [ ] **Step 8: Commit**

```bash
git add apps/production/paperless clusters/production/paperless.yaml
git commit -m "#180 paperless: SOPS secrets, production overlay and Flux Kustomization

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Homepage tile, PR, and a first start with an empty instance

**Files:**
- Modify: `apps/base/homepage/configmap.yaml` (Paperless tile, around lines 85-94)

- [ ] **Step 1: Repoint the Paperless tile** (drop `server`/`container`, which referenced the lab1 Docker API; the widget logs in with `HOMEPAGE_VAR_PAPERLESS_PASSWORD`, which is the user's own password, restored by the import)

Replace

```yaml
        - Paperless:
            href: http://192.168.1.28:8001/
            icon: paperless.png
            server: docker-lab1
            container: paperless-webserver-1
            widget:
                type: paperlessngx
                url: http://192.168.1.28:8001/
```

with

```yaml
        - Paperless:
            href: https://paperless.homelab.cs-ol.de/
            icon: paperless.png
            siteMonitor: http://paperless.paperless.svc.cluster.local:8000/accounts/login/
            widget:
                type: paperlessngx
                url: http://paperless.paperless.svc.cluster.local:8000/
```

Run: `kubectl kustomize apps/base/homepage >/dev/null && echo ok` → `ok`.

- [ ] **Step 2: Commit, push, open the PR, return to main**

```bash
git add apps/base/homepage/configmap.yaml
git commit -m "#180 homepage: point the Paperless tile at the cluster

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
git push
gh pr create --title "Closes #180: Migrate Paperless-ngx into the cluster" --body "$(cat <<'EOF'
Moves Paperless-ngx, FTP/SMB ingest and a NAS backup into the cluster (spec and plan under `docs/superpowers/`).

The ingest pod ships with `replicas: 0`: lab1 must give up 192.168.1.28 first (see the cutover tasks in the plan). InfluxDB (#171) must be moved before that.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
git checkout main && git pull origin main
```

- [ ] **Step 3: After the user merges: check the empty instance** (Flux pulls it in; ensure the 70 % memory gate first)

```bash
flux reconcile kustomization flux-system --with-source && flux reconcile kustomization paperless --with-source
kubectl -n paperless get pods -o wide
kubectl -n paperless get pvc
kubectl top nodes
kubectl -n paperless get pods -o wide
```
The scheduler places by requests, not by real use: node `talos-isv-pq4` was at 67-71 % real memory on 2026-10-09 with only ~150 MiB headroom to the 70 % gate. If a heavy pod (tika, paperless, gotenberg) landed on a node above ~70 %: `kubectl -n paperless delete pod <pod>` so it reschedules (repeat), or cordon that node during the deploy (`kubectl cordon <node>`, and `kubectl uncordon <node>` after). Cordoning is a cluster change the user must approve.
`PaperlessBackupStale` will fire from the merge until the first successful scheduled backup after the cutover (the empty instance fails the 1 MB check). That is expected; optionally silence it in Alertmanager.
Expected: `paperless-db-0`, redis, gotenberg, tika and `paperless` `Running`/`Ready`, each on a node; the ingest deployment at `0/0`; all four PVCs `Bound`; nodes below ~70 %.
Run: `kubectl -n paperless exec deploy/paperless -- sh -c 'ls -ld /usr/src/paperless/consume /usr/src/paperless/data /usr/src/paperless/media; id paperless'` → mount roots owned by `paperless` (1000).
Run: `curl -sk -o /dev/null -w '%{http_code}\n' https://paperless.homelab.cs-ol.de/accounts/login/` → `200`.
Run: `kubectl -n paperless get pod -l app=paperless -o jsonpath='{.items[0].spec.nodeName}'` and the same for `app=paperless-db`; note them (the backup job and the ingest pod will join the webserver's node).

If a pod does not start: `kubectl -n paperless describe pod <pod>` and logs; do not continue to the import until the empty instance is healthy.

---

### Task 8: Move the data (lab1 to cluster)

Operational runbook, run with the user: the stop commands may need `! …`. Start only after Task 7 is green.

- [ ] **Step 1: Record the baseline on lab1**

```bash
ssh root@192.168.1.28 'docker exec paperless-db-1 psql -U paperless -tAc "select count(*) from documents_document"; docker exec paperless-db-1 psql -U paperless -tAc "select count(*) from auth_user"; ls -R /docker/paperless/consume | head -20'
```
Write down the document count (602 on 2026-10-09) and the user count.

- [ ] **Step 2: Stop intake and lab1 Paperless**, then export (final state)

First stop intake so nothing new arrives mid-export. The user runs: `! ssh root@192.168.1.28 'systemctl disable --now docker-compose@proftpd docker-compose@samba'` (check the unit names with `systemctl list-units "docker-compose@*"`). Then export while the webserver container is still up (the exporter runs inside it), and stop Paperless afterwards:

```bash
ssh root@192.168.1.28 'docker exec paperless-webserver-1 sh -c "rm -rf /usr/src/paperless/export/* && document_exporter ../export -d --no-progress-bar"; ls /docker/paperless/export | head; du -sh /docker/paperless/export; test -s /docker/paperless/export/manifest.json && echo MANIFEST_OK'
```
Expected: `MANIFEST_OK` and a size close to media + archive (about 0.5-1 GB). Then the user stops and disables lab1 Paperless, so a lab1 reboot cannot start it again: `! ssh root@192.168.1.28 'systemctl disable --now docker-compose@paperless'` (check the unit name with `systemctl list-units "docker-compose@*"`).

- [ ] **Step 3: Copy the export into the cluster** (nothing else may write to the ssh stdout)

```bash
kubectl -n paperless exec deploy/paperless -- sh -c 'rm -rf /usr/src/paperless/export/import && mkdir -p /usr/src/paperless/export/import'
ssh root@192.168.1.28 'tar -C /docker/paperless/export -cf - .' | kubectl -n paperless exec -i deploy/paperless -- tar -C /usr/src/paperless/export/import -xf -
kubectl -n paperless exec deploy/paperless -- sh -c 'du -sh /usr/src/paperless/export/import; test -s /usr/src/paperless/export/import/manifest.json && echo MANIFEST_OK'
```
Verify by checksum, comparing the two sides without printing paths with spaces badly:
```bash
ssh root@192.168.1.28 'cd /docker/paperless/export && find . -type f -exec md5sum {} + | sort -k2' > /tmp/claude-1000/-home-umueller-Workspace-k8s-homelab/1e0c7c91-7767-46d5-b0bb-4a67529663ca/scratchpad/lab1.md5
kubectl -n paperless exec deploy/paperless -- sh -c 'cd /usr/src/paperless/export/import && find . -type f -exec md5sum {} + | sort -k2' > /tmp/claude-1000/-home-umueller-Workspace-k8s-homelab/1e0c7c91-7767-46d5-b0bb-4a67529663ca/scratchpad/k8s.md5
diff /tmp/claude-1000/-home-umueller-Workspace-k8s-homelab/1e0c7c91-7767-46d5-b0bb-4a67529663ca/scratchpad/lab1.md5 /tmp/claude-1000/-home-umueller-Workspace-k8s-homelab/1e0c7c91-7767-46d5-b0bb-4a67529663ca/scratchpad/k8s.md5 && echo IDENTICAL
```
Expected: `MANIFEST_OK` and `IDENTICAL`.

- [ ] **Step 4: Import** (the database is empty: no admin env was set)

```bash
kubectl -n paperless exec deploy/paperless -- document_importer /usr/src/paperless/export/import
```
Expected: ends without a traceback, reports the number of documents. If it says the database is not empty, stop: check `kubectl -n paperless exec deploy/paperless -- sh -c 'python3 manage.py shell -c "from documents.models import Document; print(Document.objects.count())"'`; if a stray user/document exists, wipe the Postgres volume (scale `paperless` to 0, delete the StatefulSet's PVC, scale back) and retry; do not force.

- [ ] **Step 5: Verify against the baseline**

```bash
kubectl -n paperless exec paperless-db-0 -- psql -U paperless -tAc "select count(*) from documents_document"
kubectl -n paperless exec paperless-db-0 -- psql -U paperless -tAc "select count(*) from auth_user"
kubectl -n paperless exec deploy/paperless -- sh -c 'ls /usr/src/paperless/media/documents/originals | wc -l; ls /usr/src/paperless/media/documents/archive | wc -l'
```
Expected: the document and user counts equal the lab1 baseline from Step 1; the file counts match the number of `documents/originals` and `documents/archive` entries in the export (`ls import/documents/originals | wc -l` on the same pod; the md5 diff in Step 3 already proves the export arrived intact). Then restart the webserver so it re-indexes: `kubectl -n paperless rollout restart deploy/paperless`, `kubectl -n paperless rollout status deploy/paperless --timeout=600s`, then `kubectl -n paperless exec deploy/paperless -- document_index reindex`.

- [ ] **Step 6: Copy lab1's consume backlog and folder tree** (after the import and reindex, so consumption does not run against an empty database and attach the backlog to nothing)

```bash
ssh root@192.168.1.28 'tar -C /docker/paperless/consume -cpf - .' | kubectl -n paperless exec -i deploy/paperless -- tar -C /usr/src/paperless/consume -xpf -
kubectl -n paperless exec deploy/paperless -- chmod -R a+rwX /usr/src/paperless/consume
```
Verify: `ssh root@192.168.1.28 'cd /docker/paperless/consume && find . -type d | sort'` and `kubectl -n paperless exec deploy/paperless -- sh -c 'cd /usr/src/paperless/consume && find . -type d | sort'` list the same directories (`jule`, `meike`, `svenja`, `udo` exist on lab1 as of 2026-10-09), and `kubectl -n paperless exec deploy/paperless -- ls -la /usr/src/paperless/consume` shows them world-writable. Watch the backlog being consumed: `kubectl -n paperless logs deploy/paperless --since=5m | grep -i consum`.

- [ ] **Step 7: The user logs in and spot-checks**

Ask the user to open `https://paperless.homelab.cs-ol.de`, log in with their existing account, search for a known document, open its PDF and check the thumbnail. Confirm the Homepage tile shows document counts (`kubectl -n homepage rollout restart deploy/homepage` after the ConfigMap changed).

---

### Task 9: Move the scanner address from lab1 to the cluster

**Gate (all must hold; stop and tell the user otherwise):**
- InfluxDB is no longer on lab1. #172 is merged (2026-10-09) but that only deployed an empty instance: the data restore, HA repointed to `influxdb.influxdb.svc:8086` and lab1's `influxdb` container stopped must be done too. Check `ssh root@192.168.1.28 'docker ps --format "{{.Names}}" | grep -i influx'` prints nothing and `kubectl -n influxdb get pods` is Ready.
- Task 8 verified.
- lab1's `proftpd` and `samba` are stopped (Task 8 Step 2).

- [ ] **Step 1: Pick and reserve a new IP for lab1 in UniFi**

Use the `unifi` skill (read first, then write only after the user confirms the exact change): list clients/fixed IPs, find a free address in the same /22 that is outside the DHCP range (candidates seen unanswered on 2026-10-09: 192.168.1.62, .64, .66), and create a fixed-IP reservation for lab1's MAC `bc:24:11:ca:26:03`. Write the chosen address down as `LAB1_NEW`.
Verify: `ping -c2 $LAB1_NEW` must fail now (nobody holds it).

- [ ] **Step 2: Give lab1 the new address next to the old one** (the session survives)

```bash
ssh root@192.168.1.28 "nmcli con mod ens18 +ipv4.addresses $LAB1_NEW/22 && nmcli con up ens18"
ping -c2 $LAB1_NEW && ssh root@$LAB1_NEW hostname
```
Expected: replies, and `hostname` prints lab1's name. If it fails: `ssh root@192.168.1.28 "nmcli con mod ens18 -ipv4.addresses $LAB1_NEW/22 && nmcli con up ens18"` and stop.

- [ ] **Step 3: Remove the old address from lab1**

```bash
ssh root@$LAB1_NEW "nmcli con mod ens18 -ipv4.addresses 192.168.1.28/22 && nmcli con up ens18; ip -4 -br a show ens18"
sleep 5; ping -c2 -W2 192.168.1.28 && echo "STILL ANSWERING" || echo "192.168.1.28 is free"
```
(The command ends the old ssh session, which is expected; use `$LAB1_NEW`.) Expected: `192.168.1.28 is free` and lab1 shows only `$LAB1_NEW/22`. The NAS exports (`/Paperless`, `/homelab`) have no client restrictions (`showmount -e` shows none), so lab1's NFS mounts keep working; verify: `ssh root@$LAB1_NEW 'findmnt -t nfs | head -3; ls /docker | head -3'`.

- [ ] **Step 4: Start the cluster ingest on 192.168.1.28**

Suspend Flux first, otherwise a reconcile within 10 minutes scales the pod back to 0 mid-test (it is resumed after the merge in Step 5):

```bash
flux suspend kustomization paperless
kubectl -n paperless scale deploy/paperless-ingest --replicas=1
kubectl -n paperless rollout status deploy/paperless-ingest --timeout=180s
kubectl -n paperless exec deploy/paperless-ingest -c samba -- ip -4 -br a | grep 192.168.1.28
ping -c2 192.168.1.28; nc -zv 192.168.1.28 21; nc -zv 192.168.1.28 445
```
Expected: the pod has `192.168.1.28` on `net1`; the ports answer. Note: the pod's node cannot reach its own macvlan address (macvlan limitation), the test runs from the workstation.
The same must be committed to Git so Flux does not scale it back to 0: in the next step.

- [ ] **Step 5: Make `replicas: 1` the declared state**

Edit `apps/base/paperless/ingest.yaml`: `replicas: 0` → `replicas: 1`, and update the header comment to say lab1 released `192.168.1.28` on the cutover date. Then:

```bash
git checkout -b 180-paperless-ingest-on
git add apps/base/paperless/ingest.yaml
git commit -m "#180 paperless: run the ingest pod on 192.168.1.28

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
git push -u origin 180-paperless-ingest-on
gh pr create --title "Closes #180: run the Paperless ingest pod (lab1 released the address)" --body "$(cat <<'EOF'
lab1 moved to its new address; the ingest pod now serves FTP and SMB on 192.168.1.28.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
git checkout main && git pull origin main
```
Flux stays suspended (Step 4) until the user merges the PR; then run `flux resume kustomization paperless`.

- [ ] **Step 6: End-to-end ingest tests (the "Review Focus" input classes)**

From the workstation:
```bash
T=/tmp/claude-1000/-home-umueller-Workspace-k8s-homelab/1e0c7c91-7767-46d5-b0bb-4a67529663ca/scratchpad
printf '%%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj 2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj 3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%%%EOF\n' > $T/ingest-test.pdf
# SMB into an existing and a new subfolder
smbclient //192.168.1.28/Public -U paperless%"$(kubectl -n paperless get secret paperless-ingest -o jsonpath='{.data.SMB_PASSWORD}' | base64 -d)" -c "put $T/ingest-test.pdf udo/smb-test-existing.pdf; mkdir newsub; put $T/ingest-test.pdf newsub/smb-test-new.pdf"
# FTP
curl -s -T $T/ingest-test.pdf --user "$(kubectl -n paperless get secret paperless-ingest -o jsonpath='{.data.FTP_LIST}' | base64 -d | cut -d';' -f1)" ftp://192.168.1.28/ftp-test.pdf
sleep 60
kubectl -n paperless exec deploy/paperless -- sh -c 'find /usr/src/paperless/consume -type f | head; ls -ld /usr/src/paperless/consume/newsub'
kubectl -n paperless logs deploy/paperless --since=3m | grep -iE 'consum|error' | tail -15
```
Expected: the three test files are picked up and removed from `consume` (the content is a minimal PDF, so Paperless may report a parse failure; what counts is that the file was picked up: `Consuming` appears in the log for each, or it moved to `failed`). If `newsub` stays and its files remain: the directory/file mode is too restrictive for uid 1000: `kubectl -n paperless exec deploy/paperless-ingest -c samba -- chmod -R a+rwX /shares/public` and note it in the follow-ups. The consume root and the migrated subfolders must be world-writable (the ingest pod's `consume-perms` initContainer and Task 8 Step 6 do it). If the FTP `put` is refused outright (not merely left unconsumed), the cause is the permissions: `kubectl -n paperless exec deploy/paperless-ingest -c samba -- chmod -R a+rwX /shares/public`. The user then scans a real page with the real scanner and checks that it appears; delete the test documents in the UI.

- [ ] **Step 7: Update everything that used lab1's old address** (now `$LAB1_NEW`)

- `~/.ssh/config`: `Host lab1` → `HostName $LAB1_NEW`.
- `homelab` repo (user merges there): `inventory/hosts.yml` (lines for `192.168.1.28` under lab1; **the file is CRLF, keep it and check `git diff --stat`**), `roles/qnapexporter/defaults/main.yml` (`qnapexporter_stage_host`). Branch `180-lab1-new-ip` there, PR, no direct commit to main.
- `k8s-homelab` `apps/base/homepage/configmap.yaml`: `docker.yaml` → `docker-lab1: host: $LAB1_NEW`. (The InfluxDB tile at lines ~131-132 is handled by #171.)
- Check Pi-hole local DNS, Uptime Kuma and Prometheus targets for `192.168.1.28`/`lab1`; list hits to the user.
Then `kubectl -n homepage rollout restart deploy/homepage`. Commit the k8s-homelab part on a branch `180-lab1-new-ip` and open a PR as in Step 5.

- [ ] **Step 8: Rollback, if the scanner path breaks** (keep available through the whole verification)

```bash
kubectl -n paperless scale deploy/paperless-ingest --replicas=0     # (suspend Flux first)
ssh root@$LAB1_NEW "nmcli con mod ens18 +ipv4.addresses 192.168.1.28/22 && nmcli con up ens18"
ssh root@192.168.1.28 'systemctl enable --now docker-compose@proftpd docker-compose@samba'
```
(Paperless on lab1 can be resumed with `docker compose up -d` in `/docker/paperless`; its NFS volumes were not touched.) While lab1's proftpd/samba are back but lab1's Paperless is stopped, scans pile up in lab1's consume folder: either restart lab1 Paperless (`docker compose up -d` in `/docker/paperless`) or copy the files over later.

---

### Task 10: Backup and restore test, MEGA check

- [ ] **Step 1: Run the backup once**

```bash
kubectl -n paperless create job --from=cronjob/paperless-backup backup-manual-1
kubectl -n paperless wait --for=condition=complete job/backup-manual-1 --timeout=900s
kubectl -n paperless logs job/backup-manual-1 -c rsync
```
Expected: `Backed up … KB to 192.168.1.240:/share/MD0_DATA/Backup/paperless/<date> and /share/MD0_DATA/Paperless/offsite`. If `export` fails (e.g. `document_exporter: not found` or a permission error), read `kubectl -n paperless logs job/backup-manual-1 -c export` and fix the command in `backup.yaml` (the exporter wrapper must run as root and drop to uid 1000).

- [ ] **Step 2: Run it a second time and check the hardlinks**

```bash
kubectl -n paperless delete job backup-manual-1
kubectl -n paperless create job --from=cronjob/paperless-backup backup-manual-2
kubectl -n paperless wait --for=condition=complete job/backup-manual-2 --timeout=900s
sshpass -e ssh $SSH_OPTS "$NAS_USER@192.168.1.240" 'du -sh /share/MD0_DATA/Backup/paperless/*; ls -l /share/MD0_DATA/Paperless/offsite | head'
```
(Reuse `NAS_USER`/`SSHPASS`/`SSH_OPTS` from Task 0 Step 2; same-day runs write into the same dated folder, so the second `du` stays about the size of the first: that proves nothing is duplicated.) Expected: one dated snapshot, `offsite/` has `tree` and `db`.

- [ ] **Step 3: Restore test into a scratch instance**

Apply the memory gate first: `kubectl top nodes`; skip or defer this test if any node is above ~70 %, since it starts a second full stack.

Import the NAS copy into a throwaway Postgres + Paperless pair in a scratch namespace, built from the same manifests with empty PVCs. Leave out the ingest pod and the NetworkAttachmentDefinition (they would claim `192.168.1.28`), the IngressRoute and the CronJob.

```bash
cd ~/Workspace/k8s-homelab
NS=paperless-restore-test
kubectl create namespace $NS
kubectl kustomize apps/base/paperless \
  | python3 -c '
import sys, yaml
skip = {("Deployment","paperless-ingest"), ("NetworkAttachmentDefinition","lan-macvlan"),
        ("IngressRoute","paperless"), ("CronJob","paperless-backup"), ("PrometheusRule","paperless"),
        ("Namespace","paperless")}
out = []
for d in yaml.safe_load_all(sys.stdin):
    if (d["kind"], d["metadata"]["name"]) in skip: continue
    d["metadata"]["namespace"] = "'$NS'"
    out.append(d)
print(yaml.safe_dump_all(out))' | kubectl apply -f -
for s in paperless-secrets paperless-passwords; do
  kubectl -n paperless get secret $s -o json \
   | python3 -c 'import sys,json; s=json.load(sys.stdin); s["metadata"]={"name":s["metadata"]["name"],"namespace":"'$NS'"}; print(json.dumps(s))' | kubectl apply -f -
done
kubectl -n $NS rollout status deploy/paperless --timeout=600s
sshpass -e ssh $SSH_OPTS "$NAS_USER@192.168.1.240" 'tar -C /share/MD0_DATA/Paperless/offsite/tree -cf - .' \
  | kubectl -n $NS exec -i deploy/paperless -- sh -c 'mkdir -p /usr/src/paperless/export/import && tar -C /usr/src/paperless/export/import -xf -'
kubectl -n $NS exec deploy/paperless -- document_importer /usr/src/paperless/export/import
kubectl -n $NS exec paperless-db-0 -- psql -U paperless -tAc "select count(*) from documents_document"
kubectl -n paperless exec paperless-db-0 -- psql -U paperless -tAc "select count(*) from documents_document"
# Validate the dump as well (copy one dump from the NAS offsite/db/ and list it)
sshpass -e ssh $SSH_OPTS "$NAS_USER@192.168.1.240" 'cat $(ls -1t /share/MD0_DATA/Paperless/offsite/db/paperless-*.dump | head -1)' > /tmp/claude-1000/-home-umueller-Workspace-k8s-homelab/1e0c7c91-7767-46d5-b0bb-4a67529663ca/scratchpad/restore.dump
kubectl -n $NS cp /tmp/claude-1000/-home-umueller-Workspace-k8s-homelab/1e0c7c91-7767-46d5-b0bb-4a67529663ca/scratchpad/restore.dump paperless-db-0:/tmp/restore.dump
kubectl -n $NS exec paperless-db-0 -- pg_restore -l /tmp/restore.dump | head
# optionally restore into a scratch database
kubectl -n $NS exec paperless-db-0 -- sh -c 'createdb -U paperless scratch && pg_restore -U paperless -d scratch /tmp/restore.dump && echo DUMP_RESTORE_OK'
kubectl delete namespace $NS
# The namespace deletion leaves the Released Retain PVs behind; their Longhorn volumes keep storage until deleted.
kubectl get pv | grep paperless-restore-test
kubectl delete pv <names from the line above>
```
Expected: both counts are equal, `pg_restore -l` lists the dump's contents, and no `paperless-restore-test` PVs remain. (If the scratch pods cannot land on one node because of the RWO volumes, that is fine here: each scratch volume is used by one pod, except `paperless-consume` and `paperless-export`, which the single scratch webserver owns alone.)

- [ ] **Step 4: MEGA cap check**

```bash
sshpass -e ssh $SSH_OPTS "$NAS_USER@192.168.1.240" 'du -sh /share/MD0_DATA/Paperless; du -sh /share/MD0_DATA/Paperless/* | sort -h | tail -8'
```
Expected: the synced share is well below 15 GB. Tell the user the figure; stale lab1 directories under `/Paperless` (`pgdata`, `redisdata`, `data`, `media`) are still synced by `mega_io` until they are retired in the decommission phase, and the live Postgres directory is not a useful offsite copy: recommend excluding or deleting them after the two-week rollback window.

- [ ] **Step 5: Alert wiring**

Run: `kubectl -n paperless get prometheusrule paperless`. Check in Prometheus (`/rules`, or Grafana's Alerting page) that `PaperlessDown` and `PaperlessBackupStale` are loaded and `PaperlessBackupStale` is **not** firing after the manual run (its metric `kube_cronjob_status_last_successful_time` is only set by scheduled runs: if it still fires, wait for the first nightly run at 03:30 and re-check next day, and say so in the report).

- [ ] **Step 6: Clean up**

```bash
kubectl -n paperless delete job backup-manual-2
kubectl -n paperless exec deploy/paperless -- rm -rf /usr/src/paperless/export/import
```

---

### Task 11: Decommission on lab1 and document

- [ ] **Step 1: Disable the lab1 units** (compose files and volumes stay for rollback)

Paperless, proftpd and samba were already disabled in Task 8 Step 2 (proftpd/samba) and Step 2's Paperless stop. Verify: `ssh root@$LAB1_NEW 'systemctl is-enabled docker-compose@paperless docker-compose@proftpd docker-compose@samba'` prints `disabled` three times (adjust unit names to `systemctl list-units "docker-compose@*"`); disable any that is not. Verify: `ssh root@$LAB1_NEW 'docker ps --format "{{.Names}}"'` lists neither Paperless nor proftpd nor samba. `mega_io` and (until #171) InfluxDB stay.

- [ ] **Step 2: Update #107 and #180**

```bash
gh issue comment 107 --body "Paperless migrated to the cluster (#180): ingest on 192.168.1.28 (cluster), lab1 now at <new IP>. Decision 'scanner ingest' resolved: FTP and SMB. Backup: nightly export to the NAS (mega_io on lab1 syncs the offsite/ mirror). Remaining: retire lab1's stale Paperless NFS dirs after the rollback window."
```
Tick the matching boxes in #180's body; close #180 once the PRs are merged and the nightly backup (03:30) has run once from the CronJob schedule.

- [ ] **Step 3: Update the project notes**

Update the memory files `lab-decommission-state.md` (Paperless done, lab1 has a new IP, what is left: `mega_io` stays, stale NFS dirs, ownership repair, InfluxDB) and `mega-offsite-backup.md` (the `offsite/` mirror is the thing to sync; hardlinked history lives outside the synced share).

- [ ] **Step 4: Write the second-brain entry** per `~/Workspace/second-brain/CLAUDE.md`: topic `kubernetes-homelab`, with the findings worth keeping: MetalLB cannot serve LAN addresses (macvlan NAD instead), `document_exporter` 3.0.4 has no `--delta`, `mega_io` syncs the whole share so hardlinked backups must stay outside it, the IP swap order (secondary address, verify, remove), and any cutover surprises. Cache any web pages read as source notes.

---

## Self-review (done while writing)

- **Spec coverage:** components (Tasks 1-4), access/secrets/mail (Tasks 1, 3, 6), backup (Task 5, run in Task 10), cutover and rollback (Tasks 8, 9), alerts (Task 5), out-of-scope items left alone. Spec corrections in Task 0.
- **Placeholders:** the only runtime values are `$LAB1_NEW` (chosen in Task 9 Step 1 with the user in UniFi, cannot be known earlier) and `<new IP>` in the #107 comment, both set from that step.
- **Names:** Secrets and keys match across Tasks 2-6 (`paperless-secrets`: `POSTGRES_PASSWORD`, `PAPERLESS_DBPASS`, `PAPERLESS_SECRET_KEY`; `paperless-ingest`: `FTP_LIST`, `SMB_PASSWORD`; `paperless-backup-nas`; `paperless-passwords`). Services match `paperless-env` (`paperless-db`, `paperless-redis`, `paperless-gotenberg`, `paperless-tika`). PVC names match every `claimName`. Label `app: paperless` is the affinity target in Tasks 4 and 5.
