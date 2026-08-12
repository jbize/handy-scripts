# OpenVPN Kill Switch — verification test plan

Step-by-step checks so anyone can confirm traffic behaves as expected with this tool. Use on an **untrusted network** (guest Wi‑Fi, coffee shop, hotel) when possible; a trusted home LAN is fine for dry runs and Disarm recovery, but it will not prove “coffee shop LAN is blocked” as clearly.

Keep the [README emergency escape hatch](README.md#emergency-escape-hatch-if-disarm-fails) available (phone note) before live Arm tests.

## What “pass” means for this tool

| Situation | Expected |
| --- | --- |
| **Disarmed** | Normal Windows networking (your real public IP, normal DNS) |
| **Armed, VPN down** | Almost no useful internet. DHCP OK. **No uplink DNS.** VPN server IP:port allowed (hostname remotes pinned in hosts at Arm). Guest LAN blocked. |
| **Armed, VPN up** | Traffic (including DNS) via the tunnel. Public IP = VPN egress. Guest LAN blocked. |
| **Disarm** | Firewall restored; hosts pin removed if we added it; profile disconnected (best-effort). |

**DNS:** uplink resolvers are **not** allowed while Armed. Use `verify_roadwarrior.py` and leak sites when the tunnel is **up**; when down, general local DNS should fail.

WebRTC test sites often show **local** adapter addresses. Local/private IPs alone are not a kill-switch fail; a **public ISP IP** on WebRTC while you expect only VPN is more concerning (browser/WebRTC issue, not always fixed by this firewall tool).

### Automated helper

```powershell
python verify_roadwarrior.pyw baseline           # Disarmed first
python verify_roadwarrior.pyw expect-armed-down  # Armed, VPN disconnected
python verify_roadwarrior.pyw expect-armed-up    # Armed, VPN connected
python verify_roadwarrior.pyw expect-disarmed    # after Disarm
```

(Or double-click `verify_roadwarrior.pyw` and pick a mode.)

---

## 0. Prep

1. Create a Windows **restore point** (optional but recommended before first live Arm).
2. Note the escape hatch from the README on another device.
3. Join the test network; complete any **captive portal** while **Disarmed**.
4. Open the kill-switch GUI; confirm profile list (user first, e.g. `john\john`).
5. Open an elevated PowerShell for the command-line checks below (optional but useful).

Record a short log (paper or text file): date, network name, profile used, pass/fail per section.

### Handy PowerShell snippets

```powershell
# Public IPv4 (fails or hangs when blocked — that can be a PASS while Armed + VPN down)
try { (Invoke-RestMethod -Uri 'https://api.ipify.org' -TimeoutSec 8) } catch { $_.Exception.Message }

# Default outbound actions
Get-NetFirewallProfile | Select-Object Name, DefaultOutboundAction

# Kill-switch rules present?
Get-NetFirewallRule -Group 'HandyScripts-OpenVPN-KillSwitch' -ErrorAction SilentlyContinue |
  Select-Object DisplayName, Enabled, Direction, Action

# Ping examples (replace addresses as needed)
ping -n 2 1.1.1.1
ping -n 2 8.8.8.8
```

Useful browser sites (bookmark before Arming if you like):

| Site | Use |
| --- | --- |
| [https://api.ipify.org](https://api.ipify.org) / [https://ifconfig.me](https://ifconfig.me) | Quick public IP |
| [https://ipleak.net](https://ipleak.net) | IP + DNS + WebRTC overview |
| [https://dnsleaktest.com](https://dnsleaktest.com) | Standard / Extended DNS test |
| [https://browserleaks.com/ip](https://browserleaks.com/ip) | IPv4/IPv6 view |
| [https://browserleaks.com/webrtc](https://browserleaks.com/webrtc) | WebRTC candidates |
| [https://ipv6-test.com](https://ipv6-test.com) | Whether IPv6 is in play |

---

## 1. Baseline — Disarmed

**Goal:** Know what “normal” looks like on this Wi‑Fi.

1. Confirm kill switch shows **DISARMED**.
2. Record **public IP** (`api.ipify.org` or PowerShell snippet). Call this `IP_REAL`.
3. Optional: [dnsleaktest.com](https://dnsleaktest.com) → Standard test. Note resolvers (often the venue or ISP).
4. Optional: ping `1.1.1.1` — should succeed.
5. Optional: note guest gateway (e.g. `ipconfig` → Default Gateway). Call this `GW_LOCAL`.

**Pass:** Internet works; you have a documented `IP_REAL` (and optionally DNS/`GW_LOCAL`).

---

## 2. Dry-run (no firewall change)

1. In the GUI: **Show endpoints** for your profile — confirm VPN server IP/port look right.
2. **Dry-run Arm** — read the plan (Block outbound, allows for DHCP/DNS/VPN/tunnels). Apply nothing.

**Pass:** Plan matches expectations; still Disarmed; internet still works.

---

## 3. Arm only (VPN not connected) — core kill-switch test

**Goal:** Prove that without a tunnel, you do **not** get a normal internet path.

1. Select profile → **Arm** (not Arm + Connect). Confirm **ARMED**.
2. Leave OpenVPN **disconnected**.
3. Try:
   - Browser: `https://api.ipify.org` or any news site → should **fail** / spin / time out.
   - PowerShell `Invoke-RestMethod https://api.ipify.org` → should **fail**.
   - `ping 1.1.1.1` → should **fail** (or no replies).
4. Optional local-LAN check: `ping GW_LOCAL` (guest gateway) → should **fail** (untrusted LAN blocked).
5. Optional: you may still resolve some names via link DNS (`nslookup example.com`) even though browsing fails — **allowed by design**.

**Pass:** No usable general internet; status ARMED; you did not see `IP_REAL` load successfully via HTTPS.

**Then:** **Disarm** → confirm internet and `IP_REAL` return. (Validates recovery before harder tests.)

---

## 4. Arm + Connect — tunnel path

**Goal:** Protected session works end-to-end.

1. **Arm + Connect** with your real profile (e.g. `john\john`).
2. Wait until OpenVPN GUI shows connected. **Arm + Connect** auto-polls for tunnel allows (~30s). If traffic still fails, use **Advanced → Retry tunnel allows** once.
3. Record public IP again → call this `IP_VPN`.  
   **Pass:** `IP_VPN` ≠ `IP_REAL` (and matches your VPN/home egress expectation).
4. Browse a few HTTPS sites — should work.
5. Optional: reach a **VPN-side** resource (home NAS/printer IP) if you have one — should work via tunnel.
6. Optional: `ping GW_LOCAL` (guest gateway) — should still **fail**.

**Pass:** Internet works through VPN; public IP is VPN-side; guest LAN still blocked.

---

## 5. Browser leak / identity checks (Armed + connected)

Run with tunnel **up**. Interpret with the DNS design caveat above.

| Step | Action | Prefer |
| --- | --- | --- |
| 5a | [ipleak.net](https://ipleak.net) | Public IP ≈ `IP_VPN`, not `IP_REAL` |
| 5b | [dnsleaktest.com](https://dnsleaktest.com) → Extended | Resolvers are VPN/pushed DNS if your `.ovpn` forces that; **guest/ISP resolvers may appear** because this tool allows link DNS — note result, don’t panic solely on that |
| 5c | [browserleaks.com/ip](https://browserleaks.com/ip) | No surprise **IPv6** showing `IP_REAL` if you thought you were IPv4-only |
| 5d | [browserleaks.com/webrtc](https://browserleaks.com/webrtc) | Should not advertise `IP_REAL` as a public candidate; local/private IPs alone are OK |

**Pass (kill switch):** Public IP path is VPN.  
**Note separately:** DNS/WebRTC findings for hardening `.ovpn` / browser later if needed.

---

## 6. Simulated VPN outage (kill switch while “session” still Armed)

**Goal:** If the tunnel dies and you remain **Armed**, traffic must not fall back to the guest ISP path.

Do **not** press Disarm for this section.

1. Stay **Armed** and **connected**; confirm `IP_VPN` in a tab.
2. Simulate outage — pick one:
   - OpenVPN tray → **Disconnect** that profile only, **or**
   - OpenVPN GUI → disconnect, **or**
   - (Advanced) disable the OpenVPN tunnel adapter briefly in Adapter settings.
3. Immediately retry:
   - Reload `https://api.ipify.org` / news site → should **fail**.
   - PowerShell public-IP call → should **fail**.
   - You must **not** successfully load a page that shows `IP_REAL`.
4. Reconnect from the tray (still Armed) → `IP_VPN` returns; browsing works again.  
   If tunnels needed new allows: **Advanced → Retry tunnel allows**.

**Pass:** During outage, no return to `IP_REAL` over the guest network. Fail = any easy browse/IP check shows the real ISP IP while still Armed.

---

## 7. End session — Disarm

1. Press **Disarm**; confirm the dialog; elevate if asked.
2. Expect: profile disconnect (best-effort) + **DISARMED**.
3. Confirm:
   - Public IP is `IP_REAL` again (or current guest IP).
   - Browsing works without VPN.
   - `Get-NetFirewallRule -Group 'HandyScripts-OpenVPN-KillSwitch'` returns nothing (or empty).
   - Profiles’ `DefaultOutboundAction` look normal (typically Allow).

**Pass:** Full recovery without using the escape hatch.

---

## 8. Captive portal path (if the venue uses one)

1. **Disarm** if Armed.
2. Complete portal login in a browser.
3. **Arm + Connect** again.
4. Spot-check section 4 (public IP = VPN).

**Pass:** You can get through the portal only while Disarmed; protected session works after.

---

## 9. Optional stress / regression

| Test | How | Pass |
| --- | --- | --- |
| Close GUI while Armed | Exit kill-switch window; stay Armed | Internet still kill-switched; Disarm from a new GUI instance still works |
| Refresh status | **Refresh** after Arm/Disarm | Labels and log match reality |
| Wrong order | Connect from tray first, then **Advanced → Arm** | Arm still applies; use **Retry tunnel allows** after if needed |
| Sleep/wake (optional) | Sleep while Armed+connected | After wake: either still protected correctly or you Disarm/re-Arm intentionally — note behavior |

---

## 10. Results template

```text
Date / network:
Profile:
IP_REAL:
IP_VPN:

[ ] 1 Baseline Disarmed
[ ] 2 Dry-run
[ ] 3 Arm only — no general net; Disarm recovers
[ ] 4 Arm + Connect — IP_VPN; guest LAN blocked
[ ] 5 Leak sites (notes on DNS/WebRTC):
[ ] 6 VPN outage while Armed — no IP_REAL fallback
[ ] 7 Disarm end-session recovery
[ ] 8 Captive portal (if any)

Issues:
```

---

## Related docs

- [README.md](README.md) — concept of operations, escape hatch  
- [PLAN.md](PLAN.md) — product decisions  
