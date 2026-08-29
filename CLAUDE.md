# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

This is a GitOps-managed Kubernetes homelab using Flux CD. The repository follows a layered architecture with base configurations, environment overlays (production/staging), and Flux Kustomizations that orchestrate deployment order and dependencies.

## Architecture

### Directory Structure

```
├── apps/
│   ├── base/           # Base Kubernetes manifests and HelmReleases
│   ├── production/     # Production overlays and patches
│   └── staging/        # Staging overlays and patches
├── infrastructure/     # Core infrastructure components (MetalLB, storage)
└── clusters/
    ├── production/     # Production-specific Flux Kustomizations and configs
    └── staging/        # Staging-specific Flux Kustomizations
```

### GitOps Model

- **Flux CD** continuously reconciles this Git repository with the cluster
- **Flux Kustomizations** (in `clusters/`) define dependencies and deployment order:
  - `infrastructure` deploys first (MetalLB, Longhorn storage)
  - `apps` depends on infrastructure
  - `certificates` depends on infrastructure (cert-manager must be ready)
  - Individual apps use Flux Kustomizations (e.g., `headlamp.yaml`, `monitoring.yaml`)

### Application Pattern

Applications follow a three-layer pattern:

1. **Base** (`apps/base/<app>/`): Core manifests (namespaces, HelmReleases, deployments)
2. **Environment overlay** (`apps/{production,staging}/<app>/`): Environment-specific patches via Kustomize
3. **Flux Kustomization** (`clusters/{env}/<app>.yaml`): Flux metadata (interval, dependencies, source)

### Key Components

- **Traefik**: Ingress controller with LoadBalancer service (MetalLB), automatic Let's Encrypt via ACME
- **cert-manager**: TLS certificate management with DNS-01 solver (cPanel webhook for wildcard certs)
- **Longhorn**: Distributed block storage (default StorageClass, 3 replicas)
- **MetalLB**: Bare-metal load balancer providing external IPs
- **Prometheus stack**: Monitoring (kube-prometheus-stack via Helm)
- **Homepage**: Dashboard aggregating services with Kubernetes/Docker/Proxmox integrations
- **Pi-hole**: DNS ad-blocker
- **Headlamp**: Kubernetes dashboard

## Common Commands

### Flux CD Operations

```bash
# Check overall Flux system health
flux get all

# Check specific Kustomizations
flux get kustomizations

# Check HelmReleases
flux get helmreleases --all-namespaces

# Force reconciliation of a specific resource
flux reconcile kustomization <name> --with-source

# Suspend/resume a Kustomization
flux suspend kustomization <name>
flux resume kustomization <name>

# View logs from Flux controllers
flux logs --all-namespaces --follow
```

### Kubectl Operations

```bash
# View resources in flux-system namespace
kubectl -n flux-system get kustomizations

# Check status of an application
kubectl -n <namespace> get all

# View events for troubleshooting
kubectl -n <namespace> get events --sort-by='.lastTimestamp'

# Check certificate status
kubectl -n traefik get certificates

# View Longhorn volumes
kubectl -n longhorn-system get volumes
```

### Testing Changes

```bash
# Validate Kubernetes manifests (requires kubeconform if installed)
find . -name "*.yaml" -exec kubeconform {} \;

# Dry-run Kustomize build
kubectl kustomize apps/production

# Validate a specific HelmRelease
flux diff helmrelease <name> -n <namespace>
```

## Development Workflow

### Issue-Driven Development

**IMPORTANT**: All features and changes must be tracked via GitHub issues. Never commit directly to the `main` branch.

#### Adding a New Feature

1. **Create a GitHub issue first**: `gh issue create --title "Feature: <description>" --body "<details>"`
   - Before creating the issue, gather comprehensive requirements by asking:
     - What is the purpose and expected outcome?
     - What are the specific configuration requirements?
     - Are there dependencies on other services or infrastructure?
     - What are the resource requirements (CPU, memory, storage)?
     - What monitoring or observability is needed?
     - Are there security considerations (secrets, certificates, network policies)?
     - What is the rollback plan if something goes wrong?
2. **Reference the issue** when creating branches: `git checkout -b <issue-number>-<feature-name>`

#### Making Changes

