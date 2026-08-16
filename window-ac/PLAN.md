# Window AC control — plan of record

Living plan for `window-ac/`. Update this when behavior or scope changes.

How to run it: `window-ac/README.md`.

## Goal

A click-to-open Windows panel that talks to the Hykolity window AC on the local LAN. Closing the window stops all polling. **No background service, no tray resident, no always-on server.**

## Device

| Field | Value |
| --- | --- |
| Brand | hykolity |
| Model | ACB-2608 |
| Type | 8,000 BTU WiFi window AC |
| Phone app | **Smart Life** (blue house icon). Region: Canada/United States. |
| Data center (wizard only) | **Western America** (`us`) |
| Also on the phone | Google Home on/off |
| Setpoint | 61–88°F (16–31°C) |
| Protocol | Tuya LAN **3.4** (3.3 fails on this unit) |
| Display name in config | Master Bedroom |

**Phone app is primary.** The script does not unpair, re-pair, reset Wi‑Fi, or unlink Google Home.

## What the panel does

| Control | Notes |
| --- | --- |
| Identity | Name + brand/model at the top |
| Power | On / off |
| Room / setpoint | From the AC; refreshed while the window is open |
| °F / °C | **Panel display and ±1° step only.** Saved as `display_unit` in `config.json`. This firmware ignores a local Celsius command; Smart Life has no scale switch. |
| Cooler / Warmer | ±1° in the selected display unit. Disabled in dry and fan-only. |
| Mode | Cool, Dry, Fan, Auto (no Heat — the unit rejects it) |
| Fan | Auto, Low, Med, High |
| Humidity | Shown only if the unit reports a reading (this unit currently does not) |
| More | Diagnostics and one-time setup (config folder, TinyTuya import, Tuya IoT site) |

Poll while open: about **10 seconds**. One local session while the window is open; last command wins if phone and PC both change the unit.

Config is a **list of devices** (one entry now). The UI still controls the default device only.

## Launch / taskbar

`.pyw` so double-click has no console. User-created `.lnk` (gitignored).

Shortcut **Target** must be `pyw.exe` or `pythonw.exe` followed by the path to `window_ac.pyw`. A shortcut that points at the `.pyw` file alone pins to the Python launcher with no script.

Do not copy or rename `pythonw.exe` to make a unique host — AVG flags that as a Python sideload.

## Control model

| Decision | Choice |
| --- | --- |
| Library | **TinyTuya** (local LAN after one-time key grab) |
| Live traffic | PC ↔ AC on LAN only. Not Tuya cloud, not Google. |
| UI | Python **Tkinter** |
| Secrets | `%APPDATA%\HandyScripts\window-ac\config.json` only; never commit keys |
| IP | Saved after first successful connect; TinyTuya can also find by device id |
| Wizard files | `devices.json` / `tinytuya.json` in AppData are provisioning only |

Re-run the wizard or QR key tool only if the AC is factory-reset or re-paired (local key changes).

## Out of scope

- Background / startup / always-running monitor
- Home Assistant
- Heat, eco, sleep, timer (this unit does not accept those locally)
- Changing the AC’s own °F/°C (firmware rejects it)
- Control from outside the home network
- Multi-unit UI (storage is already a list)
- Amazon product-page scraping
