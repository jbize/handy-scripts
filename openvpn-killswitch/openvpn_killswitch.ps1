#Requires -Version 5.1
<#
.SYNOPSIS
  Windows Firewall Arm/Disarm kill switch for OpenVPN GUI.

.DESCRIPTION
  While Armed, outbound traffic is deny-by-default except VPN server endpoint(s),
  DHCP, and OpenVPN tunnel adapters (DNS only via the tunnel when it is up).
  Hostname remotes are resolved once at Arm and pinned in the system hosts file
  (removed on Disarm only if we added them). Armed state persists until Disarm.

  No VPN up/down detection — the firewall policy is the kill switch.

.PARAMETER Gui
  Show the WinForms control panel (default when no other action is specified).

.PARAMETER Arm
  Apply kill-switch firewall rules for -Config.

.PARAMETER Disarm
  End the protected session: best-effort disconnect the Armed OpenVPN profile
  (no error if already down), then restore the saved pre-Arm firewall state.

.PARAMETER Status
  Print whether the kill switch is Armed.

.PARAMETER ListConfigs
  List .ovpn profiles like OpenVPN GUI (user config dir first, then system; one-level nesting).

.PARAMETER Config
  Profile display name, relative path, basename, or full path (resolved like OpenVPN GUI).

.PARAMETER Connect
  After Arm, ask OpenVPN GUI to connect that profile.

.PARAMETER RetryTunnels
  While Armed, add missing firewall allows for OpenVPN tunnel adapters (same as Advanced -> Retry tunnel allows).

.PARAMETER ExportFirewallSnapshot
  While Disarmed (recommended): write firewall_restore.bak + dated snapshot under %LOCALAPPDATA%\HandyScripts.

.PARAMETER RestoreFirewallBak
  Disarm-like restore from firewall_restore.bak (re-enable snapshot Allows, restore defaults, remove KS rules).

.EXAMPLE
  .\openvpn_killswitch.ps1 -Gui
.EXAMPLE
  .\openvpn_killswitch.ps1 -Arm -Config home.ovpn -Connect
.EXAMPLE
  .\openvpn_killswitch.ps1 -Disarm
.EXAMPLE
  .\openvpn_killswitch.ps1 -RetryTunnels
.EXAMPLE
  .\openvpn_killswitch.ps1 -ExportFirewallSnapshot
.EXAMPLE
  .\openvpn_killswitch.ps1 -RestoreFirewallBak
