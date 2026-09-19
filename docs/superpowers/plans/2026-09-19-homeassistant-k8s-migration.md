# Home Assistant Kubernetes Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move Home Assistant from the Docker Compose stack on `lab1` into the Talos Kubernetes cluster with LAN multicast discovery, the SONOFF Zigbee dongle, and all existing data intact.

**Architecture:** A second untagged NIC on the Talos VMs puts them on the `192.168.0.0/22` LAN. Multus + a macvlan `NetworkAttachmentDefinition` gives the HA pod a fixed LAN IP with an explicit multicast route. Node Feature Discovery labels whichever node holds the USB dongle, and the pod's `nodeSelector` follows that label. `/config` moves off NFS onto a Longhorn PVC.

**Tech Stack:** OpenTofu/Terraform (`bpg/proxmox` >= 0.62.0), Talos v1.13.6, Ansible, Flux CD, Kustomize, Multus CNI v4.3.1, Node Feature Discovery chart 0.19.0, Longhorn, Traefik.

**Spec:** `docs/superpowers/specs/2026-09-19-homeassistant-k8s-migration-design.md`

---

## Corrections to the spec

Three facts were verified against the live systems while writing this plan. Where they
contradict the spec, **this plan is correct** and Task 5 amends the spec.

1. **The NFD label in the spec is wrong.** The spec states
   `feature.node.kubernetes.io/usb-ff_1a86_55d4.present=true`. The dongle's actual
   `bDeviceClass` is **`02`** (CDC), read from `/sys/bus/usb/devices/2-2/bDeviceClass` on
   `lab1`. NFD's `usb` source builds the label as `usb-<class>_<vendor>_<device>.present`,
   so the real label is:
   ```
   feature.node.kubernetes.io/usb-02_1a86_55d4.present=true
   ```
   Worse, NFD's **default `deviceClassWhitelist` is `["0e","ef","fe","ff"]`** — class `02`
   is not in it, so with default configuration NFD emits **no label at all**. The
   whitelist must be extended. This is handled in Task 5.

2. **The macvlan CNI plugin is not installed.** `talosctl -n 10.98.0.10 ls /opt/cni/bin`
   returns only `bridge firewall flannel host-local loopback portmap`. Neither `macvlan`
   nor the `static` IPAM plugin is present, and Talos's flannel DaemonSet does not install
   plugin binaries (its only initContainer copies a conflist). Multus alone would fail.
   Task 3 adds a plugin installer; it did not exist in the spec.

3. **The interface is `ens18`, not `eth0`.** Talos here uses predictable names
   (`talos_interface: ens18` in `inventory/group_vars/all/all.yml`, confirmed by
   `talosctl get links`). The second NIC is therefore expected to be `ens19`, and is
   selected by MAC address rather than by name so a rename cannot silently break it.

## Global Constraints

- **Issue-driven workflow, from `CLAUDE.md`.** Every task = one GitHub issue, one branch
  `<issue-number>-<short-description>`, one PR titled `Closes #<n>: …`, commits prefixed
  `#<issue-number>`. Never commit to `main`. Switch back to `main` and pull immediately
  after creating each PR.
- **Two repositories.** Tasks 1, 2 and 7 are in `~/Workspace/homelab`. Tasks 3, 4, 5, 6
  and 8 are in `~/Workspace/k8s-homelab`.
- **Attribution.** End commit messages with
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` and PR descriptions with
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- **Exact values, copied verbatim from the spec and from live verification:**
  - Talos nodes (VERIFIED 2026-09-19 via `ens18` MAC — talos2/talos3 are NOT in IP order):
    `talos1`=VM900=`10.98.0.10`=`talos-0hr-sdd`, `talos2`=VM901=`10.98.0.12`=`talos-isv-pq4`,
    `talos3`=VM902=`10.98.0.11`=`talos-9rs-4ei`. VIP `10.98.0.9` on talos1.
  - Proxmox VMIDs: `900`, `901`, `902` on node `pve2`; `lab1` is VMID `100` on node `pve`
  - Dongle USB ID: `1a86:55d4`, device class `02`
  - HA LAN IP: `192.168.3.11/22`, LAN gateway `192.168.0.1` (deliberately **not** configured)
  - Pod CIDR `10.244.0.0/16`, service CIDR `10.96.0.0/23`
  - Storage class: `longhorn-retain` (reclaim policy `Retain`, verified)
  - HA image: `ghcr.io/home-assistant/home-assistant:2026.9.3`
  - Ingress host: `homeassistant.homelab.cs-ol.de`
- **`TALOSCONFIG`.** All `talosctl` commands need `export TALOSCONFIG=~/.talos/talosconfig`
  and explicit `-n <node> -e <node>`; the config has no default endpoint.
- **One node at a time.** Any change touching Talos VMs or machine config is applied to a
  single node, verified `Ready`, then the next. Never all three at once.
- **Never mutate the source data.** `/docker/homeassistant/config` on `lab1` is read-only
  for the entire migration. Rollback depends on it.

---

## File Structure

### `~/Workspace/homelab`

| File | Responsibility |
|---|---|
| `roles/proxmox/terraform/variables.tf` | Add optional `usb_devices` to the `vms` object |
| `roles/proxmox/terraform/main.tf` | Add `dynamic "usb"` block to the VM resource |
| `roles/proxmox/terraform/terraform.tfvars` | Second NIC on VMs 900/901/902; 12 GB RAM and USB passthrough on 900; remove USB from 100 |
| `roles/talos/tasks/configure_cluster.yml` | Add `ens19` interface + udev rule to the control-plane patch, for rebuild parity |
| `docs/talos-lan-interface.md` | Record why `ens19` has no address and no gateway |

### `~/Workspace/k8s-homelab`

| File | Responsibility |
|---|---|
| `infrastructure/cni-plugins/daemonset.yaml` | Copies `macvlan` + `static` into `/opt/cni/bin` on every node |
| `infrastructure/cni-plugins/{namespace,kustomization}.yaml` | Namespace and kustomize wiring |
| `infrastructure/cni-plugins/ks.yaml` | Flux Kustomization |
| `infrastructure/multus/multus.yaml` | Vendored upstream Multus thick DaemonSet + CRD + RBAC, pinned v4.3.1 |
| `infrastructure/multus/{kustomization,ks}.yaml` | Kustomize wiring and Flux Kustomization |
| `infrastructure/node-feature-discovery/{helmrepository,helmrelease,namespace,kustomization,ks}.yaml` | NFD with the `02` device class whitelisted |
| `infrastructure/kustomization.yaml` | Register the three new Flux Kustomizations |
| `apps/base/homeassistant/namespace.yaml` | `homeassistant` namespace |
| `apps/base/homeassistant/networkattachment.yaml` | macvlan NAD with the `224.0.0.0/4` route |
| `apps/base/homeassistant/pvc.yaml` | 20 Gi `longhorn-retain` claim |
| `apps/base/homeassistant/deployment.yaml` | The HA Deployment |
| `apps/base/homeassistant/service.yaml` | ClusterIP on 8123 for Traefik |
| `apps/base/homeassistant/ingressroute.yaml` | Traefik route + Homepage annotations |
| `apps/base/homeassistant/kustomization.yaml` | Base wiring |
| `apps/production/homeassistant/kustomization.yaml` | Production overlay |
| `apps/production/homeassistant/deployment-patch.yaml` | Adds `nodeSelector` + `/dev/zigbee` at cutover (Task 8) |
| `clusters/production/homeassistant.yaml` | Flux Kustomization, `dependsOn: infrastructure` |
| `docs/runbooks/homeassistant-cutover.md` | The Phase B runbook and rollback |

---

## Task 1: Terraform — second NIC, memory, and USB passthrough support

**Repo:** `~/Workspace/homelab`

**Files:**
- Modify: `roles/proxmox/terraform/variables.tf`
- Modify: `roles/proxmox/terraform/main.tf`
- Modify: `roles/proxmox/terraform/terraform.tfvars`

**Interfaces:**
- Consumes: nothing.
- Produces: `ens19` present on all three Talos VMs with MACs
  `BC:24:11:B7:5A:1E` (talos1), `BC:24:11:84:92:C4` (talos2), `BC:24:11:7D:BE:EB` (talos3).
  Task 2 selects the interface by exactly these addresses. Also produces the
  `usb_devices` variable field consumed by Task 7.

This task adds the NIC and the *capability* for USB passthrough, but does **not** move the
dongle — that is Task 7, after validation has passed.

- [ ] **Step 1: Create the issue and branch**

```bash
cd ~/Workspace/homelab
git checkout main && git pull origin main
gh issue create --title "Feature: Add LAN NIC and USB passthrough support to Talos VMs" \
  --body "Home Assistant migration (k8s-homelab#61) needs the Talos VMs on the 192.168.0.0/22 LAN for multicast discovery, and needs the SONOFF Zigbee dongle passed through to one node. Adds a second untagged vmbr0 NIC to VMs 900-902, raises talos1 to 12 GB, and adds a usb_devices field to the VM module."
# note the issue number as $ISSUE
git checkout -b $ISSUE-talos-lan-nic-and-usb
```

- [ ] **Step 2: Verify the current state — the NIC is absent**

```bash
ssh pve2 'qm config 900 | grep -E "^net|^memory|^usb"'
```
Expected: exactly one line `net0: virtio=BC:24:11:B7:5A:1D,bridge=vmbr0,firewall=0,tag=99`
and `memory: 8192`. No `net1`, no `usb0`. This is the "failing test".

- [ ] **Step 3: Add the `usb_devices` field to the VM variable**

In `roles/proxmox/terraform/variables.tf`, inside the `vms` object type, immediately after
the `network_devices` block, add:

```hcl
    usb_devices = optional(list(object({
      host = string
    })), [])
```

- [ ] **Step 4: Add the dynamic USB block to the VM resource**

In `roles/proxmox/terraform/main.tf`, immediately after the closing brace of the
`dynamic "network_device"` block and before `boot_order`, add:

```hcl
  dynamic "usb" {
    for_each = try(each.value.usb_devices, [])

    content {
      host = usb.value.host
    }
  }
```

- [ ] **Step 5: Add the second NIC to all three Talos VMs**

In `roles/proxmox/terraform/terraform.tfvars`, for VM `900` replace the
`network_devices` list and the `ram` line so the entry reads:

```hcl
  {
    id    = 900
    name  = "talos1"
    cpu   = 4
    ram   = 12288
    disk  = 20
    disk2 = 250
    node  = "pve2"
    image = "talos"
    network_devices = [
      {
        bridge = "vmbr0"
        model  = "virtio"
        vlan   = 99
        mac    = "BC:24:11:B7:5A:1D"
      },
      {
        bridge = "vmbr0"
        model  = "virtio"
        mac    = "BC:24:11:B7:5A:1E"
      }
    ]
  },
```

For VM `901`, append to its `network_devices` list:

```hcl
      {
        bridge = "vmbr0"
        model  = "virtio"
        mac    = "BC:24:11:84:92:C4"
      }
