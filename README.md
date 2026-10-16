# handy-scripts

Personal one-off scripts and small utilities.

**Layout:** each tool lives in its own directory with a `README.md` (opens under the file list on GitHub).

No installable package — clone the repo, or fetch a single file from raw GitHub.

| Tool | Platform | Notes |
| --- | --- | --- |
| [billing-assistant](billing-assistant/) | Windows | Time tracking and email invoicing helper (Python `.pyw` + Excel). |
| [upgrade-to-trixie](upgrade-to-trixie/) | Linux (Debian) | Debian 12 → 13 upgrade helper (bash). |
| [openvpn-killswitch](openvpn-killswitch/) | Windows | Firewall Arm/Disarm kill switch for OpenVPN GUI (PowerShell + WinForms). |
| [window-ac](window-ac/) | Windows | Click-to-open Smart Life / Tuya window AC panel (Python `.pyw`). |
| [putty-session-migrate](putty-session-migrate/) | Windows and Linux | Copy PuTTY saved sessions from the Windows registry into Linux `~/.putty` (Python 3). |
| [renew-cert-dns](renew-cert-dns/) | Linux | Let's Encrypt renewal for `fringes.godswind.org` by a DNS TXT record (bash). |

**Repo rules for Windows tools:** no binaries, no build/compile steps. Scripts only.
