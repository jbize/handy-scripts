#!/usr/bin/env python3
"""
Billing Assistant
Version: 3.3.1
"""

import sys
import os
import shutil
import calendar
import webbrowser
import logging
import configparser
import errno
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List
from dataclasses import dataclass
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
from urllib.parse import quote

try:
    import openpyxl
except ImportError:
    openpyxl = None

# --- CONSTANTS ---
APP_NAME    = "Billing Assistant"
APP_VERSION = "3.3.1"
CONFIG_FILE = "config.ini"
LOG_FILE    = "billing_assistant.log"

# --- DATA CLASSES ---
@dataclass(slots=True)
class InvoiceEmail:
    to:      str
    cc:      str
    subject: str
    body:    str

@dataclass(slots=True)
class BillingData:
    rate:        float
    hours:       float
    amount:      float
    month_label: str
    config:      Dict[str, Any]

# --- CONFIGURATION MANAGER ---
class ConfigManager:
    def __init__(self):
        self.app_dir = Path(os.environ.get("APPDATA", "~")).expanduser() / APP_NAME
        self.app_dir.mkdir(parents=True, exist_ok=True)
        self.path     = self.app_dir / CONFIG_FILE
        self.log_path = self.app_dir / LOG_FILE
        self.config   = configparser.ConfigParser()
        self.load()

    def load(self):
        if self.path.exists():
            self.config.read(self.path)
        if "Settings" not in self.config:
            self.config["Settings"] = {
                "last_directory": str(Path.home()),
                "last_file":      "",
                "remember_file":  "True",
                "enable_logging": "False",
            }

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            self.config.write(f)

    def get(self, key: str, fallback: str = "") -> str:
        return self.config["Settings"].get(key, fallback)

    def set(self, key: str, value: str):
        self.config["Settings"][key] = value
        self.save()

    def get_bool(self, key: str) -> bool:
        return self.config["Settings"].getboolean(key)

# --- LOGGING ---
def setup_logging(cfg: ConfigManager):
    root_logger = logging.getLogger()
    root_logger.handlers = []
    if cfg.get_bool("enable_logging"):
        logging.disable(logging.NOTSET)
        logging.basicConfig(
            filename=cfg.log_path,
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
        )
    else:
        logging.disable(logging.CRITICAL)

def is_permission_denied(exc: BaseException) -> bool:
    return isinstance(exc, (PermissionError, OSError)) and getattr(exc, "errno", None) == errno.EACCES

def workbook_locked_message(path: Path) -> str:
    return (
        f"Cannot read:\n{path}\n\n"
        "The file appears to be open in Excel or locked by Windows/OneDrive.\n"
        "Please close the workbook in Excel, let OneDrive finish syncing if needed, and try again."
    )

def normalize_config_key(value: Any) -> str:
    return str(value).strip().lower().replace(" ", "_")

# --- EXCEL PARSING ---
def parse_workbook(path: Path) -> BillingData:
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception as e:
        if is_permission_denied(e):
            raise PermissionError(workbook_locked_message(path)) from e
        raise

    try:
        ws = wb.active

        # --- Find __CONFIG__ block ---
        config       = {}
        found_marker = False
        for row in ws.iter_rows():
            for cell in row:
                if cell.value == "__CONFIG__":
                    found_marker  = True
                    check_row     = cell.row + 1
                    col_idx       = cell.column
                    while True:
                        k = ws.cell(row=check_row, column=col_idx).value
                        v = ws.cell(row=check_row, column=col_idx + 1).value
                        if not k:
                            break
                        config[normalize_config_key(k)] = "" if v is None else str(v).strip()
                        check_row += 1
                    break
            if found_marker:
                break

        if not found_marker:
            raise ValueError("__CONFIG__ marker not found in the workbook.")

        # --- Billing cells ---
        rate   = float(ws.cell(row=1,  column=5).value or 0)   # E1
        hours  = float(ws.cell(row=15, column=2).value or 0)   # B15
        amount = float(ws.cell(row=18, column=2).value or 0)   # B18
        d5     = ws.cell(row=5,  column=4).value                # D5

        month_label = d5.strftime("%B-%Y") if isinstance(d5, datetime) else "Unknown"
        return BillingData(rate, hours, amount, month_label, config)
    finally:
        wb.close()

