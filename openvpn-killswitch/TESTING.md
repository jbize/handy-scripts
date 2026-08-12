# OpenVPN Kill Switch — verification test plan

Step-by-step checks so anyone can confirm traffic behaves as expected with this tool. Use on an **untrusted network** (guest Wi‑Fi, coffee shop, hotel) when possible; a trusted home LAN is fine for dry runs and Disarm recovery, but it will not prove “coffee shop LAN is blocked” as clearly.

Before live Arm tests: keep the [README emergency escape hatch](README.md#emergency-escape-hatch-if-disarm-fails) on another device, and run **Testing → Export firewall snapshot** while **Disarmed** (writes `firewall_restore.bak`).

## What “pass” means for this tool

Coffee-shop verdict from the verifier: **STATE** (Disarmed / Armed+VPN down / Armed+VPN up, auto-inferred), **BASELINE** freshness (default gateway vs saved — deferred while VPN up), and **PROTECTION** (**SAFE** / **SAFE (LOCKED DOWN)** / **NOT SAFE** / **UNPROTECTED**). Default run is **Check current protection**. Forced expect-* modes remain for deliberate tests.

| Situation | Expected |
| --- | --- |
| **Disarmed** | Normal Windows networking (your real public IP, normal DNS) — **UNPROTECTED** |
| **Armed, VPN down** | Almost no useful internet. DHCP OK. **No uplink DNS.** VPN server IP:port allowed (hostname remotes pinned in hosts at Arm). Guest LAN blocked. — **SAFE (LOCKED DOWN)** if no leak |
| **Armed, VPN up** | Traffic (including DNS) via the tunnel. Public IP = VPN egress. Guest LAN blocked. — **SAFE** if no leak |
| **Disarm** | Firewall restored; hosts pin removed if we added it; profile disconnected (best-effort). |

**DNS:** uplink resolvers are **not** allowed while Armed. Prefer `verify_roadwarrior.pyw` DNS checks over leak websites; optional browser sites remain secondary.

WebRTC test sites often show **local** adapter addresses. Local/private IPs alone are not a kill-switch fail; a **public ISP IP** on WebRTC while you expect only VPN is more concerning (browser/WebRTC issue, not always fixed by this firewall tool).

### Automated helper

```powershell
python verify_roadwarrior.pyw                  # Check current protection (infer STATE)
python verify_roadwarrior.pyw check-current
python verify_roadwarrior.pyw baseline           # Disarmed on THIS network, before Arm + Connect
python verify_roadwarrior.pyw expect-armed-down  # force: Armed, VPN disconnected
python verify_roadwarrior.pyw expect-armed-up    # force: Armed, VPN connected
python verify_roadwarrior.pyw expect-disarmed    # force: after Disarm
```

(Or double-click `verify_roadwarrior.pyw` — PROTECTION banner first; default action is Check current. Baseline refuses unless Disarmed. Re-run **baseline** whenever you change networks.)

---

## 0. Prep

1. Create a Windows **restore point** (optional but recommended before first live Arm).
2. Note the [escape hatch](README.md#emergency-escape-hatch-if-disarm-fails) on another device.
3. Join the test network; complete any **captive portal** while **Disarmed**.
4. Open the kill-switch GUI; confirm profile list (user first, e.g. `john\john`).
5. Confirm **DISARMED**, then **Testing → Export firewall snapshot** (creates/updates `firewall_restore.bak` + dated copy under `%LOCALAPPDATA%\HandyScripts\`).
6. Optional: elevated PowerShell for the snippets below.

Record a short log: date, network name, profile used, pass/fail per section.

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

**Goal:** Know what “normal” looks like on **this** Wi‑Fi. Re-run after changing networks; always do this **before Arm + Connect**.

1. Confirm kill switch shows **DISARMED**.
2. Run `python verify_roadwarrior.pyw baseline` (or Baseline in the verifier GUI). It **refuses** if not Disarmed. Record **public IP** as `IP_REAL`.
3. Optional: [dnsleaktest.com](https://dnsleaktest.com) → Standard test. Note resolvers (often the venue or ISP).
4. Optional: ping `1.1.1.1` — should succeed.
5. Optional: note guest gateway (e.g. `ipconfig` → Default Gateway). Call this `GW_LOCAL`.

**Pass:** Internet works; documented `IP_REAL` (and optionally DNS/`GW_LOCAL`); verifier baseline saved.

---

## 2. Dry-run (no firewall change)

1. **Show endpoints** for your profile — confirm VPN server IP/port look right.
2. **Dry-run Arm** — read the plan (create allows first, then Block; DHCP / VPN / tunnels; no uplink DNS). Apply nothing.

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
4. Optional: `ping GW_LOCAL` (guest gateway) → should **fail**.
5. Optional: `nslookup example.com` and `nslookup example.com 8.8.8.8` should **fail**. Prefer verifier `expect-armed-down` / Check current.

**Pass:** No usable general internet; status ARMED; you did not see `IP_REAL` via HTTPS.

**Then:** **Disarm** → confirm internet and `IP_REAL` return.

---

## 4. Arm + Connect — tunnel path

**Goal:** Protected session works end-to-end.

1. **Arm + Connect** with your real profile (e.g. `john\john`).
2. Wait until OpenVPN GUI shows connected. Auto-poll for tunnel allows (~30s). If traffic still fails: **Advanced → Retry tunnel allows** once.
3. Record public IP → `IP_VPN`. **Pass:** `IP_VPN` ≠ `IP_REAL`.
4. Browse a few HTTPS sites — should work.
5. Optional: VPN-side LAN resource — should work via tunnel.
6. Optional: `ping GW_LOCAL` — should still **fail**.
7. Run verifier **Check current protection** (or `check-current`) → expect **STATE: Armed + VPN up**, **PROTECTION: SAFE**.

**Pass:** Internet via VPN; guest LAN blocked; verifier SAFE.

---

## 5. Browser leak / identity checks (Armed + connected)

**Primary:** verifier Armed+up checks (system DNS via tunnel; guest DNS blocked; public `8.8.8.8` via tunnel is INFO, not a leak).

Optional browser sites (secondary):

| Step | Action | Prefer |
| --- | --- | --- |
| 5a | [ipleak.net](https://ipleak.net) | Public IP ≈ `IP_VPN`, not `IP_REAL` |
| 5b | [dnsleaktest.com](https://dnsleaktest.com) → Extended | VPN-pushed DNS, not guest/ISP — trust the verifier first if they disagree |
| 5c | [browserleaks.com/ip](https://browserleaks.com/ip) | No surprise **IPv6** showing `IP_REAL` |
| 5d | [browserleaks.com/webrtc](https://browserleaks.com/webrtc) | Should not advertise `IP_REAL` as a public candidate; local/private IPs alone are OK |

**Pass:** Public IP path is VPN; verifier DNS checks OK.  
**Note:** WebRTC findings are often browser-side, not fixed by this firewall tool.

---

## 6. Simulated VPN outage (kill switch while still Armed)

**Goal:** If the tunnel dies and you remain **Armed**, traffic must not fall back to the guest ISP path.

Do **not** press Disarm for this section.

1. Stay **Armed** and **connected**; confirm `IP_VPN`.
2. Simulate outage: OpenVPN tray → **Disconnect** that profile (or disable the tunnel adapter briefly).
3. Immediately retry public IP / news sites → should **fail**. Must **not** show `IP_REAL`.
4. Optional: verifier Check current → **Armed + VPN down**, **SAFE (LOCKED DOWN)**.
5. Reconnect from the tray (still Armed) → `IP_VPN` returns. **Retry tunnel allows** if needed.

**Pass:** During outage, no return to `IP_REAL` over the guest network.

---

## 7. Re-Arm refuse (safe regression)

**Goal:** Confirm a second Arm does not overwrite restore data.

1. While **ARMED**, press **Arm** or **Arm + Connect** again.
2. **Pass:** Clear error — already Armed, Disarm first. Firewall/session unchanged.
3. Do **not** need to force a successful re-Arm (that path is intentionally blocked).

---

## 8. End session — Disarm

1. Press **Disarm**; confirm; elevate if asked.
2. Expect: profile disconnect (best-effort) + **DISARMED**.
3. Confirm: public IP ≈ `IP_REAL`; browsing works; no rules in group `HandyScripts-OpenVPN-KillSwitch`; `DefaultOutboundAction` looks normal.
4. Optional: verifier Check current → **Disarmed**, **UNPROTECTED**.

**Pass:** Full recovery without bak restore or the escape hatch.

---

## 9. Captive portal path (if the venue uses one)

1. **Disarm** if Armed.
2. Complete portal login in a browser.
3. **Arm + Connect** again.
4. Spot-check section 4 (public IP = VPN).

**Pass:** Portal only while Disarmed; protected session works after.

---

## 10. Optional stress / recovery notes

| Test | How | Pass |
| --- | --- | --- |
| Close GUI while Armed | Exit kill-switch window; stay Armed | Still kill-switched; Disarm from a new GUI instance still works |
| Refresh status | **Refresh** after Arm/Disarm | Labels and log match reality |
| Export while Armed | Try **Export firewall snapshot** | Button disabled / refused — export only while Disarmed |
| Restore from .bak | Only if practicing break-glass | Are you sure? dialog; prefer Disarm when Armed with intact state; extra confirm when risky |
| Sleep/wake (optional) | Sleep while Armed+connected | Note behavior; Disarm if stuck |

**Recovery order if something goes wrong:** Disarm → Restore from .bak → [escape hatch](README.md#emergency-escape-hatch-if-disarm-fails).

---

## 11. Results template

```text
Date / network:
Profile:
IP_REAL:
IP_VPN:
Firewall bak exported (Disarmed)? Y/N

[ ] 0 Prep + Export snapshot
[ ] 1 Baseline Disarmed
[ ] 2 Dry-run
[ ] 3 Arm only — no general net; Disarm recovers
[ ] 4 Arm + Connect — IP_VPN; verifier SAFE
[ ] 5 Leak sites (notes):
[ ] 6 VPN outage while Armed — no IP_REAL fallback
[ ] 7 Re-Arm refuse
[ ] 8 Disarm end-session recovery
[ ] 9 Captive portal (if any)

Issues:
```

---

## Related docs

- [README.md](README.md) — concept of operations, bak, escape hatch  
- [PLAN.md](PLAN.md) — product decisions  
