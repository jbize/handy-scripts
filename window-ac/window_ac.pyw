#!/usr/bin/env python3
"""
Window AC — local LAN control panel for a Tuya / Smart Life air conditioner.
Version: 1.2.3
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import traceback
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import messagebox, ttk

try:
    import tinytuya
except ImportError:
    tinytuya = None

APP_NAME = "Window AC"
APP_VERSION = "1.2.3"
POLL_MS = 10000
TUYA_IOT_URL = "https://iot.tuya.com/"
EXAMPLE_NAME = "config.example.json"
CONFIG_NAME = "config.json"


def appdata_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "HandyScripts" / "window-ac"
    return Path.home() / "AppData" / "Roaming" / "HandyScripts" / "window-ac"


def script_dir() -> Path:
    return Path(__file__).resolve().parent


@dataclass
class DeviceConfig:
    id: str
    name: str
    brand: str
    model: str
    device_id: str
    local_key: str
    ip: str = ""
    version: float = 3.4
    setpoint_min_f: int = 61
    setpoint_max_f: int = 88
    temp_scale: str = "auto"
    display_unit: str = "F"
    dps: dict[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.device_id.strip() and self.local_key.strip())

    @property
    def model_line(self) -> str:
        return f"{self.brand} {self.model}".strip()

    def dps_id(self, name: str, default: str) -> str:
        return str(self.dps.get(name, default))


def default_config_dict() -> dict[str, Any]:
    example = script_dir() / EXAMPLE_NAME
    if example.exists():
        return json.loads(example.read_text(encoding="utf-8"))
    return {
        "default_device": "master-bedroom",
        "devices": [
            {
                "id": "master-bedroom",
                "name": "Master Bedroom",
                "brand": "hykolity",
                "model": "ACB-2608",
                "device_id": "",
                "local_key": "",
                "ip": "",
                "version": 3.4,
                "setpoint_min_f": 61,
                "setpoint_max_f": 88,
                "temp_scale": "auto",
                "display_unit": "F",
                "dps": {
                    "power": "1",
                    "set_temp": "2",
                    "cur_temp": "3",
                    "mode": "4",
                    "fan": "5",
                    "humidity": "18",
                    "temp_unit": "19",
                    "cur_temp_f": "23",
                    "set_temp_f": "24",
                },
            }
        ],
    }


def parse_device(raw: dict[str, Any]) -> DeviceConfig:
    dps = raw.get("dps") or {}
    return DeviceConfig(
        id=str(raw.get("id") or "device"),
        name=str(raw.get("name") or "Air conditioner"),
        brand=str(raw.get("brand") or ""),
        model=str(raw.get("model") or ""),
        device_id=str(raw.get("device_id") or "").strip(),
        local_key=str(raw.get("local_key") or "").strip(),
        ip=str(raw.get("ip") or "").strip(),
        version=float(raw.get("version") or 3.4),
        setpoint_min_f=int(raw.get("setpoint_min_f") or 61),
        setpoint_max_f=int(raw.get("setpoint_max_f") or 88),
        temp_scale=str(raw.get("temp_scale") or "auto"),
        display_unit="C" if str(raw.get("display_unit") or "F").upper() == "C" else "F",
        dps={str(k): str(v) for k, v in dps.items()},
    )


class ConfigStore:
    def __init__(self) -> None:
        self.dir = appdata_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / CONFIG_NAME
        self.raw: dict[str, Any] = default_config_dict()
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.save()
            return
        self.raw = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self.raw, indent=2) + "\n",
            encoding="utf-8",
        )

    def devices(self) -> list[DeviceConfig]:
        return [parse_device(item) for item in self.raw.get("devices") or []]

    def selected(self) -> DeviceConfig:
        devices = self.devices()
        if not devices:
            raise RuntimeError("No devices in config.")
        wanted = str(self.raw.get("default_device") or "")
        for device in devices:
            if device.id == wanted:
                return device
        return devices[0]

    def update_selected(self, **changes: Any) -> DeviceConfig:
        wanted = str(self.raw.get("default_device") or "")
        items: list[dict[str, Any]] = list(self.raw.get("devices") or [])
        index = 0
        for i, item in enumerate(items):
            if str(item.get("id") or "") == wanted:
                index = i
                break
        items[index].update(changes)
        self.raw["devices"] = items
        self.save()
        return self.selected()


def device_to_f(raw: Any, scale: str) -> int:
    n = float(raw)
    if scale == "F":
        return int(round(n))
    if scale == "C":
        return int(round(n * 9 / 5 + 32))
    if scale == "tenths_f":
        return int(round(n / 10))
    if scale == "tenths_c":
        return int(round((n / 10) * 9 / 5 + 32))
    raise ValueError(f"Unknown temp_scale {scale!r}")


def f_to_device(temp_f: int, scale: str) -> int:
    if scale == "F":
        return int(round(temp_f))
    if scale == "C":
        return int(round((temp_f - 32) * 5 / 9))
    if scale == "tenths_f":
        return int(round(temp_f * 10))
    if scale == "tenths_c":
        return int(round((temp_f - 32) * 5 / 9 * 10))
    raise ValueError(f"Unknown temp_scale {scale!r}")


def f_to_c_int(temp_f: int) -> int:
    return int(round((temp_f - 32) * 5 / 9))


def c_to_f_int(temp_c: int) -> int:
    return int(round(temp_c * 9 / 5 + 32))


def convert_temp(value: int | None, from_unit: str, to_unit: str) -> int | None:
    if value is None or from_unit == to_unit:
        return value
    if from_unit == "F":
        return f_to_c_int(value)
    return c_to_f_int(value)


def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


MODES = (("cold", "Cool"), ("wet", "Dry"), ("wind", "Fan"), ("auto", "Auto"))
FANS = (("auto", "Auto"), ("low", "Low"), ("mid", "Med"), ("high", "High"))
NO_SETPOINT_MODES = {"wet", "wind"}


class Tooltip:
    def __init__(self, widget: tk.Misc, text: str) -> None:
        self.widget = widget
        self.text = text
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _event: tk.Event | None = None) -> None:
        if self._tip is not None or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tip,
            text=self.text,
            justify=tk.LEFT,
            background="#ffffe0",
            relief=tk.SOLID,
            borderwidth=1,
            font=("Segoe UI", 9),
            wraplength=280,
            padx=6,
            pady=4,
        ).pack()
        self._tip = tip

    def _hide(self, _event: tk.Event | None = None) -> None:
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


def tuya_error(payload: dict[str, Any]) -> str | None:
    if payload.get("Error"):
        return str(payload["Error"])
    if payload.get("Err"):
        return f"Tuya error {payload['Err']}"
    if "dps" not in payload:
        return "No status from the AC (no data points)."
    return None


def find_wizard_devices_json() -> Path | None:
    for folder in (appdata_dir(), script_dir(), Path.cwd()):
        path = folder / "devices.json"
        if path.is_file():
            return path
    return None


def pick_wizard_device(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not entries:
        return None
    if len(entries) == 1:
        return entries[0]
    scored: list[tuple[int, dict[str, Any]]] = []
    for entry in entries:
        blob = " ".join(
            str(entry.get(key) or "")
            for key in ("name", "product_name", "category")
        ).lower()
        score = 0
        for token in ("air", "ac", "climate", "kt", "bedroom"):
            if token in blob:
                score += 1
        scored.append((score, entry))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


class AcClient:
    def __init__(self, device: DeviceConfig) -> None:
        self.device = device
        self.cached_ip = device.ip or None
        self.working_version = float(device.version or 3.4)
        self._client: Any = None

    def _make(self, version: float) -> Any:
        if tinytuya is None:
            raise RuntimeError("tinytuya is not installed. Run: pip install tinytuya")
        address = self.cached_ip or self.device.ip or "Auto"
        client = tinytuya.Device(
            self.device.device_id,
            address=address,
            local_key=self.device.local_key,
            version=version,
        )
        client.set_socketPersistent(True)
        client.set_socketTimeout(5)
        return client

    def _close(self, client: Any) -> None:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def close(self) -> None:
        if self._client is not None:
            self._close(self._client)
            self._client = None

    def _remember_ip(self, client: Any) -> None:
        address = getattr(client, "address", None)
        if address and address not in ("Auto", "0.0.0.0"):
            self.cached_ip = str(address)

    def _payload_ok(self, payload: Any) -> tuple[dict[str, Any] | None, str | None]:
        if not isinstance(payload, dict):
            return None, "Bad status payload"
        err = tuya_error(payload)
        if err:
            return None, err
        return payload, None

    def _connect(self, version: float) -> dict[str, Any]:
        self.close()
        client = self._make(version)
        payload, err = self._payload_ok(client.status() or {})
        if err:
            self._close(client)
            raise RuntimeError(err)
        self._remember_ip(client)
        self._client = client
        self.working_version = version
        return payload or {}

    def _versions(self) -> list[float]:
        seen: list[float] = []
        for version in (self.working_version, 3.4, 3.3, 3.5):
            if version not in seen:
                seen.append(version)
        return seen

    def status(self) -> dict[str, Any]:
        if self._client is not None:
            try:
                payload, err = self._payload_ok(self._client.status() or {})
                if payload is not None:
                    self._remember_ip(self._client)
                    return payload
            except Exception:
                pass
            self.close()
        last_err = "Unable to reach the AC on the local network."
        for version in self._versions():
            try:
                return self._connect(version)
            except Exception as exc:
                last_err = str(exc) or last_err
                time.sleep(0.3)
        raise RuntimeError(last_err)

    def set_value(self, dps_id: str, value: Any) -> None:
        if self._client is None:
            self.status()
        client = self._client
        if client is None:
            raise RuntimeError("Unable to reach the AC on the local network.")
        payload = client.set_value(dps_id, value)
        if payload is None:
            raise RuntimeError("The AC did not accept that command.")
        if isinstance(payload, dict) and payload.get("Error"):
            self.close()
            self.status()
            client = self._client
            if client is None:
                raise RuntimeError(str(payload["Error"]))
            payload = client.set_value(dps_id, value)
            if payload is None:
                raise RuntimeError("The AC did not accept that command.")
            if isinstance(payload, dict) and payload.get("Error"):
                raise RuntimeError(str(payload["Error"]))
        if client is not None:
            self._remember_ip(client)


@dataclass
class AcState:
    power: bool | None = None
    unit: str = "F"
    room: int | None = None
    set: int | None = None
    mode: str = ""
    fan: str = ""
    humidity: int | None = None
    scale: str = "auto"
    dps: dict[str, Any] = field(default_factory=dict)
    error: str = ""


def read_unit(dps: dict[str, Any], unit_id: str, cur_f_id: str, set_f_id: str) -> str:
    raw = str(dps.get(unit_id) or "").strip().lower()
    if raw in ("f", "fahrenheit"):
        return "F"
    if raw in ("c", "celsius"):
        return "C"
    if dps.get(cur_f_id) is not None or dps.get(set_f_id) is not None:
        return "F"
    return "C"


def read_state(client: AcClient) -> AcState:
    device = client.device
    payload = client.status()
    dps = {str(k): v for k, v in (payload.get("dps") or {}).items()}
    power_id = device.dps_id("power", "1")
    set_c_id = device.dps_id("set_temp", "2")
    cur_c_id = device.dps_id("cur_temp", "3")
    unit_id = device.dps_id("temp_unit", "19")
    set_f_id = device.dps_id("set_temp_f", "24")
    cur_f_id = device.dps_id("cur_temp_f", "23")
    unit = read_unit(dps, unit_id, cur_f_id, set_f_id)

    if unit == "F":
        if dps.get(cur_f_id) is not None:
            room = int(round(float(dps[cur_f_id])))
        elif dps.get(cur_c_id) is not None:
            room = device_to_f(dps[cur_c_id], "C")
        else:
            room = None
        if dps.get(set_f_id) is not None:
            set_temp = int(round(float(dps[set_f_id]) / 10))
            scale = "F_dps"
        elif dps.get(set_c_id) is not None:
            set_temp = device_to_f(dps[set_c_id], "tenths_c")
            scale = "C_dps"
        else:
            set_temp = None
            scale = "C_dps"
    else:
        if dps.get(cur_c_id) is not None:
            room = int(round(float(dps[cur_c_id])))
        elif dps.get(cur_f_id) is not None:
            room = f_to_c_int(int(round(float(dps[cur_f_id]))))
        else:
            room = None
        if dps.get(set_c_id) is not None:
            set_temp = int(round(float(dps[set_c_id]) / 10))
            scale = "C_dps"
        elif dps.get(set_f_id) is not None:
            set_temp = f_to_c_int(int(round(float(dps[set_f_id]) / 10)))
            scale = "F_dps"
        else:
            set_temp = None
            scale = "C_dps"

    power = dps.get(power_id)
    mode = str(dps.get(device.dps_id("mode", "4")) or "")
    fan = str(dps.get(device.dps_id("fan", "5")) or "")
    humid_raw = dps.get(device.dps_id("humidity", "18"))
    humidity = None
    if humid_raw not in (None, ""):
        try:
            humidity = int(round(float(humid_raw)))
        except (TypeError, ValueError):
            humidity = None
    return AcState(
        power=bool(power) if power is not None else None,
        unit=unit,
        room=room,
        set=set_temp,
        mode=mode,
        fan=fan,
        humidity=humidity,
        scale=scale,
        dps=dps,
    )


class WindowAcApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.store = ConfigStore()
        self.device = self.store.selected()
        self.client = AcClient(self.device)
        self.jobs: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.stop = threading.Event()
        self._poll_after: str | None = None
        self.last_state = AcState()
        self._fail_count = 0
        self._more_open = False

        self.room_var = tk.StringVar(value="—")
        self.set_var = tk.StringVar(value="—")
        self.status_var = tk.StringVar(value="Starting…")
        self.humid_var = tk.StringVar(value="")
        self.unit_var = tk.StringVar(value=self.device.display_unit)
        self.mode_var = tk.StringVar(value="")
        self.fan_var = tk.StringVar(value="")
        self._unit_lock = False
        self._choice_lock = False
        self._busy = 0
        self.mode_btns: list[ttk.Radiobutton] = []
        self.fan_btns: list[ttk.Radiobutton] = []

        self._build()
        self.worker = threading.Thread(target=self._worker, daemon=True)
        self.worker.start()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(200, self._kickoff)

    def _build(self) -> None:
        self.root.title(f"{self.device.name} AC")
        pad = {"padx": 16, "pady": 4}

        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill=tk.X)

        ttk.Label(frame, text=self.device.name, font=("Segoe UI", 16, "bold")).pack(
            anchor=tk.W
        )
        ttk.Label(
            frame,
            text=self.device.model_line or "Window air conditioner",
            font=("Segoe UI", 10),
        ).pack(anchor=tk.W, pady=(0, 12))

        temps = ttk.Frame(frame)
        temps.pack(fill=tk.X, pady=8)
        ttk.Label(temps, text="Room", font=("Segoe UI", 10)).grid(row=0, column=0, sticky=tk.W)
        ttk.Label(temps, textvariable=self.room_var, font=("Segoe UI", 22, "bold")).grid(
            row=1, column=0, sticky=tk.W
        )
        ttk.Label(temps, text="Set to", font=("Segoe UI", 10)).grid(
            row=0, column=1, sticky=tk.W, padx=(32, 0)
        )
        ttk.Label(temps, textvariable=self.set_var, font=("Segoe UI", 22, "bold")).grid(
            row=1, column=1, sticky=tk.W, padx=(32, 0)
        )
        unit_box = ttk.Frame(temps)
        unit_box.grid(row=0, column=2, rowspan=2, sticky=tk.E, padx=(24, 0))
        temps.columnconfigure(2, weight=1)
        self.unit_f = ttk.Radiobutton(
            unit_box, text="°F", value="F", variable=self.unit_var, command=self.on_unit
        )
        self.unit_c = ttk.Radiobutton(
            unit_box, text="°C", value="C", variable=self.unit_var, command=self.on_unit
        )
        self.unit_f.pack(anchor=tk.W)
        self.unit_c.pack(anchor=tk.W)
        Tooltip(self.unit_f, "Show and set temperatures in Fahrenheit on this panel.")
        Tooltip(self.unit_c, "Show and set temperatures in Celsius on this panel.")

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, pady=16)
        self.power_btn = ttk.Button(buttons, text="On/Off", command=self.on_power)
        self.power_btn.pack(fill=tk.X, ipady=6)
        Tooltip(self.power_btn, "Turn the air conditioner on or off.")
        step = ttk.Frame(buttons)
        step.pack(fill=tk.X, pady=(10, 0))
        self.minus_btn = ttk.Button(step, text="−  Cooler", command=lambda: self.on_temp(-1))
        self.plus_btn = ttk.Button(step, text="Warmer  +", command=lambda: self.on_temp(1))
        self.minus_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 6))
        self.plus_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(6, 0))
        Tooltip(self.minus_btn, "Lower the setpoint one degree in °F or °C, whichever is selected.")
        Tooltip(self.plus_btn, "Raise the setpoint one degree in °F or °C, whichever is selected.")

        self.mode_btns = self._option_row(
            buttons,
            "Mode",
            self.mode_var,
            MODES,
            self.on_mode,
            "Cool, dry, fan-only, or auto. This unit has no heat mode.",
        )
        self.fan_btns = self._option_row(
            buttons,
            "Fan",
            self.fan_var,
            FANS,
            self.on_fan,
            "Fan speed. Auto lets the AC choose.",
        )
        self.humid_label = ttk.Label(buttons, textvariable=self.humid_var)
        self.humid_label.pack(anchor=tk.W, pady=(8, 0))
        self.humid_label.pack_forget()

        ttk.Separator(frame).pack(fill=tk.X, pady=12)
        ttk.Label(frame, textvariable=self.status_var, wraplength=380).pack(anchor=tk.W)

        self.more_btn = ttk.Button(frame, text="More ▸", command=self.toggle_more)
        self.more_btn.pack(anchor=tk.W, pady=(12, 0))
        Tooltip(self.more_btn, "Show or hide diagnostics and one-time setup.")

        self.setup = ttk.Frame(frame)
        diag = ttk.LabelFrame(self.setup, text="Diagnostics")
        diag.pack(fill=tk.X, pady=(8, 4))
        copy_btn = ttk.Button(diag, text="Copy status (no keys)", command=self.on_copy_status)
        copy_btn.pack(fill=tk.X, **pad)
        Tooltip(
            copy_btn,
            "Copies temperatures and data-point values to the clipboard. Does not copy device id or local key.",
        )
        reload_btn = ttk.Button(diag, text="Reload config", command=self.on_reload)
        reload_btn.pack(fill=tk.X, **pad)
        Tooltip(
            reload_btn,
            "Re-reads config.json after you edit it. Both provisioning paths.",
        )

        once = ttk.LabelFrame(self.setup, text="Setup (once)")
        once.pack(fill=tk.X, pady=(8, 0))
        config_btn = ttk.Button(once, text="Open config folder", command=self.on_open_config)
        config_btn.pack(fill=tk.X, **pad)
        Tooltip(
            config_btn,
            "Opens AppData where config.json lives. Used by both provisioning paths.",
        )
        import_btn = ttk.Button(
            once, text="Import TinyTuya devices.json", command=self.on_import
        )
        import_btn.pack(fill=tk.X, **pad)
        Tooltip(
            import_btn,
            "Copies device id and local key from a TinyTuya wizard devices.json into config.json. Wizard path only.",
        )
        tuya_btn = ttk.Button(once, text="Open Tuya IoT (key setup)", command=self.on_open_tuya)
        tuya_btn.pack(fill=tk.X, **pad)
        Tooltip(
            tuya_btn,
            "Opens iot.tuya.com. Used only for TinyTuya wizard provisioning, not the QR-login path.",
        )

        self._set_controls_enabled(False)
        self.root.after(0, self._fit_window)

    def _option_row(
        self,
        parent: ttk.Frame,
        title: str,
        variable: tk.StringVar,
        options: tuple[tuple[str, str], ...],
        command: Any,
        tip: str,
    ) -> list[ttk.Radiobutton]:
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(row, text=title, width=5).pack(side=tk.LEFT)
        buttons: list[ttk.Radiobutton] = []
        for value, text in options:
            btn = ttk.Radiobutton(
                row,
                text=text,
                value=value,
                variable=variable,
                command=command,
                style="Toolbutton",
            )
            btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
            Tooltip(btn, tip)
            buttons.append(btn)
        return buttons

    def toggle_more(self) -> None:
        self._more_open = not self._more_open
        if self._more_open:
            self.setup.pack(fill=tk.X, pady=(8, 0))
            self.more_btn.configure(text="More ▾")
        else:
            self.setup.pack_forget()
            self.more_btn.configure(text="More ▸")
        self._fit_window()

    def _fit_window(self) -> None:
        self.root.update_idletasks()
        width = max(340, self.root.winfo_reqwidth())
        height = self.root.winfo_reqheight()
        self.root.minsize(320, height)
        self.root.geometry(f"{width}x{height}")

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        widgets: list[tk.Misc] = [
            self.power_btn,
            self.minus_btn,
            self.plus_btn,
            *self.mode_btns,
            *self.fan_btns,
        ]
        for widget in widgets:
            widget.configure(state=state)
        if enabled and self.last_state.mode in NO_SETPOINT_MODES:
            self.minus_btn.configure(state=tk.DISABLED)
            self.plus_btn.configure(state=tk.DISABLED)

    def _kickoff(self) -> None:
        if tinytuya is None:
            self.status_var.set("tinytuya is not installed. Run: pip install tinytuya")
            return
        if not self.device.configured:
            self.status_var.set(
                "Not configured yet. Put device_id and local_key in config.json "
                "(see README). If you have a TinyTuya devices.json, use Import."
            )
            return
        self.status_var.set("Connecting on the local network…")
        self._submit("poll")

    def _submit(self, action: str, extra: Any = None) -> None:
        if self.stop.is_set():
            return
        if action != "poll":
            self._busy += 1
        self.jobs.put((action, extra))

    def _worker(self) -> None:
        while not self.stop.is_set():
            try:
                action, extra = self.jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if action == "poll":
                    state = read_state(self.client)
                    self.root.after(0, lambda s=state: self._apply_state(s, reschedule=True))
                elif action == "power":
                    self._do_power()
                    state = read_state(self.client)
                    self.root.after(0, lambda s=state: self._apply_state(s, reschedule=False))
                elif action == "temp":
                    self._do_temp(int(extra))
                    state = read_state(self.client)
                    self.root.after(0, lambda s=state: self._apply_state(s, reschedule=False))
                elif action == "mode":
                    self.client.set_value(self.device.dps_id("mode", "4"), str(extra))
                    state = read_state(self.client)
                    self.root.after(0, lambda s=state: self._apply_state(s, reschedule=False))
                elif action == "fan":
                    self.client.set_value(self.device.dps_id("fan", "5"), str(extra))
                    state = read_state(self.client)
                    self.root.after(0, lambda s=state: self._apply_state(s, reschedule=False))
            except Exception as exc:
                err = AcState(error=str(exc), dps=self.last_state.dps)
                self.root.after(
                    0,
                    lambda s=err, poll=action == "poll": self._apply_state(s, reschedule=poll),
                )

    def _do_power(self) -> None:
        state = read_state(self.client)
        if state.power is None:
            raise RuntimeError("Could not read power state.")
        power_id = self.device.dps_id("power", "1")
        self.client.set_value(power_id, not state.power)

    def _do_temp(self, delta: int) -> None:
        state = read_state(self.client)
        if state.set is None:
            raise RuntimeError("Could not read setpoint.")
        if state.mode in NO_SETPOINT_MODES:
            raise RuntimeError("Setpoint is not used in dry or fan-only mode.")
        display = self.unit_var.get() or "F"
        shown = convert_temp(state.set, state.unit, display)
        if shown is None:
            raise RuntimeError("Could not read setpoint.")
        if display == "C":
            lo = f_to_c_int(self.device.setpoint_min_f)
            hi = f_to_c_int(self.device.setpoint_max_f)
        else:
            lo = self.device.setpoint_min_f
            hi = self.device.setpoint_max_f
        new_shown = clamp(shown + delta, lo, hi)
        new_device = convert_temp(new_shown, display, state.unit)
        if new_device is None:
            return
        if state.unit == "C" or state.scale != "F_dps":
            if state.unit == "F":
                payload = f_to_device(new_device, "tenths_c")
            else:
                payload = new_device * 10
            self.client.set_value(self.device.dps_id("set_temp", "2"), payload)
            return
        self.client.set_value(self.device.dps_id("set_temp_f", "24"), new_device * 10)

    def _display_temps(self, state: AcState) -> None:
        display = self.unit_var.get() or "F"
        suffix = "°F" if display == "F" else "°C"
        room = convert_temp(state.room, state.unit, display)
        setpoint = convert_temp(state.set, state.unit, display)
        self.room_var.set("—" if room is None else f"{room}{suffix}")
        self.set_var.set("—" if setpoint is None else f"{setpoint}{suffix}")

    def _apply_state(self, state: AcState, reschedule: bool) -> None:
        if self.stop.is_set():
            return
        if not reschedule:
            self._busy = max(0, self._busy - 1)
        if state.error:
            if reschedule:
                self._fail_count += 1
            state.room = state.room if state.room is not None else self.last_state.room
            state.set = state.set if state.set is not None else self.last_state.set
            state.unit = state.unit if state.unit else self.last_state.unit
            state.power = state.power if state.power is not None else self.last_state.power
            state.mode = state.mode or self.last_state.mode
            state.fan = state.fan or self.last_state.fan
            state.humidity = (
                state.humidity if state.humidity is not None else self.last_state.humidity
            )
            state.scale = state.scale if state.scale != "auto" else self.last_state.scale
            if not state.dps:
                state.dps = self.last_state.dps
            self.last_state = state
            if not reschedule:
                self._choice_lock = True
                if self.last_state.mode:
                    self.mode_var.set(self.last_state.mode)
                if self.last_state.fan:
                    self.fan_var.set(self.last_state.fan)
                self._choice_lock = False
                self.status_var.set(state.error)
            elif self._fail_count >= 2:
                self.status_var.set(state.error or "Unable to reach the AC — retrying…")
        else:
            self._fail_count = 0
            self.last_state = state
            if reschedule and self._busy:
                if self.device.configured and tinytuya is not None:
                    self._schedule_poll()
                return
            self._display_temps(state)
            self._choice_lock = True
            if state.mode:
                self.mode_var.set(state.mode)
            if state.fan:
                self.fan_var.set(state.fan)
            self._choice_lock = False
            if state.humidity:
                self.humid_var.set(f"Humidity {state.humidity}%")
                if not self.humid_label.winfo_ismapped():
                    self.humid_label.pack(anchor=tk.W, pady=(8, 0))
                    self._fit_window()
            else:
                self.humid_var.set("")
                if self.humid_label.winfo_ismapped():
                    self.humid_label.pack_forget()
                    self._fit_window()
            if state.power is True:
                self.power_btn.configure(text="Turn Off")
            elif state.power is False:
                self.power_btn.configure(text="Turn On")
            else:
                self.power_btn.configure(text="On/Off")
            self.status_var.set("Connected on the local network.")
            self._set_controls_enabled(True)
            changes: dict[str, Any] = {}
            cached = self.client.cached_ip
            if cached and cached != self.device.ip:
                changes["ip"] = cached
            if self.client.working_version != self.device.version:
                changes["version"] = self.client.working_version
            if changes:
                self.device = self.store.update_selected(**changes)
                self.client.device = self.device
        if reschedule and self.device.configured and tinytuya is not None:
            self._schedule_poll()

    def _schedule_poll(self) -> None:
        if self._poll_after is not None:
            try:
                self.root.after_cancel(self._poll_after)
            except Exception:
                pass
        self._poll_after = self.root.after(POLL_MS, lambda: self._submit("poll"))

    def on_unit(self) -> None:
        if self._unit_lock:
            return
        unit = self.unit_var.get() or "F"
        self.device = self.store.update_selected(display_unit=unit)
        self.client.device = self.device
        self._display_temps(self.last_state)
        self.status_var.set("Panel set to °C." if unit == "C" else "Panel set to °F.")

    def on_mode(self) -> None:
        if self._choice_lock or not self.device.configured:
            return
        self.status_var.set("Changing mode…")
        self._submit("mode", self.mode_var.get())

    def on_fan(self) -> None:
        if self._choice_lock or not self.device.configured:
            return
        self.status_var.set("Changing fan speed…")
        self._submit("fan", self.fan_var.get())

    def on_power(self) -> None:
        if not self.device.configured:
            return
        self.status_var.set("Sending power command…")
        self._submit("power")

    def on_temp(self, delta: int) -> None:
        if not self.device.configured:
            return
        self.status_var.set("Changing temperature…")
        self._submit("temp", delta)

    def on_open_tuya(self) -> None:
        webbrowser.open(TUYA_IOT_URL)

    def on_open_config(self) -> None:
        os.startfile(self.store.dir)  # type: ignore[attr-defined]

    def on_reload(self) -> None:
        self.store.load()
        self.device = self.store.selected()
        self.client.close()
        self.client = AcClient(self.device)
        self.root.title(f"{self.device.name} AC")
        self._kickoff()

    def on_import(self) -> None:
        path = find_wizard_devices_json()
        if path is None:
            messagebox.showinfo(
                APP_NAME,
                "No devices.json found.\n\n"
                "Run the TinyTuya wizard (see README), then put devices.json in:\n"
                f"{self.store.dir}\n\n"
                "or in the window-ac folder, and try Import again.",
            )
            return
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not read {path.name}:\n{exc}")
            return
        if isinstance(entries, dict):
            entries = entries.get("devices") or entries.get("list") or []
        picked = pick_wizard_device(list(entries))
        if not picked:
            messagebox.showerror(APP_NAME, f"{path.name} has no devices.")
            return
        device_id = str(picked.get("id") or picked.get("gwId") or "").strip()
        local_key = str(picked.get("key") or picked.get("local_key") or "").strip()
        version = picked.get("version") or self.device.version
        ip = str(picked.get("ip") or picked.get("address") or "").strip()
        if not device_id or not local_key:
            messagebox.showerror(
                APP_NAME,
                f"{path.name} did not include an id and key for that device.",
            )
            return
        changes: dict[str, Any] = {
            "device_id": device_id,
            "local_key": local_key,
            "version": float(version),
        }
        if ip:
            changes["ip"] = ip
        self.device = self.store.update_selected(**changes)
        self.client.close()
        self.client = AcClient(self.device)
        messagebox.showinfo(
            APP_NAME,
            f"Imported {picked.get('name') or 'device'} from {path.name}.\n"
            "The phone app was not changed.",
        )
        self._kickoff()

    def on_copy_status(self) -> None:
        blob = {
            "name": self.device.name,
            "model": self.device.model_line,
            "configured": self.device.configured,
            "version": self.device.version,
            "temp_scale": self.device.temp_scale,
            "display_unit": self.device.display_unit,
            "dps_map": self.device.dps,
            "last_error": self.last_state.error,
            "last_dps": self.last_state.dps,
            "power": self.last_state.power,
            "unit": self.last_state.unit,
            "room": self.last_state.room,
            "set": self.last_state.set,
            "mode": self.last_state.mode,
            "fan": self.last_state.fan,
            "humidity": self.last_state.humidity,
        }
        text = json.dumps(blob, indent=2)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_var.set("Copied status (no device id or key).")

    def on_close(self) -> None:
        self.stop.set()
        if self._poll_after is not None:
            try:
                self.root.after_cancel(self._poll_after)
            except Exception:
                pass
        self.client.close()
        self.root.destroy()


def crash_log(exc: BaseException) -> None:
    try:
        path = appdata_dir() / "crash.log"
        appdata_dir().mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            encoding="utf-8",
        )
    except Exception:
        pass


def main() -> None:
    root = tk.Tk()
    try:
        WindowAcApp(root)
        root.mainloop()
    except Exception as exc:
        crash_log(exc)
        try:
            messagebox.showerror(APP_NAME, str(exc))
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
