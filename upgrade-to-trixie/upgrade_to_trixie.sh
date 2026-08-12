#!/bin/bash
set -euo pipefail

# Debian 12 (Bookworm) → Debian 13 (Trixie) upgrade helper
#
# Safety model: fail closed. Preflight must prove the *post-change* apt
# configuration can `apt-get update` successfully before any real files
# are modified. Suite rewrites apply to all apt source files that still
# name Bookworm (or Debian_12), including third-party lines like Docker.

TARGET_CODENAME="trixie"
SOURCE_CODENAME="bookworm"
BACKUP_TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
# Matches backups this script writes: file.YYYYMMDD-HHMMSS.bak
BACKUP_GLOB_RE='\.[0-9]{8}-[0-9]{6}\.bak$'
MIN_FREE_GIB=4
CHECK_ONLY=0
CLEANUP_ONLY=0
ASSUME_YES=0
# Seconds to wait after preflight before live mutations (--yes). Ctrl-C aborts.
COLD_FEET_SECS=15
SKIP_COLD_FEET=0

usage() {
  cat <<'EOF'
Debian 12 (Bookworm) → 13 (Trixie). One-shot, fail-closed.

Default (no flags): preflight only — no system changes.
Mutations require an affirmative flag.

Typical server use:
  sudo ./upgrade_to_trixie.sh                # preflight / dry prove-out
  sudo ./upgrade_to_trixie.sh --yes          # upgrade after preflight + cold-feet pause
  sudo reboot
  # a few days later, when you no longer need apt-source rollback:
  sudo ./upgrade_to_trixie.sh --cleanup-only --yes

  --yes            Affirmative: perform the upgrade (or cleanup deletions with
                   --cleanup-only). Required for any live changes.
  --now            Skip the post-preflight cold-feet countdown (with --yes).
  --check-only     Same as default: preflight only (explicit alias).
  --cleanup-only   Later pass: with --yes, remove this script's apt *.bak and
                   purge rc packages. Always flags .dpkg-old/.dpkg-dist.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check-only) CHECK_ONLY=1 ;;
    --cleanup-only) CLEANUP_ONLY=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --now) SKIP_COLD_FEET=1 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "ERROR: Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
  shift
done

die() {
  echo "ERROR: $*" >&2
  exit 1
}

warn() {
  echo "WARNING: $*" >&2
}

if (( CHECK_ONLY && CLEANUP_ONLY )); then
  die "Use only one of --check-only or --cleanup-only."
fi

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

# Rewrite Debian release codenames (and common Debian_12 path tokens).
rewrite_codenames_stream() {
  sed \
    -e "s#\b${SOURCE_CODENAME}-security\b#${TARGET_CODENAME}-security#g" \
    -e "s#\b${SOURCE_CODENAME}-updates\b#${TARGET_CODENAME}-updates#g" \
    -e "s#\b${SOURCE_CODENAME}-backports\b#${TARGET_CODENAME}-backports#g" \
    -e "s#\b${SOURCE_CODENAME}\b#${TARGET_CODENAME}#g" \
    -e 's#Debian_12#Debian_13#g'
}

file_needs_rewrite() {
  local f="$1"
  grep -Eq "\b${SOURCE_CODENAME}\b|Debian_12" "$f"
}

# Build a temporary apt configuration that mirrors the live tree, with
# planned codename rewrites applied. Used only for preflight verification.
build_planned_apt_tree() {
  local dest="$1"
  local f base rewritten=0

  mkdir -p "$dest/sources.list.d"

  if [[ -f /etc/apt/sources.list ]]; then
    if file_needs_rewrite /etc/apt/sources.list; then
      rewrite_codenames_stream < /etc/apt/sources.list > "$dest/sources.list"
      rewritten=1
      echo "  plan rewrite: /etc/apt/sources.list"
    else
      cp -a /etc/apt/sources.list "$dest/sources.list"
    fi
  else
    : > "$dest/sources.list"
  fi

  shopt -s nullglob
  for f in /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
    [[ -f "$f" ]] || continue
    [[ "$f" == *.bak ]] && continue
    base="$(basename "$f")"
    if file_needs_rewrite "$f"; then
      rewrite_codenames_stream < "$f" > "$dest/sources.list.d/$base"
      rewritten=1
      echo "  plan rewrite: $f"
    else
      cp -a "$f" "$dest/sources.list.d/$base"
    fi
  done
  shopt -u nullglob

  (( rewritten )) || die "No apt source files contain '${SOURCE_CODENAME}' or 'Debian_12' to rewrite. Aborting rather than a no-op upgrade."
}

