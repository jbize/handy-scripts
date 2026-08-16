# Window AC

**Platform:** Windows

A small click-to-open panel for a Tuya / Smart Life window air conditioner on the local Wi‑Fi.

It shows room temperature and setpoint, with on/off, warmer/cooler, mode, and fan speed. Closing the window stops the script. Nothing runs in the background.

The Smart Life phone app stays the primary control. This tool does not unpair the AC or unlink Google Home.

Typical use: double-click `window_ac.pyw` (or a taskbar shortcut) when you want the app. Hover a button for a short tip. Setup extras sit behind **More**.

## App window

Always visible:

| Control | What it does |
| --- | --- |
| Room / Set to | Temperatures from the AC |
| **°F** / **°C** | Display and setpoint step on this panel. Saved between launches. Does not change the AC's own display or Smart Life (this firmware ignores a local °C command). |
| **Turn On** / **Turn Off** | Power |
| **Cooler** / **Warmer** | Setpoint ±1° in the selected unit. Disabled in dry and fan-only modes. |
| **Mode** | Cool, Dry, Fan, Auto |
| **Fan** | Auto, Low, Med, High |
| Humidity | Shown only if the unit reports a reading |
| Status line | Connected, retrying, or a setup hint |
| **More** | Shows or hides diagnostics and one-time setup |

Under **More → Diagnostics** (anytime):

| Button | What it does |
| --- | --- |
| **Copy status (no keys)** | Clipboard dump of temperatures and data points. Omits device id and local key. |
| **Reload config** | Re-reads `config.json` after you edit it. |

Under **More → Setup (once)** (provisioning; you should not need these afterward):