# --- PAYMENT BLOCK BUILDER ---
def build_payment_block(c: Dict[str, Any], amount: float) -> str:
    method_name = str(c.get("current_payment", "")).strip().lower()
    if not method_name:
        return ""

    if method_name == "paypal":
        paypal_email = c.get("paypal_email", "")
        paypal_me    = c.get("paypal_me", "")
        parts = []
        if paypal_email:
            parts.append(f"Please submit payment via PayPal Friends & Family to:\n{paypal_email}")
        if paypal_me:
            parts.append(f"Or use PayPal.Me:\nhttps://paypal.me/{paypal_me}/{amount:.2f}")
        return "\n\n".join(parts)

    if method_name == "zelle":
        zelle_address = c.get("zelle_address", "")
        if not zelle_address:
            return "Please submit payment via Zelle."
        return f"Please submit payment via Zelle to:\n{zelle_address}"

    if method_name == "check":
        address = c.get("check_address", "")
        if not address:
            return "Please send payment by check."
        return f"Please send check to:\n{address}"

    # Custom method: current_payment points to another config key.
    # Example: current_payment = gobbledygook, gobbledygook = Please pay by carrier pigeon.
    custom_instr = c.get(method_name, "")
    if custom_instr:
        return str(custom_instr).strip()

    # Unknown method with no matching instruction key: leave payment section out entirely.
    return ""

# --- EMAIL BUILDER ---
def build_email(data: BillingData) -> InvoiceEmail:
    c      = data.config
    slug   = c.get("invoice_slug", "invoice")
    sender = c.get("sender_first_name", "Consultant")

    subject = f"{sender}-{slug} ({data.month_label})"
    to      = c.get("client_email", "")

    # CC priority: cc_email -> sender_email -> paypal_email
    cc = c.get("cc_email") or c.get("sender_email") or c.get("paypal_email") or ""

    payment_block = build_payment_block(c, data.amount)

    body_parts = [
        f"{c.get('client_greeting', 'Client')},",
        "",
        f"{data.month_label} {slug}:",
        "",
        f" • Hours worked: {round(data.hours, 2)} @ ${data.rate:.0f}/hr",
        f" • Amount due:   ${data.amount:.2f}",
    ]

    if payment_block:
        body_parts.extend(["", payment_block])

    body_parts.extend(["", f"Thanks,\n{sender}"])
    return InvoiceEmail(to, cc, subject, "\n".join(body_parts))