# Prove the planned (rewritten) apt config can update before touching the real one.
verify_planned_apt_update() {
  local tmp planned_list planned_parts
  tmp="$(mktemp -d /tmp/upgrade-to-trixie.XXXXXX)"
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp'" RETURN

  echo "Building planned apt sources (no live files changed yet)..."
  build_planned_apt_tree "$tmp"

  planned_list="$tmp/sources.list"
  planned_parts="$tmp/sources.list.d"

  echo "Verifying planned sources with apt-get update (fail closed if this fails)..."
  if ! apt-get \
    -o Dir::Etc::sourcelist="$planned_list" \
    -o Dir::Etc::sourceparts="$planned_parts" \
    -o APT::Get::List-Cleanup=0 \
    update; then
    echo >&2
    die "Planned ${TARGET_CODENAME} apt configuration failed 'apt-get update'. Fix or disable the failing repo, then retry. No live apt sources were changed."
  fi
  echo "Planned apt configuration verified."
}

# --- Preflight (no mutations of live apt sources) -----------------------------

preflight() {
  local free_kib free_gib held audit_out

  echo
  echo "######################################################################"
  echo "# PHASE: PREFLIGHT (read-only — no suite change, no package upgrade) #"
  echo "######################################################################"
  echo "Refuse to mutate if a mid-upgrade failure looks likely."
  echo

  if [[ $EUID -ne 0 ]]; then
    die "Must run as root (use: sudo $0). Preflight needs root for apt/dpkg checks."
  fi

  [[ -r /etc/os-release ]] || die "/etc/os-release not found."
  # shellcheck source=/dev/null
  . /etc/os-release

  [[ "${ID:-}" == "debian" ]] || die "Not Debian (ID='${ID:-unknown}')."
  [[ "${VERSION_CODENAME:-}" == "$SOURCE_CODENAME" ]] \
    || die "Intended for Debian '$SOURCE_CODENAME' only (detected '${VERSION_CODENAME:-unknown}')."

  if have_cmd fuser; then
    if fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 \
      || fuser /var/lib/dpkg/lock >/dev/null 2>&1 \
      || fuser /var/lib/apt/lists/lock >/dev/null 2>&1; then
      die "apt/dpkg lock is held. Finish or stop the other package manager first."
    fi
  fi
  if [[ -e /var/lib/dpkg/lock-frontend ]] && have_cmd lsof; then
    if lsof /var/lib/dpkg/lock-frontend >/dev/null 2>&1; then
      die "apt/dpkg lock is held (lsof). Finish or stop the other package manager first."
    fi
  fi

  audit_out="$(dpkg --audit 2>&1 || true)"
  if [[ -n "$audit_out" ]]; then
    echo "$audit_out" >&2
    die "dpkg reports package problems (dpkg --audit). Fix those before upgrading."
  fi
  if ! apt-get check >/dev/null 2>&1; then
    apt-get check || true
    die "apt-get check failed. Fix dependency/state issues before upgrading."
  fi

  held="$(apt-mark showhold 2>/dev/null || true)"
  if [[ -n "$held" ]]; then
    echo "Held packages:" >&2
    echo "$held" >&2
    die "Package holds present. Unhold (apt-mark unhold ...) or remove holds first."
  fi

  free_kib="$(df -Pk / | awk 'NR==2 {print $4}')"
  free_gib=$((free_kib / 1024 / 1024))
  echo "Free space on /: ${free_gib} GiB (minimum ${MIN_FREE_GIB} GiB)."
  if (( free_gib < MIN_FREE_GIB )); then
    die "Not enough free space on /. Free at least ${MIN_FREE_GIB} GiB, then retry."
  fi

  echo "Checking that apt works on the current Bookworm configuration..."
  if ! apt-get update; then
    die "apt-get update failed on the current Bookworm configuration. Fix mirror/network/repo issues first."
  fi

  # Critical safety gate: prove rewritten repos (Docker→trixie, etc.) work first.
  verify_planned_apt_update

  echo
  echo "######################################################################"
  echo "# PHASE: PREFLIGHT COMPLETE — system not modified                    #"
  echo "######################################################################"
}

# Pause after successful preflight so an operator can Ctrl-C before mutations.
cold_feet_pause() {
  local secs="$COLD_FEET_SECS" i
  if (( SKIP_COLD_FEET )); then
    echo "Cold-feet pause skipped (--now)."
    return 0
  fi
  echo
  echo "######################################################################"
  echo "# COLD FEET: live upgrade starts in ${secs}s — Ctrl-C to abort        #"
  echo "######################################################################"
  echo "After this pause the script will rewrite apt sources and upgrade packages."
  for ((i = secs; i >= 1; i--)); do
    printf '\r  Starting live upgrade in %2d second(s)... (Ctrl-C to abort) ' "$i"
    sleep 1
  done
  printf '\r  Starting live upgrade now.                                          \n'
  echo
}

