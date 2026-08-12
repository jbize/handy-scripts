# OpenVPN Kill Switch — plan of record

Living product plan for `openvpn-killswitch/`. Update this when behavior or scope changes.

## Goal

Windows Firewall **Arm / Disarm** kill switch usable with **OpenVPN GUI**, for home OpenVPN (and later commercial `.ovpn` providers). No binaries, no build step — one PowerShell script with a WinForms UI.

## Concept of operations

A **session** is the normal unit of work:

1. **Arm + Connect** — enter protected mode and connect the selected profile  
2. **Disarm** — end the session: best-effort disconnect that Armed profile, then restore firewall  

| Decision | Choice |
| --- | --- |
| Primary start | **Arm + Connect** (green) |
| Primary end | **Disarm** (red) — disconnect Armed profile (best-effort) + restore firewall |
| Secondary | **Arm** only (connect from tray yourself); **Refresh** |
| Disarm if not connected | Still Disarm; no error / no warning about connection state |
| Disconnect scope | Only the profile recorded in Arm state — not other tray connections; do not quit OpenVPN GUI |
| Disconnect order | Disconnect first (while still Armed), then open firewall |
| Closing the GUI | Does **not** Disarm |
| Re-Arm while Armed | **Refused** — Disarm first (protects restore snapshot) |

## Control model

| Decision | Choice |
| --- | --- |
| Control plane | Windows Firewall Arm / Disarm (not VPN link up/down detection) |
| While Armed | Deny-by-default outbound except VPN `remote` endpoints, DHCP, and OpenVPN tunnel adapters. **No uplink DNS.** Hostname remotes: resolve once at Arm → hosts pin (Disarm removes only if we added). Arm audits `.ovpn` (WARN if full-tunnel/DNS options look weak). |
| Arm ordering | Snapshot restore → hosts pins → write state + `firewall_restore.bak` → **create allow rules** → disable foreign allows → Block. Failed Arm after state save **auto-Disarms** when possible. |
| Local LAN on untrusted uplink | **Blocked** (no coffee-shop/hotel LAN) |
| VPN-side LAN (e.g. home printer) | Via **tunnel** only |
| Uplink | Not pinned to one NIC (Wi‑Fi or Ethernet) |
| Persistence | Armed until **explicit Disarm** (survives GUI close, crash, reboot) |
| Taskbar | User-created `.lnk` with `-WindowStyle Hidden`; script self-elevates |

## Profile discovery (match OpenVPN GUI)

| Rule | Behavior |
| --- | --- |
| Scan order | **User** first: `%USERPROFILE%\OpenVPN\config` (or `HKCU\…\OpenVPN-GUI\config_dir`), then **system**: HKLM / `C:\Program Files\OpenVPN\config` |
| Nesting | Top-level `*.ovpn` plus **immediate** subdirectories only (same as OpenVPN GUI) |
| UI labels | Relative display names: `profile` or `folder\profile` (no `.ovpn` suffix) |
| Default selection | First profile in scan order → user profiles default when present (e.g. `john\john`) |
| Duplicate basenames | OpenVPN GUI requires unique `.ovpn` **file names** across all folders/roots. We **keep the first** (user wins) and **skip + warn** later duplicates |
| Connect / Disconnect | `openvpn-gui.exe --connect` / `--command disconnect` with **basename** (why names must be unique) |

`config-auto` / persistent service profiles are out of scope unless we explicitly add them later.

## UI / CLI surface

- Default: WinForms GUI (`-Gui`)
- **Main:** Arm + Connect, Disarm (session start/end); hover tooltips
- **Advanced:** Arm, Refresh status, Retry tunnel allows
- **Testing:** Dry-run Arm, Show endpoints, Open state folder, Copy status, Open verifier, **Export firewall snapshot**, **Restore from .bak**
- After Arm + Connect: auto-poll ~30s for OpenVPN tunnel adapter allow rules
- Verifier: `verify_roadwarrior.pyw` — default **check-current**; STATE + BASELINE freshness + PROTECTION (SAFE / NOT SAFE / UNPROTECTED, colored in GUI); Baseline refuses unless Disarmed; busy indicator; force expect-* available
- CLI: `-Arm -Config … [-Connect]`, `-Disarm`, `-Status`, `-ListConfigs`, `-WhatIf`, `-RetryTunnels`, `-ExportFirewallSnapshot`, `-RestoreFirewallBak [-Force]`
- Closing the GUI does **not** Disarm

## State and bak

| Path | Role |
| --- | --- |
| `%LOCALAPPDATA%\HandyScripts\openvpn_killswitch_state.json` | Armed session restore + ConfigPath for disconnect |
| `%LOCALAPPDATA%\HandyScripts\firewall_restore.bak` | Latest Disarm-like snapshot (defaults + enabled Allow **names**); updated on Export and Arm |
| `%LOCALAPPDATA%\HandyScripts\firewall_snapshot_*.json` | Dated snapshot copies |

Firewall rule group: `HandyScripts-OpenVPN-KillSwitch`.

**Recovery order:** Disarm → Restore from .bak (with Are you sure? / extra confirm when risky) → emergency hatch in README.

Export only while **Disarmed**. Prefer Disarm over bak when Armed with intact state.

## Documented caveats

- Microsoft-account / network-at-login when Armed across reboot (local offline account as backup)
- Captive portal: Disarm → complete portal → Arm + Connect
- `.lnk` paths are machine-specific — do not commit
- Emergency escape hatch in README — only if Disarm/state/bak fail (not a substitute for Disarm)
- Verification procedures: [TESTING.md](TESTING.md)

## Repo layout

One directory per tool with its own `README.md`; root `README.md` is an index only.

## Backlog (pinned)

- **Verifier UI color / readability (broader)** — Further leave default Tcl/Tk gray behind for the rest of the UI. **Already done:** PROTECTION headline is top-of-window with green / yellow / red. Prefer stdlib `tkinter`/`ttk`; ask before adding a new GUI toolkit.

## Explicitly not in scope (unless revisited)

- Compiling / shipping binaries
- Python GUI for the kill-switch Arm/Disarm tool (WinForms stays; verifier is already Python)
- Auto-disarm on VPN disconnect or reboot
- Deep recursion beyond OpenVPN GUI’s one-level subfolders
- Separate “Disarm only (keep VPN)” control (add only if a real need appears)
- Full `.wfw` policy museum dump as the primary bak (JSON name-list bak is the quick restore; netsh `.wfw` is best-effort on Export)
