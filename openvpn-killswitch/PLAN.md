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

- **README “why a kill switch” lead** — Open with a short GitHub-friendly explanation: what a VPN kill switch is, why stock OpenVPN GUI alone can leak on disconnect/crash/reboot, and when this tool helps. Keep current how-it-works / session / install material after that.
- **Simplify main GUI for normal users** — Above the fold: status line, session strip (profile picker + **Arm + Connect** / **Disarm**), and the status log. Bottom row: **Open verifier** + **Exit**. Fold everything below the Arm/Disarm row that is Advanced/Testing (Arm-only, Refresh, Retry tunnels, dry-run, endpoints, state folder, copy, firewall bak export/restore) behind a single expand control so power tools stay one click away without owning the first impression.
- **Verifier UI color / readability (broader)** — Further leave default Tcl/Tk gray behind for the rest of the UI. **Already done:** PROTECTION headline is top-of-window with green / yellow / red. Prefer stdlib `tkinter`/`ttk`; ask before adding a new GUI toolkit.

## Safety invariants (non-negotiable)

These must hold in any implementation (PowerShell today, Python later). Treat as pass/fail for a rewrite — not optional polish.

| Invariant | Requirement |
| --- | --- |
| Refuse re-Arm | If already Armed, Arm fails; Disarm first (protects restore snapshot) |
| Arm order | Snapshot restore data → hosts pins → write state + `firewall_restore.bak` → **create allow rules** → disable foreign allows → DefaultOutboundAction=Block |
| Allows before Block | Never set Block before KS allows exist for this Arm attempt |
| Failed Arm recovery | If Arm fails **after** state is saved → **auto-Disarm** when possible; if fails **before** state save → undo hosts from this attempt |
| Hosts pins | Resolve hostname remotes once at Arm; pin in system hosts; Disarm removes **only** pins this tool added; refuse write if hosts file is empty/wiped |
| Disarm order | Best-effort disconnect Armed profile **first** (while still Armed), then restore firewall/hosts/state |
| Disarm tolerance | Already disconnected / GUI missing → Disarm still succeeds (no error about connection state) |
| Disconnect scope | Only the profile recorded in Arm state; do not quit OpenVPN GUI; do not disconnect unrelated tray profiles |
| Close ≠ Disarm | Closing the GUI must not Disarm |
| No uplink DNS | While Armed: no ISP/uplink DNS allows; DNS via tunnel adapters when up |
| Bak export | Export / refresh of `firewall_restore.bak` only while **Disarmed** (Armed export can snapshot a locked-down policy) |
| Bak restore | Prefer normal Disarm when Armed with intact state; Are you sure? + extra confirm when risky; recovery order: Disarm → Restore from .bak → README emergency hatch |
| Persistence | Armed until **explicit** Disarm (survives GUI close, crash, reboot) |

`TESTING.md` is the acceptance suite for these invariants (especially road-warrior / leak cases). A rewrite is not done until those procedures still make sense and pass.

## Future rewrite / handoff (e.g. Python)

Keep this plan a **product + safety contract**, not a line-by-line port guide. Do **not** grow it into UI pixel specs or PowerShell cmdlet lists.

**When ready to redo the Arm/Disarm tool in Python**, ask roughly:

> Redo the kill switch in Python with **parity on safety invariants**. Prefer stdlib where practical; ask before adding new GUI/tooling dependencies. Use PLAN + current `openvpn_killswitch.ps1` + `TESTING.md` (+ README caveats/emergency hatch).

**Handoff pack (in order):**

1. This `PLAN.md` (especially **Safety invariants** and concept of operations)
2. Current `openvpn_killswitch.ps1` as the **safety oracle** (verify behavior; do not blind-translate)
3. `TESTING.md` as acceptance tests
4. README caveats / emergency hatch

**PLAN alone is not enough** for faithful safety parity — the script and tests encode sharp edges discovered in use.

Until that handoff happens, “Python GUI for the kill-switch Arm/Disarm tool” stays out of scope below; flipping it is an intentional scope change.

## Explicitly not in scope (unless revisited)

- Compiling / shipping binaries
- Python GUI for the kill-switch Arm/Disarm tool (WinForms stays; verifier is already Python) — see **Future rewrite / handoff** when ready to flip this
- Auto-disarm on VPN disconnect or reboot
- Deep recursion beyond OpenVPN GUI’s one-level subfolders
- Separate “Disarm only (keep VPN)” control (add only if a real need appears)
- Full `.wfw` policy museum dump as the primary bak (JSON name-list bak is the quick restore; netsh `.wfw` is best-effort on Export)