run_upgrade() {
  export DEBIAN_FRONTEND=noninteractive

  echo "######################################################################"
  echo "# PHASE: LIVE UPGRADE — system WILL be modified                      #"
  echo "# Debian ${SOURCE_CODENAME} → ${TARGET_CODENAME}                                       #"
  echo "######################################################################"
  echo

  echo "Step 1: Backing up APT source lists..."
  backup_file /etc/apt/sources.list
  if [[ -d /etc/apt/sources.list.d ]]; then
    local f
    shopt -s nullglob
    for f in /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
      backup_file "$f"
    done
    shopt -u nullglob
  fi

  echo "Step 2: Rewriting Bookworm/Debian_12 tokens to ${TARGET_CODENAME}/Debian_13 in apt sources..."
  apply_rewrites_to_live_tree

  echo "Step 3: apt-get update..."
  apt-get update

  echo "Step 4: minimal upgrade (apt-get upgrade --without-new-pkgs)..."
  apt-get \
    -o Dpkg::Options::="--force-confdef" \
    -o Dpkg::Options::="--force-confold" \
    upgrade --without-new-pkgs -y

  echo "Step 5: full distribution upgrade (apt-get full-upgrade)..."
  apt-get \
    -o Dpkg::Options::="--force-confdef" \
    -o Dpkg::Options::="--force-confold" \
    full-upgrade -y

  echo "Step 6: post-upgrade cleanup (autoremove + autoclean)..."
  # Drop obsolete kernels/libs left after the suite switch; clear old .deb cache.
  apt-get autoremove -y
  apt-get autoclean

  # Confirm apt still healthy after cleanup (fail loud if something broke).
  apt-get update

  echo
  echo "######################################################################"
  echo "# PHASE: LIVE UPGRADE FINISHED                                       #"
  echo "######################################################################"
  echo "REBOOT REQUIRED to load the new kernel:"
  echo "  sudo reboot"
  echo
  # Flag anything still sitting around (apt *.bak kept on purpose for a few days).
  report_leftovers || true
}

# --- Leftover reporting / later cleanup --------------------------------------