# --- MONTH ROLL ---
def roll_month(path: Path) -> Path:
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception as e:
        if is_permission_denied(e):
            raise PermissionError(workbook_locked_message(path)) from e
        raise

    try:
        curr_date = wb.active.cell(row=5, column=4).value
    finally:
        wb.close()

    if not isinstance(curr_date, datetime):
        raise ValueError("Cell D5 must contain a valid date.")

    new_start = datetime(
        curr_date.year + (curr_date.month // 12),
        (curr_date.month % 12) + 1,
        1,
    )
    new_path = path.parent / f"Bize-Jason Bills Time Tracking_{new_start.strftime('%Y-%m')}.xlsx"
    shutil.copy2(path, new_path)

    try:
        wb2 = openpyxl.load_workbook(new_path, data_only=False)
    except Exception as e:
        if is_permission_denied(e):
            raise PermissionError(workbook_locked_message(new_path)) from e
        raise

    ws2 = wb2.active

    days_in_month = calendar.monthrange(new_start.year, new_start.month)[1]
    for i in range(31):
        cell       = ws2.cell(row=5 + i, column=4)
        cell.value = (new_start + timedelta(days=i)) if i < days_in_month else None

    # Clear time-entry columns: G(7) H(8) J(10) K(11) M(13) N(14)
    for row_idx in range(5, 39):
        for col_idx in (7, 8, 10, 11, 13, 14):
            ws2.cell(row=row_idx, column=col_idx).value = None

    try:
        wb2.save(new_path)
    except Exception as e:
        if is_permission_denied(e):
            raise PermissionError(workbook_locked_message(new_path)) from e
        raise
    finally:
        wb2.close()

    return new_path

# --- SELECTABLE ERROR DIALOG ---
def show_error(parent: tk.Tk, message: str):
    dlg = tk.Toplevel(parent)
    dlg.title("Error")
    dlg.geometry("560x320")
    dlg.grab_set()
    tk.Label(dlg, text="Error", fg="red", font=("", 10, "bold")).pack(pady=8)
    txt = scrolledtext.ScrolledText(dlg, wrap="word", padx=10, pady=10)
    txt.insert("1.0", message)
    txt.config(state="normal")   # selectable / copyable
    txt.pack(fill="both", expand=True, padx=15, pady=5)
    tk.Button(dlg, text="Close", command=dlg.destroy, width=15).pack(pady=8)

# --- GUI ---
class BillingAssistantGUI:
    def __init__(self, root: tk.Tk, args: List[str]):
        self.root = root
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.root.geometry("750x220")

        self.cfg = ConfigManager()
        setup_logging(self.cfg)
        self._setup_ui()

        if len(args) > 1:
            self._set_file(Path(args[1]))
        elif self.cfg.get_bool("remember_file"):
            last = self.cfg.get("last_file")
            if last and os.path.exists(last):
                self._set_file(Path(last))

    def _setup_ui(self):
        pad = {"padx": 15, "pady": 5}

        # --- Source ---
        f_frame = tk.LabelFrame(self.root, text="Timesheet Source", padx=10, pady=10)
        f_frame.pack(fill="x", **pad)

        tk.Label(f_frame, text="Directory:").grid(row=0, column=0, sticky="e")
        self.dir_var = tk.StringVar(value=self.cfg.get("last_directory"))
        tk.Entry(f_frame, textvariable=self.dir_var, state="readonly").grid(row=0, column=1, sticky="we", padx=5)

        tk.Label(f_frame, text="File:").grid(row=1, column=0, sticky="e")
        self.file_var = tk.StringVar(value=os.path.basename(self.cfg.get("last_file")))
        tk.Entry(f_frame, textvariable=self.file_var, state="readonly").grid(row=1, column=1, sticky="we", padx=5)
        tk.Button(f_frame, text="Browse...", command=self.on_browse).grid(row=1, column=2)
        f_frame.columnconfigure(1, weight=1)

        # --- Actions ---
        a_frame = tk.Frame(self.root)
        a_frame.pack(fill="x", **pad)
        tk.Button(a_frame, text="Generate Invoice Email", command=self.on_generate,
                  width=25, bg="#e1f5fe").pack(side="left", padx=5)
        tk.Button(a_frame, text="Roll to Next Month", command=self.on_roll,
                  width=25).pack(side="left", padx=5)

        # --- Collapsible Invoice Information ---
        self._panel_open = tk.BooleanVar(value=False)

        toggle_frame = tk.Frame(self.root)
        toggle_frame.pack(fill="x", padx=15, pady=(8, 0))
        self._toggle_btn = tk.Button(
            toggle_frame, text="▶ Invoice Information",
            anchor="w", relief="flat", cursor="hand2",
            command=self._toggle_panel
        )
        self._toggle_btn.pack(fill="x")

        self._panel = tk.Frame(self.root, bd=1, relief="groove")
        # panel starts hidden — pack called in _toggle_panel when opened

        tk.Label(self._panel, text="To:").grid(row=0, column=0, sticky="e", padx=(10,2), pady=2)
        self.to_ent = tk.Entry(self._panel)
        self.to_ent.grid(row=0, column=1, sticky="we", pady=2, padx=(0,10))

        tk.Label(self._panel, text="CC:").grid(row=1, column=0, sticky="e", padx=(10,2), pady=2)
        self.cc_ent = tk.Entry(self._panel)
        self.cc_ent.grid(row=1, column=1, sticky="we", pady=2, padx=(0,10))

        tk.Label(self._panel, text="Subject:").grid(row=2, column=0, sticky="e", padx=(10,2), pady=2)
        self.subj_ent = tk.Entry(self._panel)
        self.subj_ent.grid(row=2, column=1, sticky="we", pady=2, padx=(0,10))

        self.body_txt = scrolledtext.ScrolledText(self._panel, height=12, wrap="word")
        self.body_txt.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=5, padx=10)
        self._panel.rowconfigure(3, weight=1)
        self._panel.columnconfigure(1, weight=1)

        b_frame = tk.Frame(self._panel)
        b_frame.grid(row=4, column=0, columnspan=2, sticky="we", padx=10, pady=(0,8))
        tk.Button(b_frame, text="Copy Body", command=self.on_copy).pack(side="left", padx=5)
        tk.Button(b_frame, text="Open in Mailer (With CC)", bg="#c8e6c9",
                  command=self.on_mailto).pack(side="left", padx=5)

        # --- Settings ---
        s_frame = tk.Frame(self.root)
        s_frame.pack(fill="x", padx=15, pady=2)

        self.rem_var = tk.BooleanVar(value=self.cfg.get_bool("remember_file"))
        tk.Checkbutton(s_frame, text="Remember last file",
                       variable=self.rem_var, command=self.update_prefs).pack(side="left")

        self.log_var = tk.BooleanVar(value=self.cfg.get_bool("enable_logging"))
        tk.Checkbutton(s_frame, text="Enable Logging",
                       variable=self.log_var, command=self.update_prefs).pack(side="left", padx=20)

        # --- Status Bar ---
        self.status_var = tk.StringVar(value="Ready.")
        tk.Label(self.root, textvariable=self.status_var,
                 bd=1, relief="sunken", anchor="w", pady=4).pack(fill="x", side="bottom")

    def _toggle_panel(self):
        if self._panel_open.get():
            self._panel.pack_forget()
            self._toggle_btn.config(text="▶ Invoice Information")
            self._panel_open.set(False)
            self.root.geometry("750x220")
        else:
            self._panel.pack(fill="both", expand=True, padx=15, pady=(0,5))
            self._toggle_btn.config(text="▼ Invoice Information")
            self._panel_open.set(True)
            self.root.geometry("750x750")

    def _set_file(self, path: Path):
        self.dir_var.set(str(path.parent))
        self.file_var.set(path.name)
        if self.rem_var.get():
            self.cfg.set("last_file",      str(path))
            self.cfg.set("last_directory", str(path.parent))

    def update_prefs(self):
        self.cfg.set("remember_file",  str(self.rem_var.get()))
        self.cfg.set("enable_logging", str(self.log_var.get()))
        setup_logging(self.cfg)

    def on_browse(self):
        f = filedialog.askopenfilename(
            initialdir=self.dir_var.get(),
            filetypes=[("Excel", "*.xlsx")],
        )
        if f:
            self._set_file(Path(f))

    def on_generate(self):
        path = Path(self.dir_var.get()) / self.file_var.get()
        if not path.exists():
            show_error(self.root, f"File not found:\n{path}")
            return
        try:
            self.status_var.set(f"Reading {path.name}...")
            data  = parse_workbook(path)
            email = build_email(data)
            # Always populate hidden fields so expanding shows current data
            self.to_ent.delete(0, tk.END);   self.to_ent.insert(0, email.to)
            self.cc_ent.delete(0, tk.END);   self.cc_ent.insert(0, email.cc)
            self.subj_ent.delete(0, tk.END); self.subj_ent.insert(0, email.subject)
            self.body_txt.delete("1.0", tk.END)
            self.body_txt.insert("1.0", email.body)
            logging.info(f"Generated invoice for {data.month_label}")
            if not self._panel_open.get():
                # Collapsed: fire mailto directly
                self._open_mailto(email)
                self.status_var.set("Opened mailer.")
            else:
                self.status_var.set("Invoice email generated successfully.")
        except PermissionError as e:
            show_error(self.root, str(e))
            self.status_var.set("Permission denied — close Excel first.")
        except Exception as e:
            if is_permission_denied(e):
                show_error(self.root, workbook_locked_message(path))
                self.status_var.set("Permission denied — close Excel first.")
            else:
                logging.error(f"Generate failed: {e}")
                show_error(self.root, f"Failed to parse workbook:\n{str(e)}")
                self.status_var.set("Generation failed.")

    def on_roll(self):
        path = Path(self.dir_var.get()) / self.file_var.get()
        if not path.exists():
            return
        if not messagebox.askyesno("Confirm", f"Create next-month roll from {path.name}?"):
            return
        try:
            self.status_var.set("Rolling to next month...")
            new_path = roll_month(path)
            self._set_file(new_path)
            self.status_var.set(f"Rolled: {new_path.name}")
            messagebox.showinfo("Success", f"Created: {new_path.name}")
            logging.info(f"Rolled workbook to {new_path.name}")
        except PermissionError as e:
            show_error(self.root, str(e))
            self.status_var.set("Permission denied — close Excel first.")
        except Exception as e:
            if is_permission_denied(e):
                show_error(self.root, workbook_locked_message(path))
                self.status_var.set("Permission denied — close Excel first.")
            else:
                logging.error(f"Roll failed: {e}")
                show_error(self.root, f"Roll failed:\n{str(e)}")

    def on_copy(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.body_txt.get("1.0", tk.END).strip())
        self.status_var.set("Email body copied to clipboard.")

    def _open_mailto(self, email: InvoiceEmail):
        url = (
            f"mailto:{email.to}"
            f"?cc={quote(email.cc)}"
            f"&subject={quote(email.subject)}"
            f"&body={quote(email.body)}"
        )
        webbrowser.open(url)
        logging.info("Opened mailto link.")

    def on_mailto(self):
        from types import SimpleNamespace
        email = SimpleNamespace(
            to=self.to_ent.get(),
            cc=self.cc_ent.get(),
            subject=self.subj_ent.get(),
            body=self.body_txt.get("1.0", tk.END).strip(),
        )
        self._open_mailto(email)
        self.status_var.set("Opened mailer.")


if __name__ == "__main__":
    root = tk.Tk()
    if openpyxl is None:
        root.withdraw()
        messagebox.showerror("Error", "Required library 'openpyxl' not found.\nRun: pip install openpyxl")
        sys.exit(1)
    app = BillingAssistantGUI(root, sys.argv)
    root.mainloop()
