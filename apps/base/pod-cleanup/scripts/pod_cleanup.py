#!/usr/bin/env python3
"""
Delete pods left behind by graceful node shutdown.

When a Talos node shuts down, the kubelet marks its pods Succeeded or Failed and their
controllers start replacements elsewhere. The old pod objects stay, because
kube-controller-manager only garbage-collects terminated pods above
--terminated-pod-gc-threshold (default 12500).

A pod is deleted when it is in phase Succeeded or Failed, is owned by a ReplicaSet,
StatefulSet or DaemonSet, and finished more than MIN_AGE_HOURS ago. Job pods are kept
(the CronJob history limits bound them, and their logs are the job history), and so
are bare pods (someone may still be reading them).

Configuration (environment variables):
  MIN_AGE_HOURS  hours since the pod finished before it is deleted (default: 1)
  DRY_RUN        "true" lists the pods without deleting them (default: false)

Runs in-cluster with the pod's service account. For a local dry run against
`kubectl get pods -A -o json` output: python pod_cleanup.py --from-file pods.json
"""

import argparse
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

CONTROLLER_KINDS = {'ReplicaSet', 'StatefulSet', 'DaemonSet'}
SA_DIR = '/var/run/secrets/kubernetes.io/serviceaccount'


def parse_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def finished_at(pod):
    """Latest container finish time; falls back to the Ready condition, then creation."""
    times = [
        status['state']['terminated']['finishedAt']
        for status in pod.get('status', {}).get('containerStatuses') or []
        if (status.get('state') or {}).get('terminated', {}).get('finishedAt')
    ]
    if times:
        return max(parse_time(t) for t in times)
    for condition in pod.get('status', {}).get('conditions') or []:
        if condition.get('type') == 'Ready' and condition.get('lastTransitionTime'):
            return parse_time(condition['lastTransitionTime'])
    return parse_time(pod['metadata']['creationTimestamp'])


def select(pods, now, min_age):
    """Pods to delete: terminated, controller-owned, finished before now - min_age."""
    selected = []
    for pod in pods:
        if pod.get('status', {}).get('phase') not in ('Succeeded', 'Failed'):
            continue
        owners = {ref.get('kind') for ref in pod['metadata'].get('ownerReferences') or []}
        if not owners & CONTROLLER_KINDS:
            continue
        if now - finished_at(pod) < min_age:
            continue
        selected.append(pod)
    return selected


class Api:
    def __init__(self):
        host = os.environ['KUBERNETES_SERVICE_HOST']
        port = os.environ['KUBERNETES_SERVICE_PORT']
        self.base = f'https://{host}:{port}'
        with open(f'{SA_DIR}/token') as f:
            self.token = f.read().strip()
        self.context = ssl.create_default_context(cafile=f'{SA_DIR}/ca.crt')

    def request(self, method, path):
        req = urllib.request.Request(
            self.base + path, method=method, headers={'Authorization': f'Bearer {self.token}'}
        )
        with urllib.request.urlopen(req, context=self.context, timeout=30) as resp:
            return json.load(resp)

    def terminated_pods(self):
        pods = []
        for phase in ('Succeeded', 'Failed'):
            query = urllib.parse.urlencode({'fieldSelector': f'status.phase={phase}'})
            pods += self.request('GET', f'/api/v1/pods?{query}')['items']
        return pods

    def delete(self, pod):
        meta = pod['metadata']
        self.request('DELETE', f"/api/v1/namespaces/{meta['namespace']}/pods/{meta['name']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--from-file', help='read pods from kubectl JSON instead of the API (implies dry run)')
    args = parser.parse_args()

    min_age = timedelta(hours=float(os.environ.get('MIN_AGE_HOURS', '1')))
    dry_run = os.environ.get('DRY_RUN', 'false').lower() == 'true' or bool(args.from_file)

    if args.from_file:
        with open(args.from_file) as f:
            pods, api = json.load(f)['items'], None
    else:
        api = Api()
        pods = api.terminated_pods()

    selected = select(pods, datetime.now(timezone.utc), min_age)
    failures = 0
    for pod in selected:
        meta = pod['metadata']
        line = f"{meta['namespace']}/{meta['name']} ({pod['status']['phase']}, finished {finished_at(pod):%Y-%m-%d %H:%M}Z)"
        if dry_run:
            print(f'would delete {line}')
            continue
        try:
            api.delete(pod)
            print(f'deleted {line}')
        except Exception as exc:  # keep going; one stuck pod must not block the rest
            failures += 1
            print(f'FAILED {line}: {exc}', file=sys.stderr)

    print(f'{len(selected)} of {len(pods)} pods examined selected, {failures} failed')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
