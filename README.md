# k8s-homelab

A GitOps-managed Kubernetes homelab using Flux CD for automated deployment and reconciliation.

## Overview

This repository contains the complete Infrastructure as Code (IaC) for a production-grade Kubernetes homelab. It follows GitOps principles with Flux CD continuously monitoring this repository and automatically applying changes to the cluster.

### Key Features

- **GitOps with Flux CD**: Automated reconciliation from Git
- **Layered Architecture**: Base configurations with environment-specific overlays
- **High Availability Storage**: Longhorn distributed block storage
- **Automatic TLS**: Let's Encrypt certificates via cert-manager with DNS-01 challenge
- **Ingress**: Traefik with MetalLB load balancing
- **Monitoring**: Prometheus/Grafana stack with custom dashboards
- **Services**: Homepage dashboard, Pi-hole DNS, Headlamp Kubernetes dashboard

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   GitOps Flow                       │
│  GitHub Repo → Flux CD → Kubernetes Cluster        │
└─────────────────────────────────────────────────────┘

Infrastructure Layer (deployed first)
├── MetalLB: Load balancer (10.98.0.200-10.98.0.254)
└── Longhorn: Distributed storage (default StorageClass)

Application Layer (depends on infrastructure)
├── Traefik: Ingress controller with TLS
├── cert-manager: Certificate automation (DNS-01)
├── Monitoring: Prometheus + Grafana
├── Homepage: Service dashboard
├── Pi-hole: Network-wide ad blocking
└── Headlamp: Kubernetes dashboard
```

### Directory Structure

- **`apps/base/`**: Base Kubernetes manifests and HelmReleases
- **`apps/production/`**: Production overlays and patches (Kustomize)
- **`apps/staging/`**: Staging overlays for testing
- **`clusters/production/`**: Flux Kustomizations defining deployment order
- **`infrastructure/`**: Core infrastructure (MetalLB, Longhorn)

## Prerequisites

- Kubernetes cluster (v1.27+)
- `kubectl` configured with cluster access
- `flux` CLI installed ([installation guide](https://fluxcd.io/flux/installation/))
- GitHub personal access token for Flux

## Quick Start

### 1. Bootstrap Flux CD

```bash
# Export your GitHub token
export GITHUB_TOKEN=<your-token>

# Bootstrap Flux to your cluster
flux bootstrap github \
  --owner=umueller74 \
  --repository=k8s-homelab \
  --branch=main \
  --path=clusters/production \
  --personal
```

### 2. Verify Deployment

```bash
# Check Flux system
flux get all

# Watch Kustomizations deploy
flux get kustomizations -w

# Check HelmReleases
flux get helmreleases --all-namespaces
```

### 3. Access Services

Once deployed, services are available at:

- **Homepage**: https://homepage.homelab.cs-ol.de
- **Traefik Dashboard**: https://traefik.homelab.cs-ol.de
- **Grafana**: https://grafana.homelab.cs-ol.de
- **Headlamp**: https://headlamp.homelab.cs-ol.de
- **Pi-hole**: https://pihole.homelab.cs-ol.de/admin

## Making Changes

All changes follow a strict GitOps workflow documented in [`CLAUDE.md`](./CLAUDE.md).

**Important**: Never commit directly to `main`. All changes must go through pull requests.

### Workflow Summary

1. Create a GitHub issue for the feature/fix
2. Create a feature branch: `git checkout -b <issue-number>-<description>`
3. Make changes to manifests
4. Commit with issue reference: `git commit -m "#<issue> description"`
5. Push and create pull request
6. Merge PR → Flux automatically deploys changes

See [`CLAUDE.md`](./CLAUDE.md) for detailed development guidelines.

## Common Operations

### Force Reconciliation

```bash
# Reconcile all Flux resources
flux reconcile kustomization flux-system --with-source

# Reconcile specific app
flux reconcile kustomization apps --with-source
```

### Check Application Status

```bash
# View all Kustomizations
flux get kustomizations

# Check specific namespace
kubectl -n <namespace> get all

# View recent events
kubectl -n <namespace> get events --sort-by='.lastTimestamp'
```

### Certificate Status

```bash
# Check certificates
kubectl -n traefik get certificates

# View cert-manager logs
kubectl -n cert-manager logs deployment/cert-manager -f
```

## Components

### Infrastructure

- **MetalLB** (v0.14.x): Bare-metal load balancer providing external IPs
- **Longhorn** (v1.7.x): Distributed block storage with 3-replica redundancy

### Applications

- **Traefik** (v33.x): Ingress controller with automatic HTTPS
- **cert-manager** (v1.16.x): TLS certificate automation via Let's Encrypt
- **Prometheus Stack** (v87.x): Metrics collection and visualization
- **Homepage** (v0.9.x): Unified dashboard with Kubernetes/Docker integration
- **Pi-hole** (2026.07.2): Network-level ad blocking and DNS
- **Headlamp** (v0.28.x): User-friendly Kubernetes dashboard

## Monitoring

Access Grafana at https://grafana.homelab.cs-ol.de

Pre-configured dashboards:
- Cluster resource utilization
- Node metrics via node-exporter
- Kubernetes object metrics via kube-state-metrics
- Custom etcd metrics from Talos control plane

## Troubleshooting

See [`CLAUDE.md`](./CLAUDE.md) for detailed troubleshooting guides.

### Quick Diagnostics

```bash
# Check Flux health
flux check

# View Flux logs
flux logs --all-namespaces --follow

# Inspect failed HelmRelease
kubectl -n <namespace> describe helmrelease <name>
```

## Documentation

- **[CLAUDE.md](./CLAUDE.md)**: Comprehensive development guide, architecture details, and troubleshooting
- **[Flux CD Docs](https://fluxcd.io/)**: Official Flux documentation
- **[Kustomize](https://kustomize.io/)**: Kubernetes configuration management

## Contributing

1. Issues are tracked in [GitHub Issues](https://github.com/umueller74/k8s-homelab/issues)
2. All changes require a linked issue and pull request
3. Follow the development workflow in `CLAUDE.md`
4. Test changes in staging environment when possible

## License

This is a personal homelab configuration. Use at your own risk.

## Author

**Udo Mueller** - [umueller74](https://github.com/umueller74)