```

For VM `902`, append to its `network_devices` list:

```hcl
      {
        bridge = "vmbr0"
        model  = "virtio"
        mac    = "BC:24:11:7D:BE:EB"
      }
```

Leave `ram = 8192` on 901 and 902. Only talos1 hosts Home Assistant.

- [ ] **Step 6: Validate the plan without applying**

```bash
cd ~/Workspace/homelab/roles/proxmox/terraform
tofu init -upgrade && tofu validate
tofu plan -target='proxmox_virtual_environment_vm.vms["900"]'
```
Expected: `Success! The configuration is valid.`, and a plan showing an **update in place**
adding one `network_device` and changing `memory.dedicated` from 8192 to 12288. If the plan
shows `must be replaced`, **stop** — replacement would destroy the node's disks.

- [ ] **Step 7: Apply to talos3 first**

Apply to the node that does *not* hold the VIP and will *not* hold the dongle, so a
mistake is cheapest.

```bash
tofu apply -target='proxmox_virtual_environment_vm.vms["902"]'
ssh pve2 'qm config 902 | grep -E "^net"'
```
Expected: two `net` lines, the second `net1: virtio=BC:24:11:7D:BE:EB,bridge=vmbr0`
with no `tag=`.

- [ ] **Step 8: Reboot talos3 and confirm the cluster stays healthy**

```bash
export TALOSCONFIG=~/.talos/talosconfig
talosctl -n 10.98.0.12 -e 10.98.0.12 reboot   # talos2 = VM901 = 10.98.0.12   # talos3 = VM902 = 10.98.0.11
# wait, then:
kubectl get nodes
talosctl -n 10.98.0.11 -e 10.98.0.11 get links | grep -E "ens1[89]"
```
Expected: all three nodes `Ready`; `ens19` listed with hardware address
`bc:24:11:7d:be:eb`. If `ens19` does not appear, record the actual name — Task 2 selects by
MAC so the name does not matter, but the discrepancy should be noted in the PR.

- [ ] **Step 9: Repeat for talos2, then talos1**

```bash
tofu apply -target='proxmox_virtual_environment_vm.vms["901"]'
talosctl -n 10.98.0.11 -e 10.98.0.11 reboot
kubectl get nodes   # wait for all Ready before continuing

tofu apply -target='proxmox_virtual_environment_vm.vms["900"]'
talosctl -n 10.98.0.10 -e 10.98.0.10 reboot
kubectl get nodes
```
Expected after each: all three nodes `Ready`. talos1 additionally shows 12 GB:
```bash
kubectl get node talos-0hr-sdd -o jsonpath='{.status.capacity.memory}{"\n"}'
```

- [ ] **Step 10: Commit and open the PR**

```bash
cd ~/Workspace/homelab
git add roles/proxmox/terraform/variables.tf roles/proxmox/terraform/main.tf roles/proxmox/terraform/terraform.tfvars
git commit -m "#$ISSUE Add untagged LAN NIC to Talos VMs and usb_devices support

The Talos VMs are VLAN-99 only, so LAN multicast cannot reach them. Adds a
second untagged vmbr0 NIC to VMs 900-902 with fixed MACs, raises talos1 to
12 GB for the Home Assistant workload, and adds an optional usb_devices
field so the Zigbee dongle can be passed through in a later change.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push -u origin $ISSUE-talos-lan-nic-and-usb
gh pr create --title "Closes #$ISSUE: Add LAN NIC and USB passthrough support to Talos VMs" --body "$(git log -1 --pretty=%B)

Part of the Home Assistant migration, k8s-homelab#61.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
git checkout main && git pull origin main
```

---

## Task 2: Talos machine config — bring up `ens19` and add the udev rule

**Repo:** `~/Workspace/homelab`

**Files:**
- Modify: `roles/talos/tasks/configure_cluster.yml:35-58` (the `nodeip-patch-cp.yaml` content block)
- Create: `docs/talos-lan-interface.md`

**Interfaces:**
- Consumes: the MACs from Task 1.
- Produces: `ens19` up on each node, and a `/dev/zigbee` symlink on whichever node holds
  the dongle. Task 3's macvlan `master` and Task 6's NAD both depend on the interface
  name resolved here; Task 8's `hostPath` depends on `/dev/zigbee`.

⚠️ **Do not re-run the Ansible role to apply this.** The existing `configure_cluster.yml`
patch tasks are not idempotent — the live config already shows the `machine.disks` stanza
duplicated five times from previous runs. Apply the change with `talosctl patch
machineconfig` directly, and edit the Ansible content only so a future rebuild produces the
same result.

- [ ] **Step 1: Create the issue and branch**

```bash
cd ~/Workspace/homelab
git checkout main && git pull origin main
gh issue create --title "Feature: Configure ens19 and Zigbee udev rule in Talos machine config"
git checkout -b $ISSUE-talos-ens19-and-udev
```

- [ ] **Step 2: Verify the current state — no udev rule, ens19 unconfigured**

```bash
export TALOSCONFIG=~/.talos/talosconfig
talosctl -n 10.98.0.10 -e 10.98.0.10 get machineconfig -o yaml | grep -c "udev" || echo "no udev stanza"
talosctl -n 10.98.0.10 -e 10.98.0.10 get addresses | grep ens19 || echo "ens19 has no addresses"
```
Expected: no udev stanza, and `ens19` has no addresses. This is the "failing test".

- [ ] **Step 3: Write the patch file**

```bash
mkdir -p ~/.talos/patches
cat > ~/.talos/patches/lan-nic-talos1.yaml <<'EOF'
machine:
  udev:
    rules:
      - SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d4", SYMLINK+="zigbee"
  network:
    interfaces:
      - deviceSelector:
          hardwareAddr: "bc:24:11:b7:5a:1e"
        dhcp: false
        addresses:
          - 192.168.3.12/22
EOF
```

Three deliberate choices:
- **`deviceSelector.hardwareAddr`, not `interface: ens19`.** The MAC is fixed by Terraform;
  the kernel name is not guaranteed.
- **A static address rather than an address-less link.** macvlan only needs its parent to
  be UP, but a configured address is the reliable way to guarantee that, and it makes the
  node pingable on the LAN for debugging. `192.168.3.12` is free (verified by ping sweep),
  and adjacent to HA's `192.168.3.11`.
- **No `gateway` anywhere.** A second default route would compete with the VLAN-99 route
  and is the most likely way to break cluster networking. The node keeps its identity on
  `10.98.0.10` because `kubelet.nodeIP.validSubnets` is pinned to `10.98.0.0/24`.

Write two more files, `lan-nic-talos2.yaml` and `lan-nic-talos3.yaml`, identical except:
- talos2: `hardwareAddr: "bc:24:11:84:92:c4"`, `addresses: [192.168.3.13/22]`
- talos3: `hardwareAddr: "bc:24:11:7d:be:eb"`, `addresses: [192.168.3.14/22]`

- [ ] **Step 4: Validate the patch against a copy before touching a node**

```bash
cp ~/.talos/controlplane.yaml /tmp/cp-check.yaml
talosctl machineconfig patch /tmp/cp-check.yaml --patch @~/.talos/patches/lan-nic-talos1.yaml --output /tmp/cp-checked.yaml
talosctl validate --config /tmp/cp-checked.yaml --mode cloud
```
Expected: `/tmp/cp-checked.yaml is valid for cloud mode`. (This exact patch shape was
validated against the live `controlplane.yaml` while writing this plan.)

### ⚠️ Verified node mapping — do not infer it

`talos2` and `talos3` are **not** in IP order. Confirmed 2026-09-19 from each node's `ens18`
MAC (`talosctl get links`) matched against `terraform.tfvars`:

| Logical | VMID | `ens18` MAC | Node IP | Kubernetes node | `ens19` MAC | LAN address |
|---|---|---|---|---|---|---|
| talos1 | 900 | `bc:24:11:b7:5a:1d` | `10.98.0.10` | `talos-0hr-sdd` | `bc:24:11:b7:5a:1e` | `192.168.3.12/22` |
| talos2 | 901 | `bc:24:11:84:92:c3` | **`10.98.0.12`** | `talos-isv-pq4` | `bc:24:11:84:92:c4` | `192.168.3.13/22` |
| talos3 | 902 | `bc:24:11:7d:be:ea` | **`10.98.0.11`** | `talos-9rs-4ei` | `bc:24:11:7d:be:eb` | `192.168.3.14/22` |

Applying a patch to the wrong node is not destructive — the `deviceSelector` matches no
interface — but it leaves that node silently unconfigured, which is harder to debug than a
clean failure. Re-verify if any node is ever rebuilt:

```bash
export TALOSCONFIG=~/.talos/talosconfig
for n in 10.98.0.10 10.98.0.11 10.98.0.12; do
  echo -n "$n -> "; talosctl -n $n -e $n get links 2>/dev/null | awk '$4=="ens18"{print $0}' | grep -oE "bc:[0-9a-f:]+"
done
```

Also confirmed 2026-09-19: the second interface is named **`ens19`**, and it comes up
(`OPER STATE: up`) with no machine config at all. The static address below is therefore not
what brings the link up — it guarantees it, and makes the node reachable on the LAN for
debugging.

- [ ] **Step 5: Apply to talos3 first**

talos3 is `10.98.0.11`. It holds neither the VIP nor (later) the dongle, so a mistake is
cheapest here.

```bash
talosctl -n 10.98.0.11 -e 10.98.0.11 patch machineconfig --patch @~/.talos/patches/lan-nic-talos3.yaml
```
Talos applies network and udev changes without a reboot. If it reports a reboot is required,
let it reboot and wait for `Ready`.

- [ ] **Step 6: Verify on talos3**

```bash
talosctl -n 10.98.0.11 -e 10.98.0.11 get addresses | grep 192.168.3.14
ping -c2 192.168.3.14
kubectl get nodes -o wide   # INTERNAL-IP must still be 10.98.0.11
talosctl -n 10.98.0.11 -e 10.98.0.11 get routes | grep default
```
Expected: the address is present and pingable from the LAN; the node's `INTERNAL-IP` is
**unchanged** at `10.98.0.11`; exactly **one** default route, via the VLAN-99 gateway. If a
second default route appeared, revert this node immediately and stop.

- [ ] **Step 7: Apply to talos2, then talos1, verifying after each**

```bash
talosctl -n 10.98.0.12 -e 10.98.0.12 patch machineconfig --patch @~/.talos/patches/lan-nic-talos2.yaml
kubectl get nodes && ping -c2 192.168.3.13

talosctl -n 10.98.0.10 -e 10.98.0.10 patch machineconfig --patch @~/.talos/patches/lan-nic-talos1.yaml
kubectl get nodes && ping -c2 192.168.3.12
```
Expected after each: all three nodes `Ready`, `INTERNAL-IP` unchanged, new address pingable.
talos1 (`10.98.0.10`) holds the VIP `10.98.0.9` — confirm
`curl -k https://10.98.0.9:6443/version` still answers.

- [ ] **Step 8: Mirror the change into the Ansible role for rebuild parity**

