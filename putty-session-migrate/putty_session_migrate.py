#!/usr/bin/env python3
"""Migrate PuTTY saved sessions from Windows to Linux.

Windows stores sessions in the registry. Linux PuTTY reads text files under
~/.putty/sessions and uses a stricter file-name encoding than the registry.
This script captures the registry on Windows and installs it on Linux.

Windows (Python 3, standard library only):

    py -3 putty_session_migrate.py export --output %USERPROFILE%\\Desktop\\putty-migrate
    py -3 putty_session_migrate.py export --output %USERPROFILE%\\Desktop\\putty-migrate --ppk-dir %USERPROFILE%\\keys

Copy the putty-migrate folder to the Linux machine. It contains the raw
putty.reg capture plus session files, host keys, and any .ppk files the
script could see.

Linux:

    python3 putty_session_migrate.py install putty-migrate

install rewrites PublicKeyFile to the Linux key path, replaces FontName
values fontconfig does not have, leaves Pageant (TryAgent) sessions on the
agent, and writes ~/.putty/load-pageant.sh for the keys it copied.

Sessions that authenticate only through Pageant do not name a key file.
Export also copies keys named on the Pageant startup shortcut and the Run
key. Keys loaded into Pageant by hand are invisible to the registry; pass
--ppk or --ppk-dir on export so those files travel with the bundle.

A raw .reg file also works, if the .ppk files are available beside it or
via --keys:

    python3 putty_session_migrate.py install putty.reg --keys ./keys
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

PUTTY_REG = r"HKEY_CURRENT_USER\Software\SimonTatham\PuTTY"
PUTTY_REG_KEY = r"Software\SimonTatham\PuTTY"
SESSIONS_SUFFIX = "\\Sessions\\"
HOSTKEYS_SUFFIX = "\\SshHostKeys"
HOSTCAS_SUFFIX = "\\SshHostCAs\\"

MONO_FONTS = (
    "DejaVu Sans Mono",
    "Liberation Mono",
    "Noto Sans Mono",
    "Nimbus Mono PS",
    "Courier 10 Pitch",
)

HEX_DIGITS = set("0123456789ABCDEFabcdef")
UNIX_NAME_SAFE = set(
    b"+-._@0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)
PPK_RE = re.compile(r'"([^"]+\.ppk)"|(\S+\.ppk)', re.IGNORECASE)
WINDOWS_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
VALUE_RE = re.compile(r'^"((?:\\.|[^"\\])*)"=(.*)$')
COM_PORT_RE = re.compile(r"^COM\d+$", re.IGNORECASE)


class MigrateError(Exception):
    pass


@dataclass
class Session:
    name: str
    filename: str
    values: dict[str, str]


@dataclass
class HostKey:
    keytype: str
    port: int
    hostname: str
    key: str

    @property
    def ident(self) -> str:
        return f"{self.keytype}@{self.port}:{self.hostname}"

    def line(self) -> str:
        return f"{self.ident} {self.key}"


@dataclass
class HostCA:
    name: str
    filename: str
    values: dict[str, str]


@dataclass
class KeyFile:
    source: str
    bundle_name: str
    via: str


@dataclass
class Model:
    sessions: list[Session] = field(default_factory=list)
    host_keys: list[HostKey] = field(default_factory=list)
    host_cas: list[HostCA] = field(default_factory=list)
    keys: list[KeyFile] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def percent_decode(text: str) -> str:
    """Undo PuTTY's %XX escaping. Windows registry names and Unix filenames share this."""
    raw = bytearray()
    i = 0
    while i < len(text):
        if (
            text[i] == "%"
            and i + 2 < len(text)
            and text[i + 1] in HEX_DIGITS
            and text[i + 2] in HEX_DIGITS
        ):
            raw.append(int(text[i + 1 : i + 3], 16))
            i += 3
            continue
        encoded = text[i].encode("utf-8")
        if len(encoded) != 1:
            raise MigrateError(f"registry name is not PuTTY percent-encoding: {text!r}")
        raw.append(encoded[0])
        i += 1
    return bytes(raw).decode("utf-8", errors="replace")


def unix_session_filename(name: str) -> str:
    """Encode a session name the way PuTTY 0.83 writes ~/.putty/sessions."""
    out: list[str] = []
    for byte in name.encode("utf-8"):
        if byte in UNIX_NAME_SAFE:
            out.append(chr(byte))
        else:
            out.append(f"%{byte:02X}")
    filename = "".join(out)
    if filename in ("", ".", "..") or "/" in filename or "\\" in filename:
        raise MigrateError(f"refusing session file name {filename!r} for {name!r}")
    return filename


