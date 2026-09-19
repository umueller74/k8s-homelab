# Home Assistant: rollback and operational notes

Migrated from Docker Compose on `lab1` to Kubernetes on 2026-09-19. See
`docs/superpowers/specs/2026-09-19-homeassistant-k8s-migration-design.md`.

## Rollback

The compose stack and its data on the NAS were only ever read, never mutated,
so rollback stays available until the stack is explicitly decommissioned.

1. `kubectl -n homeassistant scale deploy/homeassistant --replicas=0`
   (`flux suspend kustomization homeassistant` first, or Flux scales it back.)
2. Remove the USB passthrough from talos1 and power-cycle it:
   ```sh
   ssh pve2 'qm set 900 --delete usb0'
   talosctl -n 10.98.0.10 -e 10.98.0.10 shutdown && ssh pve2 'qm start 900'
   ```
   **Not `terraform apply`** — see "Terraform cannot manage the passthrough".
   **Not `talosctl reboot`** — a guest reboot cannot detach a device from a
   QEMU process that already has it. The full stop/start is required.
3. Physically move the dongle from `pve2` back to `pve`.
4. `ssh pve 'qm set 100 --usb1 host=1a86:55d4'` and reboot `lab1`.
   ⚠️ After any lab1 reboot, check `/docker` — see "lab1's /docker mount".
5. `ssh lab1 'sudo systemctl start docker-compose@homeassistant.service'`

The Longhorn PVC uses `longhorn-retain`, so the migrated data survives even if
the Kustomization is deleted.

Per-file rollbacks taken during the migration, all inside the PVC:
`configuration.yaml.pre-k8s` and `.storage/core.config_entries.pre-k8s`.

## Where things are

| Thing | Location |
|---|---|
| LAN address | `192.168.3.11` (macvlan, `apps/base/homeassistant/networkattachment.yaml`) |
| Web UI | `https://homeassistant.homelab.cs-ol.de` and `http://192.168.3.11:8123` |
| Config | 20 Gi `longhorn-retain` PVC `homeassistant-config` |
| Zigbee dongle | `/dev/zigbee` on whichever node has the passthrough; udev rule in Talos machine config |
| Node pinning | `feature.node.kubernetes.io/usb-02_1a86_55d4.present`, published by NFD |

## Moving the dongle to a different node

Physically move it, then on the Proxmox host:

```sh
ssh pve2 'qm set <old-vmid> --delete usb0'
ssh pve2 'qm set <new-vmid> --usb0 host=1a86:55d4'
```

Power-cycle both VMs (`talosctl shutdown` + `qm start`, not `reboot`). NFD
moves the label and the Deployment follows it. **No manifest change in this
repo is needed — that is the point of labelling by detection.** Update
`usb_devices` in `~/Workspace/homelab`'s `terraform.tfvars` to match, so the
declaration keeps reflecting reality.

## Terraform cannot manage the passthrough

`terraform apply` fails on the USB block:

```
HTTP 500 - only root can set 'usb0' config for real devices
```

Proxmox refuses real-device passthrough over API-token auth; it requires
genuine `root@pam` ticket auth, and `roles/proxmox/terraform/providers.tf` is
token-only. The `usb_devices` entry in `terraform.tfvars` is therefore
**documentation that happens to match reality** — `terraform plan` reads it
back clean and reports no drift, but it cannot create or change it. Apply the
change with `qm set` and keep the tfvars in step by hand.

To make this properly managed, give the provider `username`/`password` instead
of `api_token`.

## lab1's /docker mount (rollback dependency)

`/etc/fstab` on lab1 mounts the QNAP export at `/docker`, but that export lands
**one directory above** the compose root — the stacks live in a `docker/`
subdirectory inside it, and the units use `WorkingDirectory=/docker/%i`. The
QNAP returns the same directory for every sub-path requested, so this cannot be
fixed by correcting the fstab path alone. A bind mount is required:

```sh
sudo mount --bind /docker/docker /docker
```

**This is not currently persistent.** After any lab1 reboot, all 20 compose
units fail until it is re-applied. This predates the migration, but rollback
depends on lab1's compose stack working, so it is listed here.

## Gotchas

- **Never `RollingUpdate`.** SQLite and a serial device tolerate one writer.
- **Never add a `gateway` to the macvlan config or to `ens19`.** A second
  default route competes with the VLAN-99 route and silently wins.
- **The `224.0.0.0/4` route in the NAD is what makes discovery work.** If
  ESPHome/WLED/Shelly stop being discovered, check it first.
- **The pod cannot reach its own node over the LAN.** macvlan children are
  isolated from their parent's host stack. Verify LAN reachability from another
  host (lab1), never from the node running the pod.
- **HomeKit advertises one address.** If Apple devices lose the bridge, set the
  HomeKit integration's Advertise IP to `192.168.3.11`.
- **`trusted_proxies` must contain `10.244.0.0/16`.** Without it Home Assistant
  returns a bare `400: Bad Request` through the Traefik ingress while working
  fine on `http://192.168.3.11:8123`.