⚠️ `configure_cluster.yml` generates **one** `controlplane.yaml` and applies it to all three
nodes. The udev rule is identical everywhere and belongs in that shared patch. The LAN
interface is **not** — each node needs its own address, so a single-valued variable would
silently give talos2 and talos3 talos1's IP. Split them.

**a) The udev rule goes in the shared patch.** In the `content:` block of the
**"Patch controlplane config"** task, add `udev` at the `machine:` level, leaving everything
else as it is:

```yaml
          machine:
            udev:
              rules:
                - SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d4", SYMLINK+="zigbee"
            disks:
              - device: /dev/sdb
```

**b) The per-node LAN patches are committed as files**, so a rebuild applies the same three
patches this task applied by hand:

```bash
mkdir -p ~/Workspace/homelab/roles/talos/files/lan-patches
for spec in "talos1 bc:24:11:b7:5a:1e 192.168.3.12" \
            "talos2 bc:24:11:84:92:c4 192.168.3.13" \
            "talos3 bc:24:11:7d:be:eb 192.168.3.14"; do
  set -- $spec
  cat > ~/Workspace/homelab/roles/talos/files/lan-patches/$1.yaml <<EOF
# Second NIC on the untagged LAN: the macvlan parent for Home Assistant.
# Per-node because each needs its own address; see docs/talos-lan-interface.md.
# Never add a gateway here.
machine:
  network:
    interfaces:
      - deviceSelector:
          hardwareAddr: "$2"
        dhcp: false
        addresses:
          - $3/22
EOF
done
ls ~/Workspace/homelab/roles/talos/files/lan-patches/
```
Expected: `talos1.yaml talos2.yaml talos3.yaml`.

**c) Record how to apply them** by appending to `roles/talos/README.md`:

```markdown
## Per-node LAN interface patches

`files/lan-patches/talos<N>.yaml` configure each node's second NIC, the macvlan
parent for Home Assistant. They are per-node because each carries a distinct
address, so they cannot live in the shared `controlplane.yaml` patch. After a
rebuild, apply each to its own node:

    # talos2 and talos3 are NOT in IP order; verify against the ens18 MAC
    # after any rebuild before trusting this mapping.
    for pair in "talos1 10.98.0.10" "talos2 10.98.0.12" "talos3 10.98.0.11"; do
      set -- $pair
      talosctl -n $2 -e $2 patch machineconfig \
        --patch @roles/talos/files/lan-patches/$1.yaml
    done

See `docs/talos-lan-interface.md` for why they have no gateway.
```

Do **not** add `talos_lan_interface_mac`/`talos_lan_interface_address` variables — a single
value cannot be correct for three nodes, which is exactly the trap this split avoids.

- [ ] **Step 9: Document why there is no gateway**

```bash
cat > ~/Workspace/homelab/docs/talos-lan-interface.md <<'EOF'
# The second Talos NIC (`ens19`)

Each Talos node has a second, untagged NIC on `vmbr0`, putting it on the
`192.168.0.0/22` LAN alongside the rest of the household.

It exists for exactly one reason: to be the **parent interface for the macvlan
network attachment** that gives the Home Assistant pod a real LAN address, so
mDNS and SSDP discovery work. See `k8s-homelab`'s
`docs/superpowers/specs/2026-09-19-homeassistant-k8s-migration-design.md`.

## Why it has an address at all

macvlan only requires its parent to be UP. A static address is the reliable way
to guarantee that, and it makes the node reachable on the LAN for debugging.

| Node | MAC | LAN address |
|---|---|---|
| talos1 | `bc:24:11:b7:5a:1e` | `192.168.3.12/22` |
| talos2 | `bc:24:11:84:92:c4` | `192.168.3.13/22` |
| talos3 | `bc:24:11:7d:be:eb` | `192.168.3.14/22` |

## Why it must never have a gateway

The node's identity lives on VLAN 99 (`10.98.0.0/24`). A default route arriving
on this interface would compete with the VLAN-99 route and can black-hole the
Kubernetes control plane. `kubelet.nodeIP.validSubnets` pins the node IP to
`10.98.0.0/24`, but that does not protect the routing table. **Do not add a
`gateway` here.**

## Why the interface is selected by MAC

The kernel name (`ens19`) is not guaranteed across reboots or hardware changes.
The MACs are fixed in `roles/proxmox/terraform/terraform.tfvars`, so
`deviceSelector.hardwareAddr` is stable in a way `interface:` is not.
EOF
```

- [ ] **Step 10: Commit and open the PR**

```bash
cd ~/Workspace/homelab
git add roles/talos/tasks/configure_cluster.yml roles/talos/defaults/main.yml docs/talos-lan-interface.md
git commit -m "#$ISSUE Configure the LAN NIC and Zigbee udev rule in Talos machine config

Brings ens19 up with a static address and no gateway, selected by MAC so a
kernel rename cannot break it, and adds a udev rule giving the SONOFF dongle
a stable /dev/zigbee symlink.

Applied to the live nodes with talosctl patch machineconfig rather than by
re-running the role: the role's patch tasks are not idempotent and have
already duplicated the machine.disks stanza five times in the live config.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push -u origin $ISSUE-talos-ens19-and-udev
gh pr create --title "Closes #$ISSUE: Configure ens19 and Zigbee udev rule in Talos machine config" --body "$(git log -1 --pretty=%B)

Part of the Home Assistant migration, k8s-homelab#61.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
git checkout main && git pull origin main
```

---

## Task 3: Install the `macvlan` and `static` CNI plugins

**Repo:** `~/Workspace/k8s-homelab`

**Files:**
- Create: `infrastructure/cni-plugins/namespace.yaml`
- Create: `infrastructure/cni-plugins/daemonset.yaml`
- Create: `infrastructure/cni-plugins/kustomization.yaml`
- Create: `infrastructure/cni-plugins/ks.yaml`
- Modify: `infrastructure/kustomization.yaml`

**Interfaces:**
- Consumes: nothing.
- Produces: `/opt/cni/bin/macvlan` and `/opt/cni/bin/static` on every node. Task 4's Multus
  and Task 6's NAD both hard-depend on these two binaries existing.

This task does not appear in the spec. It was added after verifying that Talos ships
neither plugin. `ghcr.io/siderolabs/install-cni:v1.4.0` was inspected and **does** contain
both, built by Sidero for this Talos version.

- [ ] **Step 1: Create the issue and branch**

```bash
cd ~/Workspace/k8s-homelab
git checkout main && git pull origin main
gh issue create --title "Feature: Install macvlan and static CNI plugins on all nodes" \
  --body "Talos ships only bridge, firewall, flannel, host-local, loopback and portmap in /opt/cni/bin. The Home Assistant migration (#61) needs the macvlan plugin and the static IPAM plugin for its LAN attachment. Installs both from ghcr.io/siderolabs/install-cni."
git checkout -b $ISSUE-install-macvlan-cni-plugin
```

- [ ] **Step 2: Verify the plugins are absent**

```bash
export TALOSCONFIG=~/.talos/talosconfig
for n in 10.98.0.10 10.98.0.11 10.98.0.12; do echo "== $n"; talosctl -n $n -e $n ls /opt/cni/bin; done
```
Expected: `bridge firewall flannel host-local loopback portmap` on each — no `macvlan`, no
`static`. This is the "failing test".

- [ ] **Step 3: Write the namespace**

```bash
mkdir -p infrastructure/cni-plugins
cat > infrastructure/cni-plugins/namespace.yaml <<'EOF'
apiVersion: v1
kind: Namespace
metadata:
  name: cni-plugins
EOF
```

- [ ] **Step 4: Write the installer DaemonSet**

```bash
cat > infrastructure/cni-plugins/daemonset.yaml <<'EOF'
# Talos ships a minimal /opt/cni/bin: bridge, firewall, flannel, host-local,
# loopback, portmap. Multus + macvlan additionally need the macvlan plugin and
# the static IPAM plugin.
#
# ghcr.io/siderolabs/install-cni is Sidero's own image and carries the full
# upstream plugin set built for Talos. Its bundled script copies *everything*,
# which would overwrite the running flannel and bridge binaries; this DaemonSet
# deliberately copies only the two plugins that are missing.
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: install-cni-plugins
  namespace: cni-plugins
  labels:
    app: install-cni-plugins
spec:
  selector:
    matchLabels:
      app: install-cni-plugins
  template:
    metadata:
      labels:
        app: install-cni-plugins
    spec:
      hostNetwork: true
      priorityClassName: system-node-critical
      tolerations:
        - operator: Exists
      initContainers:
        - name: install
          image: ghcr.io/siderolabs/install-cni:v1.4.0
          command:
            - /bin/sh
            - -c
            - |
              set -eu
              mkdir -p /host/opt/cni/bin
              for plugin in macvlan static; do
                cp -f "/opt/cni/bin/$plugin" "/host/opt/cni/bin/$plugin.tmp"
                mv -f "/host/opt/cni/bin/$plugin.tmp" "/host/opt/cni/bin/$plugin"
                echo "installed $plugin"
              done
          securityContext:
            privileged: true
          volumeMounts:
            - name: host-cni-bin
              mountPath: /host/opt/cni/bin
      containers:
        - name: pause
          image: registry.k8s.io/pause:3.10
          resources:
            requests:
              cpu: 1m
              memory: 4Mi
            limits:
              memory: 16Mi
      volumes:
        - name: host-cni-bin
          hostPath:
            path: /opt/cni/bin
            type: DirectoryOrCreate
EOF
```

The copy goes via a `.tmp` file and an atomic `mv` so a plugin is never observed
half-written by a concurrent CNI invocation.

- [ ] **Step 5: Write the kustomization and Flux Kustomization**

```bash
cat > infrastructure/cni-plugins/kustomization.yaml <<'EOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - daemonset.yaml
EOF

cat > infrastructure/cni-plugins/ks.yaml <<'EOF'
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: cni-plugins
  namespace: flux-system

spec:
  interval: 10m
  path: ./infrastructure/cni-plugins
  prune: true
  wait: true

  sourceRef:
    kind: GitRepository
    name: flux-system

  timeout: 5m
EOF
```

- [ ] **Step 6: Register it**

Rewrite `infrastructure/kustomization.yaml` as:

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - metallb/ks-controller.yaml
  - metallb/ks-config.yaml
  - cni-plugins/ks.yaml
```

- [ ] **Step 7: Build locally before pushing**

```bash
kubectl kustomize infrastructure/cni-plugins
kubectl kustomize infrastructure
```
Expected: both render without error.

- [ ] **Step 8: Commit, PR, merge, and let Flux reconcile**

```bash
git add infrastructure/cni-plugins infrastructure/kustomization.yaml
git commit -m "#$ISSUE Install the macvlan and static CNI plugins on all nodes

Talos ships a minimal /opt/cni/bin without macvlan or the static IPAM plugin,
and its flannel DaemonSet installs no plugin binaries. The Home Assistant LAN
attachment needs both. Copies only those two from Sidero's install-cni image,
atomically, so the running flannel and bridge binaries are left alone.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push -u origin $ISSUE-install-macvlan-cni-plugin
gh pr create --title "Closes #$ISSUE: Install macvlan and static CNI plugins on all nodes" --body "$(git log -1 --pretty=%B)

