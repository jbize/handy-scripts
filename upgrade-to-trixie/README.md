# Upgrade to Trixie

Debian 12 (Bookworm) → Debian 13 (Trixie) upgrade helper.

**Safety model:** fail closed. Default is preflight only. Live upgrade requires `--yes` (15s cold-feet pause; `--now` skips it).

## Typical use

```bash
sudo ./upgrade_to_trixie.sh                # preflight only
sudo ./upgrade_to_trixie.sh --yes          # upgrade after preflight + pause
sudo reboot
# later, when apt-source rollback is no longer needed:
sudo ./upgrade_to_trixie.sh --cleanup-only --yes
```

See `./upgrade_to_trixie.sh -h` for flags.
