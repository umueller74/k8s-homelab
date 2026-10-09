# Paperless-ngx migration to the cluster: design

Date: 2026-10-09. Part of phase 4 of #107 (lab1 decommission).

## Goal

Move Paperless-ngx from lab1 (Docker) into the Talos cluster, together with the two scanner
ingest paths (FTP and SMB) and a NAS backup. Success means:

- All documents (602 in the database on 2026-10-09, plus the `consume/` backlog) are in the cluster and match lab1.
- The scanner delivers into the cluster through FTP and SMB, on the same address as today (`192.168.1.28`).
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
| paperless-ingest | Deployment, 1 replica | `consume` PVC | One pod, two containers: proftpd (passive 50000-50100) and samba (`Public` share). Macvlan LAN address `192.168.1.28/22`. |

**Ingest address.** The scanners and other devices use lab1's own address `192.168.1.28`, and
reconfiguring them is not wanted. MetalLB cannot serve it (its pool `10.98.0.200-254` is on the node
subnet), so the ingest pod gets a Multus macvlan address like Mosquitto does. Before the pod starts,
lab1 is moved to a new, reserved address (UniFi fixed IP plus NetworkManager), and everything that
used lab1's old address is updated. FTP and SMB credentials stay unchanged for the same reason.

The ingest pod mounts `consume` together with the webserver. An RWO volume is enforced per node, so
the pod is scheduled onto the webserver's node with `podAffinity`, as the php-apache backup job is.
The ingest pod has a root initContainer that makes the consume root `0777`, and the webserver has a
soft `podAffinity` to the ingest pod's node, so a webserver restart does not land elsewhere
(Multi-Attach on the RWO consume volume).
Both ingest containers write as uid/gid 33 (as on lab1) while Paperless consumes as 1000, so the
consume tree stays world-writable.

**Pre-consume script.** lab1 mounts `removepassword.py` (unlocks encrypted PDFs, extracts PDF
attachments) and `passwords.txt` into the webserver. The script goes into a ConfigMap, the passwords
file into a SOPS Secret.

Images, ports, users and share definitions for proftpd and samba are taken from lab1's compose
files; proftpd and samba are pinned by digest to the images lab1 runs, Tika to its 3.3.1 digest.

## Access, secrets, mail

- Traefik `IngressRoute` at `paperless.homelab.cs-ol.de`, wildcard certificate.
- `*.sops.yaml` Secrets: Postgres password, `PAPERLESS_SECRET_KEY`, FTP and SMB users, NAS backup
  credentials, `passwords.txt`. No admin user is created: the exported users are restored by the
  import. The Postgres password and `PAPERLESS_SECRET_KEY` are new random values. The FTP/SMB
  credentials, the NAS backup credentials (the existing QNAP backup user) and `passwords.txt` are
  carried over unchanged, so no device has to be reconfigured.
- Flux Kustomization `clusters/production/paperless.yaml`: SOPS decryption enabled,
  `dependsOn: infrastructure`, `wait: true`.
- Mail through `smtp-relay.smtp-relay.svc.cluster.local:587`, sender `@cs-ol.de`.
- Homepage: Paperless tile and widget repointed to the new URL. The change is merged together with
  the manifests, so the tile shows login errors until the import is done; then
  `kubectl -n homepage rollout restart deploy/homepage`.

## Backup (rsync to the NAS)

A nightly CronJob in the php-apache style (pinned NAS host key, `sshpass -e`, no secrets on the
command line):

1. `document_exporter -d` (no zip) into a staging tree on the export PVC, plus `pg_dump` with a
   dated name. paperless-ngx 3.0.4 has no `--delta` option: the exporter skips unchanged files on
   its own and `-d` removes files of deleted documents.
2. `rsync -a` over SSH to `192.168.1.240:/share/MD0_DATA/Backup/paperless/<YYYY-MM-DD>/` with
   `--link-dest=<previous snapshot>`: unchanged files are hardlinked, so history costs little extra
   space. A second `rsync -a --delete` keeps a single mirror in
   `/share/MD0_DATA/Paperless/offsite/`. `mega_io` syncs the whole `Paperless` share to MEGA, so the
   hardlinked history stays outside it (every hardlink would be uploaded as its own file).
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
2. Run `document_exporter` on lab1. Stop and disable (`systemctl disable --now`) lab1's Paperless,
   proftpd and samba units so a reboot cannot start them. Keep the compose files on disk.
3. Run `document_importer` in the cluster. Compare the document count and a sample of file
   checksums against lab1's export. Then copy lab1's consume backlog and folder tree into the
   consume volume (after the import, so consumption does not run against an empty database).
4. Give lab1 its new address (secondary address first, verify, then remove `.28`), scale the ingest
   pod up on `192.168.1.28`, test FTP and SMB drops (existing and new subfolder), then update the
   references to lab1's old address and Homepage.
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

- Nothing about images remains open: Paperless 3.0.4 and the proftpd/samba/Tika digests are pinned in the plan.
- PVC sizes (measure `media` and `data` on the NAS, add headroom).
- The new address for lab1 (picked in UniFi at cutover time); the `homelab` repo needs a PR for the
  inventory and `qnapexporter` defaults (user merges those).
- InfluxDB (#171) must be fully moved before lab1's address changes: HA writes to
  `192.168.1.28:8086` until it is repointed.