Part of the Home Assistant migration, k8s-homelab#61.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
git checkout main && git pull origin main
```

- [ ] **Step 9: Verify the plugins are now present**

```bash
flux reconcile kustomization infrastructure --with-source
kubectl -n cni-plugins rollout status ds/install-cni-plugins --timeout=3m
for n in 10.98.0.10 10.98.0.11 10.98.0.12; do echo "== $n"; talosctl -n $n -e $n ls /opt/cni/bin | grep -E "macvlan|static"; done
```
Expected: `macvlan` and `static` listed on all three nodes.

- [ ] **Step 10: Confirm nothing broke**

```bash
kubectl get nodes
kubectl get pods -A | grep -vE "Running|Completed" || echo "all pods healthy"
```
Expected: three `Ready` nodes and no pods outside `Running`/`Completed`. Overwriting CNI
binaries is the kind of change that shows up as pod networking failures, so check before
moving on.

---

## Task 4: Deploy Multus CNI

**Repo:** `~/Workspace/k8s-homelab`

**Files:**
- Create: `infrastructure/multus/multus.yaml`
- Create: `infrastructure/multus/kustomization.yaml`
- Create: `infrastructure/multus/ks.yaml`
- Modify: `infrastructure/kustomization.yaml`

**Interfaces:**
- Consumes: `/opt/cni/bin/macvlan` and `/opt/cni/bin/static` from Task 3.
- Produces: the `NetworkAttachmentDefinition` CRD (`k8s.cni.cncf.io/v1`) and the
  `k8s.v1.cni.cncf.io/networks` pod annotation, both used by Task 6.

- [ ] **Step 1: Create the issue and branch**

```bash
cd ~/Workspace/k8s-homelab
git checkout main && git pull origin main
gh issue create --title "Feature: Deploy Multus CNI for secondary pod networks" \
  --body "Home Assistant (#61) needs a macvlan interface on the LAN for mDNS/SSDP discovery. Multus provides secondary pod networks alongside flannel. Pinned to v4.3.1."
git checkout -b $ISSUE-deploy-multus-cni
```

- [ ] **Step 2: Verify Multus is absent**

```bash
kubectl get crd network-attachment-definitions.k8s.cni.cncf.io 2>&1
```
Expected: `Error from server (NotFound)`. This is the "failing test".

- [ ] **Step 3: Vendor the upstream manifest at a pinned tag**

Do not reference the manifest by URL — the repo must describe exactly what runs.

```bash
mkdir -p infrastructure/multus
{
  echo "# Vendored from k8snetworkplumbingwg/multus-cni v4.3.1"
  echo "# Source: https://raw.githubusercontent.com/k8snetworkplumbingwg/multus-cni/v4.3.1/deployments/multus-daemonset-thick.yml"
  echo "# Fetched: 2026-09-19. Re-vendor rather than edit in place when upgrading."
  echo "---"
  curl -fsSL https://raw.githubusercontent.com/k8snetworkplumbingwg/multus-cni/v4.3.1/deployments/multus-daemonset-thick.yml
} > infrastructure/multus/multus.yaml
grep -n "image:" infrastructure/multus/multus.yaml
```
Expected: the image references are `ghcr.io/k8snetworkplumbingwg/multus-cni:snapshot-thick`
or similar. **Replace any `snapshot` tag with `v4.3.1-thick`** so the deployment is
reproducible:

```bash
sed -i 's|multus-cni:snapshot-thick|multus-cni:v4.3.1-thick|g' infrastructure/multus/multus.yaml
grep -n "image:" infrastructure/multus/multus.yaml
```

- [ ] **Step 4: Confirm the manifest's host paths match Talos**

```bash
grep -n -A3 "hostPath" infrastructure/multus/multus.yaml
```
Expected: `/opt/cni/bin` and `/etc/cni/net.d`. Both were confirmed present on the nodes in
Task 3. If the manifest uses `/var/lib/cni/bin` or another path, edit it to `/opt/cni/bin`
and note the edit in the vendoring header.

- [ ] **Step 5: Write the kustomization and Flux Kustomization**

```bash
cat > infrastructure/multus/kustomization.yaml <<'EOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - multus.yaml
EOF

cat > infrastructure/multus/ks.yaml <<'EOF'
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: multus
  namespace: flux-system

spec:
  interval: 10m
  path: ./infrastructure/multus
  prune: true
  wait: true

  sourceRef:
    kind: GitRepository
    name: flux-system

  dependsOn:
    - name: cni-plugins

  timeout: 5m
EOF
```

`dependsOn: cni-plugins` matters: Multus starting before the macvlan binary exists would
leave the first pod attachment failing for no obvious reason.

- [ ] **Step 6: Register it**

Append `- multus/ks.yaml` to the `resources` list in `infrastructure/kustomization.yaml`,
so the file reads:

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - metallb/ks-controller.yaml
  - metallb/ks-config.yaml
  - cni-plugins/ks.yaml
  - multus/ks.yaml
```

- [ ] **Step 7: Build locally**

```bash
kubectl kustomize infrastructure/multus > /dev/null && echo "multus renders"
kubectl kustomize infrastructure > /dev/null && echo "infrastructure renders"
```
Expected: both lines print.

- [ ] **Step 8: Commit, PR, merge**

```bash
git add infrastructure/multus infrastructure/kustomization.yaml
git commit -m "#$ISSUE Deploy Multus CNI v4.3.1 for secondary pod networks

Vendored from upstream at a pinned tag rather than referenced by URL, so the
repo describes exactly what runs. Depends on cni-plugins: Multus starting
before the macvlan binary exists fails in a way that is hard to read.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push -u origin $ISSUE-deploy-multus-cni
gh pr create --title "Closes #$ISSUE: Deploy Multus CNI for secondary pod networks" --body "$(git log -1 --pretty=%B)

Part of the Home Assistant migration, k8s-homelab#61.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
git checkout main && git pull origin main
```

- [ ] **Step 9: Verify Multus is running and flannel still works**

```bash
flux reconcile kustomization infrastructure --with-source
kubectl -n kube-system rollout status ds/kube-multus-ds --timeout=3m
kubectl get crd network-attachment-definitions.k8s.cni.cncf.io
talosctl -n 10.98.0.10 -e 10.98.0.10 ls /etc/cni/net.d
```
Expected: the DaemonSet is rolled out, the CRD exists, and `/etc/cni/net.d` now contains
both `10-flannel.conflist` and Multus's generated config.

- [ ] **Step 10: Prove ordinary pod networking is unaffected**

```bash
kubectl run multus-check --image=busybox:1.36 --restart=Never --rm -it -- \
  sh -c 'wget -qO- -T5 http://kubernetes.default.svc.cluster.local 2>&1 | head -1; ip -br addr'
kubectl get pods -A | grep -vE "Running|Completed" || echo "all pods healthy"
```
Expected: the pod starts with a normal `10.244.x.x` address on `eth0` and the cluster is
otherwise healthy. Multus taking over CNI delegation is the highest-blast-radius step in
this plan; do not proceed until this passes.

---

## Task 5: Deploy Node Feature Discovery and correct the spec's label

**Repo:** `~/Workspace/k8s-homelab`

**Files:**
- Create: `infrastructure/node-feature-discovery/namespace.yaml`
- Create: `infrastructure/node-feature-discovery/helmrepository.yaml`
- Create: `infrastructure/node-feature-discovery/helmrelease.yaml`
- Create: `infrastructure/node-feature-discovery/kustomization.yaml`
- Create: `infrastructure/node-feature-discovery/ks.yaml`
- Modify: `infrastructure/kustomization.yaml`
- Modify: `docs/superpowers/specs/2026-09-19-homeassistant-k8s-migration-design.md`

**Interfaces:**
- Consumes: nothing.
- Produces: the node label
  **`feature.node.kubernetes.io/usb-02_1a86_55d4.present=true`** on whichever node holds
  the dongle. Task 8's `nodeSelector` uses this exact string.

⚠️ **The class is `02`, not `ff`.** `/sys/bus/usb/devices/2-2/bDeviceClass` on `lab1` reads
`02`. NFD's default `deviceClassWhitelist` is `["0e","ef","fe","ff"]`, which does **not**
include `02`, so default configuration produces no label at all. The whitelist below fixes
that.

- [ ] **Step 1: Create the issue and branch**

```bash
cd ~/Workspace/k8s-homelab
git checkout main && git pull origin main
gh issue create --title "Feature: Deploy Node Feature Discovery to label the node holding the Zigbee dongle" \
  --body "Part of #61. NFD's usb source labels nodes from attached USB devices, so Home Assistant can be scheduled by detection rather than by a hardcoded node name. Also corrects the label recorded in the design spec: the dongle's device class is 02, not ff, and 02 is not in NFD's default whitelist."
git checkout -b $ISSUE-node-feature-discovery
```

- [ ] **Step 2: Verify no USB labels exist**

```bash
kubectl get nodes -o json | grep -c "feature.node.kubernetes.io" || echo "no NFD labels"
```
Expected: `no NFD labels`. This is the "failing test".

- [ ] **Step 3: Write the namespace and HelmRepository**

```bash
mkdir -p infrastructure/node-feature-discovery
cat > infrastructure/node-feature-discovery/namespace.yaml <<'EOF'
# The nfd-worker DaemonSet mounts eight hostPath volumes (/sys, /boot,
# /etc/os-release, /usr/lib, /lib ...) to read hardware features. Talos
# enforces PodSecurity "baseline" in every namespace except kube-system, and
# baseline forbids hostPath volumes, so this namespace must be labelled
# privileged -- the same convention metallb-system, longhorn-system,
# monitoring and cni-plugins already use in this repo.
#
# Without these labels the failure is SILENT: the DaemonSet is created and
# Flux reports Ready, because enforcement applies to pods, not workload
# templates. Only the pods are rejected, so `rollout status` simply hangs.
apiVersion: v1
kind: Namespace
metadata:
  name: node-feature-discovery
  labels:
    pod-security.kubernetes.io/enforce: privileged
    pod-security.kubernetes.io/audit: privileged
    pod-security.kubernetes.io/warn: privileged
EOF

cat > infrastructure/node-feature-discovery/helmrepository.yaml <<'EOF'
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: node-feature-discovery
  namespace: flux-system
spec:
  interval: 12h
  url: https://kubernetes-sigs.github.io/node-feature-discovery/charts
EOF
```

- [ ] **Step 4: Write the HelmRelease with the corrected device class whitelist**

