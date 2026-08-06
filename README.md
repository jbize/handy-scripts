# handy-scripts

Personal one-off scripts and small utilities (Python, shell, etc.).

Not an installable package — run scripts from a clone, or fetch a single file:

```bash
wget -O upgrade_to_trixie.sh \
  https://raw.githubusercontent.com/jbize/handy-scripts/main/shell/upgrade_to_trixie.sh
chmod +x upgrade_to_trixie.sh
```

## Layout

- `python/` — `.py` / `.pyw` utilities
- `shell/` — shell scripts

## Contents

| Script | Notes |
| --- | --- |
| `python/billing_assistant.pyw` | Billing helper (Windows `.pyw`-friendly). Reads invoice details from a local Excel workbook; no secrets in-repo. |
| `shell/upgrade_to_trixie.sh` | Debian 12 → 13 one-shot. Ends with `autoremove`/`autoclean` and a leftover report. Later: `sudo ./upgrade_to_trixie.sh --cleanup-only --yes` removes apt `*.bak` and purges `rc` packages; flags `.dpkg-old`/`.dpkg-dist`. |
