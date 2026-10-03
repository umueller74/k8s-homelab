#!/usr/bin/env python3
"""
Sync UniFi client names into Pi-hole client descriptions (the `comment` field).

For every client IP, the Pi-hole "Clients" entry gets the UniFi name as its description.
Name = UniFi alias (`name`), falling back to the DHCP `hostname`, the local DNS record's
first label, then "<vendor> <MAC tail>". Adopted UniFi devices (APs, switches) are included.

By default only IPs Pi-hole has actually seen (network table + existing clients) are
touched, so the Clients list does not fill up with every offline device UniFi remembers.
Existing non-empty descriptions are kept unless --force is given.

Configuration (environment variables, or the unifi skill's .env):
  PIHOLE_URL       Pi-hole base URL (default: https://pihole.homelab.cs-ol.de)
  PIHOLE_PASSWORD  Pi-hole web password; if unset, read from the k8s secret
                   `pihole-secret` in namespace `pihole` via kubectl
  UDM_HOST / UNIFI_API_KEY  as for udm.py

Usage: python pihole_sync.py [--dry-run] [--force] [--all-unifi] [--json]
"""

import argparse
import base64
import ipaddress
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any

import udm

DEFAULT_URL = "https://pihole.homelab.cs-ol.de"


def get_pihole_password() -> str:
    env_pw = os.environ.get("PIHOLE_PASSWORD")
    if env_pw:
        return env_pw
    result = subprocess.run(
        ["kubectl", "get", "secret", "-n", "pihole", "pihole-secret",
         "-o", "jsonpath={.data.password}"],
        capture_output=True, text=True, check=True,
    )
    return base64.b64decode(result.stdout).decode()


class PiholeClient:
    def __init__(self, base: str, password: str):
        self.base = base.rstrip("/")
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE
        self.sid = self._request(
            "POST", "auth", {"password": password}, auth=False
        )["session"]["sid"]

    def _request(self, method: str, path: str, body: dict | None = None,
                 auth: bool = True) -> Any:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if auth:
            headers["X-FTL-SID"] = self.sid
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base}/api/{path}", data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=self.ctx) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            print(f"Pi-hole HTTP {e.code} on {method} {path}: {e.read().decode()}",
                  file=sys.stderr)
            sys.exit(1)

    def clients(self) -> dict[str, dict]:
        return {c["client"]: c for c in self._request("GET", "clients")["clients"]}

    def seen_ips(self) -> set[str]:
        devices = self._request("GET", "network/devices")["devices"]
        return {i["ip"] for d in devices for i in d.get("ips", [])}

    def create(self, ip: str, comment: str) -> None:
        self._request("POST", "clients", {"client": ip, "comment": comment, "groups": [0]})

    def update(self, ip: str, comment: str, groups: list[int]) -> None:
        self._request("PUT", f"clients/{ip}", {"comment": comment, "groups": groups})

    def logout(self) -> None:
        try:
            self._request("DELETE", "auth")
        except SystemExit:
            pass


def client_name(c: dict) -> str:
    """Best available label for a UniFi client: alias, DHCP hostname, local DNS record
    (first label), then "<vendor> <last 2 MAC bytes>". Empty if nothing identifies it."""
    for key in ("name", "hostname"):
        if (c.get(key) or "").strip():
            return c[key].strip()
    dns = (c.get("local_dns_record") or "").strip()
    if dns:
        return dns.split(".")[0]
    oui = (c.get("oui") or "").strip()
    if oui and c.get("mac"):
        return f"{oui} {c['mac'][-5:]}"
    return ""


def unifi_names() -> dict[str, str]:
    """Map IP -> name for every UniFi client and adopted device that has an IP and a name."""
    client = udm.UDMClient(udm.DEFAULT_HOST, udm.get_api_key())
    names: dict[str, str] = {}
    # alluser is ordered arbitrarily; prefer clients seen most recently for a shared IP
    for c in sorted(client.clients_all(), key=lambda c: c.get("last_seen", 0)):
        ip = c.get("ip") or c.get("fixed_ip")
        name = client_name(c)
        if ip and name:
            names[ip] = name
    # UniFi's own gear (APs, switches) is not in the client list; skip the UDM's public WAN IP
    for d in client.devices():
        ip, name = d.get("ip"), (d.get("name") or "").strip()
        if ip and name and ipaddress.ip_address(ip).is_private:
            names.setdefault(ip, name)
    return names


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Show changes, apply nothing")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing non-empty descriptions")
    parser.add_argument("--all-unifi", action="store_true",
                        help="Also create entries for UniFi clients Pi-hole has not seen")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    names = unifi_names()
    pihole = PiholeClient(os.environ.get("PIHOLE_URL", DEFAULT_URL), get_pihole_password())
    try:
        existing = pihole.clients()
        ips = set(names) if args.all_unifi else (pihole.seen_ips() | set(existing)) & set(names)

        result: dict[str, list] = {"created": [], "updated": [], "unchanged": [], "kept": []}
        for ip in sorted(ips):
            name = names[ip]
            current = existing.get(ip)
            if current is None:
                action = "created"
            elif current.get("comment") == name:
                result["unchanged"].append({"ip": ip, "name": name})
                continue
            elif current.get("comment") and not args.force:
                result["kept"].append({"ip": ip, "name": name, "comment": current["comment"]})
                continue
            else:
                action = "updated"

            result[action].append({"ip": ip, "name": name})
            if args.dry_run:
                continue
            if action == "created":
                pihole.create(ip, name)
            else:
                pihole.update(ip, name, current.get("groups", [0]))
    finally:
        pihole.logout()

    if args.json:
        print(json.dumps(result))
        return
    prefix = "[dry-run] would have " if args.dry_run else ""
    for action in ("created", "updated"):
        for r in result[action]:
            print(f"{prefix}{action}: {r['ip']:<16} {r['name']}")
    for r in result["kept"]:
        print(f"kept:    {r['ip']:<16} has description {r['comment']!r} (UniFi: {r['name']!r}, use --force)")
    print(f"\n{len(result['created'])} created, {len(result['updated'])} updated, "
          f"{len(result['unchanged'])} unchanged, {len(result['kept'])} kept")


if __name__ == "__main__":
    main()
