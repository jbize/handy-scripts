#!/usr/bin/env python3
"""
Road-warrior path verifier for OpenVPN + kill switch.

Stdlib only. Infers STATE (Disarmed / Armed+down / Armed+up) and reports
PROTECTION (SAFE / NOT SAFE / UNPROTECTED). Default action: check-current.

Console:
  python verify_roadwarrior.pyw                  # check-current
  python verify_roadwarrior.pyw check-current
  python verify_roadwarrior.pyw baseline
  python verify_roadwarrior.pyw expect-disarmed
  python verify_roadwarrior.pyw expect-armed-down
  python verify_roadwarrior.pyw expect-armed-up

Double-click (.pyw / pythonw): menu shows live STATE; busy window while
collecting; report shows STATE + PROTECTION.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TIMEOUT_SEC = 4.0
DNS_PROBE_NAME = "example.com"
UPLINK_DNS_PROBE = "8.8.8.8"
# Well-known public resolvers: OK via full tunnel when Armed+up; not "guest leak" signals.
WELL_KNOWN_PUBLIC_DNS = frozenset(
    {
        "8.8.8.8",
        "8.8.4.4",
        "1.1.1.1",
        "1.0.0.1",
        "9.9.9.9",
        "149.112.112.112",
        "208.67.222.222",
        "208.67.220.220",
        "64.6.64.6",
        "64.6.65.6",
    }
)
# Hide console flashes when spawning powershell/nslookup from pythonw.
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _subprocess_kwargs() -> Dict[str, Any]:
    if os.name != "nt":
        return {}
    return {"creationflags": _CREATE_NO_WINDOW}


def _state_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "HandyScripts"
    return Path.home() / ".handy-scripts"


BASELINE_PATH = _state_dir() / "roadwarrior_baseline.json"
KS_STATE_PATH = _state_dir() / "openvpn_killswitch_state.json"


@dataclass
class Check:
    name: str
    expected: str
    observed: str
    status: str  # pass | fail | warn | info
    detail: str = ""


@dataclass
class Posture:
    """Inferred kill-switch / tunnel situation (coffee-shop STATE)."""

    key: str  # disarmed | armed-down | armed-up | uncertain
    label: str
    detail: str
    armed: bool
    tunnel_up: bool


@dataclass
class Report:
    mode: str
    checks: List[Check] = field(default_factory=list)
    map_rows: List[Tuple[str, str]] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    posture: Optional[Posture] = None

    def add(
        self,
        name: str,
        expected: str,
        observed: str,
        status: str,
        detail: str = "",
    ) -> None:
        self.checks.append(Check(name, expected, observed, status, detail))
        if status == "fail":
            self.reasons.append(f"{name}: {detail or observed}")
        elif status == "warn":
            self.reasons.append(f"WARN {name}: {detail or observed}")


# Last completed check — shown on the menu until the next full run.
_last_protection: Dict[str, Optional[str]] = {
    "label": None,
    "detail": None,
}

def http_get_text(url: str, timeout: float = TIMEOUT_SEC) -> Tuple[Optional[str], Optional[str]]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "handy-scripts-verify/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8", errors="replace").strip()
            return data, None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def public_ip() -> Tuple[Optional[str], Optional[str]]:
    last_err: Optional[str] = "all public-IP probes failed"
    for url in (
        "https://api.ipify.org",
        "http://api.ipify.org",
        "https://ifconfig.me/ip",
        "http://ifconfig.me/ip",
    ):
        text, err = http_get_text(url)
        if text:
            return text.split()[0], None
        last_err = err or last_err
    return None, last_err


def tcp_connect(host: str, port: int, timeout: float = TIMEOUT_SEC) -> Tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "connected"
    except OSError as exc:
        return False, str(exc)


def load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        # PowerShell Set-Content -Encoding UTF8 writes a BOM; utf-8-sig strips it.
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def save_baseline(ip: str, extra: Dict[str, Any]) -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"public_ip": ip, **extra}
    BASELINE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _is_tunnel_adapter_name(alias: str) -> bool:
    low = (alias or "").lower()
    return any(
        k in low
        for k in (
            "openvpn",
            "tap-windows",
            "tap-win",
            "wintun",
            "ovpn-dco",
            "data channel offload",
            "protonvpn",
            "wireguard",
        )
    )


def tunnel_dns_ips(entries: List[Dict[str, str]]) -> List[str]:
    """DNS servers configured on Up tunnel adapters."""
    seen = set()
    out: List[str] = []
    for e in entries:
        if e.get("status", "").lower() != "up":
            continue
        if not _is_tunnel_adapter_name(e.get("alias", "")):
            continue
        ip = e["ip"]
        if ip in seen:
            continue
        seen.add(ip)
        out.append(ip)
    return out


def _is_well_known_public_dns(ip: str) -> bool:
    return (ip or "").strip() in WELL_KNOWN_PUBLIC_DNS


def guest_dns_targets(
    entries: List[Dict[str, str]],
    gw: Optional[str] = None,
    *,
    for_armed_up: bool = False,
) -> List[str]:
    """
    DNS servers that represent the local/guest uplink path (must be blocked while Armed).
    Sources: baseline gateway/dns, current non-tunnel Up adapter DNS, current default GW.

    When for_armed_up=True: skip current default GW (often the tunnel) and well-known
    public resolvers (full tunnel can reach them without it being a guest leak).
    """
    seen = set()
    out: List[str] = []

    def _add(ip: Optional[str]) -> None:
        if not ip or ip.count(".") != 3 or ip in seen:
            return
        if for_armed_up and _is_well_known_public_dns(ip):
            return
        seen.add(ip)
        out.append(ip)

    base = load_json(BASELINE_PATH) or {}
    _add(base.get("gateway") if isinstance(base.get("gateway"), str) else None)
    for s in base.get("dns_servers") or []:
        _add(str(s))
    # Current default GW is a guest signal when Disarmed / Armed+down; when Armed+up
    # it is often the tunnel next-hop — do not treat that as a coffee-shop leak probe.
    if not for_armed_up:
        _add(gw)

    for e in entries:
        if e.get("status", "").lower() != "up":
            continue
        if _is_tunnel_adapter_name(e.get("alias", "")):
            continue
        _add(e.get("ip"))

    return out


def _add_dns_blocked_via(
    rep: Report,
    check_name: str,
    server: str,
    *,
    fail_detail: str,
) -> None:
    ok, detail = nslookup_via(DNS_PROBE_NAME, server)
    if ok:
        rep.add(
            check_name,
            f"nslookup via {server} fail",
            detail,
            "fail",
            fail_detail,
        )
    else:
        rep.add(
            check_name,
            f"nslookup via {server} fail",
            detail,
            "pass",
        )


def _add_dns_system_ok(rep: Report) -> Tuple[bool, str]:
    ok, detail = system_resolve(DNS_PROBE_NAME)
    if ok:
        rep.add("dns_system_works", f"resolve {DNS_PROBE_NAME}", detail, "pass")
    else:
        rep.add(
            "dns_system_works",
            f"resolve {DNS_PROBE_NAME}",
            detail,
            "fail",
            "System DNS resolve failed",
        )
    return ok, detail


def _add_dns_armed_down(
    rep: Report,
    entries: List[Dict[str, str]],
    gw: Optional[str],
) -> None:
    """Firewall posture: no system DNS, no public resolver, no guest DNS."""
    ok, detail = system_resolve(DNS_PROBE_NAME)
    if ok:
        rep.add(
            "dns_system_blocked",
            "resolve fail",
            detail,
            "fail",
            "System DNS still resolves while Armed + tunnel down",
        )
    else:
        rep.add("dns_system_blocked", "resolve fail", detail, "pass")

    # Public resolver probe (no tunnel path when down) — firewall must block this.
    _add_dns_blocked_via(
        rep,
        "dns_public_resolver_blocked",
        UPLINK_DNS_PROBE,
        fail_detail="Public DNS reachable while Armed + tunnel down (firewall hole)",
    )

    guests = guest_dns_targets(entries, gw)
    if not guests:
        rep.add(
            "dns_guest_blocked",
            "guest DNS probe",
            "no guest DNS known",
            "warn",
            "Could not determine guest/uplink DNS to probe",
        )
    else:
        for g in guests:
            ok_g, detail_g = nslookup_via(DNS_PROBE_NAME, g)
            name = f"dns_guest_blocked_{g}"
            if ok_g:
                rep.add(
                    name,
                    f"nslookup via {g} fail",
                    detail_g,
                    "fail",
                    "Guest/uplink DNS reachable while Armed + tunnel down",
                )
            else:
                rep.add(
                    name,
                    f"nslookup via {g} fail",
                    detail_g,
                    "pass",
                )


def _add_dns_armed_up(
    rep: Report,
    entries: List[Dict[str, str]],
    gw: Optional[str],
) -> None:
    """
    VPN posture: system DNS works (tunnel), guest DNS blocked, tunnel DNS present.
    Do NOT require public 8.8.8.8 to fail — full tunnel routes that via VPN (false leak).
    """
    ok, detail = system_resolve(DNS_PROBE_NAME)
    if ok:
        rep.add("dns_system_works", f"resolve {DNS_PROBE_NAME}", detail, "pass")
    else:
        rep.add(
            "dns_system_works",
            f"resolve {DNS_PROBE_NAME}",
            detail,
            "fail",
            "Tunnel DNS/resolve failed while Armed + up",
        )

    tunnel_dns = tunnel_dns_ips(entries)
    if tunnel_dns:
        rep.add(
            "dns_tunnel_present",
            "DNS on OpenVPN adapter",
            format_dns_servers(tunnel_dns),
            "pass",
        )
    else:
        rep.add(
            "dns_tunnel_present",
            "DNS on OpenVPN adapter",
            "(none)",
            "fail",
            "No DNS on tunnel adapter - Windows may fall back to guest DNS",
        )

    guests = guest_dns_targets(entries, gw, for_armed_up=True)
    # Don't probe a guest target that is also a tunnel-pushed resolver.
    guests = [g for g in guests if g not in set(tunnel_dns)]
    if not guests:
        rep.add(
            "dns_guest_blocked",
            "guest DNS probe",
            "no guest DNS known",
            "warn",
            "Could not determine guest/uplink DNS to probe (baseline gateway / LAN DNS)",
        )
    else:
        for g in guests:
            ok_g, detail_g = nslookup_via(DNS_PROBE_NAME, g)
            name = f"dns_guest_blocked_{g}"
            if ok_g:
                rep.add(
                    name,
                    f"nslookup via {g} fail",
                    detail_g,
                    "fail",
                    "Guest/uplink DNS reachable while Armed + tunnel up (leak)",
                )
            else:
                rep.add(
                    name,
                    f"nslookup via {g} fail",
                    detail_g,
                    "pass",
                )

    # Info only: public resolver via full tunnel often works; not an uplink leak signal.
    pub_ok, pub_detail = nslookup_via(DNS_PROBE_NAME, UPLINK_DNS_PROBE)
    rep.add(
        "dns_public_via_tunnel_info",
        f"nslookup via {UPLINK_DNS_PROBE} (may work via VPN)",
        pub_detail if pub_ok else pub_detail,
        "info",
        "Success here usually means full-tunnel routed the query via VPN, not a guest leak",
    )


def killswitch_hint() -> Tuple[str, str]:
    st = load_json(KS_STATE_PATH)
    if st and st.get("ConfigPath"):
        return "likely ARMED (state file present)", str(st.get("ConfigPath"))
    if KS_STATE_PATH.is_file():
        return "state file present", str(KS_STATE_PATH)
    return "no kill-switch state file", "Disarmed or non-Windows / other tool"


def killswitch_armed() -> Tuple[bool, str]:
    st = load_json(KS_STATE_PATH)
    if st and st.get("ConfigPath"):
        return True, str(st.get("ConfigPath"))
    return False, "no kill-switch state file"


def tunnel_adapter_rows() -> List[Dict[str, str]]:
    """
    OpenVPN-like adapters from Get-NetAdapter (Name, Description, Status).
    Lighter than DNS enumeration — used for STATE inference.
    """
    if os.name != "nt":
        return []
    ps = r"""
