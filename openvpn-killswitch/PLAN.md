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

## Control model

| Decision | Choice |
| --- | --- |
| Control plane | Windows Firewall Arm / Disarm (not VPN link up/down detection) |
| While Armed | Deny-by-default outbound except VPN `remote` endpoints, DHCP, and OpenVPN tunnel adapters. **No uplink DNS.** Hostname remotes: resolve once at Arm → hosts pin (Disarm removes only if we added). Arm audits `.ovpn` (WARN if full-tunnel/DNS options look weak). |
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
- **Main:** Arm + Connect, Disarm (session start/end); hover tooltips on controls
- **Advanced:** Arm, Refresh status, Retry tunnel allows (occasional operational use)
- **Testing:** Dry-run Arm, Show endpoints, Open state folder, Copy status (diagnostics only)
- After Arm + Connect: auto-poll ~30s for OpenVPN tunnel adapter allow rules
- Verifier: `verify_roadwarrior.pyw` (baseline / expect-disarmed / expect-armed-down / expect-armed-up)
- CLI: `-Arm -Config … [-Connect]`, `-Disarm`, `-Status`, `-ListConfigs`, `-WhatIf`
- Closing the GUI does **not** Disarm

## State

`%LOCALAPPDATA%\HandyScripts\openvpn_killswitch_state.json` — restore firewall settings on Disarm; holds Armed `ConfigPath` for disconnect.

Firewall rule group: `HandyScripts-OpenVPN-KillSwitch`.

## Documented caveats

- Microsoft-account / network-at-login when Armed across reboot (local offline account as backup)
- Captive portal: Disarm → complete portal → Arm + Connect
- `.lnk` paths are machine-specific — do not commit
- Emergency escape hatch in README — only if Disarm/state fail (not a substitute for Disarm)
- Verification procedures: [TESTING.md](TESTING.md) (includes DNS design caveat: link resolvers allowed while Armed)

## Repo layout

One directory per tool with its own `README.md`; root `README.md` is an index only.

## Explicitly not in scope (unless revisited)

- Compiling / shipping binaries
- Python GUI for this Windows tool
- Auto-disarm on VPN disconnect or reboot
- Deep recursion beyond OpenVPN GUI’s one-level subfolders
- Separate “Disarm only (keep VPN)” control (add only if a real need appears)
