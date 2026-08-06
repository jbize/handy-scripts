#!/bin/bash
set -euo pipefail

# Robust Debian 12 (Bookworm) to Debian 13 (Trixie) Upgrade Script
# Current Date: 2026-03-15
# Target: Debian 13 "Trixie" (Stable)
# Kernel: 6.12 series

TARGET_CODENAME="trixie"
SOURCE_CODENAME="bookworm"
BACKUP_TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

# 1. Root check
if [[ $EUID -ne 0 ]]; then
  echo "ERROR: This script must be run as root (use: sudo $0)" >&2
  exit 1
fi

# 2. Verify current Debian release
if [[ -r /etc/os-release ]]; then
  . /etc/os-release
else
  echo "ERROR: /etc/os-release not found. Aborting." >&2
  exit 1
fi

if [[ "${ID:-}" != "debian" ]]; then
  echo "ERROR: This system is not identified as Debian. Aborting." >&2
  exit 1
fi

if [[ "${VERSION_CODENAME:-}" != "$SOURCE_CODENAME" ]]; then
  echo "ERROR: This script is intended for Debian '$SOURCE_CODENAME' only."
  echo "Detected VERSION_CODENAME='${VERSION_CODENAME:-unknown}'."
  exit 1
fi

echo "----------------------------------------------------------"
echo "Debian 12 to 13 Upgrade Script"
echo "Target: ${TARGET_CODENAME}"
echo "----------------------------------------------------------"

# 3. Backup function
backup_file() {
  local f="$1"
  if [[ -f "$f" ]]; then
    local backup="${f}.${BACKUP_TIMESTAMP}.bak"
    cp -a "$f" "$backup"
    echo "Backup created: $backup"
  fi
}

echo "Step 1: Backing up APT source lists..."
backup_file /etc/apt/sources.list

if [[ -d /etc/apt/sources.list.d ]]; then
  for f in /etc/apt/sources.list.d/*.list; do
    [[ -e "$f" ]] || continue
    backup_file "$f"
  done
fi

# 4. Update official Debian repos
echo "Step 2: Updating /etc/apt/sources.list to ${TARGET_CODENAME}..."
sed -i \
  -e "s#\b${SOURCE_CODENAME}-security\b#${TARGET_CODENAME}-security#g" \
  -e "s#\b${SOURCE_CODENAME}-updates\b#${TARGET_CODENAME}-updates#g" \
  -e "s#\b${SOURCE_CODENAME}\b#${TARGET_CODENAME}#g" \
  /etc/apt/sources.list

# 5. Update package lists
echo "Step 3: Running apt update..."
apt update

# 6. Minimal upgrade first (Safety Step)
echo "Step 4: Running minimal upgrade (apt upgrade --without-new-pkgs)..."
apt upgrade --without-new-pkgs -y

# 7. Full distribution upgrade
echo "Step 5: Running full distribution upgrade (apt full-upgrade)..."
echo "This may take a while and require user interaction for config files."
apt full-upgrade -y

echo "----------------------------------------------------------"
echo "Upgrade process finished."
echo "Please check for any .dpkg-dist or .dpkg-old files in /etc/."
echo "REBOOT REQUIRED to load the new kernel (6.12)."
echo "Command: sudo reboot"
echo "----------------------------------------------------------"