$ErrorActionPreference = 'SilentlyContinue'
Get-NetAdapter |
  ForEach-Object {
    Write-Output ($_.Name + "`t" + $_.InterfaceDescription + "`t" + $_.Status)
  }
"""
    try:
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-WindowStyle",
                "Hidden",
                "-Command",
                ps,
            ],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            errors="replace",
            **_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    rows: List[Dict[str, str]] = []
    for line in out.splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 3:
            continue
        name, desc, status = parts[0].strip(), parts[1].strip(), parts[2].strip()
        blob = f"{name} {desc}"
        if not _is_tunnel_adapter_name(blob):
            continue
        rows.append({"name": name, "desc": desc, "status": status})
    return rows


def infer_posture() -> Posture:
    """
    Infer Disarmed / Armed+down / Armed+up from kill-switch state + tunnel adapters.
    Does not run full leak checks — that is what Check current protection does next.
    """
    armed, armed_detail = killswitch_armed()
    tunnels = tunnel_adapter_rows()
    up = [t for t in tunnels if t.get("status", "").lower() == "up"]
    tunnel_up = len(up) > 0
    if tunnels:
        tun_detail = "; ".join(f"{t['name']}={t['status']}" for t in tunnels)
    else:
        tun_detail = "no OpenVPN-like adapters found"

    if not armed:
        return Posture(
            key="disarmed",
            label="Disarmed",
            detail=f"{armed_detail}; tunnel: {tun_detail}",
            armed=False,
            tunnel_up=tunnel_up,
        )
    if tunnel_up:
        names = ", ".join(t["name"] for t in up)
        return Posture(
            key="armed-up",
            label="Armed + VPN up",
            detail=f"state: {armed_detail}; tunnel Up: {names}",
            armed=True,
            tunnel_up=True,
        )
    return Posture(
        key="armed-down",
        label="Armed + VPN down",
        detail=f"state: {armed_detail}; tunnel: {tun_detail}",
        armed=True,
        tunnel_up=False,
    )


def posture_for_mode(mode: str) -> Posture:
    """Assumed posture when the user forces a specific expect-* / baseline mode."""
    mapping = {
        "baseline": ("disarmed", "Disarmed (baseline)"),
        "expect-disarmed": ("disarmed", "Disarmed"),
        "expect-armed-down": ("armed-down", "Armed + VPN down"),
        "expect-armed-up": ("armed-up", "Armed + VPN up"),
    }
    key, label = mapping.get(mode, ("uncertain", "Uncertain"))
    return Posture(
        key=key,
        label=label,
        detail=f"assumed from selected mode ({mode})",
        armed=key.startswith("armed"),
        tunnel_up=key == "armed-up",
    )


def gateway_ipv4() -> Optional[str]:
    if os.name != "nt":
        return None
    try:
        out = subprocess.check_output(
            ["route", "print", "0.0.0.0"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            errors="replace",
            **_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "0.0.0.0":
            cand = parts[2]
            if cand.count(".") == 3:
                return cand
    return None


@dataclass
class BaselineFreshness:
    """Is the saved baseline still for this LAN? Gateway compare is the cheap signal."""

    key: str  # ok | stale | missing | unknown | deferred
    label: str
    detail: str
    baseline_gateway: Optional[str] = None
    current_gateway: Optional[str] = None


def baseline_freshness(
    posture: Optional[Posture] = None,
    current_gateway: Optional[str] = None,
) -> BaselineFreshness:
    """
    Compare current default gateway to the gateway stored with Baseline.
    While Armed + VPN up, the default route is often the tunnel — defer then.
    """
    base = load_json(BASELINE_PATH) or {}
    base_ip = base.get("public_ip")
    base_gw = base.get("gateway") if isinstance(base.get("gateway"), str) else None
    if not base_ip:
        return BaselineFreshness(
            key="missing",
            label="BASELINE: missing",
            detail="Run Baseline while Disarmed on this network before Arm + Connect",
        )

    cur_gw = current_gateway if current_gateway is not None else gateway_ipv4()
    if posture is not None and posture.key == "armed-up":
        return BaselineFreshness(
            key="deferred",
            label="BASELINE: gateway check deferred (VPN up)",
            detail=(
                f"saved REAL IP {base_ip}"
                + (f", gateway was {base_gw}" if base_gw else "")
                + (f"; default route now {cur_gw} (often tunnel)" if cur_gw else "")
                + " — re-check gateway when Disarmed or Armed + VPN down"
            ),
            baseline_gateway=base_gw,
            current_gateway=cur_gw,
        )

    if not base_gw:
        return BaselineFreshness(
            key="unknown",
            label="BASELINE: unknown freshness",
            detail=f"saved REAL IP {base_ip} but no gateway stored — re-run Baseline",
            baseline_gateway=None,
            current_gateway=cur_gw,
        )
    if not cur_gw:
        return BaselineFreshness(
            key="unknown",
            label="BASELINE: unknown freshness",
            detail=f"saved gateway {base_gw}; could not read current gateway",
            baseline_gateway=base_gw,
            current_gateway=None,
        )
    if cur_gw == base_gw:
        return BaselineFreshness(
            key="ok",
            label="BASELINE: current network",
            detail=f"gateway still {cur_gw} (REAL IP {base_ip})",
            baseline_gateway=base_gw,
            current_gateway=cur_gw,
        )
    return BaselineFreshness(
        key="stale",
        label="BASELINE: out of date for this network",
        detail=f"gateway was {base_gw}, now {cur_gw} — re-run Baseline (saved REAL IP {base_ip})",
        baseline_gateway=base_gw,
        current_gateway=cur_gw,
    )


def add_baseline_freshness_check(rep: Report) -> BaselineFreshness:
    """Attach a check/map row for baseline vs current gateway."""
    fresh = baseline_freshness(rep.posture)
    rep.map_rows = [("Baseline", f"{fresh.label} — {fresh.detail}"), *rep.map_rows]
    if fresh.key == "missing":
        # Other cmds already fail on missing baseline IP where needed.
        if not any(c.name == "baseline" for c in rep.checks):
            rep.add(
                "baseline_network",
                "baseline present for this network",
                fresh.detail,
                "warn",
            )
    elif fresh.key == "stale":
        rep.add(
            "baseline_network",
            f"gateway {fresh.baseline_gateway}",
            f"gateway {fresh.current_gateway}",
            "warn",
            fresh.detail,
        )
    elif fresh.key == "ok":
        rep.add(
            "baseline_network",
            f"gateway {fresh.baseline_gateway}",
            f"gateway {fresh.current_gateway}",
            "pass",
            fresh.detail,
        )
    elif fresh.key == "deferred":
        rep.add(
            "baseline_network",
            "gateway match when not on tunnel default route",
            fresh.detail,
            "info",
        )
    else:
        rep.add(
            "baseline_network",
            "gateway comparable to baseline",
            fresh.detail,
            "warn",
        )
    return fresh


def ping_host(host: str) -> Tuple[bool, str]:
    if not host:
        return False, "no host"
    try:
        if os.name == "nt":
            cmd = ["ping", "-n", "1", "-w", "2000", host]
        else:
            cmd = ["ping", "-c", "1", "-W", "2", host]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
            **_subprocess_kwargs(),
        )
        return proc.returncode == 0, f"exit={proc.returncode}"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def dns_server_entries() -> List[Dict[str, str]]:
    """
    Per-adapter IPv4 DNS from Windows.
    Each entry: {ip, alias, status} with status Up/Disconnected/etc.
    """
    if os.name != "nt":
        return []
    # One line per server: IP<TAB>Alias<TAB>Status
    ps = r"""
