# OpenVPN Kill Switch

**Platform:** Windows

Windows Firewall **Arm / Disarm** kill switch for [OpenVPN GUI](https://openvpn.net/community-downloads/).

While **Armed**, outbound traffic is deny-by-default except:

- VPN server endpoint(s) from the selected `.ovpn` (any uplink — Wi‑Fi or Ethernet)
- DHCP (to keep a link)
- OpenVPN tunnel adapters (TAP / Wintun / DCO) — **all tunnel traffic including DNS** when the VPN is up

**No uplink/ISP DNS** while Armed. If `remote` is a **hostname** (DDNS), Arm resolves it **once**, pins it in the system hosts file (`C:\Windows\System32\drivers\etc\hosts`), and allows the VPN IP:port so OpenVPN can reconnect without local DNS. Disarm removes that hosts pin **only if this tool added it**. Hosts updates use a same-folder temp + `File.Replace` (never truncate-in-place); the previous hosts is saved as `hosts.handy-scripts.bak`, and Arm refuses to write if hosts is already empty.

Arm also **audits** the `.ovpn` for full-tunnel / DNS-related options and prints **warnings** if they look weak (does not block Arm).

Armed state **persists** across closing this UI, crashes, and reboots until you **explicitly Disarm**. The firewall policy is the kill switch; we do not watch VPN up/down events.

**Safety (Arm):** Allow rules are created **before** outbound is set to Block. **Re-Arm while already Armed is refused** — Disarm first (avoids overwriting restore data). If Arm fails after state is saved, the tool **auto-Disarms** when it can.

No binaries and no build step — one PowerShell script.

## Concept of operations

Think in **sessions**, not separate firewall vs VPN toggles.

| Step | Action | What happens |
| --- | --- | --- |
| Start a protected session | **Arm + Connect** | Apply kill-switch firewall rules for the selected profile, then ask OpenVPN GUI to connect it |
| End the session | **Disarm** | Best-effort **disconnect** that Armed profile, then restore the saved pre-Arm firewall settings |

That pair is the normal mode of operation: enter protected mode on VPN, leave protected mode and drop that VPN.

**Disarm details (important):**

- Always restores the firewall (that is the kill switch).
- Also asks OpenVPN GUI to disconnect the profile recorded when you Armed (from the state file).
- If that profile is **already disconnected**, or OpenVPN GUI is not running, Disarm **still succeeds** — no error dialog, no “you weren’t connected” warning. Disconnect is best-effort.
- Disarm does **not** quit OpenVPN GUI, and does **not** disconnect other profiles you started only from the tray.

**Arm** (Advanced) is secondary: apply the kill switch first and connect from the OpenVPN tray yourself (e.g. after a captive-portal login). If you are already Armed, Disarm first — a second Arm is refused.

Closing this window does **not** Disarm — the session stays Armed until you press **Disarm**.

**Note:** Arm and Disarm rewrite many Windows Firewall rules. The UI shows a “please wait” status and may still be busy for a short time; that is expected. Wait for the success dialog before clicking again.

## Requirements

- Windows 10/11 with PowerShell 5.1+
- OpenVPN Community / OpenVPN GUI installed
- Administrator (UAC) to Arm, Disarm, Export snapshot, or Restore from .bak

## Run (ready to go)

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\path\to\handy-scripts\openvpn-killswitch\openvpn_killswitch.ps1" -Gui
```

Or omit `-Gui` — the script defaults to the GUI when no other action is specified. `-WindowStyle Hidden` avoids a PowerShell console behind the WinForms window (preferred over “Run minimized”). The script also hides its console when the GUI starts.

### Buttons

Hover any control for a tooltip. Layout:

| Area | Controls | Meaning |
| --- | --- | --- |
| **Main (session)** | **Arm + Connect**, **Disarm** | Start / end a protected session |
| **Advanced** | **Arm**, **Refresh status**, **Retry tunnel allows** | Occasional real use (not the happy path) |
| **Testing** | Dry-run, endpoints, state folder, copy status, verifier, firewall snapshot | Diagnostics / recovery aids — not required for a normal session |

| Button | Meaning |
| --- | --- |
| **Arm + Connect** | Arm kill switch, connect profile, then auto-poll (~30s) for OpenVPN tunnel allows. Refused if already Armed — **Disarm first**. |
| **Disarm** | Disconnect Armed profile (best-effort), restore firewall |
| **Arm** | Kill switch only — connect from the tray yourself (e.g. after captive portal). Refused if already Armed — **Disarm first**. |
| **Refresh status** | Reload the status log only — does not change firewall or VPN |
| **Retry tunnel allows** | If Armed + connected but traffic fails, add missing tunnel-adapter allows (usually auto after Arm + Connect) |
| **Dry-run Arm** | Show what Arm would change — applies nothing |
| **Show endpoints** | Resolve `remote` lines from the selected `.ovpn` |
| **Open state folder** | `%LOCALAPPDATA%\HandyScripts\` (state, bak, dated snapshots) |
| **Copy status** | Copy the status log to the clipboard |
| **Open verifier** | Launch `verify_roadwarrior.pyw` (needs Python + script next to this tool) |
| **Export firewall snapshot** | While **Disarmed**: write `firewall_restore.bak` + dated snapshot (quick Disarm-like restore list). Disabled while Armed. |
| **Restore from .bak** | Disarm-like recovery from bak. Always asks **Are you sure?** (extra confirm when risky). Prefer **Disarm** when Armed with intact state. |

## Taskbar shortcut (recommended)

Do **not** commit a `.lnk` (paths are machine-specific). Create one locally:

1. Right-click the desktop → **New** → **Shortcut**
2. Location:
  ```text
   powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\Users\<you>\git\handy-scripts\openvpn-killswitch\openvpn_killswitch.ps1" -Gui
  ```
   Use your real full path to `openvpn_killswitch.ps1`. Include `-WindowStyle Hidden` so no console window appears.
3. Name it e.g. `OpenVPN Kill Switch`
4. Optional: shortcut **Properties** → **Advanced** → **Run as administrator**
  (The script also **self-elevates** via UAC when Arm/Disarm need admin.)
5. Pin that shortcut to the taskbar

Admin / UAC prompts are expected: changing Windows Firewall requires elevation.

## Profile discovery (matches OpenVPN GUI)

Profiles are discovered like OpenVPN GUI:

1. **User** first: `%USERPROFILE%\OpenVPN\config` (or `HKCU\SOFTWARE\OpenVPN-GUI\config_dir` if set)
2. **System** next: `C:\Program Files\OpenVPN\config` (from HKLM `config_dir`)
3. **Nesting**: top-level `*.ovpn` plus **one level** of subfolders (e.g. `john\john`, `ra4wvpn\…`)
4. **Default selection**: first profile in that order (your user profile when present)
5. **Duplicates**: same basename in two places is invalid in OpenVPN GUI; we keep the first (user wins) and skip later copies with a warning

The combo shows nested labels without `.ovpn`. `-ListConfigs` prints `Source`, display name, and full path (tab-separated).

## CLI

```powershell
# Status
.\openvpn_killswitch.ps1 -Status

# List profiles (Source, display name, full path)
.\openvpn_killswitch.ps1 -ListConfigs

# Start session / end session (Config may be display name, relative path, or full path)
.\openvpn_killswitch.ps1 -Arm -Config john\john -Connect
.\openvpn_killswitch.ps1 -Disarm

# Kill switch only (secondary)
.\openvpn_killswitch.ps1 -Arm -Config john\john

# Dry-run
.\openvpn_killswitch.ps1 -Arm -Config john\john -WhatIf

# Tunnel allows (while Armed)
.\openvpn_killswitch.ps1 -RetryTunnels

# Firewall bak (Export while Disarmed; Restore prompts unless -Force)
.\openvpn_killswitch.ps1 -ExportFirewallSnapshot
.\openvpn_killswitch.ps1 -RestoreFirewallBak
```

`-Disarm` always restores the firewall; it also best-effort disconnects the Armed profile when state knows which one that was.

## OpenVPN tray vs this tool

- **OpenVPN GUI** (system tray): right-click → configs (user folder first, then system, nested submenus), connect/disconnect.
- **This shortcut**: session control — Arm+Connect / Disarm — plus the same profile discovery rules.

See [PLAN.md](PLAN.md) for the living product plan and [TESTING.md](TESTING.md) for step-by-step verification (IP, DNS sites, simulated VPN outage).

## Automated verifier (Python)

`verify_roadwarrior.pyw` (stdlib only) is the coffee-shop check:

- Infers **STATE**: Disarmed / Armed+VPN down / Armed+VPN up
- Headline **PROTECTION**: `SAFE` / `SAFE (LOCKED DOWN)` / `NOT SAFE` / `UNPROTECTED` (colored green / yellow / red in the GUI)
- **BASELINE** freshness via default-gateway compare (deferred while VPN up — tunnel often owns the default route)
- Default action: **Check current protection** (infer STATE, then verify)
- **Baseline refuses to save** unless live STATE is Disarmed
- Busy “please wait” window while collecting; forced `expect-*` modes remain for deliberate tests

DNS probes: system resolve; guest/uplink DNS must fail while Armed (Armed+up skips well-known public resolvers and the tunnel default gateway); public `8.8.8.8` must fail when Armed + tunnel **down** (INFO only when up); tunnel-adapter DNS present when up.

Double-click the `.pyw` (or **Testing → Open verifier**) for the menu. Or from a terminal (no args = check-current):

```powershell
cd openvpn-killswitch
python verify_roadwarrior.pyw                 # infer STATE + verify protection
python verify_roadwarrior.pyw check-current
python verify_roadwarrior.pyw baseline
python verify_roadwarrior.pyw expect-armed-up # force a specific situation
```

Run `baseline` while **Disarmed** on the **test network** first (re-run whenever you change networks; always before Arm + Connect). Exit codes: `0` as expected, `1` not as expected, `2` warnings.

## Captive portal (hotel login)

1. **Disarm** (restores firewall and disconnects the Armed profile if it was up)
2. Complete the portal login in a browser
3. **Arm + Connect** (or **Advanced → Arm**, then connect from the tray)

## Reboot / Microsoft account sign-in

If you leave the kill switch **Armed** across reboot (intended), Windows may block internet until VPN reconnects or you Disarm — similar to Proton’s advanced kill switch. If you sign in with a Microsoft account that needs the network at login, create a **local offline Windows account** as a backup so you can Disarm after signing in without the cloud.

## State and firewall bak

| File | Role |
| --- | --- |
| `%LOCALAPPDATA%\HandyScripts\openvpn_killswitch_state.json` | Armed session: profile path, firewall restore data, hosts pins — used by **Disarm** |
| `%LOCALAPPDATA%\HandyScripts\firewall_restore.bak` | Latest Disarm-like snapshot (outbound defaults + **enabled** Allow rule **names**). Updated by **Export** and on every Arm. |
| `%LOCALAPPDATA%\HandyScripts\firewall_snapshot_YYYYMMDD-HHMMSS.json` | Dated copies of the same snapshot |

**Recovery order:**

1. **Disarm** (normal — uses state.json)
2. **Restore from .bak** (break-glass when state is missing/corrupt or Disarm cannot run). Export only while **Disarmed**. Always confirms in the GUI.
3. [Emergency escape hatch](#emergency-escape-hatch-if-disarm-fails) (phone note) — unblocks outbound; does **not** re-enable your other Allow rules

The bak is a **fast name list**, not a full dump of every firewall rule definition. Prefer Export while Disarmed before risky tests.

## Emergency escape hatch (if Disarm fails)

**Normal recovery is Disarm.** When the state file is intact and Disarm can run elevated, it already:

1. Best-effort disconnects the Armed OpenVPN profile
2. Removes kill-switch rules (group `HandyScripts-OpenVPN-KillSwitch`)
3. Restores each profile’s saved `DefaultOutboundAction`
4. Re-enables the exact outbound Allow rules it disabled

Use the hatch below only when **Disarm cannot run**, the **state file is missing/corrupt**, bak restore is unavailable, or the UI/script will not start — break-glass, not the normal path.

Elevated PowerShell:

```powershell
# Unblock outbound (forces Allow — does not restore your prior saved values)
Set-NetFirewallProfile -Profile Domain,Private,Public -DefaultOutboundAction Allow

# Remove this tool's rules
Get-NetFirewallRule -Group 'HandyScripts-OpenVPN-KillSwitch' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
```

**Caveats vs Disarm / bak:**

| | Disarm (normal) | Restore from .bak | This hatch |
| --- | --- | --- | --- |
| Needs state JSON | Yes (for full session restore) | No (uses bak) | No |
| Outbound default | Restores what was saved at Arm | Restores from bak | Forces `Allow` |
| Other Allow rules | Re-enables the list disabled at Arm | Re-enables names from bak | Does **not** — some rules may remain Disabled |
| VPN | Best-effort disconnect | Best-effort if state still present | No — use OpenVPN tray if needed |

If outbound works again but some apps still misbehave, check Windows Firewall for rules left **Disabled**, or as a last resort use Windows Security → Firewall → **Restore defaults** (resets more than this tool).

Keep a copy of the hatch on your phone (or another device) if this PC has no network while Armed and stuck.
