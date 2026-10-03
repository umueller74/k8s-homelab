#!/usr/bin/env bash
# Move the Secrets that roles/talos/tasks/create_k8s_secrets.yaml (umueller74/homelab) creates
# with Ansible into this repository as SOPS-encrypted manifests (issue #165).
#
# The live Secret is the source of truth, so Flux adopts the existing objects without changing
# a byte of their data. No value is ever printed: the script only reports names and
# OK / DIFFERS / MISSING.
#
#   scripts/migrate-ansible-secrets.sh            # write the missing <name>.sops.yaml files
#   scripts/migrate-ansible-secrets.sh --force    # also rewrite files that already exist
#   scripts/migrate-ansible-secrets.sh --verify   # compare each file with the live Secret
#
# Needs kubectl (KUBECONFIG, default ~/.kube/config), sops, jq and python3 with PyYAML, and the
# age private key where sops looks for it (~/.config/sops/age/keys.txt).
set -euo pipefail
cd "$(dirname "$0")/.."
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

# namespace  secret  directory it belongs in (next to its consumer)
SECRETS=(
  "pihole        pihole-secret                  apps/production/pihole"
  "pihole        pihole-backup-nas              apps/production/pihole"
  "cert-manager  cpanel-api                     apps/production/cert-manager"
  "monitoring    grafana-admin                  apps/production/monitoring"
  "gitea         gitea-admin-credentials        apps/production/gitea"
  "gitea         gitea-postgresql-credentials   apps/production/gitea"
  "gitea         gitea-user-credentials         apps/production/gitea"
  "gitea         cs-ol-website-deploy           apps/production/gitea"
)

mode=write
case "${1:-}" in
  "") ;;
  --force) mode=force ;;
  --verify) mode=verify ;;
  *) echo "usage: $0 [--force|--verify]" >&2; exit 2 ;;
esac

status=0
for entry in "${SECRETS[@]}"; do
  read -r ns name dir <<<"$entry"
  file="$dir/$name.sops.yaml"

  if ! live=$(kubectl -n "$ns" get secret "$name" -o json 2>/dev/null); then
    echo "MISSING  $ns/$name (not in the cluster)"; status=1; continue
  fi

  if [[ $mode == verify ]]; then
    if [[ ! -f $file ]]; then echo "MISSING  $file"; status=1; continue; fi
    want=$(jq -S '.data' <<<"$live")
    have=$(sops --decrypt --output-type json "$file" | jq -S '.data')
    if [[ $want == "$have" ]]; then echo "OK       $file"; else echo "DIFFERS  $file"; status=1; fi
    continue
  fi

  if [[ -f $file && $mode != force ]]; then echo "SKIP     $file (exists)"; continue; fi

  # Encrypt in a temp file with a name that matches the creation rule in .sops.yaml, and never
  # leave plaintext behind: the trap removes it if anything fails.
  tmp="$dir/.$name.new.sops.yaml"
  trap 'rm -f "$tmp"' EXIT
  {
    echo "# Created by Ansible (roles/talos create_k8s_secrets) and now managed here with SOPS (#165)."
    jq '{apiVersion: "v1", kind: "Secret",
         metadata: {name: .metadata.name, namespace: .metadata.namespace},
         type: (.type // "Opaque"), data: .data}' <<<"$live" |
      python3 -c 'import json, sys, yaml; yaml.safe_dump(json.load(sys.stdin), sys.stdout, sort_keys=False)'
  } >"$tmp"
  sops --encrypt --in-place "$tmp"
  grep -q 'ENC\[AES256_GCM' "$tmp" || { echo "FAILED   $file (not encrypted)"; rm -f "$tmp"; status=1; continue; }
  mv "$tmp" "$file"
  trap - EXIT
  echo "WROTE    $file"
done
exit $status
