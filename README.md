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
| `shell/upgrade_to_trixie.sh` | Debian 12 → 13. Default = preflight only; `--yes` required to upgrade (15s cold-feet pause; `--now` skips it). Ends with leftover report. Later: `--cleanup-only --yes`. |