```bash
cat > infrastructure/node-feature-discovery/helmrelease.yaml <<'EOF'
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: node-feature-discovery
  namespace: node-feature-discovery

spec:
  interval: 30m

  chart:
    spec:
      chart: node-feature-discovery
      version: "0.19.x"
      sourceRef:
        kind: HelmRepository
        name: node-feature-discovery
        namespace: flux-system

  values:
    worker:
      config:
        # No `core:` block. NFD 0.19's keys are core.featureSources and
        # core.labelSources (not core.sources), and both already default to
        # [all] - which includes usb. Naming a non-existent key here would
        # be ignored at best, and an explicit list would only narrow what
        # already works.
        sources:
          usb:
            # NFD's default deviceClassWhitelist is ["0e","ef","fe","ff"].
            # The SONOFF ZBDongle-P V2 (1a86:55d4) reports bDeviceClass 02
            # (CDC), so without "02" here NFD emits no label for it at all
            # and the Home Assistant nodeSelector never matches.
            deviceClassWhitelist:
              - "02"
              - "0e"
              - "ef"
              - "fe"
              - "ff"
            deviceLabelFields:
              - class
              - vendor
              - device
      resources:
        requests:
          cpu: 10m
          memory: 64Mi
        limits:
          memory: 128Mi

    master:
      replicaCount: 1
      resources:
        requests:
          cpu: 50m
          memory: 128Mi
        limits:
          memory: 256Mi

    gc:
      enable: true
EOF
```

- [ ] **Step 5: Write the kustomization and Flux Kustomization**

```bash
cat > infrastructure/node-feature-discovery/kustomization.yaml <<'EOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - helmrepository.yaml
  - helmrelease.yaml
EOF

cat > infrastructure/node-feature-discovery/ks.yaml <<'EOF'
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: node-feature-discovery
  namespace: flux-system

spec:
  interval: 10m
  path: ./infrastructure/node-feature-discovery
  prune: true
  wait: true

  sourceRef:
    kind: GitRepository
    name: flux-system

  timeout: 10m
EOF
```

- [ ] **Step 6: Register it**

`infrastructure/kustomization.yaml` becomes:

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - metallb/ks-controller.yaml
  - metallb/ks-config.yaml
  - cni-plugins/ks.yaml
  - multus/ks.yaml
  - node-feature-discovery/ks.yaml
```

- [ ] **Step 7: Correct the spec**

In `docs/superpowers/specs/2026-09-19-homeassistant-k8s-migration-design.md`, replace both
occurrences of `usb-ff_1a86_55d4` with `usb-02_1a86_55d4`, and in §4.2 replace the sentence

> with the `usb` source enabled and device class `ff` (vendor-specific, which the dongle's
> CH9102 bridge presents) whitelisted

with

> with the `usb` source enabled and device class `02` (CDC — verified from
> `/sys/bus/usb/devices/2-2/bDeviceClass` on `lab1`) added to the whitelist. NFD's default
> `deviceClassWhitelist` is `["0e","ef","fe","ff"]` and does not include `02`, so the
> default configuration emits no label for this device.

- [ ] **Step 8: Build locally, commit, PR, merge**

```bash
kubectl kustomize infrastructure/node-feature-discovery > /dev/null && echo "nfd renders"
git add infrastructure/node-feature-discovery infrastructure/kustomization.yaml docs/superpowers/specs/2026-09-19-homeassistant-k8s-migration-design.md
git commit -m "#$ISSUE Deploy Node Feature Discovery and correct the dongle's device class

NFD labels nodes from attached USB devices so Home Assistant can be scheduled
by detection rather than a hardcoded node name.

Corrects the design spec: the SONOFF dongle reports bDeviceClass 02 (CDC),
not ff, so the label is usb-02_1a86_55d4.present. Class 02 is also absent
from NFD's default deviceClassWhitelist, so the default configuration would
have produced no label at all.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push -u origin $ISSUE-node-feature-discovery
gh pr create --title "Closes #$ISSUE: Deploy Node Feature Discovery to label the node holding the Zigbee dongle" --body "$(git log -1 --pretty=%B)

Part of the Home Assistant migration, k8s-homelab#61.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
git checkout main && git pull origin main
```

- [ ] **Step 9: Verify NFD is emitting USB labels at all**

The dongle is still on `pve` at this point, so its label cannot appear yet. Prove the
source works using the devices `pve2` already passes through, and by checking the nodes'
virtio and QEMU devices:

```bash
flux reconcile kustomization infrastructure --with-source
kubectl -n node-feature-discovery rollout status ds/node-feature-discovery-worker --timeout=5m

# If rollout status hangs, check for silent PodSecurity rejection before
# anything else - it is the most likely cause and produces no pod to inspect:
kubectl -n node-feature-discovery get events --sort-by=.lastTimestamp | grep -i forbidden || echo "no PodSecurity rejections"
kubectl -n node-feature-discovery get ds node-feature-discovery-worker -o jsonpath='desired={.status.desiredNumberScheduled} ready={.status.numberReady}{"\n"}'
kubectl get nodes -o json | python3 -c "
import sys, json
for n in json.load(sys.stdin)['items']:
    labs = [k for k in n['metadata']['labels'] if k.startswith('feature.node.kubernetes.io/')]
    print(n['metadata']['name'], len(labs), 'NFD labels')
    for k in sorted(labs):
        if 'usb' in k or 'pci' in k:
            print('   ', k)
"
```
Expected: each node reports a non-zero NFD label count and at least some
`feature.node.kubernetes.io/pci-*` labels. This proves the worker is running and publishing.
If `usb-*` labels are entirely absent on every node, the whitelist or the `usb` source is
misconfigured — fix it here, because Task 8's scheduling depends on it and the dongle move
in Task 7 is much harder to debug after the fact.

---

## Task 6: Home Assistant manifests and Phase A validation

**Repo:** `~/Workspace/k8s-homelab`

**Files:**
- Create: `apps/base/homeassistant/namespace.yaml`
- Create: `apps/base/homeassistant/networkattachment.yaml`
- Create: `apps/base/homeassistant/pvc.yaml`
- Create: `apps/base/homeassistant/deployment.yaml`
- Create: `apps/base/homeassistant/service.yaml`
- Create: `apps/base/homeassistant/ingressroute.yaml`
- Create: `apps/base/homeassistant/kustomization.yaml`
- Create: `apps/production/homeassistant/kustomization.yaml`
- Create: `clusters/production/homeassistant.yaml`

**Interfaces:**
- Consumes: `macvlan`/`static` plugins (Task 3), the `NetworkAttachmentDefinition` CRD
  (Task 4), the NFD worker (Task 5), and `ens19` up on every node (Tasks 1–2).
- Produces: a running `homeassistant` Deployment reachable at `192.168.3.11:8123` and
  `https://homeassistant.homelab.cs-ol.de`, and an empty 20 Gi PVC named
  `homeassistant-config`. Task 8 fills the PVC and patches the Deployment.

**This task deliberately ships the validation variant** — no `nodeSelector` and no
`/dev/zigbee` mount. The dongle is still on `pve`, so no node carries the NFD label and no
node has the device. A pod with either would stay `Pending` or fail to start, and nothing
downstream could be tested. Task 8 adds both.

- [ ] **Step 1: Create the issue and branch**

```bash
cd ~/Workspace/k8s-homelab
git checkout main && git pull origin main
gh issue create --title "Feature: Home Assistant manifests and LAN discovery validation" \
  --body "Part of #61, Phase A. Adds the Home Assistant app manifests and validates the risky plumbing - macvlan LAN attachment, multicast discovery, Longhorn PVC and Traefik ingress - with an empty config and no Zigbee, before any downtime."
git checkout -b $ISSUE-homeassistant-manifests
```

- [ ] **Step 2: Verify the namespace does not exist**

```bash
kubectl get ns homeassistant 2>&1
```
Expected: `Error from server (NotFound)`. This is the "failing test".

- [ ] **Step 3: Write the namespace and the network attachment**

```bash
mkdir -p apps/base/homeassistant apps/production/homeassistant
cat > apps/base/homeassistant/namespace.yaml <<'EOF'
# The Deployment below adds NET_ADMIN and NET_RAW. Talos enforces PodSecurity
# "baseline" in every namespace except kube-system, and baseline's allowed
# capabilities.add list contains neither -- verified against this cluster:
#
#   Error from server (Forbidden): pods "pss-probe" is forbidden: violates
#   PodSecurity "baseline:latest": non-default capabilities (container "c"
#   must not include "NET_ADMIN", "NET_RAW" in securityContext.capabilities.add)
#
# Without these labels the failure is SILENT in the usual place: enforcement
# applies to pods, not to workload templates, so the Deployment is created and
# only the ReplicaSet's pods are rejected. `rollout status` hangs with nothing
# obviously wrong. Same trap as cni-plugins (Task 3) and NFD (Task 5).
apiVersion: v1
kind: Namespace
metadata:
  name: homeassistant
  labels:
    pod-security.kubernetes.io/enforce: privileged
    pod-security.kubernetes.io/audit: privileged
    pod-security.kubernetes.io/warn: privileged
EOF

cat > apps/base/homeassistant/networkattachment.yaml <<'EOF'
# Gives the Home Assistant pod a real address on the household LAN so mDNS and
# SSDP discovery work. Roughly 40 of its config entries - esphome, homekit,
# wled, shelly, cast, dlna, matter and others - depend on this.
#
# Two details are load-bearing and neither is optional:
#
#  * No "gateway". A macvlan default route would take precedence over the pod
#    network and break cluster DNS. The /22 address alone routes LAN traffic
#    out net1; internet and in-cluster traffic keeps using eth0.
#
#  * The explicit 224.0.0.0/4 route. Without it, queries to 224.0.0.251 follow
#    the default route out eth0 into flannel and are never seen on the LAN.
#    This single line is what makes discovery work.
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: lan-macvlan
  namespace: homeassistant
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
          { "address": "192.168.3.11/22" }
        ],
        "routes": [
          { "dst": "224.0.0.0/4" }
        ]
      }
    }
EOF
```

If Task 1 Step 8 recorded a kernel name other than `ens19`, use that name for `master`
here.

- [ ] **Step 4: Write the PVC**

```bash
cat > apps/base/homeassistant/pvc.yaml <<'EOF'
# longhorn-retain, not the default longhorn class: an accidental Kustomization
# delete must not take the Home Assistant database with it.
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: homeassistant-config
  namespace: homeassistant
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: longhorn-retain
  resources:
    requests:
      storage: 20Gi
EOF
```

- [ ] **Step 5: Write the Deployment (validation variant)**