# Apt-source backups written by this script (timestamped *.YYYYMMDD-HHMMSS.bak).
collect_script_apt_backups() {
  local f
  SCRIPT_APT_BACKUPS=()
  OTHER_APT_BACKUPS=()
  shopt -s nullglob
  for f in /etc/apt/sources.list.* \
           /etc/apt/sources.list.d/*; do
    [[ -f "$f" ]] || continue
    [[ "$f" == *.bak ]] || continue
    if [[ "$f" =~ $BACKUP_GLOB_RE ]]; then
      SCRIPT_APT_BACKUPS+=("$f")
    else
      OTHER_APT_BACKUPS+=("$f")
    fi
  done
  shopt -u nullglob
}

collect_dpkg_config_leftovers() {
  DPKG_CONFIG_LEFTOVERS=()
  local f
  # -print0 / readarray keeps paths with spaces safe; /etc is usually fine either way.
  while IFS= read -r -d '' f; do
    DPKG_CONFIG_LEFTOVERS+=("$f")
  done < <(find /etc -type f \( -name '*.dpkg-old' -o -name '*.dpkg-dist' \) -print0 2>/dev/null || true)
}

collect_rc_packages() {
  RC_PACKAGES=()
  local pkg
  while IFS= read -r pkg; do
    [[ -n "$pkg" ]] || continue
    RC_PACKAGES+=("$pkg")
  done < <(dpkg -l 2>/dev/null | awk '/^rc/{print $2}' || true)
}

# Print leftover inventory. Returns 0 if nothing notable, 1 if anything flagged.
report_leftovers() {
  local flagged=0

  collect_script_apt_backups
  collect_dpkg_config_leftovers
  collect_rc_packages

  echo "=== Leftover check ==="

  if ((${#SCRIPT_APT_BACKUPS[@]})); then
    flagged=1
    echo "Apt source backups from this script (${#SCRIPT_APT_BACKUPS[@]}):"
    printf '  %s\n' "${SCRIPT_APT_BACKUPS[@]}"
  else
    echo "Apt source backups from this script: none"
  fi

  if ((${#OTHER_APT_BACKUPS[@]})); then
    flagged=1
    echo "Other apt *.bak under /etc/apt (not auto-deleted) (${#OTHER_APT_BACKUPS[@]}):"
    printf '  %s\n' "${OTHER_APT_BACKUPS[@]}"
  else
    echo "Other apt *.bak under /etc/apt: none"
  fi

  if ((${#RC_PACKAGES[@]})); then
    flagged=1
    echo "Packages in 'rc' state (removed, config kept) (${#RC_PACKAGES[@]}):"
    printf '  %s\n' "${RC_PACKAGES[@]}"
  else
    echo "Packages in 'rc' state: none"
  fi

  if ((${#DPKG_CONFIG_LEFTOVERS[@]})); then
    flagged=1
    echo "dpkg config leftovers under /etc (review manually; not auto-deleted) (${#DPKG_CONFIG_LEFTOVERS[@]}):"
    printf '  %s\n' "${DPKG_CONFIG_LEFTOVERS[@]}"
  else
    echo "dpkg config leftovers (.dpkg-old / .dpkg-dist): none"
  fi

  if (( flagged )); then
    echo "Follow-up (removes this script's apt *.bak + purges rc; other items stay flagged):"
    echo "  sudo $0 --cleanup-only --yes"
    return 1
  fi
  echo "No leftover items flagged."
  return 0
}

remove_script_apt_backups() {
  local f
  collect_script_apt_backups
  if ((${#SCRIPT_APT_BACKUPS[@]} == 0)); then
    echo "No script apt *.bak backups to remove."
    return 0
  fi
  echo "Removing ${#SCRIPT_APT_BACKUPS[@]} apt source backup(s)..."
  for f in "${SCRIPT_APT_BACKUPS[@]}"; do
    rm -f -- "$f"
    echo "  removed: $f"
  done
}

purge_rc_packages() {
  collect_rc_packages
  if ((${#RC_PACKAGES[@]} == 0)); then
    echo "No rc packages to purge."
    return 0
  fi
  echo "Purging ${#RC_PACKAGES[@]} rc package(s)..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get purge -y "${RC_PACKAGES[@]}"
}

run_cleanup_only() {
  [[ $EUID -eq 0 ]] || die "Must run as root (use: sudo $0 --cleanup-only)."

  echo "=== Cleanup-only pass (no suite upgrade) ==="
  report_leftovers || true

  if (( ! ASSUME_YES )); then
    echo
    die "Refusing to delete/purge without --yes. Re-run: sudo $0 --cleanup-only --yes"
  fi

  echo
  remove_script_apt_backups
  purge_rc_packages

  echo
  echo "=== After cleanup ==="
  if report_leftovers; then
    echo "Cleanup-only finished; nothing left flagged."
  else
    echo "Cleanup-only finished. Remaining items above need manual review (especially .dpkg-old/.dpkg-dist)."
  fi
}

# --- Mutations (only after preflight) ----------------------------------------

backup_file() {
  local f="$1"
  if [[ -f "$f" ]]; then
    local backup="${f}.${BACKUP_TIMESTAMP}.bak"
    cp -a "$f" "$backup"
    echo "Backup created: $backup"
  fi
}

apply_rewrites_to_live_tree() {
  local f

  if [[ -f /etc/apt/sources.list ]] && file_needs_rewrite /etc/apt/sources.list; then
    local tmp
    tmp="$(mktemp)"
    rewrite_codenames_stream < /etc/apt/sources.list > "$tmp"
    cat "$tmp" > /etc/apt/sources.list
    rm -f "$tmp"
    echo "Rewrote: /etc/apt/sources.list"
  fi

  shopt -s nullglob
  for f in /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
    [[ -f "$f" ]] || continue
    [[ "$f" == *.bak ]] && continue
    if file_needs_rewrite "$f"; then
      local tmp
      tmp="$(mktemp)"
      rewrite_codenames_stream < "$f" > "$tmp"
      cat "$tmp" > "$f"
      rm -f "$tmp"
      echo "Rewrote: $f"
    fi
  done
  shopt -u nullglob
}

if (( CLEANUP_ONLY )); then
  run_cleanup_only
  exit 0
fi

preflight

# Default and --check-only: prove-out only. Live upgrade requires --yes.
if (( CHECK_ONLY || ! ASSUME_YES )); then
  echo
  echo "Preflight passed. No changes made."
  if (( ! ASSUME_YES )); then
    echo "To perform the upgrade, re-run with the affirmative flag:"
    echo "  sudo $0 --yes"
  else
    echo "Check-only mode (--check-only): upgrade not started."
  fi
  exit 0
fi

cold_feet_pause
run_upgrade
