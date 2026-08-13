# Billing assistant

**Platform:** Windows

A simple time-tracking and billing / email-invoicing system for Windows.
Easy to set up: track hourly work in a spreadsheet through the month, then
generate an invoice email when it's time to bill.

The helper is a small Python app (`billing_assistant.pyw`) that runs without a
console window. It reads rate, hours, and amount from your timesheet, plus
client and payment details from a `__CONFIG__` block in the same workbook.

Typical use: right-click a timesheet → **Send to** → **Billing Assistant**.

## First-time setup

### 1. Install the tools

- Windows with Python 3 (Tkinter is included with most installs)
- Make sure `.pyw` files open with `pythonw` (that is the Windows Python
  launcher for scripts without a console window)
- Install the Excel library: `pip install openpyxl`

### 2. Wire up “Send to”

1. Put `billing_assistant.pyw` somewhere permanent (this folder, or a copy under
   something like `%USERPROFILE%\bin`). The shortcut must keep pointing there.
2. Press **Win+R**, type `shell:sendto`, press Enter.
3. Right-click in that folder → **New** → **Shortcut** → browse to
   `billing_assistant.pyw` → name it `Billing Assistant`.

You can also double-click the `.pyw` and use **Browse...**. App preferences
(last file, logging) live in `%APPDATA%\Billing Assistant\config.ini`.

### 3. Create your timesheet from the model

1. Copy `Time Tracking_2026-01.xlsx` and rename it for your work, keeping the
   `_YYYY-MM` suffix (for example `Acme Consulting_2026-03.xlsx`). Month roll
   preserves whatever basename you choose.
2. Open the copy in Excel and fill in the basics:
   - **A1**: your name / label as you want it on the sheet
   - **E1**: hourly rate
   - **D5**: first day of the billing month (a real date). Fill or drag the
     date column down for the rest of the month (the model already has this
     pattern; **Roll to Next Month** will redo it for you later).
