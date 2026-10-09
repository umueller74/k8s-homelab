# Paperless-ngx migration to the cluster: design

Date: 2026-10-09. Part of phase 4 of #107 (lab1 decommission).

## Goal

Move Paperless-ngx from lab1 (Docker) into the Talos cluster, together with the two scanner
ingest paths (FTP and SMB) and a NAS backup. Success means:

- All documents (542 at 2026-09-27, plus the `consume/` backlog) are in the cluster and match lab1.
- The scanner delivers into the cluster through FTP and SMB.
- A nightly backup lands on the NAS and a restore has been shown to work.
- lab1's Paperless, proftpd and samba units are disabled, with their compose files kept for rollback.

## Decisions already taken

- **Ingest:** both FTP and SMB (decided 2026-10-09).
- **Offsite:** nightly export to the NAS; `mega_io` stays on lab1 and keeps syncing the NAS folder
  (decided 2026-10-09). MEGA is a free account with a 20 GB total cap.
- **Storage:** Longhorn PVCs only. Nothing live stays on the QNAP NFS share (Postgres and SQLite on
  that share have already corrupted data; see #107).
- **Migration method:** `document_exporter` on lab1, `document_importer` in the cluster. No raw
  Postgres or media copy.
- **Node RAM stays as it is.** Every pod gets requests and limits. Start only if nodes are below
  ~70 % memory (49-64 % on 2026-10-09).

## Approach

Plain Kustomize manifests in `apps/base/paperless/` and `apps/production/paperless/`, plus
`clusters/production/paperless.yaml`, like Mosquitto, Semaphore and php-apache. No community Helm
chart: the stack is small and the repo's SOPS and Kustomize conventions fit better.

## Components (namespace `paperless`)

| Component | Kind | Storage | Notes |
|---|---|---|---|
| Postgres 17 | StatefulSet | Longhorn PVC | Same major version as lab1, so no upgrade is mixed into the move. |
| Redis | Deployment | none | |
| Gotenberg | Deployment | none | Small memory limit. |
| Tika | Deployment | none | Small memory limit. |
| Paperless webserver | Deployment, 1 replica | PVCs `data`, `media`, `consume` | RWO volumes, single replica. |
| proftpd | Deployment | `consume` PVC | MetalLB LoadBalancer IP, narrowed passive port range (lab1 uses 50000-50100). |
| samba | Deployment | `consume` PVC | Own LoadBalancer service. |

proftpd and samba must mount `consume` while the webserver does too. An RWO volume is enforced per
node, so they are scheduled onto the webserver's node with `podAffinity`, the same way the
php-apache backup job does. Their uid/gid must match the Paperless user so files can be consumed
and removed.

Images, ports, users and share definitions for proftpd and samba are taken from lab1's compose
files (read-only). The `plan` step records them.

## Access, secrets, mail

- Traefik `IngressRoute` at `paperless.homelab.cs-ol.de`, wildcard certificate.
- `*.sops.yaml` Secrets: Postgres password, `PAPERLESS_SECRET_KEY`, admin password, FTP and SMB
  users, NAS backup credentials. All values are newly generated, none copied from lab1.
- Flux Kustomization `clusters/production/paperless.yaml`: SOPS decryption enabled,
  `dependsOn: infrastructure`, `wait: true`.
- Mail through `smtp-relay.smtp-relay.svc.cluster.local:587`, sender `@cs-ol.de`.
- Homepage: Paperless tile and widget repointed to the new URL after cutover; then
  `kubectl -n homepage rollout restart deploy/homepage`.

## Backup (rsync to the NAS)

A nightly CronJob in the php-apache style (pinned NAS host key, `sshpass -e`, no secrets on the
command line):

1. `document_exporter --delta` (no zip) into a staging directory, plus `pg_dump` with a dated name.
2. `rsync -a --delete --link-dest=<previous snapshot>` over SSH to
   `192.168.1.240:/share/MD0_DATA/Backup/paperless/<YYYY-MM-DD>/`. Unchanged files are hardlinked,
   so history costs little extra space.
3. Retention: keep the newest 14 snapshots.
4. Size sanity check before upload (as in php-apache) and a `PrometheusRule` alert when the last
   successful run is older than 36 hours.

The CronJob must reach the staging area and the Paperless data. It runs the Paperless image so
`document_exporter` is available, and mounts the same PVCs via `podAffinity`.

**To verify before the plan:**
- `rsync` is installed on the QNAP (the NAS runs OpenSSH 7.6). Fallback: the NAS rsync daemon on
  port 873.
- The snapshot folder is inside the path `mega_io` syncs, and its size stays under the 20 GB MEGA
  cap together with the other services. If not, `mega_io` syncs only the latest snapshot or a
  compressed form.

## Cutover

Follows the phase-2 recipe in #107.

1. Merge with the app deployed empty. Check that every pod starts within its limits and that
   ownership/`fsGroup` is correct.
2. Run `document_exporter` on lab1. Stop lab1 Paperless, proftpd and samba. Keep the compose files
   on disk.
3. Run `document_importer` in the cluster. Compare the document count and a sample of file
   checksums against lab1's export.
4. Move the FTP and SMB service IPs, repoint the scanner, then Homepage.
5. Run the backup Job once with `kubectl create job --from=cronjob/paperless-backup` and verify
   the snapshot on the NAS.
6. Restore test: import the snapshot into a scratch instance or namespace.

**Rollback:** scale the cluster app to 0 and `systemctl enable --now` the lab1 units. lab1's NAS
volumes remain untouched until the migration is verified.

**Risks**
- Memory pressure from Tika and Gotenberg (1.5-2.5 GiB estimated). Mitigation: tight limits and the
  70 % gate.
- Scanner firmware may be pinned to an FTP or SMB address. Mitigation: if it cannot be repointed,
  give the cluster service lab1's address after lab1's unit is disabled.
- `document_importer` needs a matching Paperless version. Mitigation: pin the cluster image to the
  version running on lab1 for the import, then upgrade separately.

## Out of scope

- Moving `mega_io` into the cluster.
- The ownership repair of `homelab/docker` and the fstab/Ansible NFS path (other #107 items).
- Paperless upgrades or configuration changes beyond what the import requires.

## Open items (resolved in the implementation plan)

- Exact image tags (match lab1's Paperless version) and proftpd/samba images.
- MetalLB IPs for FTP and SMB.
- PVC sizes (measure `media` and `data` on the NAS, add headroom).
- Whether the `homelab` repo needs a PR for the lab1 unit changes (user merges those).