| Button | Provisioning | What it does |
| --- | --- | --- |
| **Open config folder** | Both | Opens `%APPDATA%\HandyScripts\window-ac\` (`config.json`). |
| **Import TinyTuya devices.json** | TinyTuya wizard path only | Copies device id and local key from `devices.json` into `config.json`. After QR login, paste those fields yourself and skip this. |
| **Open Tuya IoT (key setup)** | TinyTuya wizard path only | Opens [iot.tuya.com](https://iot.tuya.com/). Not used for QR login. |

The unit can also expose heat, sleep, eco, and a timer on some Tuya models. This hykolity window AC does not accept those locally, so they are not on the panel.

## Requirements

- Windows, Python 3 (Tkinter is included with the usual installer)
- `.pyw` files opening with `pythonw` (no console)
- `pip install tinytuya`
- PC on the **same LAN** as the AC
- The AC already in **Smart Life** (this tool does not pair Wi‑Fi)

## How it works

The AC already has a **device id** and a **local key** (a 16-character LAN password). Smart Life does not show the local key. Once those two values are in `config.json`, the panel talks to the AC on the LAN only. It does not use Tuya’s cloud or Google for live control.

A LAN scan can find the device. It cannot read the local key. Getting that key is the whole provisioning step.

## Files

### In this folder (git)

| File | Used by the panel? | What it is |
| --- | --- | --- |
| `window_ac.pyw` | Yes | The panel. |
| `config.example.json` | Yes, once | Template copied to AppData as `config.json` on first run (`device_id` / `local_key` empty). Edit name, brand, model, setpoint range here or in the live config. |
| `README.md` | No | This file. |
| `PLAN.md` | No | Design notes for this repo; not required to run the tool. |

Secrets must not be committed. `.gitignore` at the repo root ignores `tinytuya.json`, `devices.json`, `tuya-raw.json`, `snapshot.json`, and `*.lnk`.

### On this PC (not git)

Live data is `%APPDATA%\HandyScripts\window-ac\` (**Open config folder** in the panel). First run creates that directory.

| File | Used by the panel? | What it is |
| --- | --- | --- |
| `config.json` | **Yes — this is what it reads every launch** | Display name, model, `device_id`, `local_key`, optional IP, protocol version, temperature data-point ids. |
| `devices.json` | **Only if you click Import** | Dump of Tuya devices **including local keys**, written by `python -m tinytuya wizard` (or copied from another key tool). Import copies id + key into `config.json`. After a successful import you can delete it; the panel will not look at it again until the next Import. |
| `tinytuya.json` | No | Access ID, Access Secret, and region for TinyTuya’s **cloud wizard**. Needed only to run the wizard again. The panel never reads this file. |
| `tuya-raw.json` / `snapshot.json` | No | Optional extra wizard/scan output. Safe to delete. |

So: **runtime = `window_ac.pyw` + `config.json`.** `devices.json` is a provisioning handoff, not a second config.

## Provisioning

Fill `device_id` and `local_key` in `%APPDATA%\HandyScripts\window-ac\config.json`. That is the only place the panel looks. Easiest path first.

The AC must already be in **Smart Life**. Do **not** delete, reset, or re-pair it. Neither path below creates the appliance; they only copy secrets Tuya already assigned.

### 1. Easiest: QR login (no iot.tuya.com)

Tuya will hand out local keys if you scan a QR code from Smart Life (**Me** → scan), the same idea as Home Assistant. A small standalone tool that does only that is [tuya-local-key](https://github.com/vineetchoudhary/tuya-local-key) (CLI or Docker). This project does not bundle it.

1. Run that tool, scan the QR from Smart Life, confirm. Do not unpair the AC.
2. Copy the unit’s **id** and **key**.
3. Run `window_ac.pyw` once so AppData `config.json` exists, or copy `config.example.json` there.
4. Paste id → `device_id`, key → `local_key`. Set `"version": 3.4` if 3.3 will not stay connected (this Hykolity class of unit needs 3.4).
5. Save, reopen the panel.

### 2. TinyTuya wizard (iot.tuya.com)

Use this if you would rather stay with TinyTuya only. You still do **not** add a new AC on the website. You create a cloud project and **link your existing Smart Life account**.

1. Create a free Cloud project at [iot.tuya.com](https://iot.tuya.com/) (Smart Home). Data center must match Smart Life **Me → Settings → Account and Security → Region**. US/Canada = **Western America** (`us`). The free IoT Core trial allows **one** data center.
2. Authorize **IoT Core** and **Authorization Token Management**.
3. **Devices → Link App Account → Tuya App Account Authorization**, Automatic, Read only. Scan the QR from Smart Life **Me**. Do not click **Reset Key**. Ignore **Project Code**.
4. In `%APPDATA%\HandyScripts\window-ac` (not the git clone):

   ```text
   python -m tinytuya wizard -region us
   ```

   **API Key** = Access ID / Client ID. **API Secret** = Access Secret. Device ID = `scan` or the id from All Devices.
5. That writes `devices.json` (the key dump) and `tinytuya.json` (cloud credentials). Open the panel → **Import TinyTuya devices.json**.
6. Set `"version": 3.4` if needed.

If the wizard dies with an SSL error on `openapi.tuyaus.com`, HTTPS-scanning antivirus is intercepting the certificate (`pip install truststore`, or disable HTTPS scanning for that download).

### 3. Run the panel

Double-click `window_ac.pyw`. For a taskbar pin, make a shortcut whose **Target** is `pyw.exe` or `pythonw.exe` followed by the path to `window_ac.pyw`. A shortcut that points at the `.pyw` file alone will pin to the Python launcher. `.lnk` files are gitignored.

Same LAN as the AC. Last command wins if the phone and the panel both change the setpoint. While the window is open it holds one local session; Smart Life may use the cloud until you close it.

## `config.json` fields

| Field | Role |
| --- | --- |
| `name` | Title (e.g. room name) |
| `brand` / `model` | Subtitle |
| `device_id` / `local_key` | From QR tool or wizard / `devices.json` |
| `ip` | Optional; filled after first successful connect |
| `version` | Tuya LAN protocol (`3.4` for hykolity ACB-2608-class units) |
| `setpoint_min_f` / `setpoint_max_f` | Clamp for + / − |
| `display_unit` | Panel °F / °C preference |
| `dps` | Tuya data-point ids (power, temps, mode, fan) |

The AC stores both Celsius (setpoint in tenths) and Fahrenheit. The panel’s °F / °C radios only change how this window shows and steps the temperature.

## Names on iot.tuya.com (wizard path only)

On **project Overview / Authorization**, three copy icons are for the *developer project*, not the AC:

| Label | Meaning | Used by |
| --- | --- | --- |
| Access ID / Client ID | Cloud project login (wizard **API Key**) | Wizard only → `tinytuya.json` |
| Access Secret / Client Secret | Cloud project password (wizard **API Secret**) | Wizard only → `tinytuya.json` |
| Project Code (`p…`) | Tuya’s id for the project | Unused |

On **Devices → All Devices**, **Device ID** is this AC. The **local key** is usually not shown there; the QR tool or wizard copies it into `devices.json` / `config.json`.
