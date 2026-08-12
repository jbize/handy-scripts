# OpenVPN Kill Switch

Windows Firewall **Arm / Disarm** kill switch for [OpenVPN GUI](https://openvpn.net/community-downloads/).

While **Armed**, outbound traffic is deny-by-default except:

- VPN server endpoint(s) from the selected `.ovpn` (any uplink — Wi‑Fi or Ethernet)
- DHCP (to keep a link)
- OpenVPN tunnel adapters (TAP / Wintun / DCO) — **all tunnel traffic including DNS** when the VPN is up

**No uplink/ISP DNS** while Armed. If `remote` is a **hostname** (DDNS), Arm resolves it **once**, pins it in the system hosts file (`C:\Windows\System32\drivers\etc\hosts`), and allows the VPN IP:port so OpenVPN can reconnect without local DNS. Disarm removes that hosts pin **only if this tool added it**. Hosts updates use a same-folder temp + `File.Replace` (never truncate-in-place); the previous hosts is saved as `hosts.handy-scripts.bak`, and Arm refuses to write if hosts is already empty.

Arm also **audits** the `.ovpn` for full-tunnel / DNS-related options and prints **warnings** if they look weak (does not block Arm).

Armed state **persists** across closing this UI, crashes, and reboots until you **explicitly Disarm**. The firewall policy is the kill switch; we do not watch VPN up/down events.

No binaries and no build step — one PowerShell script.

## Concept of operations

Think in **sessions**, not separate firewall vs VPN toggles.


| Step                      | Action            | What happens                                                                                    |
| ------------------------- | ----------------- | ----------------------------------------------------------------------------------------------- |
| Start a protected session | **Arm + Connect** | Apply kill-switch firewall rules for the selected profile, then ask OpenVPN GUI to connect it   |
| End the session           | **Disarm**        | Best-effort **disconnect** that Armed profile, then restore the saved pre-Arm firewall settings |


That pair is the normal mode of operation: enter protected mode on VPN, leave protected mode and drop that VPN.

**Disarm details (important):**

- Always restores the firewall (that is the kill switch).
- Also asks OpenVPN GUI to disconnect the profile recorded when you Armed (from the state file).
- If that profile is **already disconnected**, or OpenVPN GUI is not running, Disarm **still succeeds** — no error dialog, no “you weren’t connected” warning. Disconnect is best-effort.
- Disarm does **not** quit OpenVPN GUI, and does **not** disconnect other profiles you started only from the tray.

**Arm** (Advanced) is secondary: apply the kill switch first and connect from the OpenVPN tray yourself, or re-arm after a captive-portal login.

Closing this window does **not** Disarm — the session stays Armed until you press **Disarm**.

**Note:** Arm and Disarm rewrite many Windows Firewall rules. The UI shows a “please wait” status and may still be busy for a short time; that is expected. Wait for the success dialog before clicking again.

## Requirements

- Windows 10/11 with PowerShell 5.1+
- OpenVPN Community / OpenVPN GUI installed
- Administrator (UAC) to Arm or Disarm

## Run (ready to go)

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\path\to\handy-scripts\openvpn-killswitch\openvpn_killswitch.ps1" -Gui
```

Or omit `-Gui` — the script defaults to the GUI when no other action is specified. `-WindowStyle Hidden` avoids a PowerShell console behind the WinForms window (preferred over “Run minimized”). The script also hides its console when the GUI starts.

### Buttons

Hover any control for a tooltip. Layout:


| Area               | Controls                                             | Meaning                                              |
| ------------------ | ---------------------------------------------------- | ---------------------------------------------------- |
| **Main (session)** | **Arm + Connect**, **Disarm**                        | Start / end a protected session                      |
| **Advanced**       | **Arm**, **Refresh status**, **Retry tunnel allows** | Occasional real use (not the happy path)             |
| **Testing**        | Dry-run, endpoints, state folder, copy status        | Diagnostics only — not required for normal operation |



| Button                  | Meaning                                                                                                      |
| ----------------------- | ------------------------------------------------------------------------------------------------------------ |
| **Arm + Connect**       | Arm kill switch, connect profile, then auto-poll (~30s) for OpenVPN tunnel allows                            |
| **Disarm**              | Disconnect Armed profile (best-effort), restore firewall                                                     |
| **Arm**                 | Kill switch only — connect from the tray yourself (e.g. after captive portal)                                |
| **Refresh status**      | Reload the status log only — does not change firewall or VPN                                                 |
| **Retry tunnel allows** | If Armed + connected but traffic fails, add missing tunnel-adapter allows (usually auto after Arm + Connect) |


**Testing** controls: Dry-run Arm, Show endpoints, Open state folder, Copy status.

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
```

`-Disarm` always restores the firewall; it also best-effort disconnects the Armed profile when state knows which one that was.

## OpenVPN tray vs this tool

- **OpenVPN GUI** (system tray): right-click → configs (user folder first, then system, nested submenus), connect/disconnect.
- **This shortcut**: session control — Arm+Connect / Disarm — plus the same profile discovery rules.

See [PLAN.md](PLAN.md) for the living product plan and [TESTING.md](TESTING.md) for step-by-step verification (IP, DNS sites, simulated VPN outage).

## Automated verifier (Python)

`verify_roadwarrior.pyw` (stdlib only) prints a path **map** and a **GO / NO-GO / UNCLEAR** verdict.

Double-click the `.pyw` to pick a mode in a window (no console). Or from a terminal:

```powershell
cd openvpn-killswitch
python verify_roadwarrior.pyw baseline
python verify_roadwarrior.pyw expect-disarmed
python verify_roadwarrior.pyw expect-armed-down
python verify_roadwarrior.pyw expect-armed-up
```

Run `baseline` while Disarmed on the test network first. Then use the `expect-*` commands for the situation you believe you are in. Exit codes: `0` GO, `1` NO-GO, `2` UNCLEAR/warnings.

## Captive portal (hotel login)

1. **Disarm** (restores firewall and disconnects the Armed profile if it was up)
2. Complete the portal login in a browser
3. **Arm + Connect** (or **Advanced → Arm**, then connect from the tray)

## Reboot / Microsoft account sign-in

If you leave the kill switch **Armed** across reboot (intended), Windows may block internet until VPN reconnects or you Disarm — similar to Proton’s advanced kill switch. If you sign in with a Microsoft account that needs the network at login, create a **local offline Windows account** as a backup so you can Disarm after signing in without the cloud.

## State file

`%LOCALAPPDATA%\HandyScripts\openvpn_killswitch_state.json` — profile path, firewall restore data; used on Disarm.

## Emergency escape hatch (if Disarm fails)

**Normal recovery is Disarm.** When the state file is intact and Disarm can run elevated, it already:

1. Best-effort disconnects the Armed OpenVPN profile
2. Removes kill-switch rules (group `HandyScripts-OpenVPN-KillSwitch`)
3. Restores each profile’s saved `DefaultOutboundAction`
4. Re-enables the exact outbound Allow rules it disabled

Use the hatch below only when **Disarm cannot run**, the **state file is missing/corrupt**, or the UI/script will not start — break-glass, not the normal path.

Elevated PowerShell:

```powershell
# Unblock outbound (forces Allow — does not restore your prior saved values)
Set-NetFirewallProfile -Profile Domain,Private,Public -DefaultOutboundAction Allow

# Remove this tool's rules
Get-NetFirewallRule -Group 'HandyScripts-OpenVPN-KillSwitch' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
```

**Caveats vs Disarm:**


|                   | Disarm (normal)                           | This hatch                                    |
| ----------------- | ----------------------------------------- | --------------------------------------------- |
| Needs state JSON  | Yes (for full restore)                    | No                                            |
| Outbound default  | Restores what was saved at Arm            | Forces `Allow`                                |
| Other Allow rules | Re-enables the exact list disabled at Arm | Does **not** — some rules may remain Disabled |
| VPN               | Best-effort disconnect                    | No — use OpenVPN tray if needed               |


If outbound works again but some apps still misbehave, check Windows Firewall for rules left **Disabled**, or as a last resort use Windows Security → Firewall → **Restore defaults** (resets more than this tool).

Keeping a copy of the hatch on your phone (or another device) is useful if this PC has no network while Armed and stuck.