#>
[CmdletBinding(DefaultParameterSetName = 'Gui', SupportsShouldProcess = $true)]
param(
    [Parameter(ParameterSetName = 'Gui')]
    [Parameter(ParameterSetName = 'Arm')]
    [Parameter(ParameterSetName = 'Disarm')]
    [Parameter(ParameterSetName = 'RetryTunnels')]
    [Parameter(ParameterSetName = 'ExportFirewall')]
    [Parameter(ParameterSetName = 'RestoreFirewall')]
    [switch] $Gui,

    [Parameter(ParameterSetName = 'Arm', Mandatory = $true)]
    [switch] $Arm,

    [Parameter(ParameterSetName = 'Disarm', Mandatory = $true)]
    [switch] $Disarm,

    [Parameter(ParameterSetName = 'Status')]
    [switch] $Status,

    [Parameter(ParameterSetName = 'ListConfigs')]
    [switch] $ListConfigs,

    [Parameter(ParameterSetName = 'RetryTunnels', Mandatory = $true)]
    [switch] $RetryTunnels,

    [Parameter(ParameterSetName = 'ExportFirewall', Mandatory = $true)]
    [switch] $ExportFirewallSnapshot,

    [Parameter(ParameterSetName = 'RestoreFirewall', Mandatory = $true)]
    [switch] $RestoreFirewallBak,

    [Parameter(ParameterSetName = 'RestoreFirewall')]
    [switch] $Force,

    [Parameter(ParameterSetName = 'Arm')]
    [string] $Config,

    [Parameter(ParameterSetName = 'Arm')]
    [switch] $Connect
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:RuleGroup = 'HandyScripts-OpenVPN-KillSwitch'
$script:RulePrefix = 'HandyScripts-OpenVPN-KS'
$script:StateDir = Join-Path $env:LOCALAPPDATA 'HandyScripts'
$script:StatePath = Join-Path $script:StateDir 'openvpn_killswitch_state.json'
$script:FirewallRestoreBakPath = Join-Path $script:StateDir 'firewall_restore.bak'

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

function Write-Info([string] $Message) { Write-Host $Message }
function Write-WarnMsg([string] $Message) { Write-Warning $Message }

# -----------------------------------------------------------------------------
# Admin / elevation
# -----------------------------------------------------------------------------

function Test-IsAdministrator {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Request-AdministratorElevation {
    param([string[]] $ExtraArgs = @())

    if (Test-IsAdministrator) { return $false }

    $argList = New-Object System.Collections.Generic.List[string]
    [void]$argList.Add('-NoProfile')
    [void]$argList.Add('-ExecutionPolicy')
    [void]$argList.Add('Bypass')
    [void]$argList.Add('-WindowStyle')
    [void]$argList.Add('Hidden')
    [void]$argList.Add('-File')
    [void]$argList.Add($PSCommandPath)
    foreach ($a in $ExtraArgs) { [void]$argList.Add($a) }

    Write-Info 'Requesting Administrator elevation (UAC)...'
    $proc = Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argList.ToArray() -Wait -PassThru
    exit $proc.ExitCode
}

function Ensure-AdministratorForMutation {
    if ($WhatIfPreference) { return }
    if (-not (Test-IsAdministrator)) {
        $pass = @()
        foreach ($e in $PSBoundParameters.GetEnumerator()) {
            if ($e.Value -is [switch]) {
                if ($e.Value.IsPresent) { $pass += ('-{0}' -f $e.Key) }
            }
            elseif ($null -ne $e.Value -and "$($e.Value)" -ne '') {
                $pass += ('-{0}' -f $e.Key)
                $pass += "$($e.Value)"
            }
        }
        Request-AdministratorElevation -ExtraArgs $pass
    }
}

# -----------------------------------------------------------------------------
# OpenVPN install / configs
# -----------------------------------------------------------------------------

function Get-RegistryStringValue {
    param(
        [string] $Path,
        [string] $Name
    )
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $item = Get-ItemProperty -LiteralPath $Path -ErrorAction SilentlyContinue
    if (-not $item) { return $null }
    if (-not ($item.PSObject.Properties.Name -contains $Name)) { return $null }
    $val = $item.$Name
    if ($null -eq $val -or [string]::IsNullOrWhiteSpace([string]$val)) { return $null }
    return [string]$val
}

function Get-OpenVpnInstallInfo {
    $guiKey = 'HKLM:\SOFTWARE\OpenVPN-GUI'
    $ovpnKey = 'HKLM:\SOFTWARE\OpenVPN'
    $hkcuGuiKey = 'HKCU:\SOFTWARE\OpenVPN-GUI'
    $globalConfigDir = $null
    $userConfigDir = $null
    $guiExe = $null

    $globalConfigDir = Get-RegistryStringValue -Path $guiKey -Name 'config_dir'
    if (-not $globalConfigDir) {
        $globalConfigDir = Get-RegistryStringValue -Path $ovpnKey -Name 'config_dir'
    }
    $binDir = Get-RegistryStringValue -Path $ovpnKey -Name 'bin_dir'
    if ($binDir) {
        $c = Join-Path $binDir 'openvpn-gui.exe'
        if (Test-Path $c) { $guiExe = $c }
    }
    $userConfigDir = Get-RegistryStringValue -Path $hkcuGuiKey -Name 'config_dir'
    if (-not $userConfigDir) {
        $userConfigDir = Join-Path $env:USERPROFILE 'OpenVPN\config'
    }
    if (-not $globalConfigDir) {
        $globalConfigDir = Join-Path $env:ProgramFiles 'OpenVPN\config'
    }
    if (-not $guiExe) {
        $c = Join-Path $env:ProgramFiles 'OpenVPN\bin\openvpn-gui.exe'
        if (Test-Path $c) { $guiExe = $c }
    }

    $userConfigDir = $userConfigDir.TrimEnd('\')
    $globalConfigDir = $globalConfigDir.TrimEnd('\')

    $userExists = Test-Path -LiteralPath $userConfigDir
    $globalExists = Test-Path -LiteralPath $globalConfigDir
    if (-not $userExists -and -not $globalExists) {
        throw 'Could not locate OpenVPN GUI config directories (user or system).'
    }

    # ConfigDir: primary root for display / backward-compatible single-dir callers (user preferred).
    $primary = if ($userExists) { $userConfigDir } else { $globalConfigDir }

    [pscustomobject]@{
        UserConfigDir      = $userConfigDir
        GlobalConfigDir    = $globalConfigDir
        UserConfigExists   = $userExists
        GlobalConfigExists = $globalExists
        ConfigDir          = $primary
        GuiExe             = $guiExe
    }
}

function Get-OvpnConfigProfiles {
    <#
      Mirror OpenVPN GUI discovery:
      - Parse %USERPROFILE%\OpenVPN\config (or HKCU config_dir) first
      - Then the system/global config_dir (HKLM / Program Files)
      - Top-level *.ovpn plus immediate subdirectories only (not deep recursion)
      - Basenames must be unique across all folders; later duplicates are skipped (GUI rule)
    #>
    $install = Get-OpenVpnInstallInfo
    $seenNames = @{}
    $list = New-Object System.Collections.Generic.List[object]

    $roots = @(
        [pscustomobject]@{ Source = 'User'; Path = $install.UserConfigDir; Exists = $install.UserConfigExists }
        [pscustomobject]@{ Source = 'System'; Path = $install.GlobalConfigDir; Exists = $install.GlobalConfigExists }
    )

    foreach ($root in $roots) {
        if (-not $root.Exists) { continue }
        $rootPath = (Resolve-Path -LiteralPath $root.Path).Path

        $files = New-Object System.Collections.Generic.List[object]
        foreach ($f in @(Get-ChildItem -LiteralPath $rootPath -Filter '*.ovpn' -File -ErrorAction SilentlyContinue | Sort-Object Name)) {
            [void]$files.Add([pscustomobject]@{
                    File         = $f
                    RelativePath = $f.Name
                    DisplayName  = [System.IO.Path]::GetFileNameWithoutExtension($f.Name)
                })
        }
        foreach ($dir in @(Get-ChildItem -LiteralPath $rootPath -Directory -ErrorAction SilentlyContinue | Sort-Object Name)) {
            foreach ($f in @(Get-ChildItem -LiteralPath $dir.FullName -Filter '*.ovpn' -File -ErrorAction SilentlyContinue | Sort-Object Name)) {
                $rel = Join-Path $dir.Name $f.Name
                $disp = Join-Path $dir.Name ([System.IO.Path]::GetFileNameWithoutExtension($f.Name))
                [void]$files.Add([pscustomobject]@{
                        File         = $f
                        RelativePath = $rel
                        DisplayName  = $disp
                    })
            }
        }

        foreach ($entry in $files) {
            $base = $entry.File.Name
            $key = $base.ToLowerInvariant()
            if ($seenNames.ContainsKey($key)) {
                Write-WarnMsg ("Skipping duplicate config name '{0}' (OpenVPN GUI requires unique names even across folders): {1}" -f $base, $entry.File.FullName)
                continue
            }
            $seenNames[$key] = $true
            [void]$list.Add([pscustomobject]@{
                    DisplayName  = $entry.DisplayName
                    RelativePath = $entry.RelativePath
                    FullPath     = $entry.File.FullName
                    BaseName     = $base
                    Source       = $root.Source
                    RootPath     = $rootPath
                })
        }
    }

    if ($list.Count -eq 0) {
        return @()
    }
    return [object[]]$list.ToArray()
}

function Resolve-OvpnConfigPath {
    param(
        [string] $ConfigArgument,
        [string] $ConfigDir = $null
    )
    if (-not $ConfigArgument) {
        throw 'Config profile name or path is required.'
    }
    if (Test-Path -LiteralPath $ConfigArgument) {
        return (Resolve-Path -LiteralPath $ConfigArgument).Path
    }

    $profiles = @(Get-OvpnConfigProfiles)
    $arg = $ConfigArgument.Trim().TrimEnd('\')
    $argLower = $arg.ToLowerInvariant()
    $argWithExt = if ($argLower.EndsWith('.ovpn')) { $arg } else { "$arg.ovpn" }
    $argWithExtLower = $argWithExt.ToLowerInvariant()
    $argNoExtLower = if ($argLower.EndsWith('.ovpn')) {
        $argLower.Substring(0, $argLower.Length - 5)
    }
    else { $argLower }

    foreach ($p in $profiles) {
        $candidates = @(
            $p.FullPath
            $p.DisplayName
            $p.RelativePath
            $p.BaseName
            ([System.IO.Path]::GetFileNameWithoutExtension($p.BaseName))
        )
        foreach ($c in $candidates) {
            if (-not $c) { continue }
            $cl = ([string]$c).ToLowerInvariant()
            if ($cl -eq $argLower -or $cl -eq $argWithExtLower -or $cl -eq $argNoExtLower) {
                return $p.FullPath
            }
        }
    }

    # Optional legacy single-dir join when caller still passes ConfigDir
    if ($ConfigDir) {
        $joined = Join-Path $ConfigDir $ConfigArgument
        if (Test-Path -LiteralPath $joined) {
            return (Resolve-Path -LiteralPath $joined).Path
        }
        $joinedOv = Join-Path $ConfigDir $argWithExt
        if (Test-Path -LiteralPath $joinedOv) {
            return (Resolve-Path -LiteralPath $joinedOv).Path
        }
    }

    throw "Config not found: '$ConfigArgument' (searched user then system OpenVPN config dirs)"
}

function Test-IsIpAddress([string] $Value) {
    try {
        $null = [System.Net.IPAddress]::Parse($Value)
        return $true
    }
    catch { return $false }
}

function Get-OvpnRemotes {
    param([string] $ConfigPath)
    $lines = Get-Content -LiteralPath $ConfigPath -ErrorAction Stop
    $defaultProto = 'udp'
    $defaultPort = 1194
    $remotes = @()
    foreach ($raw in $lines) {
        $line = ($raw -replace '#.*$', '').Trim()
        if (-not $line) { continue }
        if ($line -match '^\s*proto\s+(udp6?|tcp6?)\s*$') {
            $p = ([string]$Matches[1]).ToLowerInvariant()
            $defaultProto = if ($p.StartsWith('tcp')) { 'tcp' } else { 'udp' }
            continue
        }
        if ($line -match '^\s*port\s+(\d+)\s*$') {
            $defaultPort = [int]([string]$Matches[1])
            continue
        }
        if ($line -match '^\s*remote\s+(\S+)(?:\s+(\d+))?(?:\s+(udp6?|tcp6?))?\s*$') {
            $hostName = [string]$Matches[1]
            $port = if ($Matches[2]) { [int]([string]$Matches[2]) } else { $defaultPort }
            if ($Matches[3]) {
                $rp = ([string]$Matches[3]).ToLowerInvariant()
                $proto = if ($rp.StartsWith('tcp')) { 'tcp' } else { 'udp' }
            }
            else { $proto = $defaultProto }
            $remotes += [pscustomobject]@{ Host = $hostName; Port = $port; Proto = $proto }
        }
    }
    if ($remotes.Count -eq 0) { throw "No 'remote' lines found in $ConfigPath" }
    return $remotes
}

function Resolve-RemoteEndpoints {
    param([object[]] $Remotes)
    $endpoints = @()
    foreach ($r in $Remotes) {
        $ips = @()
        if (Test-IsIpAddress $r.Host) { $ips += $r.Host }
        else {
            try {
                foreach ($a in [System.Net.Dns]::GetHostAddresses($r.Host)) {
                    $ips += $a.IPAddressToString
                }
            }
            catch {
                throw "Failed to resolve '$($r.Host)': $($_.Exception.Message)"
            }
            if ($ips.Count -eq 0) { throw "No addresses for '$($r.Host)'" }
        }
        foreach ($ip in ($ips | Select-Object -Unique)) {
            $endpoints += [pscustomobject]@{
                Host = $r.Host; IP = $ip; Port = $r.Port; Proto = $r.Proto
            }
        }
    }
    return $endpoints
}

function Test-IsTunnelAdapter {
    param($Adapter)
    $desc = [string]$Adapter.InterfaceDescription
    $name = [string]$Adapter.Name
    $blob = ($name + ' ' + $desc).ToLowerInvariant()
    foreach ($n in @('tap-windows', 'tap-win', 'wintun', 'openvpn data channel', 'ovpn-dco', 'protonvpn')) {
        if ($blob.Contains($n)) { return $true }
    }
    if ($desc -match '(?i)TAP-Windows|Wintun|OpenVPN Data Channel Offload') { return $true }
    return $false
}

function Get-OpenVpnTunnelAdapters {
    @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { Test-IsTunnelAdapter $_ })
}

# -----------------------------------------------------------------------------
# State / rules
# -----------------------------------------------------------------------------

function Get-ObjectPropertyValue {
    param($Object, [string] $Name, $Default = $null)
    if ($null -eq $Object) { return $Default }
    $prop = $Object.PSObject.Properties[$Name]
    if ($null -eq $prop) { return $Default }
    return $prop.Value
}

function Get-ExistingKillSwitchRules {
    # Get-NetFirewallRule returns $null (not empty) when nothing matches.
    # @( $null ) has Count=1 and StrictMode then dies on .Action / pipeline bind.
    $rules = Get-NetFirewallRule -Group $script:RuleGroup -ErrorAction SilentlyContinue
    if ($null -eq $rules) { return @() }
    return @($rules | Where-Object { $_ })
}

function Test-IsKillSwitchArmed {
    $rules = @(Get-ExistingKillSwitchRules)
    if ($rules.Count -gt 0) { return $true }
    if (Test-Path -LiteralPath $script:StatePath) { return $true }
    return $false
}

function Read-KillSwitchState {
    if (-not (Test-Path -LiteralPath $script:StatePath)) { return $null }
    Get-Content -LiteralPath $script:StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Write-KillSwitchState {
    param([object] $State)
    if (-not (Test-Path -LiteralPath $script:StateDir)) {
        New-Item -ItemType Directory -Path $script:StateDir -Force | Out-Null
    }
    $json = $State | ConvertTo-Json -Depth 8
    # utf8NoBOM so Python/other tools can json.loads without utf-8-sig
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($script:StatePath, $json, $utf8NoBom)
}

function Remove-KillSwitchStateFile {
    if (Test-Path -LiteralPath $script:StatePath) {
        Remove-Item -LiteralPath $script:StatePath -Force
    }
}

function Get-CurrentDefaultOutboundActions {
    $saved = @{}
    foreach ($p in @('Domain', 'Private', 'Public')) {
        $fp = Get-NetFirewallProfile -Name $p
        $saved[$p] = [string]$fp.DefaultOutboundAction
    }
    return $saved
}

function Get-EnabledOutboundAllowRuleNames {
    # Names of outbound Allow rules currently Enabled (excluding this tool's group).
    $otherAllows = @(Get-NetFirewallRule -Direction Outbound -Action Allow -Enabled True -ErrorAction SilentlyContinue |
        Where-Object { $_.Group -ne $script:RuleGroup })
    return @($otherAllows | ForEach-Object { $_.Name })
}

function New-FirewallRestoreSnapshot {
    param(
        [hashtable] $DefaultOutboundAction = $null,
        [string[]] $EnabledOutboundAllowRules = $null
    )
    if ($null -eq $DefaultOutboundAction) {
        $DefaultOutboundAction = Get-CurrentDefaultOutboundActions
    }
    if ($null -eq $EnabledOutboundAllowRules) {
        $EnabledOutboundAllowRules = @(Get-EnabledOutboundAllowRuleNames)
    }
    return [pscustomobject]@{
        Kind                       = 'HandyScripts-FirewallRestore'
        Version                    = 1
        SavedAt                    = (Get-Date).ToString('o')
        DefaultOutboundAction      = $DefaultOutboundAction
        EnabledOutboundAllowRules  = @($EnabledOutboundAllowRules)
        RuleCount                  = @($EnabledOutboundAllowRules).Count
    }
}

function Save-FirewallRestoreSnapshot {
    <#
      Writes firewall_restore.bak (quick Disarm-like restore) and a dated
      firewall_snapshot_YYYYMMDD-HHMMSS.json copy under %LOCALAPPDATA%\HandyScripts.
    #>
    param(
        [Parameter(Mandatory = $true)]
        $Snapshot
    )
    if (-not (Test-Path -LiteralPath $script:StateDir)) {
        New-Item -ItemType Directory -Path $script:StateDir -Force | Out-Null
    }
    $json = $Snapshot | ConvertTo-Json -Depth 8
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($script:FirewallRestoreBakPath, $json, $utf8NoBom)
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $dated = Join-Path $script:StateDir ("firewall_snapshot_{0}.json" -f $stamp)
    [System.IO.File]::WriteAllText($dated, $json, $utf8NoBom)
    return [pscustomobject]@{ BakPath = $script:FirewallRestoreBakPath; DatedPath = $dated }
}

function Read-FirewallRestoreBak {
    param([string] $Path = $script:FirewallRestoreBakPath)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Get-FirewallBakAssessment {
    <#
      Decide Export/Restore enablement and warnings.
      Serious: block. Mild: warn + confirm. Armed+intact state: extra confirm (prefer Disarm).
    #>
    param([string] $BakPath = $script:FirewallRestoreBakPath)

    $armed = Test-IsKillSwitchArmed
    $state = Read-KillSwitchState
    $hasState = $null -ne $state
    $bak = Read-FirewallRestoreBak -Path $BakPath
    $hasBak = $null -ne $bak
    $ruleCount = 0
    $savedAt = $null
    $ageDays = $null
    if ($hasBak) {
        $ruleCount = @($bak.EnabledOutboundAllowRules).Count
        $savedAt = Get-ObjectPropertyValue -Object $bak -Name 'SavedAt' -Default $null
        if ($savedAt) {
            try {
                $ageDays = [math]::Round(((Get-Date) - [datetime]$savedAt).TotalDays, 1)
            }
            catch { $ageDays = $null }
        }
    }

    $exportBlocked = $null
    if ($armed) {
        $exportBlocked = 'Export only while DISARMED. Exporting while Armed can save a bad (already locked-down) restore list.'
    }

    $restoreBlocked = $null
    if (-not $hasBak) {
        $restoreBlocked = 'No firewall_restore.bak yet. Use Export firewall snapshot while DISARMED first.'
    }

    $warnings = New-Object System.Collections.Generic.List[string]
    $extraConfirm = $false

    if ($hasBak -and $armed -and $hasState) {
        [void]$warnings.Add('Kill switch is ARMED and state.json exists. Prefer DISARM. Use Restore from .bak only if Disarm failed or cannot run.')
        $extraConfirm = $true
    }
    elseif ($hasBak -and $armed -and -not $hasState) {
        [void]$warnings.Add('Armed but no state file - Restore from .bak is the intended break-glass path.')
    }
    elseif ($hasBak -and -not $armed) {
        [void]$warnings.Add('Currently DISARMED. Restore will re-enable Allows from the bak and clear any leftover kill-switch rules.')
    }

    if ($null -ne $ageDays -and $ageDays -ge 7) {
        [void]$warnings.Add(("Bak is {0} days old - may re-enable rules you later turned off on purpose." -f $ageDays))
    }

    if ($hasBak -and $ruleCount -gt 0 -and $ruleCount -lt 20) {
        [void]$warnings.Add(("Bak lists only {0} enabled outbound Allows (unusually low). It may have been captured while already locked down." -f $ruleCount))
        $extraConfirm = $true
    }

    if ($hasBak -and $ruleCount -eq 0) {
        $restoreBlocked = 'Bak has zero EnabledOutboundAllowRules - refusing restore (would not bring Allows back). Export again while DISARMED.'
    }

    return [pscustomobject]@{
        Armed                 = $armed
        HasState              = $hasState
        HasBak                = $hasBak
        BakPath               = $BakPath
        SavedAt               = $savedAt
        AgeDays               = $ageDays
        RuleCount             = $ruleCount
        ExportBlockedReason   = $exportBlocked
        RestoreBlockedReason  = $restoreBlocked
        Warnings              = @($warnings)
        NeedsExtraConfirm     = $extraConfirm
        CanExport             = [string]::IsNullOrEmpty($exportBlocked)
        CanRestore            = [string]::IsNullOrEmpty($restoreBlocked)
    }
}

function Get-RestoreFirewallBakConfirmText {
    param($Assessment)
    $lines = New-Object System.Collections.Generic.List[string]
    [void]$lines.Add('RESTORE FROM firewall_restore.bak')
    [void]$lines.Add('')
    [void]$lines.Add('This will:')
    [void]$lines.Add('  1. Best-effort disconnect Armed VPN profile (if state knows it)')
    [void]$lines.Add('  2. Undo hosts pins from Armed state (if any)')
    [void]$lines.Add('  3. Remove HandyScripts kill-switch firewall rules')
    [void]$lines.Add('  4. Restore DefaultOutboundAction from the bak')
    [void]$lines.Add(('  5. Re-enable {0} outbound Allow rule name(s) from the bak' -f $Assessment.RuleCount))
    [void]$lines.Add('  6. Delete openvpn_killswitch_state.json')
    [void]$lines.Add('')
    [void]$lines.Add(('Bak file: {0}' -f $Assessment.BakPath))
    if ($Assessment.SavedAt) {
        [void]$lines.Add(('Bak SavedAt: {0}' -f $Assessment.SavedAt))
    }
    if ($null -ne $Assessment.AgeDays) {
        [void]$lines.Add(('Bak age: {0} days' -f $Assessment.AgeDays))
    }
    [void]$lines.Add(('Current: {0}' -f $(if ($Assessment.Armed) { 'ARMED' } else { 'DISARMED' })))
    if ($Assessment.Warnings -and $Assessment.Warnings.Count -gt 0) {
        [void]$lines.Add('')
        [void]$lines.Add('WARNINGS:')
        foreach ($w in $Assessment.Warnings) {
            [void]$lines.Add(('  - {0}' -f $w))
        }
    }
    [void]$lines.Add('')
    [void]$lines.Add('Prefer normal DISARM when Armed with intact state.')
    [void]$lines.Add('')
    [void]$lines.Add('Yes = continue.  No = do nothing.')
    return ($lines -join [Environment]::NewLine)
}

function Export-FirewallSnapshot {
    <#
      Capture current (pre-mutation) outbound defaults + enabled Allow rule names.
      Primary: JSON bak + dated snapshot. Optional: netsh .wfw if elevation works.
    #>
    $assess = Get-FirewallBakAssessment
    if (-not $assess.CanExport) {
        throw $assess.ExportBlockedReason
    }
    Ensure-AdministratorForMutation
    $snap = New-FirewallRestoreSnapshot
    $paths = Save-FirewallRestoreSnapshot -Snapshot $snap
    Write-Info ("Firewall restore snapshot: {0} enabled outbound Allow rule(s)" -f $snap.RuleCount)
    Write-Info ("  Quick restore: {0}" -f $paths.BakPath)
    Write-Info ("  Dated copy:    {0}" -f $paths.DatedPath)

    $wfw = Join-Path $script:StateDir ('firewall_snapshot_{0}.wfw' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    $wfwNote = $null
    try {
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $null = netsh advfirewall export $wfw 2>&1
        $ErrorActionPreference = $prevEap
        if ((Test-Path -LiteralPath $wfw) -and ((Get-Item -LiteralPath $wfw).Length -gt 0)) {
            Write-Info ("  Full policy (.wfw): {0}" -f $wfw)
            $wfwNote = $wfw
        }
        else {
            $wfwNote = 'netsh .wfw export skipped/failed (NAPMONTR warning is often harmless; JSON bak is the quick-restore source of truth)'
            Write-WarnMsg $wfwNote
            if (Test-Path -LiteralPath $wfw) { Remove-Item -LiteralPath $wfw -Force -ErrorAction SilentlyContinue }
        }
    }
    catch {
        $wfwNote = ("netsh .wfw export failed: {0}" -f $_.Exception.Message)
        Write-WarnMsg $wfwNote
    }

    return [pscustomobject]@{
        BakPath    = $paths.BakPath
        DatedPath  = $paths.DatedPath
        WfwPath    = $wfwNote
        RuleCount  = $snap.RuleCount
    }
}

function Invoke-RestoreFirewallFromBak {
    <#
      Disarm-like recovery from firewall_restore.bak (or -Path).
      Re-enables the outbound Allows that were Enabled when the snapshot was taken,
      restores DefaultOutboundAction, removes kill-switch rules, clears Armed state.
    #>
    param(
        [string] $Path = $script:FirewallRestoreBakPath,
        [switch] $Force
    )
    if ($WhatIfPreference) {
        Write-Info ("WhatIf: would restore firewall from {0}" -f $Path)
        return
    }

    $assess = Get-FirewallBakAssessment -BakPath $Path
    if (-not $assess.CanRestore) {
        throw $assess.RestoreBlockedReason
    }

    if (-not $Force) {
        Write-Host (Get-RestoreFirewallBakConfirmText -Assessment $assess)
        if ($assess.NeedsExtraConfirm) {
            Write-Host ''
            Write-Host 'EXTRA CONFIRMATION REQUIRED (risky situation).'
            $ans = Read-Host 'Type RESTORE (all caps) to continue, or press Enter to cancel'
            if ($ans -ne 'RESTORE') {
                throw 'Restore cancelled.'
            }
        }
        else {
            $ans = Read-Host 'Type YES to continue, or press Enter to cancel'
            if ($ans -ne 'YES') {
                throw 'Restore cancelled.'
            }
        }
    }

    Ensure-AdministratorForMutation
    $bak = Read-FirewallRestoreBak -Path $Path
    if (-not $bak) {
        throw ("No firewall restore bak at '{0}'. Use Testing -> Export firewall snapshot first (while Disarmed)." -f $Path)
    }
    $kind = [string](Get-ObjectPropertyValue -Object $bak -Name 'Kind' -Default '')
    if ($kind -and $kind -ne 'HandyScripts-FirewallRestore') {
        Write-WarnMsg ("Unexpected bak Kind='{0}' - continuing anyway." -f $kind)
    }

    # Best-effort: if Armed state exists, disconnect + undo hosts from that session.
    $state = Read-KillSwitchState
    if ($state) {
        try {
            $install = Get-OpenVpnInstallInfo
            $cfg = [string](Get-ObjectPropertyValue -Object $state -Name 'ConfigPath' -Default '')
            if ($cfg) {
                Write-Info ("Best-effort disconnect of Armed profile: {0}" -f $cfg)
                Disconnect-OpenVpnGuiProfile -GuiExe $install.GuiExe -ConfigPath $cfg
            }
        }
        catch {
            Write-Info ("Disconnect skipped: {0}" -f $_.Exception.Message)
        }
        try {
            Undo-KillSwitchHostsFromState -State $state
        }
        catch {
            Write-WarnMsg ("Hosts undo from Armed state failed: {0}" -f $_.Exception.Message)
        }
    }

    $existing = @(Get-ExistingKillSwitchRules)
    if ($existing.Count -gt 0) {
        Write-Info ("Removing {0} kill-switch rule(s)..." -f $existing.Count)
        $existing | Remove-NetFirewallRule -ErrorAction Stop
    }

    if ($bak.DefaultOutboundAction) {
        foreach ($p in @('Domain', 'Private', 'Public')) {
            $val = $bak.DefaultOutboundAction.$p
            if ($val) {
                Write-Info ("Restoring {0} DefaultOutboundAction={1}" -f $p, $val)
                Set-NetFirewallProfile -Profile $p -DefaultOutboundAction $val
            }
        }
    }

    $names = @($bak.EnabledOutboundAllowRules)
    if ($names.Count -gt 0) {
        Write-Info ("Re-enabling {0} outbound Allow rule(s) from bak..." -f $names.Count)
        [void](Set-OutboundAllowRulesEnabled -Names $names -Enabled $true)
    }
    else {
        Write-WarnMsg 'Bak has no EnabledOutboundAllowRules list.'
    }

    Remove-KillSwitchStateFile
    Write-Info ("Restored from bak: {0}" -f $Path)
}

function New-KillSwitchRuleName([string] $Suffix) {
    $safe = ($Suffix -replace '[^\w\-]', '_')
    return "$script:RulePrefix-$safe"
}

function Set-OutboundAllowRulesEnabled {
    <#
      Enable or disable many firewall rules by name.
      One-by-one Disable/Enable-NetFirewallRule is very slow and freezes a WinForms UI;
      batching by Name[] is much faster.
    #>
    param(
        [string[]] $Names,
        [bool] $Enabled,
        [int] $ChunkSize = 80
    )
    if (-not $Names -or $Names.Count -eq 0) { return 0 }
    $ok = 0
    $fail = 0
    for ($i = 0; $i -lt $Names.Count; $i += $ChunkSize) {
        $end = [Math]::Min($i + $ChunkSize - 1, $Names.Count - 1)
        $chunk = @($Names[$i..$end])
        try {
            if ($Enabled) {
                Enable-NetFirewallRule -Name $chunk -ErrorAction Stop
            }
            else {
                Disable-NetFirewallRule -Name $chunk -ErrorAction Stop
            }
            $ok += $chunk.Count
        }
        catch {
            # Fall back per-rule for this chunk so one bad name does not abort the rest
            foreach ($n in $chunk) {
                try {
                    if ($Enabled) { Enable-NetFirewallRule -Name $n -ErrorAction Stop }
                    else { Disable-NetFirewallRule -Name $n -ErrorAction Stop }
                    $ok++
                }
                catch { $fail++ }
            }
        }
    }
    if ($fail -gt 0) {
        Write-WarnMsg ("{0} firewall rule(s) could not be {1}." -f $fail, $(if ($Enabled) { 're-enabled' } else { 'disabled' }))
    }
    return $ok
}

function New-FwAllowRule {
    param([hashtable] $Properties)
    $params = @{
        DisplayName = $Properties.DisplayName
        Name        = $Properties.Name
        Group       = $script:RuleGroup
        Direction   = 'Outbound'
        Action      = 'Allow'
        Enabled     = 'True'
        Profile     = 'Any'
        ErrorAction = 'Stop'
    }
    if ($Properties.ContainsKey('Protocol')) { $params.Protocol = $Properties.Protocol }
    if ($Properties.ContainsKey('LocalPort')) { $params.LocalPort = $Properties.LocalPort }
    if ($Properties.ContainsKey('RemotePort')) { $params.RemotePort = $Properties.RemotePort }
    if ($Properties.ContainsKey('RemoteAddress')) { $params.RemoteAddress = $Properties.RemoteAddress }
    if ($Properties.ContainsKey('InterfaceAlias')) { $params.InterfaceAlias = $Properties.InterfaceAlias }
    New-NetFirewallRule @params | Out-Null
}

function Get-SystemHostsPath {
    Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
}

function Get-KillSwitchHostsBackupPath {
    # Sidecar next to hosts; do not overwrite Microsoft's hosts.bak sample.
    Join-Path (Split-Path -Parent (Get-SystemHostsPath)) 'hosts.handy-scripts.bak'
}

function Read-HostsFileLines {
    param(
        [string] $Path,
        [int] $Retries = 20,
        [int] $DelayMs = 250
    )
    $expectedLen = (Get-Item -LiteralPath $Path -ErrorAction Stop).Length
    $last = $null
    for ($attempt = 1; $attempt -le $Retries; $attempt++) {
        try {
            $share = [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete
            $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, $share)
            try {
                $sr = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::ASCII, $true)
                try {
                    $list = New-Object System.Collections.Generic.List[string]
                    while ($null -ne ($line = $sr.ReadLine())) {
                        [void]$list.Add($line)
                    }
                    # Empty read of a non-empty file would make a later write wipe hosts.
                    if ($expectedLen -gt 0 -and $list.Count -eq 0) {
                        throw "Hosts file is $expectedLen bytes but read returned 0 lines."
                    }
                    # Comma prevents PowerShell from unwrapping List -> fixed Object[] (breaks .Add/.RemoveAt).
                    return ,$list
                }
                finally { $sr.Dispose() }
            }
            finally { $fs.Dispose() }
        }
        catch {
            $last = $_
            Start-Sleep -Milliseconds $DelayMs
        }
    }
    throw ("Could not read hosts file after {0} tries: {1}" -f $Retries, $last.Exception.Message)
}

function Write-HostsFileLines {
    <#
      Safe hosts update:
      - refuse empty payload when current file has content (never wipe)
      - write to a same-directory temp first
      - File.Replace moves current hosts to hosts.handy-scripts.bak, then moves temp into place
      Never Set-Content / redirect onto hosts (those truncate on open).
    #>
    param(
        [string] $Path,
        $Lines,
        [int] $Retries = 20,
        [int] $DelayMs = 250
    )
    $existingLen = 0
    if (Test-Path -LiteralPath $Path) {
        $existingLen = (Get-Item -LiteralPath $Path).Length
    }

    $sb = New-Object System.Text.StringBuilder
    foreach ($ln in $Lines) {
        [void]$sb.Append([string]$ln)
        [void]$sb.Append("`r`n")
    }
    $bytes = [System.Text.Encoding]::ASCII.GetBytes($sb.ToString())
    if ($bytes.Length -eq 0 -and $existingLen -gt 0) {
        throw ("Refusing to write empty hosts content over a {0}-byte file." -f $existingLen)
    }

    $dir = Split-Path -Parent $Path
    $backupPath = Get-KillSwitchHostsBackupPath
    $temp = Join-Path $dir ('hosts.handy-scripts.{0}.tmp' -f [guid]::NewGuid().ToString('n'))
    [System.IO.File]::WriteAllBytes($temp, $bytes)
    $tempLen = (Get-Item -LiteralPath $temp).Length
    if ($tempLen -ne $bytes.Length) {
        Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
        throw ("Hosts temp write size mismatch ({0} vs {1})." -f $tempLen, $bytes.Length)
    }

    $last = $null
    try {
        for ($attempt = 1; $attempt -le $Retries; $attempt++) {
            try {
                if ($existingLen -gt 0) {
                    # Atomic-ish: current -> backup, temp -> hosts. Does not truncate hosts first.
                    [System.IO.File]::Replace($temp, $Path, $backupPath)
                }
                else {
                    [System.IO.File]::Copy($temp, $Path, $true)
                    Remove-Item -LiteralPath $temp -Force -ErrorAction Stop
                }
                $finalLen = (Get-Item -LiteralPath $Path).Length
                if ($finalLen -eq 0 -and $bytes.Length -gt 0) {
                    throw "Hosts file is 0 bytes after replace; refusing to leave it wiped."
                }
                if ($finalLen -ne $bytes.Length) {
                    throw ("Hosts size after write is {0}, expected {1}." -f $finalLen, $bytes.Length)
                }
                Write-Info ("Hosts: updated ({0} bytes); previous copy at {1}" -f $finalLen, $backupPath)
                return
            }
            catch {
                $last = $_
                Start-Sleep -Milliseconds $DelayMs
            }
        }
        throw ("Could not update hosts file after {0} tries (often antivirus locking it). Close hosts editors and retry. {1}" -f $Retries, $last.Exception.Message)
    }
    finally {
        if (Test-Path -LiteralPath $temp) {
            Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-OvpnConfigAuditWarnings {
    param([string] $ConfigPath)
    $raw = Get-Content -LiteralPath $ConfigPath -Raw -ErrorAction Stop
    $text = [regex]::Replace($raw, '(?m)^\s*#.*$', '')
    $warnings = New-Object System.Collections.Generic.List[string]

    if ($text -notmatch '(?im)^\s*redirect-gateway\b') {
        [void]$warnings.Add('No redirect-gateway - profile may be split-tunnel (not all IPv4 via VPN).')
    }
    $hasBlockDns = ($text -match '(?im)block-outside-dns') -or ($text -match '(?im)setenv\s+opt\s+block-outside-dns')
    if (-not $hasBlockDns) {
        [void]$warnings.Add('No block-outside-dns - Windows may still use uplink DNS outside the tunnel.')
    }
    if ($text -notmatch '(?im)^\s*dhcp-option\s+DNS\b' -and $text -notmatch '(?im)^\s*pull\b' -and $text -notmatch '(?im)^\s*client\b') {
        [void]$warnings.Add('No dhcp-option DNS / pull / client hints - VPN DNS push not obvious from the file.')
    }
    if ($text -notmatch '(?im)redirect-gateway.*ipv6' -and $text -notmatch '(?im)^\s*tun-ipv6\b') {
        [void]$warnings.Add('No IPv6 redirect/tun-ipv6 - IPv6 may bypass the tunnel if enabled on the link.')
    }
    if ($warnings.Count -eq 0) { return @() }
    return [object[]]$warnings.ToArray()
}

function Sync-KillSwitchHostsEntry {
    <#
      Once-per-Arm hosts pin for a VPN hostname.
      Returns management record for state / Disarm undo.
    #>
    param(
        [Parameter(Mandatory = $true)][string] $Hostname,
        [Parameter(Mandatory = $true)][string] $Ip
    )
    $hostsPath = Get-SystemHostsPath
    if (-not (Test-Path -LiteralPath $hostsPath)) {
        throw "Hosts file not found: $hostsPath"
    }
    $hostsLen = (Get-Item -LiteralPath $hostsPath).Length
    if ($hostsLen -eq 0) {
        throw "Hosts file is empty (0 bytes). Restore from hosts.bak (or another known-good copy) before Arming - refusing to write onto a wiped hosts file."
    }
    $hostLower = $Hostname.ToLowerInvariant()
    $marker = "# handy-scripts-ks: $Hostname"
    $lines = Read-HostsFileLines -Path $hostsPath
    if ($lines -isnot [System.Collections.IList] -or $lines.IsFixedSize) {
        $tmp = New-Object System.Collections.Generic.List[string]
        foreach ($ln in @($lines)) { [void]$tmp.Add([string]$ln) }
        $lines = $tmp
    }

    $matchIndexes = New-Object System.Collections.Generic.List[int]
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $trim = $lines[$i].Trim()
        if (-not $trim -or $trim.StartsWith('#')) { continue }
        $parts = $trim -split '\s+'
        if ($parts.Count -lt 2) { continue }
        $names = @($parts[1..($parts.Count - 1)] | ForEach-Object { $_.ToLowerInvariant() })
        if ($names -contains $hostLower) {
            [void]$matchIndexes.Add($i)
        }
    }

    if ($matchIndexes.Count -eq 0) {
        if (-not $WhatIfPreference) {
            if ($lines.Count -gt 0 -and -not [string]::IsNullOrWhiteSpace($lines[$lines.Count - 1])) {
                [void]$lines.Add('')
            }
            [void]$lines.Add($marker)
            [void]$lines.Add("$Ip`t$Hostname")
            Write-HostsFileLines -Path $hostsPath -Lines $lines
        }
        Write-Info ("Hosts: added {0} -> {1}" -f $Hostname, $Ip)
        return [pscustomobject]@{
            Hostname   = $Hostname
            Ip         = $Ip
            Action     = 'added'
            Owned      = $true
            PreviousIp = $null
        }
    }

    $primaryIdx = $matchIndexes[0]
    $primaryParts = $lines[$primaryIdx].Trim() -split '\s+'
    $currentIp = $primaryParts[0]
    if ($currentIp -eq $Ip) {
        Write-Info ("Hosts: unchanged {0} -> {1}" -f $Hostname, $Ip)
        return [pscustomobject]@{
            Hostname   = $Hostname
            Ip         = $Ip
            Action     = 'unchanged'
            Owned      = $false
            PreviousIp = $null
        }
    }

    $previousLine = [string]$lines[$primaryIdx]
    if (-not $WhatIfPreference) {
        # Preserve any other hostnames on the same line (do not wipe aliases).
        $otherNames = @($primaryParts[1..($primaryParts.Count - 1)] | Where-Object {
                $_.ToLowerInvariant() -ne $hostLower
            })
        $nameList = @($Hostname) + @($otherNames)
        $lines[$primaryIdx] = "$Ip`t$($nameList -join "`t")"
        # Drop duplicate hostname lines (keep first updated)
        for ($j = $matchIndexes.Count - 1; $j -ge 1; $j--) {
            [void]$lines.RemoveAt($matchIndexes[$j])
        }
        Write-HostsFileLines -Path $hostsPath -Lines $lines
    }
    Write-Info ("Hosts: updated {0} {1} -> {2}" -f $Hostname, $currentIp, $Ip)
    return [pscustomobject]@{
        Hostname     = $Hostname
        Ip           = $Ip
        Action       = 'updated'
        Owned        = $true
        PreviousIp   = $currentIp
        PreviousLine = $previousLine
    }
}

function Undo-KillSwitchHostsFromState {
    param($State)
    if (-not $State) { return }
    $managed = @()
    if (($State.PSObject.Properties.Name -contains 'HostsManaged') -and $State.HostsManaged) {
        $managed = @($State.HostsManaged)
    }
    if ($managed.Count -eq 0) { return }

    $hostsPath = Get-SystemHostsPath
    if (-not (Test-Path -LiteralPath $hostsPath)) { return }

    $lines = Read-HostsFileLines -Path $hostsPath
    if ($lines -isnot [System.Collections.IList] -or $lines.IsFixedSize) {
        $tmp = New-Object System.Collections.Generic.List[string]
        foreach ($ln in @($lines)) { [void]$tmp.Add([string]$ln) }
        $lines = $tmp
    }
    $changed = $false

    foreach ($entry in $managed) {
        if (-not $entry) { continue }
        $action = [string](Get-ObjectPropertyValue -Object $entry -Name 'Action' -Default '')
        $hostname = [string](Get-ObjectPropertyValue -Object $entry -Name 'Hostname' -Default '')
        $owned = $false
        if ($null -ne (Get-ObjectPropertyValue -Object $entry -Name 'Owned')) {
            $owned = [bool](Get-ObjectPropertyValue -Object $entry -Name 'Owned')
        }
        if (-not $owned) { continue }

        $hostLower = $hostname.ToLowerInvariant()
        $markerPrefix = '# handy-scripts-ks:'

        if ($action -eq 'added') {
            for ($i = $lines.Count - 1; $i -ge 0; $i--) {
                $trim = $lines[$i].Trim()
                if ($trim -eq "$markerPrefix $hostname" -or $trim -like "$markerPrefix $hostname") {
                    [void]$lines.RemoveAt($i)
                    $changed = $true
                    continue
                }
                if ($trim.StartsWith('#')) { continue }
                $parts = $trim -split '\s+'
                if ($parts.Count -ge 2) {
                    $names = @($parts[1..($parts.Count - 1)] | ForEach-Object { $_.ToLowerInvariant() })
                    if ($names -contains $hostLower) {
                        [void]$lines.RemoveAt($i)
                        $changed = $true
                    }
                }
            }
            Write-Info ("Hosts: removed managed entry for {0}" -f $hostname)
        }
        elseif ($action -eq 'updated' -and ($entry.PSObject.Properties.Name -contains 'PreviousIp') -and $entry.PreviousIp) {
            $previousLine = [string](Get-ObjectPropertyValue -Object $entry -Name 'PreviousLine' -Default '')
            for ($i = 0; $i -lt $lines.Count; $i++) {
                $trim = $lines[$i].Trim()
                if ($trim.StartsWith('#')) { continue }
                $parts = $trim -split '\s+'
                if ($parts.Count -ge 2) {
                    $names = @($parts[1..($parts.Count - 1)] | ForEach-Object { $_.ToLowerInvariant() })
                    if ($names -contains $hostLower) {
                        if (-not [string]::IsNullOrWhiteSpace($previousLine)) {
                            $lines[$i] = $previousLine
                        }
                        else {
                            $lines[$i] = ("{0}`t{1}" -f [string]$entry.PreviousIp, $hostname)
                        }
                        $changed = $true
                        Write-Info ("Hosts: restored {0} -> {1}" -f $hostname, $entry.PreviousIp)
                        break
                    }
                }
            }
        }
    }

    if ($changed -and -not $WhatIfPreference) {
        Write-HostsFileLines -Path $hostsPath -Lines $lines
    }
}

function Get-ArmPlanSummary {
    param(
        [object[]] $Endpoints,
        [object[]] $TunnelAdapters,
        [object[]] $HostsActions,
        [string[]] $ConfigWarnings
    )
    $lines = New-Object System.Collections.Generic.List[string]
    [void]$lines.Add('Create DHCP + VPN endpoint (+ tunnel) allow rules first')
    [void]$lines.Add('Then disable other outbound Allow rules (saved for Disarm)')
    [void]$lines.Add('Then set DefaultOutboundAction=Block on Domain/Private/Public')
    [void]$lines.Add('No uplink DNS allows - DNS only via OpenVPN tunnel adapters when present')
    foreach ($h in $HostsActions) {
        if (-not $h) { continue }
        [void]$lines.Add(('Hosts {0}: {1} -> {2}' -f
                (Get-ObjectPropertyValue -Object $h -Name 'Action' -Default '?'),
                (Get-ObjectPropertyValue -Object $h -Name 'Hostname' -Default '?'),
                (Get-ObjectPropertyValue -Object $h -Name 'Ip' -Default '?')))
    }
    foreach ($ep in $Endpoints) {
        [void]$lines.Add(('Allow VPN {0}:{1}/{2} (any interface)' -f $ep.IP, $ep.Port, $ep.Proto))
    }
    if (@($TunnelAdapters).Count -gt 0) {
        foreach ($t in $TunnelAdapters) {
            [void]$lines.Add(('Allow all outbound on tunnel "{0}"' -f $t.Name))
        }
    }
    else {
        [void]$lines.Add('Tunnel allow rules added when OpenVPN adapters appear (auto-poll / Retry tunnel allows)')
    }
    foreach ($w in $ConfigWarnings) {
        [void]$lines.Add(('WARN: {0}' -f $w))
    }
    if ($lines.Count -eq 0) { return @() }
    return [string[]]$lines.ToArray()
}

function New-KillSwitchAllowRules {
    param(
        [object[]] $Endpoints,
        [object[]] $TunnelAdapters
    )
    Write-Info ("Creating allow rules in group '{0}' (no uplink DNS)..." -f $script:RuleGroup)

    New-FwAllowRule -Properties @{
        DisplayName = "$script:RulePrefix Allow DHCP"
        Name        = (New-KillSwitchRuleName 'allow-dhcp')
        Protocol    = 'UDP'
        LocalPort   = 68
        RemotePort  = 67
    }

    $i = 0
    foreach ($ep in $Endpoints) {
        $i++
        New-FwAllowRule -Properties @{
            DisplayName   = ("$script:RulePrefix Allow VPN {0}:{1}/{2}" -f $ep.IP, $ep.Port, $ep.Proto.ToUpperInvariant())
            Name          = (New-KillSwitchRuleName "allow-vpn-$i")
            Protocol      = $ep.Proto.ToUpperInvariant()
            RemoteAddress = $ep.IP
            RemotePort    = $ep.Port
        }
    }

    foreach ($tun in $TunnelAdapters) {
        $tunSuffix = 'allow-tun-' + (($tun.Name -replace '[^\w\-]', '_'))
        New-FwAllowRule -Properties @{
            DisplayName    = ("$script:RulePrefix Allow tunnel ({0})" -f $tun.Name)
            Name           = (New-KillSwitchRuleName $tunSuffix)
            InterfaceAlias = $tun.Name
        }
    }
}

function Invoke-ArmKillSwitch {
    param(
        [string] $ConfigPath,
        [switch] $ConnectAfter
    )

    $install = Get-OpenVpnInstallInfo
    $configPath = Resolve-OvpnConfigPath -ConfigArgument $ConfigPath
    $configWarnings = @(Get-OvpnConfigAuditWarnings -ConfigPath $configPath)
    foreach ($w in $configWarnings) { Write-WarnMsg $w }

    $remotes = @(Get-OvpnRemotes -ConfigPath $configPath)

    # Resolve hostname remotes once (in memory) for planning / endpoint allows.
    $plannedHosts = New-Object System.Collections.Generic.List[object]
    foreach ($r in $remotes) {
        if (Test-IsIpAddress $r.Host) { continue }
        try {
            $resolved = @([System.Net.Dns]::GetHostAddresses($r.Host) | ForEach-Object { $_.IPAddressToString } | Select-Object -Unique)
        }
        catch {
            throw "Failed to resolve VPN hostname '$($r.Host)' before Arm: $($_.Exception.Message)"
        }
        if ($resolved.Count -eq 0) { throw "No addresses for VPN hostname '$($r.Host)'" }
        [void]$plannedHosts.Add([pscustomobject]@{
                Hostname = $r.Host
                Ip       = [string]$resolved[0]
                Action   = 'resolve'
                Owned    = $false
            })
    }

    $endpoints = @(Resolve-RemoteEndpoints -Remotes $remotes)
    $tunnels = @(Get-OpenVpnTunnelAdapters)
    $hostsForPlan = if ($plannedHosts.Count -eq 0) { @() } else { [object[]]$plannedHosts.ToArray() }
    $plan = Get-ArmPlanSummary -Endpoints $endpoints -TunnelAdapters $tunnels -HostsActions $hostsForPlan -ConfigWarnings $configWarnings

    if ($WhatIfPreference) {
        Write-Info ("WhatIf Arm for: {0}" -f $configPath)
        foreach ($p in $plan) { Write-Info ("  - {0}" -f $p) }
        return
    }

    Ensure-AdministratorForMutation

    # Re-Arm while Armed would re-snapshot Block/disabled allows and break Disarm restore.
    if (Test-IsKillSwitchArmed) {
        throw 'Kill switch is already Armed. Disarm first, then Arm again (re-Arm would overwrite restore data and risk a stuck Block policy).'
    }

    # Capture restore snapshot BEFORE hosts / firewall mutation (while still open).
    $savedOutbound = Get-CurrentDefaultOutboundActions
    $disabledNames = @(Get-EnabledOutboundAllowRuleNames)

    $hostsManaged = New-Object System.Collections.Generic.List[object]
    $stateWritten = $false
    $armedOk = $false
    try {
        # Pin hostname remotes in hosts ONCE so reconnect needs no uplink DNS.
        foreach ($h in $plannedHosts) {
            [void]$hostsManaged.Add((Sync-KillSwitchHostsEntry -Hostname $h.Hostname -Ip $h.Ip))
        }
        $endpoints = @(Resolve-RemoteEndpoints -Remotes $remotes)

        $state = [pscustomobject]@{
            SavedAt                    = (Get-Date).ToString('o')
            ConfigPath                 = $configPath
            DefaultOutboundAction      = $savedOutbound
            DisabledOutboundAllowRules = $disabledNames
            Endpoints                  = @($endpoints | ForEach-Object {
                    [pscustomobject]@{ Host = $_.Host; IP = $_.IP; Port = $_.Port; Proto = $_.Proto }
                })
            HostsManaged               = $(if ($hostsManaged.Count -eq 0) { @() } else { [object[]]$hostsManaged.ToArray() })
            ConfigWarnings             = @($configWarnings)
            ConnectRequested           = [bool]$ConnectAfter
        }
        # Persist HostsManaged + restore data before firewall mutation so Disarm can recover.
        Write-KillSwitchState -State $state
        $stateWritten = $true

        # Durable Disarm-like bak (survives if state.json is lost) + dated snapshot.
        $bakSnap = New-FirewallRestoreSnapshot -DefaultOutboundAction $savedOutbound -EnabledOutboundAllowRules $disabledNames
        $bakPaths = Save-FirewallRestoreSnapshot -Snapshot $bakSnap
        Write-Info ("Firewall restore bak updated: {0}" -f $bakPaths.BakPath)

        $existing = @(Get-ExistingKillSwitchRules)
        if ($existing.Count -gt 0) {
            Write-Info ("Removing {0} previous kill-switch rule(s)..." -f $existing.Count)
            $existing | Remove-NetFirewallRule -ErrorAction Stop
        }

        # Create escape hatches FIRST, then lock the door (avoids a Block-with-no-allows window).
        New-KillSwitchAllowRules -Endpoints $endpoints -TunnelAdapters $tunnels

        Write-Info ("Disabling {0} other outbound Allow rule(s)..." -f $disabledNames.Count)
        [void](Set-OutboundAllowRulesEnabled -Names $disabledNames -Enabled $false)

        Write-Info 'Setting DefaultOutboundAction=Block...'
        Set-NetFirewallProfile -Profile Domain, Private, Public -DefaultOutboundAction Block

        $armedOk = $true
        Write-Info ("Armed. State: {0}" -f $script:StatePath)

        if ($ConnectAfter) {
            try {
                Connect-OpenVpnGuiProfile -GuiExe $install.GuiExe -ConfigPath $configPath
                Wait-OpenVpnTunnelAllowRules -TimeoutSeconds 30 -PollSeconds 2
            }
            catch {
                # Firewall Arm already succeeded — do not roll back for a connect/poll problem.
                Write-WarnMsg ("Armed, but connect/tunnel-allow step failed: {0}" -f $_.Exception.Message)
            }
        }
    }
    catch {
        if ($armedOk) {
            throw
        }
        if (-not $stateWritten -and $hostsManaged.Count -gt 0) {
            Write-WarnMsg 'Arm failed before state was saved — undoing hosts pins from this attempt.'
            try {
                Undo-KillSwitchHostsFromState -State ([pscustomobject]@{
                        HostsManaged = [object[]]$hostsManaged.ToArray()
                    })
            }
            catch {
                Write-WarnMsg ("Hosts undo after failed Arm also failed: {0}" -f $_.Exception.Message)
            }
        }
        elseif ($stateWritten) {
            Write-WarnMsg 'Arm failed after state was saved — auto-Disarm to restore firewall and hosts.'
            try {
                Invoke-DisarmKillSwitch
                Write-WarnMsg 'Auto-Disarm finished. You are back to Disarmed; fix the error and Arm again.'
            }
            catch {
                Write-WarnMsg ("Auto-Disarm failed: {0}. Manually run Disarm (as Administrator) to restore networking." -f $_.Exception.Message)
            }
        }
        throw
    }
}

function Update-TunnelAllowRules {
    # Returns count of newly added tunnel allow rules.
    if ($WhatIfPreference) { return 0 }
    if (-not (Test-IsKillSwitchArmed)) { return 0 }
    $existing = @(Get-ExistingKillSwitchRules)
    if ($existing.Count -eq 0) { return 0 }
    $tunnels = @(Get-OpenVpnTunnelAdapters)
    $added = 0
    foreach ($tun in $tunnels) {
        $display = ("$script:RulePrefix Allow tunnel ({0})" -f $tun.Name)
        $tunSuffix = 'allow-tun-' + (($tun.Name -replace '[^\w\-]', '_'))
        $name = New-KillSwitchRuleName $tunSuffix
        $already = @($existing | Where-Object {
                $_.Name -eq $name -or $_.DisplayName -eq $display
            })
        if ($already.Count -gt 0) { continue }
        if (-not (Test-IsAdministrator)) { continue }
        New-FwAllowRule -Properties @{
            DisplayName    = $display
            Name           = $name
            InterfaceAlias = $tun.Name
        }
        Write-Info ("Added tunnel allow for '{0}'." -f $tun.Name)
        $added++
        $existing = @(Get-ExistingKillSwitchRules)
    }
    return $added
}

function Wait-OpenVpnTunnelAllowRules {
    param(
        [int] $TimeoutSeconds = 30,
        [int] $PollSeconds = 2
    )
    if ($WhatIfPreference) { return }
    if (-not (Test-IsKillSwitchArmed)) { return }

    Write-Info ("Waiting up to {0}s for OpenVPN tunnel adapters / allow rules..." -f $TimeoutSeconds)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $covered = 0
    $tunnelCount = 0
    do {
        [void](Update-TunnelAllowRules)
        $tunnels = @(Get-OpenVpnTunnelAdapters)
        $tunnelCount = $tunnels.Count
        $existing = @(Get-ExistingKillSwitchRules)
        $covered = 0
        foreach ($tun in $tunnels) {
            $hit = @($existing | Where-Object {
                    $_.DisplayName -eq ("$script:RulePrefix Allow tunnel ({0})" -f $tun.Name)
                })
            if ($hit.Count -gt 0) { $covered++ }
        }
        if ($tunnelCount -gt 0 -and $covered -eq $tunnelCount) {
            Write-Info ("Tunnel allows ready ({0} adapter(s))." -f $tunnelCount)
            return
        }
        Start-Sleep -Seconds $PollSeconds
    } while ((Get-Date) -lt $deadline)

    if ($tunnelCount -eq 0) {
        Write-Info 'No OpenVPN tunnel adapters seen within poll window. After connect, use Advanced -> Retry tunnel allows if needed.'
    }
    else {
        Write-Info ("Tunnel allow poll finished; covered {0}/{1} adapter(s). Use Advanced -> Retry tunnel allows if traffic still fails." -f $covered, $tunnelCount)
    }
}

function Invoke-DisarmKillSwitch {
    if ($WhatIfPreference) {
        $existing = @(Get-ExistingKillSwitchRules)
        $state = Read-KillSwitchState
        $cfg = if ($state -and $state.ConfigPath) { [string]$state.ConfigPath } else { '(none in state)' }
        Write-Info ("WhatIf Disarm: best-effort disconnect '{0}'; remove {1} rule(s); restore profiles/allows from state={2}" -f $cfg, $existing.Count, [bool]$state)
        return
    }

    Ensure-AdministratorForMutation

    $state = Read-KillSwitchState
    $configPath = $null
    if ($state -and $state.ConfigPath) {
        $configPath = [string]$state.ConfigPath
    }

    # End session safely: drop the Armed tunnel first (while still Armed), then open the firewall.
    # Best-effort only — already disconnected / GUI missing must not fail Disarm.
    try {
        $install = Get-OpenVpnInstallInfo
        if ($configPath) {
            Write-Info ("Best-effort disconnect of Armed profile: {0}" -f $configPath)
            Disconnect-OpenVpnGuiProfile -GuiExe $install.GuiExe -ConfigPath $configPath
        }
        else {
            Write-Info 'No Armed profile in state — skipping disconnect.'
        }
    }
    catch {
        Write-Info ("Disconnect skipped: {0}" -f $_.Exception.Message)
    }

    $existing = @(Get-ExistingKillSwitchRules)
    if ($existing.Count -gt 0) {
        Write-Info ("Removing {0} kill-switch rule(s)..." -f $existing.Count)
        $existing | Remove-NetFirewallRule -ErrorAction Stop
    }
    else {
        Write-Info 'No kill-switch rules present.'
    }

    if ($state) {
        if ($state.DefaultOutboundAction) {
            foreach ($p in @('Domain', 'Private', 'Public')) {
                $val = $state.DefaultOutboundAction.$p
                if ($val) {
                    Write-Info ("Restoring {0} DefaultOutboundAction={1}" -f $p, $val)
                    Set-NetFirewallProfile -Profile $p -DefaultOutboundAction $val
                }
            }
        }
        $names = @($state.DisabledOutboundAllowRules)
        if ($names.Count -gt 0) {
            Write-Info ("Re-enabling {0} outbound Allow rule(s)..." -f $names.Count)
            [void](Set-OutboundAllowRulesEnabled -Names $names -Enabled $true)
        }
        Undo-KillSwitchHostsFromState -State $state
        Remove-KillSwitchStateFile
    }
    else {
        Write-WarnMsg 'No state file — profile defaults / other allows may need manual check.'
    }
    Write-Info 'Disarmed.'
}

function Get-StatusText {
    $armed = Test-IsKillSwitchArmed
    $state = Read-KillSwitchState
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.AppendLine($(if ($armed) { 'Kill switch: ARMED' } else { 'Kill switch: DISARMED' }))
    foreach ($p in @(Get-NetFirewallProfile | Select-Object Name, DefaultOutboundAction)) {
        [void]$sb.AppendLine(('  Profile {0}: DefaultOutboundAction={1}' -f $p.Name, $p.DefaultOutboundAction))
    }
    if ($state) {
        [void]$sb.AppendLine(('  State file: {0}' -f $script:StatePath))
        [void]$sb.AppendLine(('  SavedAt: {0}' -f $state.SavedAt))
        [void]$sb.AppendLine(('  Config: {0}' -f $state.ConfigPath))
        [void]$sb.AppendLine(('  Disabled foreign allows: {0}' -f @($state.DisabledOutboundAllowRules).Count))
    }
    $rules = @(Get-ExistingKillSwitchRules)
    [void]$sb.AppendLine(('  Rules in group: {0}' -f $rules.Count))
    foreach ($rule in ($rules | Sort-Object DisplayName)) {
        if (-not $rule) { continue }
        $action = Get-ObjectPropertyValue -Object $rule -Name 'Action' -Default '?'
        $disp = Get-ObjectPropertyValue -Object $rule -Name 'DisplayName' -Default '(unnamed)'
        [void]$sb.AppendLine(('    [{0}] {1}' -f $action, $disp))
    }
    return $sb.ToString()
}

function Connect-OpenVpnGuiProfile {
    param(
        [string] $GuiExe,
        [string] $ConfigPath
    )
    if (-not $GuiExe -or -not (Test-Path -LiteralPath $GuiExe)) {
        throw 'openvpn-gui.exe not found.'
    }
    $fileName = [System.IO.Path]::GetFileName($ConfigPath)
    if ($WhatIfPreference) {
        Write-Info ("WhatIf: would connect {0}" -f $fileName)
        return
    }
    Write-Info ("Connecting via OpenVPN GUI: {0}" -f $fileName)
    Start-Process -FilePath $GuiExe -ErrorAction SilentlyContinue | Out-Null
    Start-Sleep -Seconds 1
    Start-Process -FilePath $GuiExe -ArgumentList @('--connect', $fileName) -ErrorAction Stop | Out-Null
}

function Disconnect-OpenVpnGuiProfile {
    param(
        [string] $GuiExe,
        [string] $ConfigPath
    )
    # Best-effort: never throw. Missing GUI / already disconnected is fine.
    if (-not $GuiExe -or -not (Test-Path -LiteralPath $GuiExe)) {
        Write-Info 'OpenVPN GUI not found — disconnect skipped.'
        return
    }
    if (-not $ConfigPath) {
        Write-Info 'No config path — disconnect skipped.'
        return
    }
    $fileName = [System.IO.Path]::GetFileName($ConfigPath)
    if ($WhatIfPreference) {
        Write-Info ("WhatIf: would disconnect {0}" -f $fileName)
        return
    }
    try {
        Write-Info ("Disconnecting via OpenVPN GUI: {0}" -f $fileName)
        Start-Process -FilePath $GuiExe -ArgumentList @('--command', 'disconnect', $fileName) -ErrorAction Stop | Out-Null
    }
    catch {
        Write-Info ("Disconnect request not confirmed (continuing): {0}" -f $_.Exception.Message)
    }
}

# -----------------------------------------------------------------------------
# WinForms GUI
# -----------------------------------------------------------------------------

function Hide-ConsoleWindow {
    # Hide the PowerShell console when running the WinForms UI.
    try {
        $sig = @'
[DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
'@
        $type = Add-Type -MemberDefinition $sig -Name 'KillSwitchConsole' -Namespace 'HandyScripts' -PassThru -ErrorAction Stop
        $hwnd = $type::GetConsoleWindow()
        if ($hwnd -ne [IntPtr]::Zero) {
            [void]$type::ShowWindow($hwnd, 0) # SW_HIDE
        }
    }
    catch {
        # Non-fatal if console hide fails
    }
}

function Get-RoadWarriorVerifierLaunch {
    <#
      Returns @{ Exe; ArgumentList; ScriptPath } if verify_roadwarrior.pyw and a Python
      launcher exist next to this script; otherwise $null.
    #>
    $scriptPath = Join-Path $PSScriptRoot 'verify_roadwarrior.pyw'
    if (-not (Test-Path -LiteralPath $scriptPath)) { return $null }

    foreach ($name in @('pythonw', 'python', 'py')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        $exe = [string]$cmd.Source
        if (-not $exe -or -not (Test-Path -LiteralPath $exe)) { continue }
        if ($name -eq 'py') {
            return [pscustomobject]@{
                Exe          = $exe
                ArgumentList = @('-3', $scriptPath)
                ScriptPath   = $scriptPath
            }
        }
        return [pscustomobject]@{
            Exe          = $exe
            ArgumentList = @($scriptPath)
            ScriptPath   = $scriptPath
        }
    }
    return $null
}

function Show-KillSwitchGui {
    Hide-ConsoleWindow
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()

    $install = Get-OpenVpnInstallInfo
    $profiles = @(Get-OvpnConfigProfiles)
    $pathByDisplay = @{}
    foreach ($p in $profiles) {
        $pathByDisplay[$p.DisplayName] = $p.FullPath
    }

    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'OpenVPN Kill Switch'
    $form.Size = New-Object System.Drawing.Size(520, 740)
    $form.StartPosition = 'CenterScreen'
    $form.MinimumSize = New-Object System.Drawing.Size(480, 620)

    $toolTip = New-Object System.Windows.Forms.ToolTip
    $toolTip.AutoPopDelay = 12000
    $toolTip.InitialDelay = 400
    $toolTip.ReshowDelay = 200
    $toolTip.ShowAlways = $true

    $lblStatus = New-Object System.Windows.Forms.Label
    $lblStatus.Location = New-Object System.Drawing.Point(12, 12)
    $lblStatus.Size = New-Object System.Drawing.Size(480, 36)
    $lblStatus.Font = New-Object System.Drawing.Font('Segoe UI', 11, [System.Drawing.FontStyle]::Bold)
    $lblStatus.Anchor = 'Top,Left,Right'
    $form.Controls.Add($lblStatus)

    $cfgLabelParts = @()
    if ($install.UserConfigExists) { $cfgLabelParts += ('User: {0}' -f $install.UserConfigDir) }
    if ($install.GlobalConfigExists) { $cfgLabelParts += ('System: {0}' -f $install.GlobalConfigDir) }
    $lblConfigDir = New-Object System.Windows.Forms.Label
    $lblConfigDir.Text = ('Config folders: {0}' -f ($cfgLabelParts -join '  |  '))
    $lblConfigDir.Location = New-Object System.Drawing.Point(12, 50)
    $lblConfigDir.Size = New-Object System.Drawing.Size(480, 18)
    $lblConfigDir.Font = New-Object System.Drawing.Font('Segoe UI', 8)
    $lblConfigDir.ForeColor = [System.Drawing.Color]::Gray
    $lblConfigDir.Anchor = 'Top,Left,Right'
    $form.Controls.Add($lblConfigDir)

    $lblConfig = New-Object System.Windows.Forms.Label
    $lblConfig.Text = 'OpenVPN profile:'
    $lblConfig.Location = New-Object System.Drawing.Point(12, 72)
    $lblConfig.AutoSize = $true
    $form.Controls.Add($lblConfig)

    $cmbConfig = New-Object System.Windows.Forms.ComboBox
    $cmbConfig.Location = New-Object System.Drawing.Point(12, 94)
    $cmbConfig.Size = New-Object System.Drawing.Size(480, 28)
    $cmbConfig.DropDownStyle = 'DropDownList'
    $cmbConfig.Anchor = 'Top,Left,Right'
    foreach ($p in $profiles) { [void]$cmbConfig.Items.Add($p.DisplayName) }
    # Default: first listed profile (user tree precedes system, matching OpenVPN GUI scan order).
    if ($cmbConfig.Items.Count -gt 0) { $cmbConfig.SelectedIndex = 0 }
    $state = Read-KillSwitchState
    if ($state -and $state.ConfigPath) {
        $wantPath = [string]$state.ConfigPath
        $matched = $false
        foreach ($p in $profiles) {
            if ($p.FullPath -eq $wantPath) {
                $idx = $cmbConfig.Items.IndexOf($p.DisplayName)
                if ($idx -ge 0) { $cmbConfig.SelectedIndex = $idx; $matched = $true; break }
            }
        }
        if (-not $matched) {
            $wantBase = [System.IO.Path]::GetFileNameWithoutExtension($wantPath)
            $idx = $cmbConfig.Items.IndexOf($wantBase)
            if ($idx -ge 0) { $cmbConfig.SelectedIndex = $idx }
        }
    }
    $form.Controls.Add($cmbConfig)
    $toolTip.SetToolTip($cmbConfig, 'OpenVPN profiles: user folder first, then system; nested as folder\name.')

    # Primary session pair
    $btnArmConnect = New-Object System.Windows.Forms.Button
    $btnArmConnect.Text = 'Arm + Connect'
    $btnArmConnect.Location = New-Object System.Drawing.Point(12, 134)
    $btnArmConnect.Size = New-Object System.Drawing.Size(230, 36)
    $btnArmConnect.BackColor = [System.Drawing.Color]::FromArgb(46, 125, 50)
    $btnArmConnect.ForeColor = [System.Drawing.Color]::White
    $btnArmConnect.FlatStyle = 'Flat'
    $form.Controls.Add($btnArmConnect)
    $toolTip.SetToolTip($btnArmConnect, 'Start a protected session: apply the kill switch, then connect this profile in OpenVPN GUI.')

    $btnDisarm = New-Object System.Windows.Forms.Button
    $btnDisarm.Text = 'Disarm'
    $btnDisarm.Location = New-Object System.Drawing.Point(252, 134)
    $btnDisarm.Size = New-Object System.Drawing.Size(230, 36)
    $btnDisarm.BackColor = [System.Drawing.Color]::FromArgb(183, 28, 28)
    $btnDisarm.ForeColor = [System.Drawing.Color]::White
    $btnDisarm.FlatStyle = 'Flat'
    $form.Controls.Add($btnDisarm)
    $toolTip.SetToolTip($btnDisarm, 'End the session: best-effort disconnect the Armed profile, then restore Windows Firewall. OK if already disconnected.')

    $txtLog = New-Object System.Windows.Forms.TextBox
    $txtLog.Location = New-Object System.Drawing.Point(12, 180)
    $txtLog.Size = New-Object System.Drawing.Size(480, 158)
    $txtLog.Multiline = $true
    $txtLog.ScrollBars = 'Vertical'
    $txtLog.ReadOnly = $true
    $txtLog.Font = New-Object System.Drawing.Font('Consolas', 9)
    $txtLog.Anchor = 'Top,Bottom,Left,Right'
    $form.Controls.Add($txtLog)
    $toolTip.SetToolTip($txtLog, 'Live status: Armed/Disarmed, saved state, and kill-switch firewall rules.')

    # Advanced: occasional operational controls (must stay usable even if Testing is ignored)
    $grpAdvanced = New-Object System.Windows.Forms.GroupBox
    $grpAdvanced.Text = 'Advanced'
    $grpAdvanced.Location = New-Object System.Drawing.Point(12, 348)
    $grpAdvanced.Size = New-Object System.Drawing.Size(480, 72)
    $grpAdvanced.Anchor = 'Bottom,Left,Right'
    $form.Controls.Add($grpAdvanced)

    $btnArm = New-Object System.Windows.Forms.Button
    $btnArm.Text = 'Arm'
    $btnArm.Location = New-Object System.Drawing.Point(12, 28)
    $btnArm.Size = New-Object System.Drawing.Size(110, 28)
    $grpAdvanced.Controls.Add($btnArm)
    $toolTip.SetToolTip($btnArm, 'Kill switch only -- does not connect. Prefer Arm + Connect for a normal session. Useful after a captive portal or if you connect from the tray.')

    $btnRefresh = New-Object System.Windows.Forms.Button
    $btnRefresh.Text = 'Refresh status'
    $btnRefresh.Location = New-Object System.Drawing.Point(132, 28)
    $btnRefresh.Size = New-Object System.Drawing.Size(120, 28)
    $grpAdvanced.Controls.Add($btnRefresh)
    $toolTip.SetToolTip($btnRefresh, 'Reload the status text only. Does not Arm, Disarm, connect, or change firewall rules.')

    $btnRetryTunnels = New-Object System.Windows.Forms.Button
    $btnRetryTunnels.Text = 'Retry tunnel allows'
    $btnRetryTunnels.Location = New-Object System.Drawing.Point(262, 28)
    $btnRetryTunnels.Size = New-Object System.Drawing.Size(200, 28)
    $grpAdvanced.Controls.Add($btnRetryTunnels)
    $toolTip.SetToolTip($btnRetryTunnels, 'If Armed and connected but traffic fails, add missing firewall allows for OpenVPN tunnel adapters. Safe to click; skips adapters that already have a rule. Arm + Connect also auto-polls for this.')

    # Testing: diagnostics only
    $grpTest = New-Object System.Windows.Forms.GroupBox
    $grpTest.Text = 'Testing'
    $grpTest.Location = New-Object System.Drawing.Point(12, 428)
    $grpTest.Size = New-Object System.Drawing.Size(480, 150)
    $grpTest.Anchor = 'Bottom,Left,Right'
    $form.Controls.Add($grpTest)

    $btnDryRun = New-Object System.Windows.Forms.Button
    $btnDryRun.Text = 'Dry-run Arm'
    $btnDryRun.Location = New-Object System.Drawing.Point(12, 28)
    $btnDryRun.Size = New-Object System.Drawing.Size(100, 28)
    $grpTest.Controls.Add($btnDryRun)
    $toolTip.SetToolTip($btnDryRun, 'Show what Arm would change. Applies nothing.')

    $btnEndpoints = New-Object System.Windows.Forms.Button
    $btnEndpoints.Text = 'Show endpoints'
    $btnEndpoints.Location = New-Object System.Drawing.Point(120, 28)
    $btnEndpoints.Size = New-Object System.Drawing.Size(110, 28)
    $grpTest.Controls.Add($btnEndpoints)
    $toolTip.SetToolTip($btnEndpoints, 'Resolve remote lines from the selected .ovpn (VPN server IP/port).')

    $btnStateFolder = New-Object System.Windows.Forms.Button
    $btnStateFolder.Text = 'Open state folder'
    $btnStateFolder.Location = New-Object System.Drawing.Point(238, 28)
    $btnStateFolder.Size = New-Object System.Drawing.Size(118, 28)
    $grpTest.Controls.Add($btnStateFolder)
    $toolTip.SetToolTip($btnStateFolder, 'Open %LOCALAPPDATA%\HandyScripts\ (state JSON, firewall_restore.bak, dated snapshots).')

    $btnCopy = New-Object System.Windows.Forms.Button
    $btnCopy.Text = 'Copy status'
    $btnCopy.Location = New-Object System.Drawing.Point(364, 28)
    $btnCopy.Size = New-Object System.Drawing.Size(104, 28)
    $grpTest.Controls.Add($btnCopy)
    $toolTip.SetToolTip($btnCopy, 'Copy the status log text to the clipboard.')

    $verifierLaunch = Get-RoadWarriorVerifierLaunch
    $btnVerifier = New-Object System.Windows.Forms.Button
    $btnVerifier.Text = 'Open verifier'
    $btnVerifier.Location = New-Object System.Drawing.Point(12, 64)
    $btnVerifier.Size = New-Object System.Drawing.Size(140, 28)
    $grpTest.Controls.Add($btnVerifier)
    if ($verifierLaunch) {
        $btnVerifier.Enabled = $true
        $toolTip.SetToolTip($btnVerifier, ('Launch road-warrior verifier ({0}). Run Baseline while Disarmed on this network before Arm + Connect.' -f $verifierLaunch.ScriptPath))
    }
    else {
        $btnVerifier.Enabled = $false
        $toolTip.SetToolTip($btnVerifier, 'Verifier unavailable: need verify_roadwarrior.pyw next to this script and python/pythonw/py on PATH.')
    }

    $btnExportFw = New-Object System.Windows.Forms.Button
    $btnExportFw.Text = 'Export firewall snapshot'
    $btnExportFw.Location = New-Object System.Drawing.Point(160, 64)
    $btnExportFw.Size = New-Object System.Drawing.Size(160, 28)
    $grpTest.Controls.Add($btnExportFw)
    $toolTip.SetToolTip($btnExportFw, 'While Disarmed: save firewall_restore.bak + dated snapshot under %LOCALAPPDATA%\HandyScripts\ (Disarm-like quick restore). Do this before risky tests.')

    $btnRestoreBak = New-Object System.Windows.Forms.Button
    $btnRestoreBak.Text = 'Restore from .bak'
    $btnRestoreBak.Location = New-Object System.Drawing.Point(328, 64)
    $btnRestoreBak.Size = New-Object System.Drawing.Size(140, 28)
    $grpTest.Controls.Add($btnRestoreBak)
    $toolTip.SetToolTip($btnRestoreBak, 'Disarm-like recovery from firewall_restore.bak: re-enable snapshot Allows, restore defaults, remove kill-switch rules. Prefer Disarm when Armed with intact state.')

    $btnExit = New-Object System.Windows.Forms.Button
    $btnExit.Text = 'Exit'
    $btnExit.Location = New-Object System.Drawing.Point(382, 600)
    $btnExit.Size = New-Object System.Drawing.Size(110, 28)
    $btnExit.Anchor = 'Bottom,Right'
    $form.Controls.Add($btnExit)
    $form.CancelButton = $btnExit
    $toolTip.SetToolTip($btnExit, 'Close this window. Does not Disarm -- the kill switch stays Armed until you Disarm.')

    function Update-GuiFirewallBakButtons {
        $a = Get-FirewallBakAssessment
        $btnExportFw.Enabled = [bool]$a.CanExport
        $btnRestoreBak.Enabled = [bool]$a.CanRestore
        if ($a.CanExport) {
            $toolTip.SetToolTip($btnExportFw, 'While Disarmed: save firewall_restore.bak + dated snapshot. Do this before risky tests.')
        }
        else {
            $toolTip.SetToolTip($btnExportFw, [string]$a.ExportBlockedReason)
        }
        if ($a.CanRestore) {
            $tip = 'Opens an Are you sure? dialog first. Disarm-like recovery from firewall_restore.bak.'
            if ($a.NeedsExtraConfirm) {
                $tip = 'RISKY: will ask for an extra confirmation. Prefer Disarm when Armed with intact state. ' + $tip
            }
            $toolTip.SetToolTip($btnRestoreBak, $tip)
        }
        else {
            $toolTip.SetToolTip($btnRestoreBak, [string]$a.RestoreBlockedReason)
        }
    }

    function Set-GuiBusy([bool] $Busy, [string] $BusyText = '') {
        $form.UseWaitCursor = $Busy
        $cmbConfig.Enabled = -not $Busy
        $btnArmConnect.Enabled = -not $Busy
        $btnDisarm.Enabled = -not $Busy
        $btnArm.Enabled = -not $Busy
        $btnRefresh.Enabled = -not $Busy
        $btnRetryTunnels.Enabled = -not $Busy
        $btnDryRun.Enabled = -not $Busy
        $btnEndpoints.Enabled = -not $Busy
        $btnStateFolder.Enabled = -not $Busy
        $btnCopy.Enabled = -not $Busy
        # Export/Restore enablement depends on Armed + bak presence (refreshed in Set-GuiStatus).
        if (-not $Busy) {
            Update-GuiFirewallBakButtons
        }
        else {
            $btnExportFw.Enabled = $false
            $btnRestoreBak.Enabled = $false
        }
        # Verifier stays disabled if Python/script missing; otherwise follow busy state.
        $btnVerifier.Enabled = (-not $Busy) -and ($null -ne $verifierLaunch)
        # Keep Exit available so the window is never trapped if something goes wrong
        if ($Busy -and $BusyText) {
            $lblStatus.Text = $BusyText
            $lblStatus.ForeColor = [System.Drawing.Color]::DarkOrange
        }
        $form.Refresh()
        [System.Windows.Forms.Application]::DoEvents()
    }

    function Invoke-GuiFirewallAction {
        param(
            [string] $BusyText,
            [scriptblock] $Action,
            [string] $SuccessMessage
        )
        if ($script:GuiBusy) { return }
        $script:GuiBusy = $true
        Set-GuiBusy $true $BusyText
        try {
            & $Action
            Set-GuiStatus
            if ($SuccessMessage) {
                [System.Windows.Forms.MessageBox]::Show($SuccessMessage, 'Kill Switch') | Out-Null
            }
        }
        catch {
            [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Kill Switch') | Out-Null
            try { Set-GuiStatus } catch { }
        }
        finally {
            $script:GuiBusy = $false
            Set-GuiBusy $false
        }
    }

    function Set-GuiStatus {
        $armed = Test-IsKillSwitchArmed
        if ($armed) {
            $lblStatus.Text = 'ARMED'
            $lblStatus.ForeColor = [System.Drawing.Color]::DarkGreen
        }
        else {
            $lblStatus.Text = 'DISARMED'
            $lblStatus.ForeColor = [System.Drawing.Color]::DarkRed
        }
        $txtLog.Text = Get-StatusText
        if (-not $script:GuiBusy) {
            Update-GuiFirewallBakButtons
        }
    }

    function Get-SelectedConfigPath {
        if ($cmbConfig.SelectedItem) {
            $display = [string]$cmbConfig.SelectedItem
            if ($pathByDisplay.ContainsKey($display)) {
                return [string]$pathByDisplay[$display]
            }
            return (Resolve-OvpnConfigPath -ConfigArgument $display)
        }
        throw 'Select an OpenVPN profile.'
    }

    function Append-GuiLog([string] $msg) {
        $txtLog.AppendText($msg + [Environment]::NewLine)
    }

    $script:GuiBusy = $false

    $btnRefresh.Add_Click({
            if ($script:GuiBusy) { return }
            $script:GuiBusy = $true
            Set-GuiBusy $true 'Refreshing...'
            try { Set-GuiStatus }
            finally { $script:GuiBusy = $false; Set-GuiBusy $false }
        })
    $btnExit.Add_Click({ $form.Close() })
    $btnCopy.Add_Click({
            [System.Windows.Forms.Clipboard]::SetText($txtLog.Text)
            [System.Windows.Forms.MessageBox]::Show('Status copied to clipboard.', 'Kill Switch') | Out-Null
        })
    $btnVerifier.Add_Click({
            if ($script:GuiBusy) { return }
            $launch = Get-RoadWarriorVerifierLaunch
            if (-not $launch) {
                [System.Windows.Forms.MessageBox]::Show(
                    'Verifier unavailable. Need verify_roadwarrior.pyw beside this script and python, pythonw, or py on PATH.',
                    'Open verifier') | Out-Null
                return
            }
            try {
                Start-Process -FilePath $launch.Exe -ArgumentList $launch.ArgumentList -ErrorAction Stop | Out-Null
            }
            catch {
                [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Open verifier failed') | Out-Null
            }
        })
    $btnStateFolder.Add_Click({
            if (-not (Test-Path $script:StateDir)) {
                New-Item -ItemType Directory -Path $script:StateDir -Force | Out-Null
            }
            Start-Process explorer.exe -ArgumentList $script:StateDir
        })
    $btnEndpoints.Add_Click({
            try {
                $path = Get-SelectedConfigPath
                $remotes = @(Get-OvpnRemotes -ConfigPath $path)
                $eps = @(Resolve-RemoteEndpoints -Remotes $remotes)
                $txtLog.Clear()
                Append-GuiLog ("Profile: {0}" -f $path)
                foreach ($ep in $eps) {
                    Append-GuiLog ("  {0} -> {1}:{2}/{3}" -f $ep.Host, $ep.IP, $ep.Port, $ep.Proto)
                }
            }
            catch {
                [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Error') | Out-Null
            }
        })
    $btnDryRun.Add_Click({
            try {
                $path = Get-SelectedConfigPath
                $remotes = @(Get-OvpnRemotes -ConfigPath $path)
                $eps = @(Resolve-RemoteEndpoints -Remotes $remotes)
                $tuns = @(Get-OpenVpnTunnelAdapters)
                $warns = @(Get-OvpnConfigAuditWarnings -ConfigPath $path)
                $hostPlan = @()
                foreach ($r in $remotes) {
                    if (Test-IsIpAddress $r.Host) { continue }
                    $hostPlan += [pscustomobject]@{ Hostname = $r.Host; Ip = '(resolve at Arm)'; Action = 'pin'; Owned = $false }
                }
                $plan = Get-ArmPlanSummary -Endpoints $eps -TunnelAdapters $tuns -HostsActions $hostPlan -ConfigWarnings $warns
                $txtLog.Clear()
                Append-GuiLog '=== Dry-run Arm (no changes) ==='
                Append-GuiLog ("Config: {0}" -f $path)
                foreach ($p in $plan) { Append-GuiLog ("  - {0}" -f $p) }
            }
            catch {
                [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Error') | Out-Null
            }
        })
    $btnRetryTunnels.Add_Click({
            try {
                if (-not (Test-IsAdministrator)) {
                    Request-AdministratorElevation -ExtraArgs @('-RetryTunnels', '-Gui')
                    return
                }
                Invoke-GuiFirewallAction -BusyText 'Retrying tunnel allows...' -Action {
                    $n = Update-TunnelAllowRules
                    Write-Info ("Retry tunnel allows finished (added {0})." -f $n)
                } -SuccessMessage $null
            }
            catch {
                [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Error') | Out-Null
            }
        })

    $btnArm.Add_Click({
            try {
                $path = Get-SelectedConfigPath
                if (-not (Test-IsAdministrator)) {
                    Request-AdministratorElevation -ExtraArgs @('-Arm', '-Config', $path, '-Gui')
                    return
                }
                Invoke-GuiFirewallAction -BusyText 'Arming -- please wait...' -Action {
                    Invoke-ArmKillSwitch -ConfigPath $path
                } -SuccessMessage 'Armed. Rules persist until you Disarm.'
            }
            catch {
                [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Arm failed') | Out-Null
                Set-GuiStatus
            }
        })

    $btnArmConnect.Add_Click({
            try {
                $path = Get-SelectedConfigPath
                if (-not (Test-IsAdministrator)) {
                    Request-AdministratorElevation -ExtraArgs @('-Arm', '-Config', $path, '-Connect', '-Gui')
                    return
                }
                Invoke-GuiFirewallAction -BusyText 'Arming + connecting -- please wait...' -Action {
                    Invoke-ArmKillSwitch -ConfigPath $path -ConnectAfter
                } -SuccessMessage 'Armed and connect requested via OpenVPN GUI.'
            }
            catch {
                [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Arm + Connect failed') | Out-Null
                Set-GuiStatus
            }
        })

    $btnDisarm.Add_Click({
            if ($script:GuiBusy) { return }
            $confirm = [System.Windows.Forms.MessageBox]::Show(
                'End the protected session? This disconnects the Armed OpenVPN profile (if connected) and restores Windows Firewall.',
                'Confirm Disarm',
                [System.Windows.Forms.MessageBoxButtons]::YesNo,
                [System.Windows.Forms.MessageBoxIcon]::Warning)
            if ($confirm -ne [System.Windows.Forms.DialogResult]::Yes) { return }
            try {
                if (-not (Test-IsAdministrator)) {
                    Request-AdministratorElevation -ExtraArgs @('-Disarm', '-Gui')
                    return
                }
                Invoke-GuiFirewallAction -BusyText 'Disarming -- please wait...' -Action {
                    Invoke-DisarmKillSwitch
                } -SuccessMessage 'Disarmed.'
            }
            catch {
                [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Disarm failed') | Out-Null
                Set-GuiStatus
            }
        })

    $btnExportFw.Add_Click({
            try {
                $a = Get-FirewallBakAssessment
                if (-not $a.CanExport) {
                    [System.Windows.Forms.MessageBox]::Show(
                        [string]$a.ExportBlockedReason,
                        'Export firewall snapshot',
                        [System.Windows.Forms.MessageBoxButtons]::OK,
                        [System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null
                    return
                }
                if (-not (Test-IsAdministrator)) {
                    Request-AdministratorElevation -ExtraArgs @('-ExportFirewallSnapshot', '-Gui')
                    return
                }
                Invoke-GuiFirewallAction -BusyText 'Exporting firewall snapshot...' -Action {
                    $r = Export-FirewallSnapshot
                    Write-Info ("Exported {0} enabled outbound Allow rule name(s)." -f $r.RuleCount)
                } -SuccessMessage ("Saved firewall_restore.bak + dated snapshot under:`n{0}`n`nUse Restore from .bak for Disarm-like recovery if state is lost." -f $script:StateDir)
            }
            catch {
                [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Export failed') | Out-Null
                Set-GuiStatus
            }
        })

    $btnRestoreBak.Add_Click({
            if ($script:GuiBusy) { return }
            $a = Get-FirewallBakAssessment
            if (-not $a.CanRestore) {
                [System.Windows.Forms.MessageBox]::Show(
                    [string]$a.RestoreBlockedReason,
                    'Restore from .bak',
                    [System.Windows.Forms.MessageBoxButtons]::OK,
                    [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
                return
            }

            $body = Get-RestoreFirewallBakConfirmText -Assessment $a
            $confirm = [System.Windows.Forms.MessageBox]::Show(
                $body,
                'Are you sure? Restore from .bak',
                [System.Windows.Forms.MessageBoxButtons]::YesNo,
                [System.Windows.Forms.MessageBoxIcon]::Warning)
            if ($confirm -ne [System.Windows.Forms.DialogResult]::Yes) { return }

            if ($a.NeedsExtraConfirm) {
                $extra = [System.Windows.Forms.MessageBox]::Show(
                    "EXTRA CONFIRMATION`n`nThis situation is risky (Armed with state, or bak looks unusually small).`n`nPrefer DISARM if the kill switch is Armed and Disarm still works.`n`nYes = I understand, restore anyway.`nNo = do nothing.",
                    'Extra confirmation required',
                    [System.Windows.Forms.MessageBoxButtons]::YesNo,
                    [System.Windows.Forms.MessageBoxIcon]::Error)
                if ($extra -ne [System.Windows.Forms.DialogResult]::Yes) { return }
            }

            try {
                if (-not (Test-IsAdministrator)) {
                    # -Force: user already confirmed in this GUI; elevated child must not hang on Read-Host.
                    Request-AdministratorElevation -ExtraArgs @('-RestoreFirewallBak', '-Force', '-Gui')
                    return
                }
                Invoke-GuiFirewallAction -BusyText 'Restoring from firewall_restore.bak...' -Action {
                    Invoke-RestoreFirewallFromBak -Force
                } -SuccessMessage 'Restored from firewall_restore.bak.'
            }
            catch {
                [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Restore failed') | Out-Null
                Set-GuiStatus
            }
        })

    # Closing the form does NOT Disarm.
    $form.Add_Shown({ Set-GuiStatus })
    [void]$form.ShowDialog()
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

# Default to GUI when no action switches were bound (DefaultParameterSetName = Gui)
if (-not $Arm -and -not $Disarm -and -not $Status -and -not $ListConfigs -and -not $RetryTunnels -and -not $ExportFirewallSnapshot -and -not $RestoreFirewallBak -and -not $Gui) {
    $Gui = $true
}

try {
    if ($ListConfigs) {
        foreach ($p in @(Get-OvpnConfigProfiles)) {
            Write-Output ("{0}`t{1}`t{2}" -f $p.Source, $p.DisplayName, $p.FullPath)
        }
        exit 0
    }

    if ($Status) {
        Write-Host (Get-StatusText)
        exit 0
    }

    if ($ExportFirewallSnapshot) {
        $r = Export-FirewallSnapshot
        Write-Info ("Done. Rules captured: {0}" -f $r.RuleCount)
        if ($Gui -and -not $WhatIfPreference) { Show-KillSwitchGui }
        exit 0
    }

    if ($RestoreFirewallBak) {
        Invoke-RestoreFirewallFromBak -Force:$Force
        if ($Gui -and -not $WhatIfPreference) { Show-KillSwitchGui }
        exit 0
    }

    if ($RetryTunnels) {
        Ensure-AdministratorForMutation
        if (-not (Test-IsKillSwitchArmed)) {
            throw 'Retry tunnel allows requires the kill switch to be Armed.'
        }
        $n = Update-TunnelAllowRules
        Write-Info ("Retry tunnel allows finished (added {0})." -f $n)
        if ($Gui -and -not $WhatIfPreference) { Show-KillSwitchGui }
        exit 0
    }

    if ($Disarm) {
        Invoke-DisarmKillSwitch
        if ($Gui -and -not $WhatIfPreference) { Show-KillSwitchGui }
        exit 0
    }

    if ($Arm) {
        if (-not $Config) { throw '-Arm requires -Config <profile.ovpn>' }
        Invoke-ArmKillSwitch -ConfigPath $Config -ConnectAfter:$Connect
        if ($Gui -and -not $WhatIfPreference) { Show-KillSwitchGui }
        exit 0
    }

    if ($Gui) {
        # GUI self-elevates per action; Status display works without admin.
        Show-KillSwitchGui
        exit 0
    }
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