$ErrorActionPreference = 'SilentlyContinue'
Get-DnsClientServerAddress -AddressFamily IPv4 |
  ForEach-Object {
    $if = Get-NetAdapter -InterfaceIndex $_.InterfaceIndex
    $alias = if ($if) { $if.Name } else { "If$($_.InterfaceIndex)" }
    $status = if ($if) { [string]$if.Status } else { 'Unknown' }
    foreach ($s in @($_.ServerAddresses)) {
      if (-not $s) { continue }
      $t = $s.ToString()
      if ($t -eq '0.0.0.0' -or $t -eq '::') { continue }
      if (($t.ToCharArray() | Where-Object { $_ -eq '.' }).Count -ne 3) { continue }
      Write-Output ($t + "`t" + $alias + "`t" + $status)
    }
  }
"""
    try:
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-WindowStyle",
                "Hidden",
                "-Command",
                ps,
            ],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=12,
            errors="replace",
            **_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    entries: List[Dict[str, str]] = []
    seen = set()
    for line in out.splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 3:
            continue
        ip, alias, status = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not ip or ip.count(".") != 3:
            continue
        key = (ip, alias)
        if key in seen:
            continue
        seen.add(key)
        entries.append({"ip": ip, "alias": alias, "status": status})
    return entries


def dns_servers_ipv4(
    up_only: bool = False,
    entries: Optional[List[Dict[str, str]]] = None,
) -> List[str]:
    """Unique DNS IPs (optionally only from Up adapters). Reuse entries to avoid a second PowerShell spawn."""
    if entries is None:
        entries = dns_server_entries()
    seen = set()
    out: List[str] = []
    for e in entries:
        if up_only and e.get("status", "").lower() != "up":
            continue
        ip = e["ip"]
        if ip in seen:
            continue
        seen.add(ip)
        out.append(ip)
    return out


def format_dns_entries(entries: List[Dict[str, str]]) -> str:
    if not entries:
        return "(none detected)"
    return "; ".join(f"{e['ip']} ({e['alias']}/{e['status']})" for e in entries)


def dns_map_rows(entries: Optional[List[Dict[str, str]]] = None) -> List[Tuple[str, str]]:
    """Map rows: active (Up) DNS first; inactive adapters called out separately."""
    if entries is None:
        entries = dns_server_entries()
    active = [e for e in entries if e.get("status", "").lower() == "up"]
    inactive = [e for e in entries if e.get("status", "").lower() != "up"]
    rows: List[Tuple[str, str]] = [
        ("DNS (Up adapters)", format_dns_entries(active)),
    ]
    if inactive:
        rows.append(("DNS (inactive)", format_dns_entries(inactive)))
    return rows


def system_resolve(name: str, timeout: float = TIMEOUT_SEC) -> Tuple[bool, str]:
    """Resolve via the OS stub resolver (no explicit server)."""
    try:
        old = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout)
        try:
            infos = socket.getaddrinfo(name, None, socket.AF_INET)
        finally:
            socket.setdefaulttimeout(old)
        if not infos:
            return False, "no addresses"
        addrs = sorted({i[4][0] for i in infos})
        return True, ",".join(addrs[:4])
    except OSError as exc:
        return False, str(exc)


def nslookup_via(name: str, server: str, timeout: float = 6.0) -> Tuple[bool, str]:
    """
    Query name via an explicit DNS server using nslookup.
    Success = at least one IPv4 answer distinct from the server IP.
    Failure = timeout, refused, NXDOMAIN, or no usable A record.
    """
    try:
        proc = subprocess.run(
            ["nslookup", name, server],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            errors="replace",
            **_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except OSError as exc:
        return False, str(exc)

    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    lower = text.lower()
    fail_markers = (
        "timed out",
        "timeout",
        "no response from server",
        "server failed",
        "refused",
        "can't find",
        "non-existent domain",
        "nxdomain",
        "dns request timed out",
        "unknown can't find",
    )
    if any(m in lower for m in fail_markers):
        snippet = " ".join(text.split())[:160] or f"exit={proc.returncode}"
        return False, snippet

    # Collect IPv4s from the whole transcript; skip the DNS server address line(s).
    ipv4 = re.findall(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", text)
    answers: List[str] = []
    seen = set()
    for ip in ipv4:
        if ip == server or ip in seen:
            continue
        # Skip obvious non-answers from "Server: / Address:" preamble when only server listed.
        seen.add(ip)
        answers.append(ip)

    # If the only IPv4 in the output is the server itself, that is not an answer.
    if answers:
        return True, ",".join(answers[:4])

    snippet = " ".join(text.split())[:160] or f"exit={proc.returncode}"
    return False, snippet


def format_dns_servers(servers: List[str]) -> str:
    if not servers:
        return "(none detected)"
    return ", ".join(servers)


def effective_posture_key(rep: Report) -> str:
    if rep.posture is not None:
        return rep.posture.key
    return posture_for_mode(rep.mode).key


def protection_status(rep: Report) -> Tuple[str, str]:
    """
    Coffee-shop headline: is traffic protected from the local network?
    Uses inferred/assumed STATE; separate from as-expected matching.
    """
    fails = [c for c in rep.checks if c.status == "fail"]
    key = effective_posture_key(rep)
    if key == "disarmed":
        return (
            "UNPROTECTED",
            "Clearnet on this network — kill switch is not protecting you (expected when Disarmed).",
        )
    if key == "uncertain":
        return (
            "NOT SAFE",
            "Could not infer a clear Armed/Disarmed posture — treat as unprotected until resolved.",
        )
    if fails:
        return (
            "NOT SAFE",
            "One or more checks suggest local/clearnet exposure or a broken tunnel path.",
        )
    if key == "armed-up":
        return (
            "SAFE",
            "No local-leak signals; egress and DNS look like they go via the VPN tunnel.",
        )
    if key == "armed-down":
        return (
            "SAFE (LOCKED DOWN)",
            "No clearnet or guest path while Armed with the tunnel down (not a usable session).",
        )
    return ("UNKNOWN", "Unrecognized state.")


def protection_colors(label: Optional[str]) -> Tuple[str, str]:
    """Background, foreground for the PROTECTION headline (green / yellow / red)."""
    if not label:
        return "#F4D03F", "#1A1A1A"  # yellow — not verified yet
    key = label.strip().upper()
    if key.startswith("SAFE"):
        return "#1E8449", "#FFFFFF"  # green — SAFE / SAFE (LOCKED DOWN)
    if key.startswith("NOT SAFE") or key == "UNKNOWN":
        return "#C0392B", "#FFFFFF"  # red
    if key.startswith("UNPROTECTED"):
        return "#F4D03F", "#1A1A1A"  # yellow
    return "#F4D03F", "#1A1A1A"


def format_status_banner(
    posture: Posture,
    protection: Optional[Tuple[str, str]] = None,
    *,
    protection_is_last: bool = False,
    freshness: Optional[BaselineFreshness] = None,
) -> str:
    """PROTECTION first, then STATE and BASELINE (console / plain-text)."""
    lines: List[str] = []
    if protection:
        tag = "PROTECTION (last check)" if protection_is_last else "PROTECTION"
        lines.append(f"{tag}: {protection[0]}")
        lines.append(f"  {protection[1]}")
    else:
        lines.append("PROTECTION: not verified yet — run Check current protection")
    lines.append(f"STATE: {posture.label}")
    lines.append(f"  {posture.detail}")
    if freshness is None:
        freshness = baseline_freshness(posture)
    lines.append(f"{freshness.label}")
    lines.append(f"  {freshness.detail}")
    return "\n".join(lines)


def format_state_baseline_block(
    posture: Posture,
    freshness: Optional[BaselineFreshness] = None,
) -> str:
    if freshness is None:
        freshness = baseline_freshness(posture)
    return "\n".join(
        [
            f"STATE: {posture.label}",
            f"  {posture.detail}",
            f"{freshness.label}",
            f"  {freshness.detail}",
        ]
    )


def format_report(rep: Report) -> Tuple[str, int]:
    mode_label = {
        "check-current": "Check current protection (auto-inferred state)",
        "baseline": "Baseline (Disarmed) — recording REAL egress for this network",
        "expect-disarmed": "Expect Disarmed — normal internet on this network",
        "expect-armed-down": "Expect Armed + VPN down — kill-switch lockdown (not a usable session)",
        "expect-armed-up": "Expect Armed + VPN up — protected road-warrior session",
    }.get(rep.mode, rep.mode)
    posture = rep.posture or posture_for_mode(rep.mode)
    fresh = baseline_freshness(posture)
    prot, prot_detail = protection_status(rep)
    fails = sum(1 for c in rep.checks if c.status == "fail")
    warns = sum(1 for c in rep.checks if c.status == "warn")
    if rep.mode == "check-current":
        if fails:
            match_summary, code = "CHECKS FAILED FOR INFERRED STATE", 1
        elif warns:
            match_summary, code = "MIXED — WARNINGS FOR INFERRED STATE", 2
        else:
            match_summary, code = "CHECKS OK FOR INFERRED STATE", 0
    elif fails:
        match_summary, code = "NOT AS EXPECTED FOR THIS STATE", 1
    elif warns and (rep.mode.startswith("expect-") or rep.mode == "check-current"):
        match_summary, code = "MIXED — WARNINGS FOR THIS STATE", 2
    else:
        match_summary, code = "AS EXPECTED FOR THIS STATE", 0

    lines: List[str] = [
        "=" * 60,
        f" PROTECTION: {prot}",
        f" {prot_detail}",
        f" STATE: {posture.label}",
        f" {posture.detail}",
        f" {fresh.label}",
        f" {fresh.detail}",
        "=" * 60,
        f" Checks: {match_summary}",
        "",
        "=" * 60,
        " ROAD WARRIOR MAP",
        "=" * 60,
        f"  {'PROTECTION':<22} {prot}",
        f"  {'Testing':<22} {mode_label}",
        f"  {'Inferred STATE':<22} {posture.label}",
        f"  {'Baseline':<22} {fresh.label}",
    ]
    for label, value in rep.map_rows:
        if label in ("Mode", "Inferred state", "Infer detail", "Baseline"):
            continue
        lines.append(f"  {label:<22} {value}")
    lines.extend(["", "-" * 60, " CHECKS (vs this state)", "-" * 60])
    for c in rep.checks:
        mark = {"pass": "YES ", "fail": "NO  ", "warn": "WARN", "info": "INFO"}.get(
            c.status, c.status
        )
        lines.append(f"  [{mark}] {c.name}")
        lines.append(f"         expected: {c.expected}")
        lines.append(f"         observed: {c.observed}")
        if c.detail:
            lines.append(f"         detail:   {c.detail}")
    lines.append("")
    if rep.reasons:
        lines.append(" Notes:")
        lines.extend(f"  - {r}" for r in rep.reasons)
    else:
        lines.append(" Notes: (none)")
    lines.append("")
    return "\n".join(lines), code


def console_available() -> bool:
    # pythonw.exe sets stdout to None; a normal/piped console should print.
    return sys.stdout is not None


def _enable_label_wrap(root: Any, label: Any, *, pad: int = 28) -> None:
    """Make a Label wrap long lines (Tk clips otherwise)."""

    def _sync(_event: Any = None) -> None:
        try:
            width = root.winfo_width()
            if width <= 1:
                width = int(root.winfo_reqwidth() or 560)
            label.configure(wraplength=max(120, width - pad))
        except Exception:  # noqa: BLE001
            pass

    root.bind("<Configure>", _sync, add="+")
    root.after(10, _sync)


def _paint_protection_label(
    label: Any,
    protection: Optional[Tuple[str, str]],
    *,
    protection_is_last: bool = False,
) -> None:
    if protection:
        tag = "PROTECTION (last check)" if protection_is_last else "PROTECTION"
        text = f"{tag}: {protection[0]}\n  {protection[1]}"
        bg, fg = protection_colors(protection[0])
    else:
        text = "PROTECTION: not verified yet\n  Run Check current protection"
        bg, fg = protection_colors(None)
    label.configure(text=text, bg=bg, fg=fg)


def _remember_protection(rep: Report) -> Tuple[str, str]:
    prot = protection_status(rep)
    _last_protection["label"] = prot[0]
    _last_protection["detail"] = prot[1]
    return prot


def show_text_window(
    title: str,
    body: str,
    posture: Posture,
    protection: Tuple[str, str],
) -> str:
    """Show report window with PROTECTION (colored) on top. Returns 'exit' or 'menu'."""
    import tkinter as tk
    from tkinter import scrolledtext

    result: Dict[str, str] = {"action": "exit"}
    root = tk.Tk()
    root.title(title)
    root.geometry("760x640")
    fresh0 = baseline_freshness(posture)

    prot_lbl = tk.Label(
        root,
        justify=tk.LEFT,
        anchor=tk.NW,
        font=("Segoe UI", 14, "bold"),
        padx=12,
        pady=12,
        wraplength=720,
    )
    prot_lbl.pack(fill=tk.X)
    _paint_protection_label(prot_lbl, protection)
    _enable_label_wrap(root, prot_lbl)

    rest = tk.Label(
        root,
        text=format_state_baseline_block(posture, fresh0),
        justify=tk.LEFT,
        anchor=tk.NW,
        font=("Segoe UI", 10),
        bg="#f0f0f0",
        fg="#1A1A1A",
        padx=12,
        pady=8,
        wraplength=720,
    )
    rest.pack(fill=tk.X)
    _enable_label_wrap(root, rest)

    txt = scrolledtext.ScrolledText(root, wrap=tk.WORD, font=("Consolas", 10))
    txt.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
    txt.insert(tk.END, body)
    txt.configure(state=tk.DISABLED)

    def _exit() -> None:
        result["action"] = "exit"
        root.destroy()

    def _menu() -> None:
        result["action"] = "menu"
        root.destroy()

    row = tk.Frame(root)
    row.pack(pady=(0, 8))
    tk.Button(row, text="Back to menu", width=14, command=_menu).pack(side=tk.LEFT, padx=6)
    tk.Button(row, text="Exit", width=10, command=_exit).pack(side=tk.LEFT, padx=6)
    root.protocol("WM_DELETE_WINDOW", _exit)
    root.mainloop()
    return result["action"]


def pick_command_gui() -> Optional[str]:
    import threading
    import tkinter as tk

    choice: Dict[str, Optional[str]] = {"value": None}
    root = tk.Tk()
    root.title("Road warrior verifier")
    root.geometry("560x580")

    prot_lbl = tk.Label(
        root,
        justify=tk.LEFT,
        anchor=tk.NW,
        font=("Segoe UI", 14, "bold"),
        padx=12,
        pady=12,
        wraplength=520,
    )
    prot_lbl.pack(fill=tk.X)
    _paint_protection_label(prot_lbl, None)
    _enable_label_wrap(root, prot_lbl)

    rest = tk.Label(
        root,
        text="Detecting current STATE / baseline…",
        justify=tk.LEFT,
        anchor=tk.NW,
        font=("Segoe UI", 10),
        bg="#f0f0f0",
        fg="#1A1A1A",
        padx=12,
        pady=8,
        wraplength=520,
    )
    rest.pack(fill=tk.X)
    _enable_label_wrap(root, rest)

    def _apply_banner(posture: Posture, fresh: BaselineFreshness) -> None:
        try:
            last_lab = _last_protection.get("label")
            last_det = _last_protection.get("detail")
            if last_lab and last_det:
                _paint_protection_label(
                    prot_lbl, (last_lab, last_det), protection_is_last=True
                )
            else:
                _paint_protection_label(prot_lbl, None)
            rest.configure(text=format_state_baseline_block(posture, fresh))
        except tk.TclError:
            pass

    def _sniff() -> None:
        try:
            rest.configure(text="Detecting current STATE / baseline…")
        except tk.TclError:
            return

        def work() -> None:
            try:
                posture = infer_posture()
                fresh = baseline_freshness(posture)
                root.after(0, lambda: _apply_banner(posture, fresh))
            except Exception as exc:  # noqa: BLE001
                def _fail() -> None:
                    try:
                        rest.configure(text=f"STATE: (detection failed)\n  {exc}")
                    except tk.TclError:
                        pass

                root.after(0, _fail)

        threading.Thread(target=work, daemon=True).start()

    tip = (
        "Goal: at a coffee shop, confirm traffic stays off the local network.\n"
        "Default: Check current protection (infers STATE, then SAFE / NOT SAFE).\n\n"
        "Baseline = this network's REAL public IP while Disarmed.\n"
        "Re-run Baseline when the banner says it is out of date (gateway changed)."
    )
    tk.Label(root, text=tip, justify=tk.LEFT, anchor=tk.W).pack(
        fill=tk.X, padx=12, pady=(12, 4)
    )

    tk.Label(
        root,
        text="Choose an action:",
        justify=tk.LEFT,
    ).pack(anchor=tk.W, padx=12, pady=(4, 8))

    options = [
        (
            "check-current",
            "Check current protection — infer STATE, then verify (recommended)",
        ),
        (
            "baseline",
            "Baseline (Disarmed) — save REAL IP for THIS network (before Arm + Connect)",
        ),
        ("expect-disarmed", "Force: Expect Disarmed — normal internet"),
        ("expect-armed-down", "Force: Expect Armed + VPN down — kill switch"),
        ("expect-armed-up", "Force: Expect Armed + VPN up — road warrior"),
    ]
    var = tk.StringVar(value="check-current")
    for value, label in options:
        tk.Radiobutton(root, text=label, variable=var, value=value, anchor=tk.W).pack(
            fill=tk.X, padx=16
        )

    def _run() -> None:
        choice["value"] = var.get()
        root.destroy()

    def _cancel() -> None:
        choice["value"] = None
        root.destroy()

    row = tk.Frame(root)
    row.pack(pady=16)
    tk.Button(row, text="Run", width=10, command=_run).pack(side=tk.LEFT, padx=6)
    tk.Button(row, text="Refresh STATE", width=14, command=_sniff).pack(
        side=tk.LEFT, padx=6
    )
    tk.Button(row, text="Exit", width=10, command=_cancel).pack(side=tk.LEFT, padx=6)

    root.after(50, _sniff)
    root.mainloop()
    return choice["value"]


def print_report(rep: Report) -> int:
    text, code = format_report(rep)
    prot, prot_detail = _remember_protection(rep)
    posture = rep.posture or posture_for_mode(rep.mode)
    if console_available():
        print(text)
        return code
    title = f"Road warrior verifier — {prot} — {posture.label}"
    action = show_text_window(title, text, posture, (prot, prot_detail))
    if action == "menu":
        picked = pick_command_gui()
        if picked:
            return run_command(picked)
    return code


def attach_posture(rep: Report, *, prefer_inferred: bool) -> Posture:
    """Set rep.posture; warn if forced mode disagrees with live inference."""
    live = infer_posture()
    if prefer_inferred or rep.mode == "check-current":
        rep.posture = live
        return live
    assumed = posture_for_mode(rep.mode)
    # Keep assumed for protection grading of forced modes, but surface live STATE.
    rep.posture = Posture(
        key=assumed.key,
        label=assumed.label,
        detail=f"{assumed.detail}; live sniff: {live.label} ({live.detail})",
        armed=assumed.armed,
        tunnel_up=assumed.tunnel_up,
    )
    if live.key != assumed.key and rep.mode != "baseline":
        rep.add(
            "state_match",
            assumed.label,
            live.label,
            "warn",
            f"You selected {assumed.label}, but live inference is {live.label}",
        )
    return rep.posture


def cmd_check_current(rep: Report) -> None:
    posture = attach_posture(rep, prefer_inferred=True)
    rep.map_rows = [
        ("Mode", "check current protection"),
        ("Inferred state", posture.label),
        ("Infer detail", posture.detail),
    ]
    if posture.key == "disarmed":
        cmd_expect_disarmed(rep)
    elif posture.key == "armed-down":
        cmd_expect_armed_down(rep)
    elif posture.key == "armed-up":
        cmd_expect_armed_up(rep)
    # Keep mode as check-current after nested cmds (they do not change mode).
    rep.mode = "check-current"
    rep.posture = posture
    # Nested cmds overwrite map_rows — put inference back on top.
    rep.map_rows = [
        ("Inferred state", posture.label),
        ("Infer detail", posture.detail),
        *rep.map_rows,
    ]


def fill_report(command: str, rep: Report) -> None:
    if command == "check-current":
        cmd_check_current(rep)
    elif command == "baseline":
        attach_posture(rep, prefer_inferred=False)
        cmd_baseline(rep)
    elif command == "expect-disarmed":
        attach_posture(rep, prefer_inferred=False)
        cmd_expect_disarmed(rep)
    elif command == "expect-armed-down":
        attach_posture(rep, prefer_inferred=False)
        cmd_expect_armed_down(rep)
    elif command == "expect-armed-up":
        attach_posture(rep, prefer_inferred=False)
        cmd_expect_armed_up(rep)
    else:
        raise ValueError(f"unknown command: {command}")
    # After posture + checks: cheap gateway compare (skip noise right after a fresh save).
    if command != "baseline":
        add_baseline_freshness_check(rep)
    else:
        # Confirm the just-written baseline matches this gateway.
        fresh = baseline_freshness(rep.posture)
        rep.map_rows = [("Baseline", f"{fresh.label} — {fresh.detail}"), *rep.map_rows]
        if fresh.key == "ok":
            rep.add(
                "baseline_network",
                f"gateway {fresh.baseline_gateway}",
                f"gateway {fresh.current_gateway}",
                "pass",
                "Saved baseline matches this network gateway",
            )


def run_command(command: str) -> int:
    """Collect checks (with a busy window in GUI mode), then show the report."""
    labels = {
        "check-current": "Check current protection",
        "baseline": "Baseline",
        "expect-disarmed": "Expect Disarmed",
        "expect-armed-down": "Expect Armed + VPN down",
        "expect-armed-up": "Expect Armed + VPN up",
    }
    msg = (
        f"Running {labels.get(command, command)} — collecting checks "
        "(can take ~30 seconds; please wait)"
    )

    if console_available():
        print(msg, flush=True)
        rep = Report(mode=command)
        fill_report(command, rep)
        return print_report(rep)

    # GUI: keep a busy window alive on the Tk main thread while checks run
    # on a worker thread (otherwise the UI freezes and looks crashed).
    import threading
    import tkinter as tk

    holder: Dict[str, Any] = {"rep": None, "err": None}
    root = tk.Tk()
    root.title("Road warrior verifier")
    root.geometry("440x110")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    lbl = tk.Label(
        root,
        text=msg,
        justify=tk.LEFT,
        anchor=tk.W,
        font=("Segoe UI", 10),
        padx=16,
        pady=16,
    )
    lbl.pack(fill=tk.BOTH, expand=True)
    n = {"i": 0}

    def _tick() -> None:
        try:
            n["i"] = (n["i"] + 1) % 4
            lbl.configure(text=msg + "." * n["i"])
            root.after(400, _tick)
        except tk.TclError:
            pass

    def _work() -> None:
        try:
            rep = Report(mode=command)
            fill_report(command, rep)
            holder["rep"] = rep
        except Exception as exc:  # noqa: BLE001
            holder["err"] = exc
        root.after(0, root.quit)

    threading.Thread(target=_work, daemon=True).start()
    root.after(400, _tick)
    root.protocol("WM_DELETE_WINDOW", lambda: None)  # ignore close while busy
    root.mainloop()
    try:
        root.destroy()
    except tk.TclError:
        pass

    if holder["err"] is not None:
        err = holder["err"]
        try:
            import tkinter as tk
            from tkinter import messagebox

            tip = tk.Tk()
            tip.withdraw()
            messagebox.showerror(
                "Road warrior verifier",
                f"Check failed:\n\n{err}",
                parent=tip,
            )
            tip.destroy()
        except Exception:  # noqa: BLE001
            pass
        return 2
    if holder["rep"] is None:
        return 2
    return print_report(holder["rep"])


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv and not console_available():
        picked = pick_command_gui()
        if not picked:
            return 0
        return run_command(picked)

    parser = argparse.ArgumentParser(
        description=(
            "Coffee-shop check: infer STATE, then PROTECTION SAFE / NOT SAFE / UNPROTECTED. "
            "Default command is check-current. "
            "Run 'baseline' while Disarmed on the test network before Arm + Connect; "
            "re-run baseline whenever you change networks."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=[
            "check-current",
            "baseline",
            "expect-disarmed",
            "expect-armed-down",
            "expect-armed-up",
        ],
        help="check-current (default) or a forced situation",
    )
    args = parser.parse_args(argv)
    if not args.command:
        if not console_available():
            picked = pick_command_gui()
            if not picked:
                return 0
            return run_command(picked)
        return run_command("check-current")
    return run_command(args.command)


def cmd_baseline(rep: Report) -> None:
    live = infer_posture()
    ip, err = public_ip()
    gw = gateway_ipv4()
    ks, ks_detail = killswitch_hint()
    dns_entries = dns_server_entries()
    dns_up = dns_servers_ipv4(up_only=True, entries=dns_entries)
    rep.map_rows = [
        ("Mode", "baseline (record REAL egress for THIS network)"),
        ("Note", "Re-run after changing Wi-Fi/LAN; do this Disarmed BEFORE Arm + Connect"),
        ("Live STATE", live.label),
        ("Public IPv4", ip or f"FAILED ({err})"),
        ("Default gateway", gw or "(unknown)"),
        *dns_map_rows(dns_entries),
        ("Kill-switch hint", f"{ks} - {ks_detail}"),
    ]
    if live.key != "disarmed":
        rep.add(
            "baseline_requires_disarmed",
            "Disarmed",
            live.label,
            "fail",
            "Refusing to save baseline while not Disarmed (would stamp tunnel IP/gateway). "
            "Disarm, then run Baseline on this network.",
        )
        return
    if not ip:
        rep.add("public_ip", "reachable", f"error: {err}", "fail", "Cannot record baseline")
        return
    # Prefer non-tunnel Up adapter DNS for baseline guest list (exclude tunnel adapters).
    dns_up_lan = [
        e["ip"]
        for e in dns_entries
        if e.get("status", "").lower() == "up"
        and not _is_tunnel_adapter_name(e.get("alias", ""))
        and e.get("ip")
    ]
    # Dedupe preserving order
    seen_d = set()
    dns_lan_unique: List[str] = []
    for d in dns_up_lan:
        if d in seen_d:
            continue
        seen_d.add(d)
        dns_lan_unique.append(d)
    save_baseline(
        ip,
        {
            "gateway": gw,
            "dns_servers": dns_lan_unique or dns_up,
            "dns_entries": dns_entries,
        },
    )
    rep.add("public_ip", "reachable", ip, "pass", f"Wrote {BASELINE_PATH}")
    rep.add("baseline_file", "saved", str(BASELINE_PATH), "info")
    ok443, detail = tcp_connect("1.1.1.1", 443)
    rep.add("tcp_443_probe", "success", detail, "pass" if ok443 else "warn")
    _add_dns_system_ok(rep)


def _load_baseline_ip(rep: Report) -> Optional[str]:
    base = load_json(BASELINE_PATH)
    if not base or not base.get("public_ip"):
        rep.add(
            "baseline",
            "present",
            "missing",
            "fail",
            f"Run baseline first (expected {BASELINE_PATH})",
        )
        return None
    return str(base["public_ip"])


def cmd_expect_disarmed(rep: Report) -> None:
    real = _load_baseline_ip(rep)
    ip, err = public_ip()
    gw = gateway_ipv4()
    ks, ks_detail = killswitch_hint()
    dns_entries = dns_server_entries()
    rep.map_rows = [
        ("Mode", "expect DISARMED / normal net"),
        ("Baseline REAL IP", real or "(none)"),
        ("Public IPv4 now", ip or f"FAILED ({err})"),
        ("Default gateway", gw or "(unknown)"),
        *dns_map_rows(dns_entries),
        ("Kill-switch hint", f"{ks} - {ks_detail}"),
        ("DNS path", "uplink OK when disarmed"),
    ]
    if not ip:
        rep.add("public_ip", "reachable", str(err), "fail")
        _add_dns_system_ok(rep)
        return
    rep.add("public_ip", "reachable", ip, "pass")
    if real and ip != real:
        rep.add(
            "matches_baseline",
            real,
            ip,
            "warn",
            "Different from baseline (VPN still up? different network?)",
        )
    elif real:
        rep.add("matches_baseline", real, ip, "pass")
    if "likely ARMED" in ks:
        rep.add("killswitch_state", "no state / disarmed", ks, "warn", ks_detail)
    _add_dns_system_ok(rep)
    return


def cmd_expect_armed_down(rep: Report) -> None:
    real = _load_baseline_ip(rep)
    ip, err = public_ip()
    gw = gateway_ipv4()
    ks, ks_detail = killswitch_hint()
    dns_entries = dns_server_entries()
    ping_ok, ping_detail = ping_host(gw) if gw else (False, "no gateway")
    guests = guest_dns_targets(dns_entries, gw)
    rep.map_rows = [
        ("Mode", "expect ARMED + tunnel DOWN (kill switch)"),
        ("Baseline REAL IP", real or "(none)"),
        ("Public IPv4 now", ip or f"blocked/failed ({err})"),
        ("Local gateway", gw or "(unknown)"),
        ("Gateway ping", "ok" if ping_ok else f"fail ({ping_detail})"),
        *dns_map_rows(dns_entries),
        ("Guest DNS probes", format_dns_servers(guests)),
        ("Kill-switch hint", f"{ks} - {ks_detail}"),
        ("DNS", "system + public + guest DNS must fail"),
        ("VPN reconnect", "hosts pin + VPN IP:port allow only"),
    ]
    if ip:
        rep.add(
            "public_ip_blocked",
            "unreachable / no REAL IP",
            ip,
            "fail",
            "Clearnet IP obtained while tunnel should be down - kill switch leak",
        )
        if real and ip == real:
            rep.add("leaked_baseline", "not equal to REAL", ip, "fail", "Exposed baseline IP")
    else:
        rep.add("public_ip_blocked", "unreachable", str(err), "pass", "No public IP (good)")
    ok443, d443 = tcp_connect("1.1.1.1", 443)
    if ok443:
        rep.add("tcp_443_blocked", "fail to connect", d443, "fail", "General HTTPS path open")
    else:
        rep.add("tcp_443_blocked", "fail to connect", d443, "pass")
    if gw:
        if ping_ok:
            rep.add("guest_lan_blocked", "ping fail", ping_detail, "fail", "Guest gateway reachable")
        else:
            rep.add("guest_lan_blocked", "ping fail", ping_detail, "pass")
    if "likely ARMED" not in ks:
        rep.add(
            "killswitch_state",
            "state file present",
            ks,
            "warn",
            "Expected kill-switch state while Armed",
        )
    else:
        rep.add("killswitch_state", "state file present", ks, "pass")
    _add_dns_armed_down(rep, dns_entries, gw)
    return


def cmd_expect_armed_up(rep: Report) -> None:
    real = _load_baseline_ip(rep)
    ip, err = public_ip()
    gw = gateway_ipv4()
    ks, ks_detail = killswitch_hint()
    dns_entries = dns_server_entries()
    st = load_json(KS_STATE_PATH) or {}
    eps = st.get("Endpoints") or []
    if isinstance(eps, dict):
        eps = [eps]
    ep_s = ", ".join(
        f"{e.get('Host')}->{e.get('IP')}:{e.get('Port')}/{e.get('Proto')}" for e in eps
    ) or "(none in state)"
    guests = guest_dns_targets(dns_entries, gw, for_armed_up=True)
    tunnel_dns = tunnel_dns_ips(dns_entries)
    base = load_json(BASELINE_PATH) or {}
    guest_gw = base.get("gateway") if isinstance(base.get("gateway"), str) else None
    # Ping the coffee-shop gateway from baseline — not the current default route (often tunnel).
    ping_target = guest_gw or None
    if ping_target:
        ping_ok, ping_detail = ping_host(ping_target)
    else:
        ping_ok, ping_detail = False, "no baseline gateway"
    rep.map_rows = [
        ("Mode", "expect ARMED + tunnel UP (road warrior)"),
        ("Baseline REAL IP", real or "(none)"),
        ("Public IPv4 now (egress)", ip or f"FAILED ({err})"),
        ("VPN endpoints (state)", ep_s),
        ("Local gateway (baseline)", guest_gw or "(unknown)"),
        ("Gateway ping", "ok" if ping_ok else f"fail ({ping_detail})"),
        *dns_map_rows(dns_entries),
        ("Tunnel DNS", format_dns_servers(tunnel_dns)),
        ("Guest DNS probes", format_dns_servers(guests)),
        ("Kill-switch hint", f"{ks} - {ks_detail}"),
        ("DNS", "tunnel DNS + system OK; guest DNS blocked"),
    ]
    if not ip:
        rep.add("vpn_egress", "public IP via VPN", str(err), "fail", "No internet via tunnel?")
        _add_dns_armed_up(rep, dns_entries, gw)
        return
    rep.add("vpn_egress", "public IP via VPN", ip, "pass")
    if real and ip == real:
        rep.add(
            "not_baseline",
            f"!= {real}",
            ip,
            "fail",
            "Egress still looks like baseline REAL IP",
        )
    elif real:
        rep.add("not_baseline", f"!= {real}", ip, "pass")
    else:
        rep.add("not_baseline", "baseline known", "no baseline", "warn")
    if ping_target and ping_ok:
        rep.add(
            "guest_lan_blocked",
            "ping fail",
            ping_detail,
            "fail",
            f"Guest LAN still reachable ({ping_target})",
        )
    elif ping_target:
        rep.add("guest_lan_blocked", "ping fail", ping_detail, "pass")
    else:
        rep.add(
            "guest_lan_blocked",
            "baseline gateway to ping",
            "none",
            "warn",
            "No baseline gateway — cannot probe guest LAN",
        )
    if "likely ARMED" not in ks:
        rep.add("killswitch_state", "state file present", ks, "warn")
    else:
        rep.add("killswitch_state", "state file present", ks, "pass")
    _add_dns_armed_up(rep, dns_entries, gw)


if __name__ == "__main__":
    sys.exit(main())