def unescape_reg_text(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append({"\\": "\\", '"': '"', "r": "\r", "n": "\n", "t": "\t"}.get(nxt, nxt))
            i += 2
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def logical_lines(text: str) -> list[str]:
    """Join .reg lines that regedit wrapped with a trailing backslash."""
    out: list[str] = []
    buf = ""
    for line in text.splitlines():
        buf += line
        trailing = len(buf) - len(buf.rstrip("\\"))
        if trailing % 2 == 1:
            buf = buf[:-1]
            continue
        out.append(buf)
        buf = ""
    if buf:
        out.append(buf)
    return out


def read_text_auto(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in raw[:400]:
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def signed_dword(hex_digits: str) -> str:
    number = int(hex_digits, 16)
    if number >= 0x80000000:
        number -= 0x100000000
    return str(number)


def parse_rhs(rhs: str) -> str | None:
    rhs = rhs.strip()
    if rhs.startswith("dword:"):
        return signed_dword(rhs[6:])
    if rhs.startswith("qword:"):
        number = int(rhs[6:], 16)
        if number >= 0x8000000000000000:
            number -= 0x10000000000000000
        return str(number)
    if len(rhs) >= 2 and rhs[0] == '"' and rhs[-1] == '"':
        return unescape_reg_text(rhs[1:-1])
    return None


def split_hostkey_name(reg_name: str, *, decode_host: bool = True) -> tuple[str, int, str]:
    at = reg_name.find("@")
    colon = reg_name.find(":", at + 1)
    if at < 1 or colon < 0:
        raise ValueError(reg_name)
    keytype = reg_name[:at]
    port_text = reg_name[at + 1 : colon]
    if not port_text.isdigit() or not keytype:
        raise ValueError(reg_name)
    hostname = reg_name[colon + 1 :]
    if decode_host:
        hostname = percent_decode(hostname)
    return keytype, int(port_text), hostname


def parse_reg(text: str) -> Model:
    model = Model()
    section = ""
    for line in logical_lines(text):
        if not line or line.startswith(";") or line.startswith("Windows Registry"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = unescape_reg_text(line[1:-1])
            continue
        match = VALUE_RE.match(line)
        if not match or not section.startswith(PUTTY_REG):
            continue
        name = unescape_reg_text(match.group(1))
        value = parse_rhs(match.group(2))
        if value is None:
            model.notes.append(f"skipped non-text registry value {name} under {section}")
            continue
        if section.startswith(PUTTY_REG + SESSIONS_SUFFIX):
            session_name = percent_decode(section[len(PUTTY_REG + SESSIONS_SUFFIX) :])
            _session_bucket(model, session_name)[name] = value
        elif section == PUTTY_REG + HOSTKEYS_SUFFIX:
            try:
                keytype, port, hostname = split_hostkey_name(name)
            except ValueError:
                model.notes.append(f"skipped host-key name {name!r}")
                continue
            model.host_keys.append(HostKey(keytype, port, hostname, value))
        elif section.startswith(PUTTY_REG + HOSTCAS_SUFFIX):
            ca_name = percent_decode(section[len(PUTTY_REG + HOSTCAS_SUFFIX) :])
            _ca_bucket(model, ca_name)[name] = value
    return model


def _session_bucket(model: Model, name: str) -> dict[str, str]:
    for session in model.sessions:
        if session.name == name:
            return session.values
    session = Session(name, unix_session_filename(name), {})
    model.sessions.append(session)
    return session.values


def _ca_bucket(model: Model, name: str) -> dict[str, str]:
    for ca in model.host_cas:
        if ca.name == name:
            return ca.values
    ca = HostCA(name, unix_session_filename(name), {})
    model.host_cas.append(ca)
    return ca.values


def load_registry() -> Model:
    if sys.platform != "win32":
        raise MigrateError("export reads the Windows registry; run it on Windows")
    import winreg

    model = Model()
    sessions_path = PUTTY_REG_KEY + r"\Sessions"
    try:
        sessions_root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, sessions_path)
    except OSError as exc:
        raise MigrateError(
            "no PuTTY sessions at HKCU\\Software\\SimonTatham\\PuTTY\\Sessions"
        ) from exc
    with sessions_root:
        index = 0
        while True:
            try:
                sub = winreg.EnumKey(sessions_root, index)
            except OSError:
                break
            index += 1
            name = percent_decode(sub)
            values = _read_reg_values(winreg, sessions_path + "\\" + sub)
            model.sessions.append(Session(name, unix_session_filename(name), values))
    _read_host_keys(winreg, model)
    _read_host_cas(winreg, model)
    if not model.sessions:
        raise MigrateError("the PuTTY sessions registry key is empty")
    return model


def _read_reg_values(winreg, path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
        index = 0
        while True:
            try:
                name, data, typ = winreg.EnumValue(key, index)
            except OSError:
                break
            index += 1
            converted = _reg_value_to_str(winreg, data, typ)
            if converted is None:
                continue
            values[name] = converted
    return values


def _reg_value_to_str(winreg, data, typ) -> str | None:
    if typ in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
        return str(data)
    if typ == winreg.REG_DWORD:
        number = int(data)
        if number >= 0x80000000:
            number -= 0x100000000
        return str(number)
    if typ == winreg.REG_QWORD:
        number = int(data)
        if number >= 0x8000000000000000:
            number -= 0x10000000000000000
        return str(number)
    return None


def _read_host_keys(winreg, model: Model) -> None:
    path = PUTTY_REG_KEY + r"\SshHostKeys"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:
        return
    with key:
        index = 0
        while True:
            try:
                name, data, typ = winreg.EnumValue(key, index)
            except OSError:
                break
            index += 1
            if typ != winreg.REG_SZ:
                model.notes.append(f"skipped non-text host key {name}")
                continue
            try:
                keytype, port, hostname = split_hostkey_name(name)
            except ValueError:
                model.notes.append(f"skipped host-key name {name!r}")
                continue
            model.host_keys.append(HostKey(keytype, port, hostname, str(data)))


def _read_host_cas(winreg, model: Model) -> None:
    path = PUTTY_REG_KEY + r"\SshHostCAs"
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:
        return
    with root:
        index = 0
        while True:
            try:
                sub = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            name = percent_decode(sub)
            values = _read_reg_values(winreg, path + "\\" + sub)
            model.host_cas.append(HostCA(name, unix_session_filename(name), values))


def format_pairs(values: dict[str, str]) -> str:
    lines: list[str] = []
    for key in sorted(values):
        value = values[key]
        if "\n" in key or "\r" in key or "\n" in value or "\r" in value:
            raise MigrateError(f"setting {key} contains a newline; PuTTY session files are one setting per line")
        lines.append(f"{key}={value}\n")
    return "".join(lines)


def agent_enabled(values: dict[str, str]) -> bool:
    return values.get("TryAgent", "1") not in ("0", "")


def public_key_file(values: dict[str, str]) -> str:
    return values.get("PublicKeyFile", "").strip()


def is_windows_path(path: str) -> bool:
    return WINDOWS_PATH_RE.match(path) is not None


def extract_ppk_paths(text: str) -> list[str]:
    found: list[str] = []
    for match in PPK_RE.finditer(text):
        found.append(match.group(1) or match.group(2))
    return found


def discover_pageant_ppks() -> list[tuple[str, str]]:
    """Keys named by the Run key or a Startup shortcut. Hand-loaded Pageant keys are not listed anywhere."""
    if sys.platform != "win32":
        return []
    import winreg

    found: list[tuple[str, str]] = []
    try:
        run_key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
        )
    except OSError:
        run_key = None
    if run_key is not None:
        with run_key:
            index = 0
            while True:
                try:
                    name, data, _typ = winreg.EnumValue(run_key, index)
                except OSError:
                    break
                index += 1
                text = str(data)
                if "pageant" in text.lower() or ".ppk" in text.lower():
                    for path in extract_ppk_paths(text):
                        found.append((path, f"Run value {name}"))
    script = r"""
$sh = New-Object -ComObject WScript.Shell
$dirs = @(
  [Environment]::GetFolderPath('Startup'),
  (Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\StartUp')
)
foreach ($d in $dirs) {
  if (-not (Test-Path -LiteralPath $d)) { continue }
  Get-ChildItem -LiteralPath $d -Filter *.lnk -ErrorAction SilentlyContinue | ForEach-Object {
    $s = $sh.CreateShortcut($_.FullName)
    $blob = ($s.TargetPath + ' ' + $s.Arguments).Trim()
    if ($blob -match '(?i)pageant|\.ppk') { Write-Output $blob }
  }
}
"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return found
    for line in proc.stdout.splitlines():
        for path in extract_ppk_paths(line):
            found.append((path, "Pageant startup shortcut"))
    return found


def extra_ppk_paths(ppk: list[str], ppk_dirs: list[str]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path in ppk:
        found.append((path, "--ppk"))
    for directory in ppk_dirs:
        root = Path(directory)
        if not root.is_dir():
            raise MigrateError(f"--ppk-dir is not a directory: {directory}")
        for path in sorted(root.rglob("*.ppk")):
            if path.is_file():
                found.append((str(path), f"--ppk-dir {directory}"))
    return found


def attach_private_keys(model: Model, extras: list[tuple[str, str]], keys_dir: Path) -> None:
    """Copy .ppk files next to the bundle and point sessions at keys/<file>."""
    requests: list[tuple[str, str]] = []
    for session in model.sessions:
        path = public_key_file(session.values)
        if path and is_windows_path(path):
            requests.append((path, f"session {session.name}"))
    requests.extend(extras)
    keys_dir.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    by_source: dict[str, str] = {}
    for source, via in requests:
        norm = str(PureWindowsPath(source))
        key = norm.casefold()
        if key in by_source:
            continue
        src = Path(source)
        if not src.is_file():
            model.notes.append(f"private key not found ({via}): {source}")
            continue
        name = _allocate_name(used, src.name)
        dest = keys_dir / name
        shutil.copy2(src, dest)
        try:
            os.chmod(dest, 0o600)
        except OSError:
            pass
        by_source[key] = name
        model.keys.append(KeyFile(str(src), name, via))
    for session in model.sessions:
        path = public_key_file(session.values)
        if not path or not is_windows_path(path):
            continue
        bundle_name = by_source.get(str(PureWindowsPath(path)).casefold())
        if bundle_name:
            session.values["PublicKeyFile"] = f"keys/{bundle_name}"


def _allocate_name(used: set[str], filename: str) -> str:
    base = PureWindowsPath(filename).name
    if base not in used:
        used.add(base)
        return base
    stem = Path(base).stem
    suffix = Path(base).suffix
    number = 2
    while True:
        candidate = f"{stem}-{number}{suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        number += 1


def capture_reg_file(dest: Path) -> None:
    proc = subprocess.run(
        ["reg", "export", PUTTY_REG, str(dest), "/y"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise MigrateError(f"reg export failed: {detail or proc.returncode}")


def write_bundle(model: Model, output: Path) -> None:
    sessions_dir = output / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    for session in model.sessions:
        if session.filename in seen:
            raise MigrateError(f"two sessions encode to {session.filename}")
        seen.add(session.filename)
        path = sessions_dir / session.filename
        path.write_text(format_pairs(session.values), encoding="utf-8")
    host_path = output / "sshhostkeys"
    host_lines = _unique_host_lines(model.host_keys)
    if host_lines:
        host_path.write_text("".join(f"{line}\n" for line in host_lines), encoding="utf-8")
    ca_dir = output / "sshhostcas"
    if model.host_cas:
        ca_dir.mkdir(parents=True, exist_ok=True)
        for ca in model.host_cas:
            (ca_dir / ca.filename).write_text(format_pairs(ca.values), encoding="utf-8")
    manifest = {
        "sessions": [
            {
                "name": session.name,
                "file": session.filename,
                "try_agent": agent_enabled(session.values),
                "public_key_file": public_key_file(session.values),
            }
            for session in model.sessions
        ],
        "keys": [
            {"source": item.source, "bundle": f"keys/{item.bundle_name}", "via": item.via}
            for item in model.keys
        ],
        "host_keys": len(model.host_keys),
        "host_cas": len(model.host_cas),
        "notes": model.notes,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output / "MIGRATION.txt").write_text(bundle_report(model), encoding="utf-8")


def _unique_host_lines(host_keys: list[HostKey]) -> list[str]:
    by_ident: dict[str, str] = {}
    for item in host_keys:
        by_ident[item.ident] = item.line()
    return [by_ident[ident] for ident in sorted(by_ident)]


def bundle_report(model: Model) -> str:
    lines = [
        "PuTTY export for Linux install.",
        "",
        "On the Linux machine:",
        "  python3 putty_session_migrate.py install <this-folder>",
        "",
        "install rewrites key paths to ~/.putty/keys and replaces fonts that",
        "fontconfig does not provide. Pageant sessions stay on the agent.",
        "",
        f"Sessions: {len(model.sessions)}",
    ]
    agent_only = [
        session.name
        for session in model.sessions
        if agent_enabled(session.values) and not public_key_file(session.values)
    ]
    keyed = [
        session.name
        for session in model.sessions
        if public_key_file(session.values)
    ]
    if keyed:
        lines.append("Sessions with a private-key file:")
        lines.extend(f"  {name}" for name in keyed)
    if agent_only:
        lines.append("Sessions that use Pageant and do not name a key file:")
        lines.extend(f"  {name}" for name in agent_only)
    if model.keys:
        lines.append("Private keys copied into keys/:")
        for item in model.keys:
            lines.append(f"  {item.bundle_name}  ({item.via}: {item.source})")
    elif agent_only:
        lines.append(
            "No .ppk files were found. Pageant does not record keys loaded by hand."
        )
        lines.append("Re-run export with --ppk or --ppk-dir so those keys are copied.")
    if model.host_keys:
        lines.append(f"SSH host keys: {len({item.ident for item in model.host_keys})}")
    if model.host_cas:
        lines.append(f"SSH host CAs: {len(model.host_cas)}")
    if model.notes:
        lines.append("Notes:")
        lines.extend(f"  {note}" for note in model.notes)
    lines.append("")
    return "\n".join(lines)


def export_bundle(output: Path, ppk: list[str], ppk_dirs: list[str]) -> str:
    if output.exists() and any(output.iterdir()):
        raise MigrateError(f"output directory is not empty: {output}")
    model = load_registry()
    extras = discover_pageant_ppks()
    extras.extend(extra_ppk_paths(ppk, ppk_dirs))
    output.mkdir(parents=True, exist_ok=True)
    attach_private_keys(model, extras, output / "keys")
    reg_path = output / "putty.reg"
    capture_reg_file(reg_path)
    write_bundle(model, output)
    return bundle_report(model)


def load_session_file(path: Path) -> Session:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return Session(percent_decode(path.name), path.name, values)


def load_bundle_dir(path: Path) -> Model:
    model = Model()
    sessions_dir = path / "sessions"
    if not sessions_dir.is_dir():
        raise MigrateError(f"bundle has no sessions directory: {path}")
    for session_path in sorted(sessions_dir.iterdir()):
        if session_path.is_file():
            model.sessions.append(load_session_file(session_path))
    host_path = path / "sshhostkeys"
    if host_path.is_file():
        model.host_keys.extend(_parse_hostkey_file(host_path))
    ca_dir = path / "sshhostcas"
    if ca_dir.is_dir():
        for ca_path in sorted(ca_dir.iterdir()):
            if not ca_path.is_file():
                continue
            values = load_session_file(ca_path).values
            model.host_cas.append(HostCA(percent_decode(ca_path.name), ca_path.name, values))
    if not model.sessions:
        raise MigrateError(f"no session files in {sessions_dir}")
    return model


def _parse_hostkey_file(path: Path) -> list[HostKey]:
    found: list[HostKey] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        ident, key = _split_hostkey_line(line)
        try:
            # Session files store the raw hostname. Registry names percent-encode it.
            keytype, port, hostname = split_hostkey_name(ident, decode_host=False)
        except ValueError:
            continue
        found.append(HostKey(keytype, port, hostname, key))
    return found


def _split_hostkey_line(line: str) -> tuple[str, str]:
    if " " not in line:
        raise MigrateError(f"host key line has no key data: {line!r}")
    ident, key = line.rsplit(" ", 1)
    return ident, key


def load_source(source: Path) -> tuple[Model, list[Path]]:
    """Return the model and directories that may hold .ppk files."""
    if source.is_dir():
        search = []
        keys = source / "keys"
        if keys.is_dir():
            search.append(keys)
        if (source / "sessions").is_dir():
            return load_bundle_dir(source), search
        reg = source / "putty.reg"
        if reg.is_file():
            return parse_reg(read_text_auto(reg)), search
        raise MigrateError(f"{source} is neither a session bundle nor a folder containing putty.reg")
    if source.is_file():
        model = parse_reg(read_text_auto(source))
        if not model.sessions:
            raise MigrateError(f"no PuTTY sessions found in {source}")
        search = []
        sibling = source.parent / "keys"
        if sibling.is_dir():
            search.append(sibling)
        return model, search
    raise MigrateError(f"not found: {source}")


def installed_font_families() -> set[str]:
    try:
        proc = subprocess.run(
            ["fc-list", ":", "family"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return set()
    if proc.returncode != 0:
        return set()
    families: set[str] = set()
    for line in proc.stdout.splitlines():
        for part in line.split(","):
            part = part.strip()
            if part:
                families.add(part.casefold())
    return families


def font_is_installed(name: str, families: set[str]) -> bool:
    cleaned = name.strip()
    if cleaned.casefold() in families:
        return True
    match = re.fullmatch(r"(.+?)\s+\d+", cleaned)
    return bool(match and match.group(1).casefold() in families)


def replacement_font(families: set[str]) -> str | None:
    for candidate in MONO_FONTS:
        if candidate.casefold() in families:
            return candidate
    if families:
        return "Monospace"
    return None


def fix_fonts(model: Model, families: set[str]) -> list[str]:
    changes: list[str] = []
    if not families:
        model.notes.append("fc-list is not available, so font names were left unchanged")
        return changes
    fallback = replacement_font(families)
    if fallback is None:
        return changes
    for session in model.sessions:
        for key in list(session.values):
            if not key.endswith("FontName"):
                continue
            current = session.values[key]
            if not current.strip() or font_is_installed(current, families):
                continue
            session.values[key] = fallback
            changes.append(f"{session.name}: {key} {current} -> {fallback}")
    return changes


def find_ppk(windows_path: str, search_dirs: list[Path]) -> Path | None:
    base = PureWindowsPath(windows_path).name.casefold()
    parent = PureWindowsPath(windows_path).parent.name.casefold()
    hits: list[Path] = []
    seen: set[str] = set()
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.ppk"):
            if not path.is_file() or path.name.casefold() != base:
                continue
            resolved = str(path.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            hits.append(path)
    if len(hits) == 1:
        return hits[0]
    narrowed = [path for path in hits if path.parent.name.casefold() == parent]
    if len(narrowed) == 1:
        return narrowed[0]
    return None


def place_private_keys(
    model: Model,
    dest_keys: Path,
    search_dirs: list[Path],
    force: bool,
) -> tuple[list[str], list[str]]:
    """Point PublicKeyFile at dest_keys and copy any key file we can see.

    Returns (copied descriptions, warnings). Does not write session files.
    """
    copied: list[str] = []
    warnings: list[str] = []
    assigned: dict[str, str] = {}

    def place(source: Path, preferred_name: str) -> str:
        name = preferred_name
        dest = dest_keys / name
        if dest.exists() and not _same_bytes(source, dest):
            if not force:
                raise MigrateError(
                    f"{dest} already exists and differs from {source}; pass --force to replace it"
                )
        assigned[str(source.resolve())] = name
        return name

    planned: list[tuple[Session, str]] = []
    for session in model.sessions:
        current = public_key_file(session.values)
        if not current:
            continue
        if current.startswith("keys/") and not is_windows_path(current):
            bundle_name = PureWindowsPath(current[5:]).name
            source = _find_named(search_dirs, bundle_name)
            if source is None:
                warnings.append(
                    f"{session.name}: bundle key {current} was not found; "
                    f"expected it under {dest_keys / bundle_name}"
                )
                session.values["PublicKeyFile"] = str(dest_keys / bundle_name)
                continue
            place(source, bundle_name)
            planned.append((session, str(source.resolve())))
            continue
        if is_windows_path(current):
            source = find_ppk(current, search_dirs)
            name = PureWindowsPath(current).name
            if source is None:
                warnings.append(
                    f"{session.name}: private key was not copied ({current}). "
                    f"Put the file at {dest_keys / name}"
                )
                session.values["PublicKeyFile"] = str(dest_keys / name)
                continue
            # Keep a stable name when several sessions share one file.
            existing = assigned.get(str(source.resolve()))
            if existing is None:
                place(source, _free_name(dest_keys, assigned.values(), name))
                existing = assigned[str(source.resolve())]
            planned.append((session, str(source.resolve())))
            continue
        # Already a path for this machine.
    copies: dict[str, Path] = {}
    for session, source_key in planned:
        source = Path(source_key)
        name = assigned[str(source.resolve())]
        session.values["PublicKeyFile"] = str(dest_keys / name)
        copies[name] = source
    dest_keys.mkdir(parents=True, exist_ok=True)
    os.chmod(dest_keys, 0o700)
    for name, source in sorted(copies.items()):
        dest = dest_keys / name
        if dest.exists() and _same_bytes(source, dest):
            copied.append(f"{name} already installed")
            continue
        shutil.copy2(source, dest)
        os.chmod(dest, 0o600)
        copied.append(f"{source} -> {dest}")
    # Keys that are only for Pageant (startup / --ppk) live in search dirs
    # and may not be named by any session. Copy every .ppk we were given.
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for source in sorted(directory.glob("*.ppk")):
            if not source.is_file():
                continue
            try:
                source.resolve().relative_to(dest_keys.resolve())
            except ValueError:
                pass
            else:
                continue
            resolved = str(source.resolve())
            if resolved in assigned:
                continue
            name = _free_name(dest_keys, list(assigned.values()) + list(copies), source.name)
            dest = dest_keys / name
            if dest.exists() and not _same_bytes(source, dest):
                if not force:
                    raise MigrateError(
                        f"{dest} already exists and differs from {source}; pass --force to replace it"
                    )
            if not (dest.exists() and _same_bytes(source, dest)):
                shutil.copy2(source, dest)
                os.chmod(dest, 0o600)
                copied.append(f"{source} -> {dest} (Pageant key, no session path)")
            assigned[resolved] = name
            copies[name] = source
    return copied, warnings


def _find_named(search_dirs: list[Path], name: str) -> Path | None:
    matches = []
    for directory in search_dirs:
        candidate = directory / name
        if candidate.is_file():
            matches.append(candidate)
    if len(matches) == 1:
        return matches[0]
    return None


def _free_name(dest_keys: Path, taken, filename: str) -> str:
    used = {Path(name).name for name in taken}
    if not (dest_keys / filename).exists() and filename not in used:
        return filename
    return _allocate_name(set(used), filename)


def _same_bytes(left: Path, right: Path) -> bool:
    if not left.is_file() or not right.is_file():
        return False
    return hashlib.sha256(left.read_bytes()).digest() == hashlib.sha256(right.read_bytes()).digest()


def linux_putty_dir() -> Path:
    override = os.environ.get("PUTTYDIR")
    if override:
        return Path(override)
    xdg_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_home:
        xdg_dir = Path(xdg_home) / "putty"
    else:
        xdg_dir = Path.home() / ".config" / "putty"
    legacy = Path.home() / ".putty"
    # PuTTY prefers an existing ~/.config/putty over ~/.putty.
    if xdg_dir.is_dir():
        return xdg_dir
    return legacy


def other_windows_paths(model: Model) -> list[str]:
    notes: list[str] = []
    for session in model.sessions:
        for key, value in sorted(session.values.items()):
            if key == "PublicKeyFile" or not value:
                continue
            if WINDOWS_PATH_RE.search(value):
                notes.append(f"{session.name}: left {key} as a Windows path ({value})")
            elif key == "SerialLine" and COM_PORT_RE.match(value):
                notes.append(f"{session.name}: left serial port {value}")
    return notes


def write_pageant_helper(dest: Path, key_paths: list[Path]) -> Path | None:
    if not key_paths:
        return None
    helper = dest / "load-pageant.sh"
    quoted = " \\\n  ".join(json.dumps(str(path)) for path in key_paths)
    helper.write_text(
        "#!/bin/sh\n"
        "# Start Pageant for this login if needed, then load migrated .ppk keys.\n"
        "# Pageant asks for each passphrase. Sessions with TryAgent=1 use these keys.\n"
        "if ! pageant -l >/dev/null 2>&1; then\n"
        "  eval \"$(pageant -X)\"\n"
        "fi\n"
        f"pageant -a \\\n  {quoted}\n",
        encoding="utf-8",
    )
    os.chmod(helper, 0o700)
    return helper


def install_model(
    model: Model,
    dest: Path,
    search_dirs: list[Path],
    force: bool,
) -> str:
    families = installed_font_families()
    font_changes = fix_fonts(model, families)
    sessions_dir = dest / "sessions"
    ca_dir = dest / "sshhostcas"
    collisions = [
        sessions_dir / session.filename
        for session in model.sessions
        if (sessions_dir / session.filename).exists()
    ]
    ca_collisions = [
        ca_dir / ca.filename for ca in model.host_cas if (ca_dir / ca.filename).exists()
    ]
    if (collisions or ca_collisions) and not force:
        names = [str(path) for path in collisions + ca_collisions]
        raise MigrateError(
            "these files already exist; pass --force to replace them:\n  "
            + "\n  ".join(names)
        )
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    copied, key_warnings = place_private_keys(model, dest / "keys", search_dirs, force)
    model.notes.extend(key_warnings)
    model.notes.extend(other_windows_paths(model))
    sessions_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(sessions_dir, 0o700)
    for session in model.sessions:
        path = sessions_dir / session.filename
        path.write_text(format_pairs(session.values), encoding="utf-8")
        os.chmod(path, 0o600)
    host_written = _merge_hostkeys(dest / "sshhostkeys", model.host_keys)
    if model.host_cas:
        ca_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(ca_dir, 0o700)
        for ca in model.host_cas:
            path = ca_dir / ca.filename
            path.write_text(format_pairs(ca.values), encoding="utf-8")
            os.chmod(path, 0o600)
    key_paths = sorted((dest / "keys").glob("*.ppk")) if (dest / "keys").is_dir() else []
    helper = write_pageant_helper(dest, key_paths)
    return install_report(model, dest, font_changes, copied, host_written, helper)


def _merge_hostkeys(path: Path, host_keys: list[HostKey]) -> int:
    incoming = {item.ident: item.line() for item in host_keys}
    if not incoming:
        return 0
    existing: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or " " not in line:
                continue
            ident, _key = _split_hostkey_line(line)
            existing[ident] = line
    existing.update(incoming)
    path.write_text("".join(f"{existing[ident]}\n" for ident in sorted(existing)), encoding="utf-8")
    os.chmod(path, 0o600)
    return len(incoming)


def install_report(
    model: Model,
    dest: Path,
    font_changes: list[str],
    copied: list[str],
    host_written: int,
    helper: Path | None,
) -> str:
    lines = [f"Installed {len(model.sessions)} PuTTY sessions in {dest / 'sessions'}", ""]
    agent_only = [
        session.name
        for session in model.sessions
        if agent_enabled(session.values) and _session_key_missing(session)
    ]
    if agent_only:
        lines.append("These sessions use Pageant and do not point at a key file:")
        lines.extend(f"  {name}" for name in agent_only)
        lines.append("Load the same .ppk files into Linux Pageant before connecting.")
    if copied:
        lines.append("Private keys:")
        lines.extend(f"  {item}" for item in copied)
    if font_changes:
        lines.append("Fonts changed (the Windows font is not installed here):")
        lines.extend(f"  {item}" for item in font_changes)
    else:
        lines.append("Fonts: no FontName needed a substitute.")
    if host_written:
        lines.append(f"SSH host keys merged into {dest / 'sshhostkeys'}: {host_written}")
    if model.host_cas:
        lines.append(f"SSH host CAs: {len(model.host_cas)}")
    if helper:
        lines.append(f"Pageant helper: {helper}")
        lines.append("Run it in a login session when you want those keys available:")
        lines.append(f"  {helper}")
    if model.notes:
        lines.append("Notes:")
        lines.extend(f"  {note}" for note in model.notes)
    lines.append("")
    lines.append("Open PuTTY and the saved-session list should match Windows.")
    lines.append("")
    return "\n".join(lines)


def _session_key_missing(session: Session) -> bool:
    """True when the session still has no key file after path rewrite."""
    current = public_key_file(session.values)
    if not current:
        return True
    return False


def default_putty_dir_note(dest: Path) -> str:
    return str(dest)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Copy PuTTY saved sessions from Windows to Linux.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Windows: export. Linux: install. See the top of this file for the full sequence.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="Windows: read the PuTTY registry and write a bundle")
    export.add_argument("--output", required=True, type=Path, help="new folder to write")
    export.add_argument("--ppk", action="append", default=[], help="extra .ppk to copy (repeatable)")
    export.add_argument(
        "--ppk-dir",
        action="append",
        default=[],
        help="directory of .ppk files to copy, including keys only held in Pageant",
    )

    install = sub.add_parser("install", help="Linux: install a bundle or a .reg file into PuTTY")
    install.add_argument("source", type=Path, help="export folder, or a putty.reg file")
    install.add_argument(
        "--dest",
        type=Path,
        help="PuTTY config directory (default: ~/.putty, or ~/.config/putty if that already exists)",
    )
    install.add_argument("--keys", type=Path, help="extra directory of .ppk files")
    install.add_argument("--force", action="store_true", help="replace existing session files and keys")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            report = export_bundle(args.output, args.ppk, args.ppk_dir)
        else:
            if sys.platform != "linux" and args.dest is None:
                raise MigrateError("install writes the Linux PuTTY config; run it on Linux or pass --dest")
            dest = args.dest if args.dest is not None else linux_putty_dir()
            model, search = load_source(args.source)
            if args.keys is not None:
                if not args.keys.is_dir():
                    raise MigrateError(f"--keys is not a directory: {args.keys}")
                search.append(args.keys)
            report = install_model(model, dest, search, args.force)
    except MigrateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(report, end="" if report.endswith("\n") else "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