```bash
cat > apps/base/homeassistant/deployment.yaml <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: homeassistant
  namespace: homeassistant
spec:
  replicas: 1

  # SQLite and a serial device tolerate exactly one writer. Never RollingUpdate.
  strategy:
    type: Recreate

  selector:
    matchLabels:
      app: homeassistant

  template:
    metadata:
      labels:
        app: homeassistant
      annotations:
        k8s.v1.cni.cncf.io/networks: homeassistant/lan-macvlan

    spec:
      containers:
        - name: homeassistant
          image: ghcr.io/home-assistant/home-assistant:2026.9.3

          securityContext:
            capabilities:
              # Parity with the compose stack's cap_add. The four ping
              # integrations need raw sockets.
              add:
                - NET_ADMIN
                - NET_RAW

          env:
            - name: TZ
              value: Europe/Berlin
            - name: LANG
              value: C.UTF-8

          ports:
            - name: http
              containerPort: 8123

          # Home Assistant with 100+ config entries and a 9 GB recorder
          # database can take several minutes to answer. A startupProbe with a
          # long window keeps the liveness probe from killing it mid-start.
          startupProbe:
            httpGet:
              path: /
              port: http
            periodSeconds: 10
            failureThreshold: 60

          livenessProbe:
            httpGet:
              path: /
              port: http
            periodSeconds: 30
            failureThreshold: 3

          readinessProbe:
            httpGet:
              path: /
              port: http
            periodSeconds: 10

          resources:
            requests:
              cpu: 250m
              memory: 1Gi
            limits:
              cpu: "2"
              memory: 4Gi

          volumeMounts:
            - name: config
              mountPath: /config

      volumes:
        - name: config
          persistentVolumeClaim:
            claimName: homeassistant-config
EOF
```

- [ ] **Step 6: Write the Service and IngressRoute**

```bash
cat > apps/base/homeassistant/service.yaml <<'EOF'
apiVersion: v1
kind: Service
metadata:
  name: homeassistant
  namespace: homeassistant
spec:
  type: ClusterIP
  selector:
    app: homeassistant
  ports:
    - name: http
      port: 8123
      targetPort: http
EOF

cat > apps/base/homeassistant/ingressroute.yaml <<'EOF'
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: homeassistant
  namespace: homeassistant
  annotations:
    gethomepage.dev/href: 'https://homeassistant.homelab.cs-ol.de'
    gethomepage.dev/enabled: 'true'
    gethomepage.dev/description: Automate your home
    gethomepage.dev/group: Home Automation
    gethomepage.dev/icon: home-assistant.png
    gethomepage.dev/app: homeassistant
    gethomepage.dev/name: Home Assistant
    gethomepage.dev/pod-selector: ''
    gethomepage.dev/weight: '1'
spec:
  entryPoints:
    - websecure
  routes:
    - kind: Rule
      match: Host(`homeassistant.homelab.cs-ol.de`)
      services:
        - kind: Service
          name: homeassistant
          port: 8123
  tls: {}
EOF
```

- [ ] **Step 7: Write the kustomizations and the Flux Kustomization**

```bash
cat > apps/base/homeassistant/kustomization.yaml <<'EOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - networkattachment.yaml
  - pvc.yaml
  - service.yaml
  - deployment.yaml
  - ingressroute.yaml
EOF

cat > apps/production/homeassistant/kustomization.yaml <<'EOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - ../../base/homeassistant
EOF

cat > clusters/production/homeassistant.yaml <<'EOF'
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization

metadata:
  name: homeassistant
  namespace: flux-system

spec:
  interval: 10m

  sourceRef:
    kind: GitRepository
    name: flux-system

  path: ./apps/production/homeassistant

  prune: true
  wait: true

  dependsOn:
    - name: infrastructure
EOF
```

- [ ] **Step 8: Build locally, commit, PR, merge**

```bash
kubectl kustomize apps/production/homeassistant > /dev/null && echo "homeassistant renders"
git add apps/base/homeassistant apps/production/homeassistant clusters/production/homeassistant.yaml
git commit -m "#$ISSUE Add Home Assistant manifests with macvlan LAN attachment

Phase A of the migration: the app runs with an empty config, no nodeSelector
and no Zigbee device, so the risky plumbing - macvlan attachment, multicast
route, Longhorn PVC and Traefik ingress - is proven before any downtime.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push -u origin $ISSUE-homeassistant-manifests
gh pr create --title "Closes #$ISSUE: Home Assistant manifests and LAN discovery validation" --body "$(git log -1 --pretty=%B)

Part of the Home Assistant migration, k8s-homelab#61.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
git checkout main && git pull origin main
```

- [ ] **Step 9: Verify the pod gets both interfaces**

```bash
flux reconcile kustomization homeassistant --with-source
kubectl -n homeassistant get pvc homeassistant-config
kubectl -n homeassistant rollout status deploy/homeassistant --timeout=10m
POD=$(kubectl -n homeassistant get pod -l app=homeassistant -o name | head -1)
kubectl -n homeassistant exec $POD -- ip -br addr
kubectl -n homeassistant exec $POD -- ip route
```

If the pod never appears at all, check for a PodSecurity rejection before
anything else -- it is the one failure mode that leaves no pod to describe:

```bash
kubectl -n homeassistant get events --sort-by=.lastTimestamp | grep -i forbidden
kubectl -n homeassistant describe replicaset -l app=homeassistant | grep -A3 Events
```
Expected: no output. Any `violates PodSecurity` line means the namespace labels
in Step 3 did not land.
Expected: the PVC is `Bound`; the pod has **`eth0` with a `10.244.x.x` address and `net1`
with `192.168.3.11/22`**; the route table shows a default via `eth0`, a `192.168.0.0/22`
route via `net1`, and **`224.0.0.0/4 dev net1`**. If `net1` is missing, read
`kubectl -n homeassistant describe pod $POD` — a missing `macvlan` binary or a wrong
`master` name both surface there.

- [ ] **Step 10: Verify multicast actually leaves the pod — the test that matters**

```bash
# From the LAN, the pod must answer:
ping -c3 192.168.3.11
curl -sS -o /dev/null -w "%{http_code}\n" http://192.168.3.11:8123/
curl -sS -o /dev/null -w "%{http_code}\n" https://homeassistant.homelab.cs-ol.de/

# And multicast must be reaching it. From lab1, which is on the same LAN:
# lab1 has no tcpdump installed and this is not the place to install one.
# Run it in a throwaway container on lab1's host network instead -- same
# packets, nothing left behind. lab1's LAN interface is ens18 (192.168.1.28/22).
ssh lab1 "timeout 20 docker run --rm --net=host --cap-add=NET_RAW --cap-add=NET_ADMIN \
  nicolaka/netshoot tcpdump -ni ens18 'host 192.168.3.11 and port 5353' -c 5" 2>&1
```
Expected: ping succeeds, both HTTP checks return `200`, and tcpdump captures mDNS packets
**sourced from `192.168.3.11`**. Traffic sourced from a `10.244.x.x` address instead means
the `224.0.0.0/4` route is not being applied — stop and fix the NAD before going further.

- [ ] **Step 11: Confirm discovery in the UI, then reset**

Open `https://homeassistant.homelab.cs-ol.de`, complete the onboarding wizard with a
throwaway account, and check **Settings → Devices & Services**. Expected: discovered
entries for ESPHome, WLED and Shelly devices appear. This is the end-to-end proof.

Onboarding writes a full config onto the PVC — `.storage` with a throwaway
account, `configuration.yaml`, an empty `home-assistant_v2.db`. **That state must
not survive into the real config.** Task 8 Step 4 restores with `tar -x`, which
overwrites files but never deletes them, so any file unique to the test instance
would quietly persist alongside the migrated data.

Deleting it is therefore Task 8 Step 3's job, immediately before the restore —
not here, hours or days earlier. Leaving the test instance running until then is
harmless: it holds no credentials worth keeping and adopts nothing unless asked.

If you would rather not leave a second Home Assistant advertising itself on the
LAN in the meantime, hold it down — but note that **Task 8 Step 3 repeats these
two commands**, so skipping this block costs nothing:

```bash
# Suspend first: Flux reconciles every 10m and would scale this straight
# back to 1. Suspend-then-scale holds it down without a commit.
flux suspend kustomization homeassistant
kubectl -n homeassistant scale deploy/homeassistant --replicas=0
kubectl -n homeassistant wait --for=delete pod -l app=homeassistant --timeout=2m
```

Record the outcome in the issue before closing it. **Do not proceed to Task 7 unless
Step 10 and Step 11 both passed** — everything after this point involves downtime.

---

## Task 7: Move the Zigbee dongle to `pve2`

**Repo:** `~/Workspace/homelab`

**Files:**
- Modify: `roles/proxmox/terraform/terraform.tfvars`

**Interfaces:**
- Consumes: the `usb_devices` field from Task 1; the udev rule from Task 2.
- Produces: `/dev/zigbee` on talos1 and the NFD label
  `feature.node.kubernetes.io/usb-02_1a86_55d4.present=true` on that node. Task 8 depends
  on both.

⚠️ **Zigbee automation stops working the moment the dongle is unplugged** and does not
resume until Task 8 completes. Everything else in Home Assistant keeps running on `lab1`
throughout. Do this immediately before Task 8, not days earlier.

- [ ] **Step 1: Create the issue and branch**

```bash
cd ~/Workspace/homelab
git checkout main && git pull origin main
gh issue create --title "Feature: Move the SONOFF Zigbee dongle from pve to talos1 on pve2"
git checkout -b $ISSUE-move-zigbee-dongle
```

- [ ] **Step 2: Record the current state for rollback**

```bash
ssh pve 'qm config 100 | grep -E "^usb"'
ssh lab1 'ls -l /dev/serial/by-id/'
```
Expected: `usb0: host=152d:0583` and `usb1: host=1a86:55d4`, and the ITEAD symlink. Save
this output in the issue — `usb0` is an unrelated USB-SATA bridge and **must not be
touched**.

- [ ] **Step 3: Stop the compose stack**

Home Assistant on lab1 is managed by **systemd**, as
`docker-compose@homeassistant.service` — a bare `docker compose stop` can be undone by the
unit, leaving the old instance racing the new one for the MQTT client ID and the dongle.

```bash
ssh lab1 'sudo systemctl stop docker-compose@homeassistant.service'
ssh lab1 'systemctl is-active docker-compose@homeassistant.service; docker ps --filter name=homeassistant --format "{{.Status}}"'
```
Expected: `inactive`, and no container line. Stop the unit rather than `docker compose down`
so the container definition survives for rollback.

- [ ] **Step 4: Remove the passthrough from `lab1` in Terraform**

In `roles/proxmox/terraform/terraform.tfvars`, VM `101` is `deployment` and VM `100`
(`macbook`/`lab1`) is **not currently managed by this Terraform**. Confirm:

```bash
cd ~/Workspace/homelab/roles/proxmox/terraform
tofu state list | grep -E '"100"' || echo "VM 100 is not in Terraform state"
```

If VM 100 is not in state, remove the passthrough directly on the hypervisor and note it in
the issue as an out-of-band change:

```bash
ssh pve 'qm set 100 --delete usb1'
ssh pve 'qm config 100 | grep -E "^usb"'
```
Expected: only `usb0: host=152d:0583` remains.

If VM 100 *is* in state, delete its `usb_devices` entry from `terraform.tfvars` and
`tofu apply -target` it instead.

- [ ] **Step 5: Shut down lab1, move the dongle, restart**

⚠️ **This shutdown is household-wide, not Home Assistant-only.** lab1 runs 20 compose
services. Several are ones Home Assistant itself integrates with, so they must come back
before the cutover in Task 8 is meaningful:

```bash
ssh lab1 'systemctl list-units "docker-compose@*.service" --state=active --no-legend | awk "{print \$1}"'
```
Expected to include at least `mosquitto` (the MQTT broker), `esphome`, `influxdb`, `pihole`,
`grafana`, `paperless`, `n8n` and `nginx`. Save this list in the issue — it is what must be
running again after the reboot.

```bash
ssh lab1 'sudo shutdown -h now' || true
ssh pve 'qm status 100'
```
Wait for `status: stopped`. **Physically unplug the SONOFF ZBDongle-P from the `pve`
machine and plug it into `pve2`.** Then:

```bash
ssh pve2 'lsusb | grep 1a86:55d4'
ssh pve  'qm start 100'
```
Expected: `pve2` now lists `SONOFF Zigbee 3.0 USB Dongle Plus V2`.

Once lab1 is back, confirm every service from the list above returned **except**
`homeassistant`, which Task 7 Step 3 deliberately stopped:

```bash
ssh lab1 'systemctl list-units "docker-compose@*.service" --state=active --no-legend | awk "{print \$1}"'
ssh lab1 'systemctl is-active docker-compose@homeassistant.service'
```
Expected: the same list minus `homeassistant`, and `inactive` for it. Note that `pve2` already
has a different CH340 device (`1a86:7523`) attached — match on `55d4`, not on `1a86`.

- [ ] **Step 6: Add the passthrough to talos1 in Terraform**

In `terraform.tfvars`, add to the VM `900` entry, after its `network_devices` list:

```hcl
    usb_devices = [
      {
        host = "1a86:55d4"
      }
    ]
```

- [ ] **Step 7: Apply and restart talos1**

```bash
cd ~/Workspace/homelab/roles/proxmox/terraform
tofu plan  -target='proxmox_virtual_environment_vm.vms["900"]'
tofu apply -target='proxmox_virtual_environment_vm.vms["900"]'
ssh pve2 'qm config 900 | grep -E "^usb"'
```
Expected: the plan is an **update in place** adding one `usb` block; afterwards
`usb0: host=1a86:55d4`. If the plan proposes replacement, **stop** — that would destroy the
node.

```bash
export TALOSCONFIG=~/.talos/talosconfig
talosctl -n 10.98.0.10 -e 10.98.0.10 reboot
kubectl get nodes   # wait for all three Ready
```

- [ ] **Step 8: Verify the device and the udev symlink**

```bash
talosctl -n 10.98.0.10 -e 10.98.0.10 ls -l /dev | grep -E "zigbee|ttyACM"
```
Expected: both `ttyACM0` and the `zigbee` symlink. If `zigbee` is missing but `ttyACM0` is
present, the udev rule from Task 2 did not apply — re-check it before continuing, because
Task 8 mounts `/dev/zigbee` by name.

- [ ] **Step 9: Verify the NFD label appeared — the whole point of the exercise**

```bash
kubectl get nodes -l feature.node.kubernetes.io/usb-02_1a86_55d4.present=true
```
Expected: exactly one node listed, the one backing `talos1`. If empty, list what NFD
actually produced and reconcile against it:

```bash
kubectl get nodes -o json | python3 -c "
import sys, json
for n in json.load(sys.stdin)['items']:
    for k in sorted(n['metadata']['labels']):
        if 'usb' in k: print(n['metadata']['name'], k)
"
```
A different class digit here means the whitelist in Task 5 needs the observed class added,
and Task 8's `nodeSelector` must use the observed label.

- [ ] **Step 10: Commit and open the PR**

```bash
cd ~/Workspace/homelab
git add roles/proxmox/terraform/terraform.tfvars
git commit -m "#$ISSUE Pass the SONOFF Zigbee dongle through to talos1

Moves the dongle from pve VM 100 (lab1) to talos1 on pve2, passed through by
vendor:product so re-plugging into a different port still works. Node Feature
Discovery then labels the node and Home Assistant schedules onto it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push -u origin $ISSUE-move-zigbee-dongle
gh pr create --title "Closes #$ISSUE: Move the SONOFF Zigbee dongle from pve to talos1" --body "$(git log -1 --pretty=%B)

Part of the Home Assistant migration, k8s-homelab#61.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
git checkout main && git pull origin main
```

---

## Task 8: Migrate the data and cut over

**Repo:** `~/Workspace/k8s-homelab`

**Files:**
- Create: `apps/production/homeassistant/deployment-patch.yaml`
- Modify: `apps/production/homeassistant/kustomization.yaml`
- Create: `docs/runbooks/homeassistant-cutover.md`
- Modify: `apps/base/homepage/configmap.yaml:86-95`

**Interfaces:**
- Consumes: the empty PVC and running Deployment (Task 6), `/dev/zigbee` and the NFD label
  (Task 7).
- Produces: Home Assistant running in Kubernetes on the migrated data.

- [ ] **Step 1: Create the issue and branch**

```bash
cd ~/Workspace/k8s-homelab
git checkout main && git pull origin main
gh issue create --title "Feature: Migrate Home Assistant data and cut over to Kubernetes" \
  --body "Phase B of #61. Copies /config onto the Longhorn PVC, pins the pod to the node holding the dongle, mounts /dev/zigbee, and cuts over."
git checkout -b $ISSUE-homeassistant-cutover
```

- [ ] **Step 2: Confirm the source is still readable and HA is stopped**

```bash
ssh lab1 'systemctl is-active docker-compose@homeassistant.service; findmnt -T /docker/homeassistant/config -o SOURCE,FSTYPE; du -sh /docker/homeassistant/config'
```
Expected: `inactive` (stopped in Task 7), the NFS source still mounted, 27 G total. If the NFS mount is gone, **stop** — the QNAP registers no `mountd`, so it cannot be
remounted, and the migration cannot proceed. Nothing has been lost; Task 7 is reversible.

- [ ] **Step 3: Start a helper pod with the PVC mounted**

```bash
flux suspend kustomization homeassistant   # see Task 6 Step 11
kubectl -n homeassistant scale deploy/homeassistant --replicas=0
kubectl -n homeassistant wait --for=delete pod -l app=homeassistant --timeout=2m

kubectl -n homeassistant apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: ha-migrate
  namespace: homeassistant
spec:
  restartPolicy: Never
  containers:
    - name: shell
      image: busybox:1.36
      command: ["sleep", "7200"]
      volumeMounts:
        - name: config
          mountPath: /data
  volumes:
    - name: config
      persistentVolumeClaim:
        claimName: homeassistant-config
EOF
kubectl -n homeassistant wait --for=condition=Ready pod/ha-migrate --timeout=2m
kubectl -n homeassistant exec ha-migrate -- ls -la /data
```

`/data` is **not** empty: Task 6 Step 11's onboarding left a complete throwaway
config on it. Look at what is there, confirm it is only that, then clear it —
`tar -x` in Step 4 overwrites but never deletes, so anything unique to the test
instance would otherwise survive into the migrated config:

```bash
# Look before you delete. Everything here should be dated from the Step 11
# onboarding and nothing else. A stale home-assistant_v2.db of any real size
# means you are pointed at the wrong volume - stop.
kubectl -n homeassistant exec ha-migrate -- sh -c 'ls -la /data; du -sh /data'

kubectl -n homeassistant exec ha-migrate -- sh -c 'rm -rf /data/..?* /data/.[!.]* /data/*'
kubectl -n homeassistant exec ha-migrate -- ls -la /data
```
Expected after the wipe: `/data` is empty (or holds only `lost+found`). The
glob triple is deliberate — a bare `/data/*` misses `.storage`, which is the one
directory that most needs to go.

- [ ] **Step 4: Stream the data in, excluding the dead weight**

```bash
ssh lab1 'tar -C /docker/homeassistant/config \
  --exclude="*.db.corrupt.*" \
  --exclude="*-wal.corrupt.*" \
  --exclude="./backups" \
  --exclude="./tmp" \
  --exclude="./home-assistant.log*" \
  -cf - .' \
| kubectl -n homeassistant exec -i ha-migrate -- tar -C /data -xf -
```
This drops 14.4 GB of `.corrupt` files, the 1.7 GB `backups` directory and the logs,
carrying roughly 10 GB. The stream goes `lab1 → workstation → API server → pod`; expect
several minutes.

- [ ] **Step 5: Verify what landed**

```bash
kubectl -n homeassistant exec ha-migrate -- sh -c '
  du -sh /data
  ls -la /data/configuration.yaml /data/home-assistant_v2.db /data/zigbee.db /data/.storage
  ls /data | grep -c corrupt || echo "no corrupt files: good"
'
ssh lab1 'ls /docker/homeassistant/config | wc -l'
kubectl -n homeassistant exec ha-migrate -- sh -c 'ls /data | wc -l'
```
Expected: roughly 10 G total; `configuration.yaml`, `home-assistant_v2.db`, `zigbee.db` and
`.storage` all present; no `corrupt` files. The two file counts differ by exactly the
excluded entries.

- [ ] **Step 6: Add the pod network to the existing `trusted_proxies`**

⚠️ `configuration.yaml` **already has an `http:` block** — verified on lab1, at line 7:

```yaml
http:
  use_x_forwarded_for: true
  trusted_proxies:
    - 10.7.0.0/24
    - 85.215.138.242/32
  ip_ban_enabled: true
  login_attempts_threshold: 10
```

Appending a second `http:` block would be a duplicate YAML key. The two existing
`trusted_proxies` entries are pre-existing remote-access paths and **must be preserved** —
this migration was not asked to change them. The only edit is to *add* the flannel pod CIDR
to the existing list.

First confirm the block is still exactly as expected:

```bash
kubectl -n homeassistant exec ha-migrate -- sed -n '1,20p' /data/configuration.yaml
```
Expected: the block above. If it differs, hand-edit rather than running the command below.

```bash
kubectl -n homeassistant exec ha-migrate -- sh -c '
  cp /data/configuration.yaml /data/configuration.yaml.pre-k8s
  # Insert the pod CIDR as the first entry under the existing trusted_proxies key.
  sed -i "/^  trusted_proxies:/a\\    - 10.244.0.0/16   # flannel pod network: Traefik proxies from here" /data/configuration.yaml
'
kubectl -n homeassistant exec ha-migrate -- sed -n '1,20p' /data/configuration.yaml
```
Expected: `10.244.0.0/16` now appears as the first list entry under `trusted_proxies`, with
`10.7.0.0/24` and `85.215.138.242/32` still below it, and exactly **one** `http:` key in the
file:

```bash
kubectl -n homeassistant exec ha-migrate -- grep -c '^http:' /data/configuration.yaml
```
Expected: `1`. `configuration.yaml.pre-k8s` is the rollback.

- [ ] **Step 7: Point ZHA at the new device path**

The migrated ZHA config entry still records `/dev/ttyACM0`. Rewrite it to `/dev/zigbee`:

