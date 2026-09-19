# Home Assistant: Docker Compose → Kubernetes migration

**Date:** 2026-09-19
**Issue:** [#61](https://github.com/umueller74/k8s-homelab/issues/61)
**Status:** Approved design, not yet implemented

---

## 1. Goal

Move Home Assistant from the Docker Compose stack on `lab1` into the Talos Kubernetes
cluster with full feature parity. Three capabilities must survive the move:

1. **Multicast discovery** on the `192.168.0.0/22` LAN.
2. **The SONOFF Zigbee dongle**, reached through a node that is labelled from detection
   rather than by hand.
3. **All existing data**, either migrated or reused.

## 2. Current state

### 2.1 The compose stack

`/docker/homeassistant/docker-compose.yml` on `lab1`:

| Property | Value |
|---|---|
| Image | `docker.io/homeassistant/home-assistant:latest` |
| Network | `network_mode: host` |
| Capabilities | drops 22, adds `NET_ADMIN`, `NET_RAW` |
| Device | `/dev/ttyACM0` |
| Volumes | `/docker/homeassistant/config:/config`, `/var/run/dbus:ro` |
| Hostname | `macbook.muellernetz.de` |
| Restart | `unless-stopped` |

`/dev/ttyACM0` resolves to
`usb-ITEAD_SONOFF_Zigbee_3.0_USB_Dongle_Plus_V2_20230221133841-if00` — a SONOFF
ZBDongle-P V2, USB ID `1a86:55d4`, consumed by the `zha` integration.

### 2.2 Physical and network topology

| Component | Location | Network |
|---|---|---|
| `lab1` (`macbook`) | Proxmox host `pve`, VM 100 | `vmbr0` untagged, `192.168.1.28/22` |
| `talos1/2/3` | Proxmox host `pve2`, VMIDs 900–902 | `vmbr0` **`tag=99`**, `10.98.0.10-12` |

Two consequences drive the entire design:

- **The dongle and the cluster are on different physical hosts.** `pve` VM 100 holds
  `usb1: host=1a86:55d4`. No Talos node can see it.
- **The Talos nodes have no LAN interface.** Their single NIC is VLAN 99. Multicast from
  `192.168.0.0/22` cannot reach them by any software means — not `hostNetwork`, not a
  CNI change. An additional interface is a prerequisite, not an optimisation.

Both hosts bridge the same `vmbr0` (`pve` at `192.168.1.27/22`, `pve2` at
`192.168.1.24/22`), so an untagged second NIC on the Talos VMs is a small change.

`pve2` additionally has a CH340 serial converter (`1a86:7523`) and a Realtek Bluetooth
radio (`0bda:b85b`) physically attached and passed to no VM. Both are out of scope here.

### 2.3 Discovery dependency

The 100+ entries in `.storage/core.config_entries` include 11× `esphome`, 7× `homekit`,
5× `wled`, 4× `homekit_controller`, 3× `shelly`, plus `cast`, `dlna_dmr`, `dlna_dms`,
`samsungtv`, `braviatv`, `apple_tv`, `ipp`, `brother`, `unifi`, `matter` and `wemo`.
Multicast is load-bearing, not a convenience.

`bluetooth` and `ibeacon` entries also exist, but `lab1` has no Bluetooth adapter, so
they are already inert.

### 2.4 Storage

`/docker` on `lab1` is an NFS mount from the QNAP at
`192.168.1.240:/share/MD0_DATA/Paperless/docker`. The HA config therefore lives on the
NAS. `rpcinfo -p 192.168.1.240` still lists only portmapper and rquotad — no `100003`
(nfs), no `100005` (mountd) — unchanged from the 2026-09-15 diagnosis. The existing
mount survives only because it is an NFSv3 mount established on the fixed
`mountport=30000` before the daemons stopped registering.

The 27 GB breaks down as:

| Path | Size | Disposition |
|---|---|---|
| `home-assistant_v2.db.corrupt.2026-05-03…` | 13 GB | **Drop** |
| `home-assistant_v2.db` | 9.3 GB | Migrate |
| `backups/` | 1.7 GB | Leave; follow-up NFS PV |
| `home-assistant_v2.db.corrupt.2024-06-03…` | 1.4 GB | **Drop** |
| `core.77` | 994 MB | Migrate |
| `*-wal.corrupt.*` | 56 MB | **Drop** |
| `custom_components`, `www`, `image`, `data`, `themes`, `zigbee.db`, YAML, `.storage` | < 100 MB | Migrate |

**Two SQLite corruptions on NFS-hosted storage, in 2024 and 2026, is a pattern.** Moving
`/config` onto Longhorn is not just a convenience — it removes the recurring cause.

### 2.5 Cluster facts

- 3 control-plane nodes, Talos v1.13.6, Kubernetes v1.36.1.
- CNI: flannel, pod CIDR `10.244.0.0/16`.
- Storage: Longhorn (`longhorn` default, `longhorn-retain`, `longhorn-static`).
- MetalLB pool `10.98.0.200-254`; Traefik with a `*.homelab.cs-ol.de` wildcard cert.
- Node memory: 8 GB each, currently 50–55% used (~3.5 GB free).
- `192.168.3.0/24` inside the LAN `/22` is entirely unallocated. Pi-hole's
  `FTLCONF_LOCAL_IPV4: 192.168.3.10` is cosmetic; its real addresses are MetalLB
  `10.98.0.201/202`.

## 3. Decisions

| # | Decision | Rejected alternative |
|---|---|---|
| D1 | Physically move the dongle to `pve2`, pass through to a Talos VM | Leave on `pve`, expose over TCP with `ser2net` — no hardware move, but makes the cluster depend on `lab1` staying up |
| D2 | Multus + macvlan with a fixed LAN IP | `hostNetwork` on the pinned node — simpler and a 1:1 match for `network_mode: host`, but HA's IP would follow the node |
| D3 | `/config` on Longhorn; `/config/backups` on NFS as a separate step | NFS PV reusing the QNAP path directly — zero-copy, but keeps SQLite on the storage that corrupted it twice |
| D4 | Parallel validation, then hard cutover | Straight hard cutover — shorter, but proves nothing before the downtime window |
| D5 | Static `192.168.3.11` + Traefik ingress | Reusing `192.168.1.28` — invisible to the LAN, but couples this work to renumbering `lab1` |

## 4. Design

### 4.1 Infrastructure layer — `~/Workspace/homelab`

**Second NIC.** `roles/proxmox/terraform` already models `network_devices` as a list with
an optional `vlan`. Append `{ bridge = "vmbr0" }` (no `vlan`) to all three Talos VMs,
giving each an untagged `eth1` on the LAN. All three, not only the pinned node, so the
workload can follow the dongle without a Terraform change.

**Talos machine config.** Bring `eth1` up with `dhcp: false`, **no address and no default
route**. It exists solely as the macvlan parent; node identity stays on VLAN 99. A
default route arriving on `eth1` would compete with the VLAN-99 route and is the most
likely way to break cluster networking during this change.

**Stable device path.** A `machine.udev.rules` entry:

```
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d4", SYMLINK+="zigbee"
```

The pod then mounts `/dev/zigbee` rather than a `ttyACM` number that can shift across
reboots or re-plugging.

**USB passthrough.** Remove `usb1: host=1a86:55d4` from `pve` VM 100. Add a
`usb { host = "1a86:55d4" }` block to the `talos1` resource on `pve2`. Passing by
vendor:product ID rather than bus path means re-plugging into a different port still
works.

**Memory.** Raise the target VM from 8 GB to 12 GB in the same change. HA with 100+
config entries typically wants 1.5–2 GB, and the nodes have ~3.5 GB free today. It
would fit, but not with room to be wrong.

### 4.2 Node labelling — `k8s-homelab`, `infrastructure/node-feature-discovery/`

Deploy Node Feature Discovery via HelmRelease, with the `usb` source enabled and device
class `02` (CDC — verified from `/sys/bus/usb/devices/2-2/bDeviceClass` on `lab1`) added
to the whitelist. NFD's default `deviceClassWhitelist` is `["0e","ef","fe","ff"]` and does
not include `02`, so the default configuration emits no label for this device. On
whichever node holds the dongle it emits:

```
feature.node.kubernetes.io/usb-02_1a86_55d4.present=true
```

The HA `nodeSelector` keys off exactly that label. Because the label is *derived from
detection*, moving the passthrough to `talos2` later moves the label, and the pod follows
with no manifest change.

A hand-rolled labeller DaemonSet was considered and rejected: it needs `nodes` patch RBAC
and is code we would own and maintain. The cost of NFD is one additional cluster
component (one Deployment, one DaemonSet).

### 4.3 LAN attachment — `k8s-homelab`, `infrastructure/multus/`

Multus CNI as a DaemonSet, plus a `NetworkAttachmentDefinition` in the `homeassistant`
namespace:

```json
{
  "cniVersion": "0.3.1",
  "type": "macvlan",
  "master": "eth1",
  "mode": "bridge",
  "ipam": {
    "type": "static",
    "addresses": [{ "address": "192.168.3.11/22" }],
    "routes":    [{ "dst": "224.0.0.0/4" }]
  }
}
```

Two details are deliberate and neither is optional:

- **No `gateway`.** A macvlan default route would take precedence over the pod network
  and break cluster DNS. The `/22` address alone routes LAN traffic out `net1`;
  internet and in-cluster traffic keeps using `eth0`.
- **The explicit `224.0.0.0/4` route.** Without it, HA's mDNS and SSDP queries to
  `224.0.0.251` follow the default route out `eth0` into flannel and are never seen on
  the LAN. This single line is what makes discovery work, and it is the highest-risk
  element of the design — hence the validation phase in §5.

The pod keeps `NET_ADMIN` and `NET_RAW`; the four `ping` integrations need raw sockets.

### 4.4 Application — `k8s-homelab`, `apps/base/homeassistant/`

Standard three-layer pattern: `apps/base/homeassistant/`,
`apps/production/homeassistant/`, and `clusters/production/homeassistant.yaml` with
`dependsOn: infrastructure`.

| Aspect | Choice | Reason |
|---|---|---|
| Image | `ghcr.io/home-assistant/home-assistant` pinned to an exact tag (e.g. `2026.9.1`), resolved from the registry at implementation time | `latest` in a GitOps repo means the manifest no longer describes what runs; a wildcard is not a pin either |
| Replicas / strategy | `1`, `Recreate` | SQLite and a serial device tolerate exactly one writer |
| PVC | 20 Gi, `longhorn-retain` | An accidental Kustomization delete must not take the data |
| Device | `hostPath: /dev/zigbee`, `type: CharDevice` | Stable path from the udev rule |
| Scheduling | `nodeSelector` on the NFD label | §4.2 |
| Network | Multus annotation for `homeassistant/lan-macvlan` | §4.3 |
| Capabilities | add `NET_ADMIN`, `NET_RAW` | Parity with compose; `ping` integrations |
| Env | `TZ=Europe/Berlin`, `LANG=C.UTF-8` | Parity with compose |
| Probes | HTTP on `:8123` | — |
| Ingress | Traefik `IngressRoute`, `homeassistant.homelab.cs-ol.de`, wildcard cert | §3 D5 |

The `/var/run/dbus` mount is **dropped** — `lab1` has no Bluetooth adapter, so the
`bluetooth` and `ibeacon` entries are already non-functional. The unused Realtek radio on
`pve2` is a separate follow-up, not part of this migration.

`configuration.yaml` gains:

```yaml
http:
  use_x_forwarded_for: true
  trusted_proxies:
    - 10.244.0.0/16
```

### 4.5 Data flow after the move

```
LAN devices (192.168.0.0/22)
    │  mDNS/SSDP via 224.0.0.0/4 → net1
    ▼
[ macvlan net1 = 192.168.3.11 ] ─┐
                                 ├── HA pod (talos node holding the dongle)
[ flannel eth0 = 10.244.x.y ] ───┘        │
    │  internet, cluster DNS               ├── /dev/zigbee  → SONOFF ZBDongle-P
    ▼                                      └── /config      → Longhorn PVC (20 Gi)
Traefik IngressRoute → homeassistant.homelab.cs-ol.de
```

## 5. Cutover

### Phase A — validation, no downtime

1. Land the infrastructure PRs (NIC, Talos config, NFD, Multus). The dongle stays on
   `pve`; the compose stack keeps running untouched.
2. Deploy HA in Kubernetes with an **empty** config on `192.168.3.11`, as a
   **validation variant**: the `nodeSelector` and the `/dev/zigbee` mount are both
   omitted. The dongle is still on `pve` at this point, so the NFD label does not exist
   on any node and the device node is absent — a pod carrying either would stay
   `Pending` or fail to start, and nothing downstream could be tested.
3. Verify, in order:
   - the PVC binds and the ingress serves over TLS;
   - `192.168.3.11:8123` answers from the LAN;
   - **zeroconf actually discovers** ESPHome, WLED and Shelly devices in the UI. This is
     the test that matters — it exercises the `224.0.0.0/4` route.
4. Separately confirm NFD itself works, without moving the dongle yet: check that the
   `usb` source already emits `feature.node.kubernetes.io/usb-*` labels for the devices
   `pve2` passes through today, proving the source and class whitelist are configured
   correctly.
5. Delete the test config and the validation variant.

No second live instance ever runs with real data, so there is no duplicate HomeKit
bridge, no MQTT client-ID clash, and no double InfluxDB writes.

### Phase B — cutover window

1. Stop the compose container on `lab1`.
2. Final copy of `/config` into the PVC, excluding `*.corrupt*`, `backups/`, `tmp/`,
   `*.log*` — roughly 10 GB of the 27 GB.
3. Move the dongle physically from `pve` to `pve2`; apply the Terraform change removing
   it from VM 100 and adding it to `talos1`.
4. Confirm the NFD label appears on the target node.
5. Deploy the full manifest — `nodeSelector` and `/dev/zigbee` mount reinstated — and
   start HA on the real data; repoint the ZHA config entry at `/dev/zigbee`.
6. Verify: Zigbee mesh online, HomeKit bridge reachable from Apple devices, energy
   dashboard and long-term statistics intact, ESPHome devices connected.

### Rollback

Move the dongle back to `pve`, revert the Terraform, `docker compose up` on `lab1`. The
compose stack's data on the NAS is only ever read during migration, never mutated, so
rollback is always available until the stack is explicitly decommissioned.

## 6. Out of scope

Tracked as separate issues, deliberately excluded from the critical path:

- **`/config/backups` NFS PV.** Gated on the QNAP registering `mountd` again. A PVC that
  cannot mount blocks pod start, so this must never be a dependency of the migration.
- **Homepage dashboard entry** — update `apps/base/homepage/configmap.yaml` from
  `http://192.168.1.28:8123` to the new address.
- **The Realtek Bluetooth radio on `pve2`** — potentially enabling `bluetooth` and
  `ibeacon`, which do not work today.
- **Decommissioning the compose stack on `lab1`** — only after the Kubernetes deployment
  has proven itself over time.
- **Recorder retention tuning** (`purge_keep_days`). The 9.3 GB DB migrates as-is.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Multicast does not traverse macvlan as designed | Phase A validates discovery before any downtime; the `224.0.0.0/4` route is the known failure point |
| Adding `eth1` disturbs Talos node networking | `eth1` gets no address and no default route; change one node at a time and confirm `Ready` before the next |
| Node memory pressure with HA added | Raise the target VM to 12 GB in the same Terraform change |
| Zigbee downtime during the dongle move | Confined to Phase B; rollback is a physical move plus `docker compose up` |
| QNAP NFS unavailable during the final copy | The copy reads through `lab1`'s existing working NFSv3 mount; if that mount is gone, Phase B stops before anything is changed |
| Multus on Talos needs machine-config changes | Verified as part of the infrastructure PR, before the app layer exists |

## 8. Implementation issues

| Repo | Work |
|---|---|
| `homelab` | Second NIC on the Talos VMs, `eth1` machine config, udev rule, 12 GB memory |
| `homelab` | Move USB passthrough `pve` VM 100 → `pve2` `talos1` |
| `k8s-homelab` | Node Feature Discovery under `infrastructure/` |
| `k8s-homelab` | Multus CNI + `NetworkAttachmentDefinition` under `infrastructure/` |
| `k8s-homelab` | `apps/base/homeassistant/`, production overlay, Flux Kustomization |
| `k8s-homelab` | Migration runbook and cutover execution |
