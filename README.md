# handy-scripts

Personal one-off scripts and small utilities.

**Layout:** each tool lives in its own directory with a `README.md` (opens under the file list on GitHub).

No installable package — clone the repo, or fetch a single file from raw GitHub.

| Tool | Platform | Notes |
| --- | --- | --- |
| [billing-assistant](billing-assistant/) | Windows | Time tracking and email invoicing helper (Python `.pyw` + Excel). |
| [upgrade-to-trixie](upgrade-to-trixie/) | Linux (Debian) | Debian 12 → 13 upgrade helper (bash). |
| [openvpn-killswitch](openvpn-killswitch/) | Windows | Firewall Arm/Disarm kill switch for OpenVPN GUI (PowerShell + WinForms). |

**Repo rules for Windows tools:** no binaries, no build/compile steps. Scripts only.
