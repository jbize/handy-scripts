#!/usr/bin/env python3
"""
Road-warrior path verifier for OpenVPN + kill switch.

Stdlib only. Prints a network "map" and a GO / NO-GO / UNCLEAR verdict.

Console:
  python verify_roadwarrior.pyw baseline
  python verify_roadwarrior.pyw expect-disarmed
  python verify_roadwarrior.pyw expect-armed-down
  python verify_roadwarrior.pyw expect-armed-up

Double-click (.pyw / pythonw): pick a mode in a window, then view the report.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TIMEOUT_SEC = 4.0


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
class Report:
    mode: str
    checks: List[Check] = field(default_factory=list)
    map_rows: List[Tuple[str, str]] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

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


def killswitch_hint() -> Tuple[str, str]:
    st = load_json(KS_STATE_PATH)
    if st and st.get("ConfigPath"):
        return "likely ARMED (state file present)", str(st.get("ConfigPath"))
    if KS_STATE_PATH.is_file():
        return "state file present", str(KS_STATE_PATH)
    return "no kill-switch state file", "Disarmed or non-Windows / other tool"


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
        )
        return proc.returncode == 0, f"exit={proc.returncode}"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def format_report(rep: Report) -> Tuple[str, int]:
    lines: List[str] = [
        "=" * 60,
        " ROAD WARRIOR MAP",
        "=" * 60,
    ]
    for label, value in rep.map_rows:
        lines.append(f"  {label:<22} {value}")
    lines.extend(["", "-" * 60, " CHECKS", "-" * 60])
    for c in rep.checks:
        mark = {"pass": "OK  ", "fail": "FAIL", "warn": "WARN", "info": "INFO"}.get(
            c.status, c.status
        )
        lines.append(f"  [{mark}] {c.name}")
        lines.append(f"         expected: {c.expected}")
        lines.append(f"         observed: {c.observed}")
        if c.detail:
            lines.append(f"         detail:   {c.detail}")
    lines.append("")
    fails = sum(1 for c in rep.checks if c.status == "fail")
    warns = sum(1 for c in rep.checks if c.status == "warn")
    if fails:
        verdict, color, code = "NO-GO", "NOT AS EXPECTED", 1
    elif warns and rep.mode.startswith("expect-"):
        verdict, color, code = "UNCLEAR", "WARNINGS", 2
    else:
        verdict, color, code = "GO", "AS EXPECTED FOR THIS MODE", 0
    lines.extend(
        [
            "=" * 60,
            f" VERDICT: {verdict} - {color}",
            "=" * 60,
        ]
    )
    if rep.reasons:
        lines.append(" Reasons:")
        lines.extend(f"  - {r}" for r in rep.reasons)
    else:
        lines.append(" Reasons: (none)")
    lines.append("")
    return "\n".join(lines), code


def console_available() -> bool:
    # pythonw.exe sets stdout to None; a normal/piped console should print.
    return sys.stdout is not None


def show_text_window(title: str, body: str) -> str:
    """Show report window. Returns 'exit' or 'menu'."""
    import tkinter as tk
    from tkinter import scrolledtext

    result: Dict[str, str] = {"action": "exit"}
    root = tk.Tk()
    root.title(title)
    root.geometry("720x520")
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
    import tkinter as tk

    choice: Dict[str, Optional[str]] = {"value": None}
    root = tk.Tk()
    root.title("Road warrior verifier")
    root.geometry("440x280")
    tk.Label(
        root,
        text="Choose the situation you believe you are in:",
        justify=tk.LEFT,
    ).pack(anchor=tk.W, padx=12, pady=(12, 8))

    options = [
        ("baseline", "Baseline (Disarmed) - record REAL public IP"),
        ("expect-disarmed", "Expect Disarmed - normal internet"),
        ("expect-armed-down", "Expect Armed + VPN down - kill switch"),
        ("expect-armed-up", "Expect Armed + VPN up - road warrior"),
    ]
    var = tk.StringVar(value="baseline")
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
    tk.Button(row, text="Cancel", width=10, command=_cancel).pack(side=tk.LEFT, padx=6)
    root.mainloop()
    return choice["value"]


def print_report(rep: Report) -> int:
    text, code = format_report(rep)
    if console_available():
        print(text)
        return code
    action = show_text_window("Road warrior verifier", text)
    if action == "menu":
        picked = pick_command_gui()
        if picked:
            return run_command(picked)
    return code


def cmd_baseline(rep: Report) -> int:
    ip, err = public_ip()
    gw = gateway_ipv4()
    ks, ks_detail = killswitch_hint()
    rep.map_rows = [
        ("Mode", "baseline (record REAL egress)"),
        ("Public IPv4", ip or f"FAILED ({err})"),
        ("Default gateway", gw or "(unknown)"),
        ("Kill-switch hint", f"{ks} - {ks_detail}"),
    ]
    if not ip:
        rep.add("public_ip", "reachable", f"error: {err}", "fail", "Cannot record baseline")
        return print_report(rep)
    save_baseline(ip, {"gateway": gw})
    rep.add("public_ip", "reachable", ip, "pass", f"Wrote {BASELINE_PATH}")
    rep.add("baseline_file", "saved", str(BASELINE_PATH), "info")
    ok443, detail = tcp_connect("1.1.1.1", 443)
    rep.add("tcp_443_probe", "success", detail, "pass" if ok443 else "warn")
    return print_report(rep)


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


def cmd_expect_disarmed(rep: Report) -> int:
    real = _load_baseline_ip(rep)
    ip, err = public_ip()
    gw = gateway_ipv4()
    ks, ks_detail = killswitch_hint()
    rep.map_rows = [
        ("Mode", "expect DISARMED / normal net"),
        ("Baseline REAL IP", real or "(none)"),
        ("Public IPv4 now", ip or f"FAILED ({err})"),
        ("Default gateway", gw or "(unknown)"),
        ("Kill-switch hint", f"{ks} - {ks_detail}"),
        ("DNS path", "uplink OK when disarmed"),
    ]
    if not ip:
        rep.add("public_ip", "reachable", str(err), "fail")
        return print_report(rep)
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
    return print_report(rep)


def cmd_expect_armed_down(rep: Report) -> int:
    real = _load_baseline_ip(rep)
    ip, err = public_ip()
    gw = gateway_ipv4()
    ks, ks_detail = killswitch_hint()
    ping_ok, ping_detail = ping_host(gw) if gw else (False, "no gateway")
    rep.map_rows = [
        ("Mode", "expect ARMED + tunnel DOWN (kill switch)"),
        ("Baseline REAL IP", real or "(none)"),
        ("Public IPv4 now", ip or f"blocked/failed ({err})"),
        ("Local gateway", gw or "(unknown)"),
        ("Gateway ping", "ok" if ping_ok else f"fail ({ping_detail})"),
        ("Kill-switch hint", f"{ks} - {ks_detail}"),
        ("DNS", "must NOT use uplink for general queries"),
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
    return print_report(rep)


def cmd_expect_armed_up(rep: Report) -> int:
    real = _load_baseline_ip(rep)
    ip, err = public_ip()
    gw = gateway_ipv4()
    ks, ks_detail = killswitch_hint()
    ping_ok, ping_detail = ping_host(gw) if gw else (False, "no gateway")
    st = load_json(KS_STATE_PATH) or {}
    eps = st.get("Endpoints") or []
    ep_s = ", ".join(
        f"{e.get('Host')}->{e.get('IP')}:{e.get('Port')}/{e.get('Proto')}" for e in eps
    ) or "(none in state)"
    rep.map_rows = [
        ("Mode", "expect ARMED + tunnel UP (road warrior)"),
        ("Baseline REAL IP", real or "(none)"),
        ("Public IPv4 now (egress)", ip or f"FAILED ({err})"),
        ("VPN endpoints (state)", ep_s),
        ("Local gateway", gw or "(unknown)"),
        ("Gateway ping", "ok" if ping_ok else f"fail ({ping_detail})"),
        ("Kill-switch hint", f"{ks} - {ks_detail}"),
        ("DNS", "via tunnel only when policy is correct"),
    ]
    if not ip:
        rep.add("vpn_egress", "public IP via VPN", str(err), "fail", "No internet via tunnel?")
        return print_report(rep)
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
    if gw and ping_ok:
        rep.add("guest_lan_blocked", "ping fail", ping_detail, "fail", "Guest LAN still reachable")
    elif gw:
        rep.add("guest_lan_blocked", "ping fail", ping_detail, "pass")
    if "likely ARMED" not in ks:
        rep.add("killswitch_state", "state file present", ks, "warn")
    else:
        rep.add("killswitch_state", "state file present", ks, "pass")
    return print_report(rep)


def run_command(command: str) -> int:
    rep = Report(mode=command)
    if command == "baseline":
        return cmd_baseline(rep)
    if command == "expect-disarmed":
        return cmd_expect_disarmed(rep)
    if command == "expect-armed-down":
        return cmd_expect_armed_down(rep)
    if command == "expect-armed-up":
        return cmd_expect_armed_up(rep)
    return 2


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv and not console_available():
        picked = pick_command_gui()
        if not picked:
            return 0
        return run_command(picked)

    parser = argparse.ArgumentParser(
        description="Verify road-warrior VPN + kill-switch posture (map + GO/NO-GO)."
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["baseline", "expect-disarmed", "expect-armed-down", "expect-armed-up"],
        help="What situation you believe you are in",
    )
    args = parser.parse_args(argv)
    if not args.command:
        if not console_available():
            picked = pick_command_gui()
            if not picked:
                return 0
            return run_command(picked)
        parser.error(
            "command is required (baseline | expect-disarmed | expect-armed-down | expect-armed-up)"
        )
    return run_command(args.command)


if __name__ == "__main__":
    sys.exit(main())