1. **Ensure a GitHub issue exists** for the work (create one if needed)
2. **Create a feature branch**: `git checkout -b <issue-number>-<short-description>`
   - Example: `git checkout -b 42-add-grafana-dashboard`
3. **Modify manifests** in `apps/base/<app>/` or environment overlays
4. **Commit changes**: `git add <files> && git commit -m "#<issue-number> descriptive message"`
   - Reference the issue number in commit messages
5. **Push branch**: `git push -u origin <issue-number>-<short-description>`
6. **Create pull request**: `gh pr create --title "Closes #<issue-number>: <description>"`
   - Link the PR to the issue using "Closes #<number>" in the PR description
7. **Review and merge**: Once approved, merge the PR to trigger Flux reconciliation
8. **Monitor deployment**: `flux get kustomizations -w` or check specific app namespace
9. **Verify and close**: Confirm deployment success, then close the issue if not auto-closed

### Branch Protection

The `main` branch is the source of truth for Flux CD. All changes must go through pull requests to ensure:
- Configuration is reviewed before deployment
- Changes are tracked and reversible via GitHub issues
- Deployment issues can be traced to specific PRs and issues
- Full audit trail of what was changed and why

### Adding a New Application

1. Create base manifests in `apps/base/<app>/`
   - Include: `kustomization.yaml`, `namespace.yaml`, and app resources
   - For Helm charts: add `helmrelease.yaml` and `helmrepository.yaml`
2. Create environment overlay in `apps/production/<app>/` with `kustomization.yaml` referencing base
3. Add to `apps/production/kustomization.yaml` resources list
4. Optionally create a Flux Kustomization in `clusters/production/<app>.yaml` for:
   - Custom reconciliation intervals
   - Dependencies on other Kustomizations
   - Health checks

### Working with HelmReleases

- HelmReleases use Flux's `helm.toolkit.fluxcd.io/v2` API
- Chart versions use semantic versioning with wildcards (e.g., `41.x`, `1.12.x`)
- Values are embedded in the HelmRelease spec under `values:`
- Flux automatically creates HelmRepository resources pointing to chart repositories

### Certificate Management

- Wildcard certificate: `*.homelab.cs-ol.de` managed by cert-manager
- Uses DNS-01 challenge via cPanel webhook solver
- ClusterIssuer `letsencrypt-production` configured in `apps/production/cert-manager/cluster-issuer.yaml`
- Domain: `homelab.cs-ol.de`

### Storage

- **Longhorn** is the default StorageClass
- Configured for 3 replicas per volume
- PVCs automatically provisioned from Longhorn
- Each pod requiring persistent storage needs its own PVC (Pi-hole uses one PVC per replica)

## Troubleshooting

### Common Issues

**Flux reconciliation stuck:**
```bash
flux reconcile kustomization <name> --with-source
kubectl -n flux-system logs deployment/kustomize-controller -f
```

**HelmRelease fails:**
```bash
kubectl -n <namespace> describe helmrelease <name>
flux logs --kind=HelmRelease --namespace=<namespace> --name=<name>
```

**Certificate not issuing:**
```bash
kubectl -n traefik describe certificate wildcard-homelab
kubectl -n cert-manager logs deployment/cert-manager -f
```

**Pod stuck on pending (PVC issues):**
```bash
kubectl describe pvc <pvc-name> -n <namespace>
kubectl -n longhorn-system get volumes
```

## Important Patterns

### Kustomize Overlays

Production apps reference base configurations and apply patches:
```yaml
# apps/production/<app>/kustomization.yaml
resources:
  - ../../base/<app>
patches:
  - path: deployment-patch.yaml
  - path: service-patch.yaml
```

### Flux Kustomization Dependencies

Use `dependsOn` to enforce ordering:
```yaml
spec:
  dependsOn:
    - name: infrastructure
  sourceRef:
    kind: GitRepository
    name: flux-system
  path: ./apps/production
```

### Environment Contexts

- **Production cluster**: `admin@homelab-1-1` (current context)
- Staging uses separate Flux Kustomizations pointing to `apps/staging` paths

## Secrets Management

Secrets are currently stored as plain Kubernetes Secret manifests in the repository. Consider migrating to sealed-secrets or SOPS for sensitive data encryption at rest.