3. Edit the **`__CONFIG__`** block (column **Q** / **R** in the model) with your
   real values. At minimum set:

   | Key | What to put |
   |-----|-------------|
   | `client_greeting` | How the email opens (e.g. `Jasper`) |
   | `client_email` | Client To: as `Name <email>` (e.g. `Client Name <client@example.com>`) |
   | `cc_email` | Optional CC, same `Name <email>` form (often yourself) |
   | `sender_first_name` | Your first name on the sign-off / subject |
   | `invoice_slug` | Short label in the subject (e.g. `acme-invoice`) |
   | `current_payment` | Which method appears in the email: `paypal`, `zelle`, or `check` (or a custom key) |
   | matching payment rows | Fill the rows for methods you use, e.g. `check_address`, `paypal_email` / `paypal_me`, `zelle_address` |

   Use `Display Name <address@example.com>` for `client_email` and `cc_email` so
   your mailer shows names instead of bare addresses. Leave unused payment rows
   blank or keep them as backups. See
   [Configuration reference](#configuration-reference) for every key.

4. Close Excel (the assistant cannot read a locked workbook), then right-click
   the file → **Send to** → **Billing Assistant** and try **Generate Invoice
   Email** once with empty hours to confirm addresses and payment text look right.
   If the model month is behind today's calendar month, **Roll to Next Month**
   will offer to jump straight to the current month (see below).

## Day to day

1. Open this month's `.xlsx` in Excel. Today's date in the date column is
   highlighted in red so you can find the current row quickly.
2. On the row for today's date, enter **In** / **Out** times. Each day has up
   to **three** working periods (three In/Out pairs: columns **G/H**, **J/K**,
   and **M/N**) for breaks or split sessions. Leave unused pairs blank.
3. Excel tip (shown on the sheet): **Ctrl+Shift+;** inserts the current time.
4. Hours for the day are calculated from those times; monthly totals and the
   amount due update from the sheet formulas.

You only need the Billing Assistant app when you want to send an invoice or
roll the file to a new month, not for daily logging.

## End of the month

1. Close the workbook in Excel.
2. Right-click it → **Send to** → **Billing Assistant**.
3. **Generate Invoice Email**: builds To / CC / Subject / body from the sheet
   and opens your mailer (or fills the Invoice Information panel if that
   section is expanded). Attach the timesheet if your client expects it, then
   send.
4. **Roll to Next Month**: copies the file forward, advances the dates, clears
   time entries, and selects the new workbook so you are ready for the next
   cycle.

   - Normally this advances one month after **D5** (the primary date column).
     The new `_YYYY-MM` suffix comes from that target month, not from today's
     clock. The part before `_YYYY-MM` is kept from the current filename
     (`Acme Consulting_2026-03.xlsx` → `Acme Consulting_2026-04.xlsx`).
   - If **D5** is already behind the real calendar month (for example you
     opened the January model in August), the app asks whether to create the
     **current** month or only roll to the **next** month after D5. Use
     current month when starting from the blank model; use next month when you
     intentionally want a one-month step.

## Configuration reference

### Workbook cells the app reads

| Cell | Meaning |
|------|---------|
| **E1** | Hourly rate |
| **D5** | First day of the billing month (a real date) |
| **B15** | Total hours (usually a formula) |
| **B18** | Amount due (usually hours × rate) |
| **D5:D35** | Calendar dates for the month (roll fills these) |
| **G/H, J/K, M/N** | Up to three In/Out pairs per day (cleared on roll) |

### `__CONFIG__` block

Somewhere on the sheet (the model uses column **Q**), put the marker
`__CONFIG__`. Directly below it, key/value pairs in adjacent columns
(key left, value right). Keys are matched case-insensitively; spaces become
underscores (`Client Email` → `client_email`).

#### Email / identity

| Key | Used for |
|-----|----------|
| `client_greeting` | Salutation in the email body |
| `client_email` | To: address; prefer `Name <email@example.com>` so the mailer shows a name |
| `cc_email` | CC: address (preferred); same `Name <email>` form |
| `sender_email` | CC: fallback if `cc_email` is empty |
| `sender_first_name` | Sign-off name and subject prefix |
| `invoice_slug` | Short label in subject/body |

Subject line shape: `{sender_first_name}-{invoice_slug} ({Month-YYYY})`.

CC resolution order: `cc_email` → `sender_email` → `paypal_email`.

#### Payment (`current_payment`)

The email's payment section is controlled by **`current_payment`**. Set it to
one of the built-in methods below, or to any other config key whose value is
free-form payment instructions. You can keep several payment rows filled in
the sheet and switch methods by changing only `current_payment`.

| `current_payment` | Related keys | Email payment section |
|-------------------|--------------|------------------------|
| `paypal` | `paypal_email`, `paypal_me` | Friends & Family line and/or `https://paypal.me/{paypal_me}/{amount}` |
| `zelle` | `zelle_address` | Zelle destination |
| `check` | `check_address` | Asks the client to mail a check to that address |
| *(other)* | that same key as a config row | Value of that key, used as-is |

If `current_payment` is empty, or names an unknown key with no value, the
payment section is omitted from the email. If you set `check` but leave
`check_address` blank, the email still asks for payment by check without an
address.

Built-in examples:

```text
current_payment    check
check_address      123 Main St, City, ST 00000
```

```text
current_payment    paypal
paypal_email       you@example.com
paypal_me          YourPayPalMe
```

```text
current_payment    zelle
zelle_address      you@example.com
```

Custom method example:

```text
current_payment    pigeon
pigeon             Please send payment by carrier pigeon to ...
```

#### Optional / unused by the app

| Key | Notes |
|-----|--------|
| `payment_note` | Present in the model for your own notes; not read by the script |

### App settings (`config.ini`)

Stored under `%APPDATA%\Billing Assistant\`, not in the workbook.

| Setting | Meaning |
|---------|---------|
| `last_directory` / `last_file` | Last browsed workbook (when “Remember last file” is on) |
| `remember_file` | Persist last workbook path |
| `enable_logging` | Write `billing_assistant.log` in the same AppData folder |