```bash
kubectl -n homeassistant exec ha-migrate -- sh -c '
  grep -o "/dev/ttyACM0" /data/.storage/core.config_entries | head -1
'
kubectl -n homeassistant exec ha-migrate -- sh -c '
  cp /data/.storage/core.config_entries /data/.storage/core.config_entries.pre-k8s
  sed -i "s|/dev/ttyACM0|/dev/zigbee|g" /data/.storage/core.config_entries
  grep -o "/dev/zigbee" /data/.storage/core.config_entries | head -1
'
```
Expected: the first command prints `/dev/ttyACM0`, the last prints `/dev/zigbee`. The
`.pre-k8s` copy is the in-place rollback.

- [ ] **Step 8: Remove the helper pod and write the production patch**

```bash
kubectl -n homeassistant delete pod ha-migrate

cat > apps/production/homeassistant/deployment-patch.yaml <<'EOF'
# Production pins Home Assistant to the node that actually holds the Zigbee
# dongle. The label is published by Node Feature Discovery from the attached
# USB device, so moving the passthrough to another node moves the workload
# with it - no manifest change needed.
#
# Device class 02 is CDC, read from /sys/bus/usb/devices/2-2/bDeviceClass.
# It is not in NFD's default whitelist; see infrastructure/node-feature-discovery.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: homeassistant
  namespace: homeassistant
spec:
  template:
    spec:
      nodeSelector:
        feature.node.kubernetes.io/usb-02_1a86_55d4.present: "true"
      containers:
        - name: homeassistant
          volumeMounts:
            - name: config
              mountPath: /config
            - name: zigbee
              mountPath: /dev/zigbee
      volumes:
        - name: config
          persistentVolumeClaim:
            claimName: homeassistant-config
        - name: zigbee
          hostPath:
            path: /dev/zigbee
            type: CharDevice
EOF

cat > apps/production/homeassistant/kustomization.yaml <<'EOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - ../../base/homeassistant

patches:
  - path: deployment-patch.yaml
EOF

kubectl kustomize apps/production/homeassistant | grep -A4 nodeSelector
```
Expected: the rendered output contains the `nodeSelector` and the `zigbee` volume.

- [ ] **Step 9: Update the Homepage dashboard entry**

In `apps/base/homepage/configmap.yaml`, the `Home Assistant` block currently points at
`http://192.168.1.28:8123`. Replace the `href`, `widget.url`, `server` and `container`
keys so the block reads:

```yaml
        - Home Assistant:
            href: https://homeassistant.homelab.cs-ol.de/
            description: Automate your home
            icon: home-assistant.png
            widget:
                type: homeassistant
                url: http://homeassistant.homeassistant.svc.cluster.local:8123
                key: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJjMjc1MjA1ZWJiMTE0ZWQzYWRmZmI2NmJjOTEyMjJiNyIsImlhdCI6MTc2NDUwODkxNCwiZXhwIjoyMDc5ODY4OTE0fQ.dMsrNydPd0ZSEpD3U-nJyHcCZp_bP0PAGc8ynIVnvzY
```

Drop the `server:`/`container:` keys — they pointed at the Docker integration on
`docker-lab1`, which no longer runs this container.

- [ ] **Step 10: Commit, PR, merge, and bring it up**

```bash
git add apps/production/homeassistant apps/base/homepage/configmap.yaml
git commit -m "#$ISSUE Pin Home Assistant to the node holding the Zigbee dongle

Adds the nodeSelector on the Node Feature Discovery USB label and mounts
/dev/zigbee, and repoints the Homepage entry from lab1 to the in-cluster
service.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push -u origin $ISSUE-homeassistant-cutover
gh pr create --title "Closes #$ISSUE: Migrate Home Assistant data and cut over to Kubernetes" --body "$(git log -1 --pretty=%B)

Part of the Home Assistant migration, k8s-homelab#61.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
git checkout main && git pull origin main

flux resume kustomization homeassistant
flux reconcile kustomization homeassistant --with-source
kubectl -n homeassistant scale deploy/homeassistant --replicas=1
kubectl -n homeassistant rollout status deploy/homeassistant --timeout=15m
```

- [ ] **Step 11: Verify the cutover**

```bash
kubectl -n homeassistant get pod -o wide -l app=homeassistant
POD=$(kubectl -n homeassistant get pod -l app=homeassistant -o name | head -1)
kubectl -n homeassistant exec $POD -- ls -l /dev/zigbee
kubectl -n homeassistant exec $POD -- ip -br addr
kubectl -n homeassistant logs $POD --tail=50 | grep -iE "zha|zigbee|error" | head -20
curl -sS -o /dev/null -w "%{http_code}\n" https://homeassistant.homelab.cs-ol.de/
```
Expected: the pod runs on the labelled node, `/dev/zigbee` is present inside the container,
`net1` holds `192.168.3.11`, and the ingress returns `200`.

- [ ] **Step 12: Verify in the UI, including the HomeKit advertise address**

Open `https://homeassistant.homelab.cs-ol.de` and check:

1. **Settings → Devices & Services → ZHA** — the Zigbee mesh is online and devices respond.
2. **History / Energy dashboard** — pre-migration data and long-term statistics are intact.
3. **ESPHome devices** — connected.
4. **HomeKit bridge.** ⚠️ The bridge advertises whichever address Home Assistant picks, and
   the pod now has two. If Apple devices cannot see the bridge, set the HomeKit integration's
   **Advertise IP address** to `192.168.3.11` (Settings → Devices & Services → HomeKit
   Bridge → Configure) and restart Home Assistant. This does not affect existing pairings.

- [ ] **Step 13: Write the runbook**

```bash
mkdir -p docs/runbooks
cat > docs/runbooks/homeassistant-cutover.md <<'EOF'
# Home Assistant: rollback and operational notes

Migrated from Docker Compose on `lab1` to Kubernetes on 2026-09-19. See
`docs/superpowers/specs/2026-09-19-homeassistant-k8s-migration-design.md`.

## Rollback

The compose stack and its data on the NAS were only ever read, never mutated,
so rollback stays available until the stack is explicitly decommissioned.

1. `kubectl -n homeassistant scale deploy/homeassistant --replicas=0`
2. Remove the USB passthrough from talos1 and reboot it:
   `tofu apply` after deleting the `usb_devices` entry for VM 900, then
   `talosctl -n 10.98.0.10 -e 10.98.0.10 reboot`
3. Physically move the dongle from `pve2` back to `pve`.
4. `ssh pve 'qm set 100 --usb1 host=1a86:55d4'` and reboot `lab1`.
5. `ssh lab1 'sudo systemctl start docker-compose@homeassistant.service'`

The Longhorn PVC uses `longhorn-retain`, so the migrated data survives even if
the Kustomization is deleted.

## Where things are

| Thing | Location |
|---|---|
| LAN address | `192.168.3.11` (macvlan, `apps/base/homeassistant/networkattachment.yaml`) |
| Web UI | `https://homeassistant.homelab.cs-ol.de` and `http://192.168.3.11:8123` |
| Config | 20 Gi `longhorn-retain` PVC `homeassistant-config` |
| Zigbee dongle | `/dev/zigbee` on whichever node has the passthrough; udev rule in Talos machine config |
| Node pinning | `feature.node.kubernetes.io/usb-02_1a86_55d4.present`, published by NFD |

## Moving the dongle to a different node

Change the `usb_devices` entry in `~/Workspace/homelab`'s `terraform.tfvars`
from VM 900 to the target VMID, apply, and reboot both nodes. NFD moves the
label and the Deployment follows. No manifest change in this repo is needed —
that is the point of labelling by detection.

## Gotchas

- **Never `RollingUpdate`.** SQLite and a serial device tolerate one writer.
- **Never add a `gateway` to the macvlan config or to `ens19`.** A second
  default route competes with the VLAN-99 route.
- **The `224.0.0.0/4` route in the NAD is what makes discovery work.** If
  ESPHome/WLED/Shelly stop being discovered, check it first.
- **HomeKit advertises one address.** If Apple devices lose the bridge, set the
  HomeKit integration's Advertise IP to `192.168.3.11`.
EOF

git add docs/runbooks/homeassistant-cutover.md
git commit -m "#$ISSUE Add the Home Assistant rollback and operations runbook

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push
```

- [ ] **Step 14: Open the follow-up issues**

```bash
cd ~/Workspace/k8s-homelab
gh issue create --title "Feature: Mount the QNAP share at /config/backups for Home Assistant" \
  --body "Deferred from #61. Blocked on the QNAP at 192.168.1.240 registering nfs/mountd with rpcbind again - it currently registers neither, and lab1's working mount is a pre-existing NFSv3 one on the fixed mountport=30000. A PVC that cannot mount blocks pod start, which is why this was kept off the migration's critical path."
gh issue create --title "Feature: Pass the Realtek Bluetooth radio on pve2 through to a Talos node" \
  --body "Deferred from #61. pve2 has a Realtek Bluetooth radio (0bda:b85b) physically attached and passed to no VM. Home Assistant has bluetooth and ibeacon config entries that have never worked, because lab1 has no Bluetooth adapter. Passing this through would make them functional. Needs the D-Bus/BlueZ story worked out on Talos first."
gh issue create --title "Chore: Decommission the Home Assistant Docker Compose stack on lab1" \
  --body "Deferred from #61. Only after the Kubernetes deployment has proven itself over time - the stopped compose stack and its untouched data on the NAS are the rollback path. Closing this means giving that up."
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §4.1 second NIC, memory | Task 1 |
| §4.1 Talos config, udev rule | Task 2 |
| §4.1 USB passthrough | Tasks 1 (capability), 7 (the move) |
| §4.2 node labelling | Task 5 |
| §4.3 Multus + macvlan NAD | Tasks 3 (prerequisite plugins), 4, 6 |
| §4.4 application manifests | Tasks 6, 8 |
| §4.4 `trusted_proxies` | Task 8 Step 6 |
| §4.4 dropping `/var/run/dbus` | Never added; confirmed by the user |
| §5 Phase A | Task 6 Steps 9–11 |
| §5 Phase B | Tasks 7, 8 |
| §5 rollback | Task 8 Step 13 |
| §6 out of scope | Task 8 Step 14 |
| §7 risks | Mitigations carried into the verification steps of Tasks 1, 2, 4, 6 |

Two gaps in the spec were found and closed by this plan: the missing `macvlan`/`static` CNI
plugins (Task 3, which the spec does not mention at all) and the wrong NFD label class
(Task 5, which also amends the spec).

One item deserves calling out as **not** covered by any task: the spec's §2.4 observation
that the recorder database is 9.3 GB. It migrates as-is, and retention tuning is listed as
out of scope in §6. That is deliberate, not an omission.

**Type consistency:** the label `feature.node.kubernetes.io/usb-02_1a86_55d4.present` is
identical in Tasks 5, 7 and 8. The PVC name `homeassistant-config` is identical in Tasks 6
and 8. The NAD name `homeassistant/lan-macvlan` matches between the NAD metadata and the
pod annotation. The volume name `config` and mount path `/config` match between the base
Deployment and the production patch. MACs match between Task 1's tfvars and Task 2's
`deviceSelector`. The interface name `ens19` appears in Task 2's documentation and Task 6's
macvlan `master`, with Task 1 Step 8 instructing that a different observed name be carried
into Task 6.
