# PuTTY session migrate

**Platform:** Windows and Linux

Copy PuTTY saved sessions from the Windows registry into the text files Linux PuTTY reads under `~/.putty`.

Windows keeps sessions in `HKCU\Software\SimonTatham\PuTTY`. Linux uses one file per session, a different file-name encoding, and fontconfig font names. The script captures the registry on Windows and installs it on Linux.

Python 3 only. No extra packages.

## Windows

```bat
py -3 putty_session_migrate.py export --output %USERPROFILE%\Desktop\putty-migrate
```

That writes `putty.reg` plus Linux-format session files. It also copies `.ppk` files named by a session, by the Pageant startup shortcut, or by the Run key.

Keys you loaded into Pageant by hand are not in the registry. Point export at the folder that holds them:

```bat
py -3 putty_session_migrate.py export --output %USERPROFILE%\Desktop\putty-migrate --ppk-dir %USERPROFILE%\keys
```

Copy the whole `putty-migrate` folder to the Linux machine. It contains private keys. Do not commit it.

## Linux

```bash
python3 putty_session_migrate.py install putty-migrate
```

`install` writes `~/.putty` (or `~/.config/putty` if that directory already exists):

- Session files go in `sessions/`. Existing session files are left alone unless you pass `--force`.
- `PublicKeyFile` is rewritten to `~/.putty/keys/`, and the `.ppk` files are copied there (`0600`).
- A `FontName` is replaced only when `fc-list` shows that face is not installed. Fonts that are installed stay as they are.
- Sessions that use Pageant (`TryAgent`) and do not name a key file stay that way. `~/.putty/load-pageant.sh` starts Linux Pageant and loads the copied keys. Pageant asks for each passphrase.

```bash
~/.putty/load-pageant.sh
```

Then open `putty`. The saved-session list should match Windows.

A raw `.reg` file works too, if the `.ppk` files are available:

```bash
python3 putty_session_migrate.py install putty.reg --keys ./keys
```

Log-file paths and `COM` ports are reported and left unchanged.

See `python3 putty_session_migrate.py -h` for flags.
