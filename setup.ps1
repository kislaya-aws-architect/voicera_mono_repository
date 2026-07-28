# =============================================================================
# VoicEra — Windows Full Setup & Go Live
# One command to go from fresh Windows EC2 to running application.
#
# Usage:
#   1. Open PowerShell as ADMINISTRATOR (right-click PowerShell → "Run as
#      Administrator"). The script will refuse to run otherwise — this is
#      not optional on AWS Windows Server AMIs.
#   2. Run:
#   Set-ExecutionPolicy Bypass -Scope Process -Force; $s="$env:TEMP\voicera_setup.ps1"; Invoke-RestMethod https://raw.githubusercontent.com/kislaya-aws-architect/voicera_mono_repository/feature/windows-setup-script/setup.ps1 -OutFile $s; &$s
#
# Optional env vars (set before running):
#   $env:NGROK_TOKEN          get from https://dashboard.ngrok.com/get-started/your-authtoken
#   $env:HF_TOKEN              (required only if ENABLE_TTS=local — gated model) get from https://huggingface.co/settings/tokens
#   $env:VOBIZ_AUTH_ID         (telephony, legacy — see Section "Telephony" below) get from https://www.vobiz.in dashboard
#   $env:VOBIZ_AUTH_TOKEN      (telephony, legacy) get from https://www.vobiz.in dashboard
#   $env:ENABLE_STT            yes|no  (default: yes)
#   $env:ENABLE_TTS            local|remote|no  (default: remote — see change #7 below)
#   $env:TTS_REMOTE_URL        ws://<host>:8002 — required if ENABLE_TTS=remote
#   $env:ENABLE_LLM            none|openai|grok  (default: openai)
#   $env:OPENAI_API_KEY
#   $env:XAI_API_KEY
#   $env:VOICERA_REPO_URL      which app repo to clone (default: COSS-India org repo)
#   $env:VOICERA_REPO_BRANCH   which branch to clone (default: dev)
#
# -----------------------------------------------------------------------------
# CHANGES FROM UPSTREAM (kislaya-aws-architect fork, forked from
# PRANABraight/voicera_mono_repository dev branch, 2026-07-28):
#   1. ENABLE_LLM default changed none -> openai.
#   2. "vllm" removed as a selectable LLM option. On AWS, vLLM-via-WSL2
#      requires nested virtualization, which AWS only exposes on bare-metal
#      or the C8i/M8i/R8i family — none of which have a GPU. There is no
#      right-sized AWS instance that satisfies both "has a GPU" and
#      "supports nested virtualization" other than bare-metal SKUs like
#      g4dn.metal (8x T4, ~$5.7k/mo on-demand — oversized for this use).
#      vLLM also has no official native-Windows support (see
#      https://docs.vllm.ai/en/stable/getting_started/installation/gpu/).
#      Upstream's vllm branch was also never fully implemented — it starts
#      a WSL2 process but never provisions WSL2, creates the venv, or
#      installs vLLM into it. If you need vLLM, run it on a separate native
#      Linux GPU box (no WSL2/nested-virt constraint at all — this is the
#      same pattern the Sajag GPU box already uses for STT/translation) and
#      point ENABLE_LLM=openai at that box's OpenAI-compatible endpoint via
#      $env:OPENAI_API_KEY / a custom base URL in llm_server's config.
#   3. V2V .env now writes INDIC_STT_SERVER_URL / INDIC_TTS_SERVER_URL —
#      confirmed via the actual Python service code that these are the
#      variable names read at runtime. AI4BHARAT_STT_URL/AI4BHARAT_TTS_URL
#      (upstream's names) are kept alongside as a harmless fallback in case
#      other code paths still reference them, but they are not the live
#      lookup.
#   4. Added an explicit winget presence check before Phase 1. AWS Windows
#      Server AMIs (2019/2022/2025) do not reliably ship winget even where
#      documented as pre-installed — see
#      https://github.com/microsoft/winget-cli/issues/5207. Failing loudly
#      here avoids a confusing cascade of package-install failures.
#   5. Repo clone target is now configurable via $env:VOICERA_REPO_URL /
#      $env:VOICERA_REPO_BRANCH. Defaults preserve upstream behavior
#      (COSS-India org repo, dev branch) — override these if you want this
#      box deploying your own fork/branch instead.
#   6. Vobiz block left functionally unchanged but flagged: Vobiz is being
#      decommissioned org-wide in favor of Vodafone Vi Business Managed SIP.
#      Treat these fields as a placeholder pending that cutover.
#   7. ENABLE_TTS now takes local|remote|no (was yes|no), default changed to
#      remote. Root cause: the TTS server's inference/paging.py imports
#      flashinfer, which has no official Windows distribution — see
#      https://docs.flashinfer.ai/installation.html (Linux-only PyPI wheels)
#      and https://github.com/SystemPanic/flashinfer-windows (unofficial
#      Windows builds, only for one exact Python/CUDA/torch combination, or
#      a from-source build requiring Visual Studio). Confirmed live via
#      ModuleNotFoundError on a Windows Server 2025 AMI, 2026-07-28. TTS is
#      already designed to be called remotely (V2V talks to it over
#      INDIC_TTS_SERVER_URL/ws://), so ENABLE_TTS=remote points this box at
#      a TTS instance running on a native Linux GPU box instead of fighting
#      the Windows build — the same pattern already proven for STT/
#      translation on the Sajag GPU box. ENABLE_TTS=local is left available
#      for anyone willing to chase the community Windows build, with a
#      warning printed up front rather than a silent doomed pip install.
#      Separately, added pytorch-lightning to the STT venv's explicit
#      install list — its absence caused a live ModuleNotFoundError on the
#      same run; unlike flashinfer this is an ordinary cross-platform
#      package, so this actually resolves it rather than working around it.
#   8. GPU check now attempts an automated driver install (from AWS's own
#      public ec2-windows-nvidia-drivers S3 bucket, no credentials needed)
#      instead of just warning. Confirmed live: g4dn.xlarge Windows
#      instances do not show the T4 in Win32_VideoController pre-driver —
#      it's a 3D-controller-class PCI device (0x0302), not VGA-class
#      (0x0300), so it won't appear there even with zero driver installed,
#      unlike the base Amazon console adapter. Falls back to manual
#      nvidia.com instructions if the automated path fails for any reason
#      (there are reports of intermittent 404s against this bucket). Either
#      way, a reboot is required before nvidia-smi will work — this script
#      cannot reboot itself mid-run, so re-run it after rebooting.
# =============================================================================
#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"
# Best-effort only: on AWS Windows Server AMIs this is frequently overridden by a
# machine/GPO-level execution policy, which makes this call raise a non-terminating
# error. Under $ErrorActionPreference = "Stop" that becomes fatal and kills the whole
# script before Phase 1 even starts — confirmed live on a Windows Server 2025 AMI,
# 2026-07-28. The process-scope Bypass set by the invoking one-liner is what this
# script actually depends on to run, so a failure here should never be fatal.
try {
    Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force -ErrorAction Stop
} catch {
    Write-Host "  WARN Could not set CurrentUser execution policy (likely blocked by a machine/GPO policy) — continuing with process-level Bypass." -ForegroundColor Yellow
}

# ── Defaults ──────────────────────────────────────────────────────────────────
$NGROK_TOKEN    = if ($env:NGROK_TOKEN)     { $env:NGROK_TOKEN }     else { "" } # get from https://dashboard.ngrok.com/get-started/your-authtoken
$HF_TOKEN       = if ($env:HF_TOKEN)        { $env:HF_TOKEN }        else { "" } # get from https://huggingface.co/settings/tokens
$ENABLE_STT     = if ($env:ENABLE_STT)      { $env:ENABLE_STT }      else { "yes" }
$ENABLE_TTS     = if ($env:ENABLE_TTS)      { $env:ENABLE_TTS }      else { "remote" }  # CHANGED: was "yes" (=local); see change #7
$TTS_REMOTE_URL = if ($env:TTS_REMOTE_URL)  { $env:TTS_REMOTE_URL }  else { "" }
$ENABLE_LLM     = if ($env:ENABLE_LLM)      { $env:ENABLE_LLM }      else { "openai" }   # CHANGED: was "none"
$VOBIZ_AUTH_ID  = if ($env:VOBIZ_AUTH_ID)   { $env:VOBIZ_AUTH_ID }   else { "PLACEHOLDER" } # legacy telephony, pending Vodafone Vi cutover
$VOBIZ_AUTH_TOKEN = if ($env:VOBIZ_AUTH_TOKEN) { $env:VOBIZ_AUTH_TOKEN } else { "PLACEHOLDER" } # legacy telephony, pending Vodafone Vi cutover
$OPENAI_API_KEY = if ($env:OPENAI_API_KEY)  { $env:OPENAI_API_KEY }  else { "" }
$XAI_API_KEY    = if ($env:XAI_API_KEY)     { $env:XAI_API_KEY }     else { "" }
$REPO_DIR       = "C:\VoicEra"
$VOICERA_REPO_URL    = if ($env:VOICERA_REPO_URL)    { $env:VOICERA_REPO_URL }    else { "https://github.com/COSS-India/voicera_mono_repository.git" }
$VOICERA_REPO_BRANCH = if ($env:VOICERA_REPO_BRANCH) { $env:VOICERA_REPO_BRANCH } else { "dev" }

# ── Helpers ───────────────────────────────────────────────────────────────────
function log  { param($m) Write-Host "`n[VoicEra] $m" -ForegroundColor Green }
function ok   { param($m) Write-Host "  OK  $m" -ForegroundColor Cyan }
function warn { param($m) Write-Host "  WARN $m" -ForegroundColor Yellow }
function err  { param($m) Write-Host "[ERROR] $m" -ForegroundColor Red; exit 1 }

function Refresh-Path {
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("PATH","User")
}

function Test-Port {
    param([int]$Port, [int]$Timeout = 3000)
    $tcp = New-Object System.Net.Sockets.TcpClient
    try {
        $r = $tcp.BeginConnect("localhost", $Port, $null, $null)
        $r.AsyncWaitHandle.WaitOne($Timeout) | Out-Null
        return $tcp.Connected
    } catch { return $false }
    finally { $tcp.Close() }
}

function Test-WingetAvailable {
    try {
        winget --version 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

# ── Banner ────────────────────────────────────────────────────────────────────
function Show-Banner {
    Clear-Host
    Write-Host ""
    Write-Host "    ██╗   ██╗ ██████╗ ██╗ ██████╗███████╗██████╗  █████╗ " -ForegroundColor Cyan
    Write-Host "    ██║   ██║██╔═══██╗██║██╔════╝██╔════╝██╔══██╗██╔══██╗" -ForegroundColor Cyan
    Write-Host "    ██║   ██║██║   ██║██║██║     █████╗  ██████╔╝███████║" -ForegroundColor Blue
    Write-Host "    ╚██╗ ██╔╝██║   ██║██║██║     ██╔══╝  ██╔══██╗██╔══██║" -ForegroundColor DarkBlue
    Write-Host "     ╚████╔╝ ╚██████╔╝██║╚██████╗███████╗██║  ██║██║  ██║" -ForegroundColor Blue
    Write-Host "      ╚═══╝   ╚═════╝ ╚═╝ ╚═════╝╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝" -ForegroundColor Blue
    Write-Host ""
    Write-Host "  ────────────────────────────────────────────────────────" -ForegroundColor DarkGray
    Write-Host "   Voice AI for Every Language  │  Built by COSS India" -ForegroundColor White
    Write-Host "  ────────────────────────────────────────────────────────" -ForegroundColor DarkGray
    Write-Host ""
}

Show-Banner

# ── Pre-flight: winget must actually work before Phase 1 needs it ────────────
if (-not (Test-WingetAvailable)) {
    err @"
winget was not found (or not functional) on this machine.

AWS Windows Server AMIs (2019/2022/2025) do not reliably ship a working
winget, even on images documented as having it pre-installed — see
https://github.com/microsoft/winget-cli/issues/5207 for the same report
against Windows Server 2025.

Fix, then re-run this script:
  1. Download the latest App Installer .msixbundle from:
     https://github.com/microsoft/winget-cli/releases/latest
  2. Install it:  Add-AppxPackage -Path <path-to-msixbundle>
  3. Close and reopen PowerShell (as Administrator), then re-run this script.
"@
}
ok "winget available"

# ── Interactive Config ────────────────────────────────────────────────────────
Write-Host "  Configure Services" -ForegroundColor White
Write-Host "  ─────────────────────────────────────────────────────" -ForegroundColor DarkGray

if ($NGROK_TOKEN -eq "") {
    warn "No ngrok token found. Get one at: https://dashboard.ngrok.com/get-started/your-authtoken"
    $NGROK_TOKEN = Read-Host "  ngrok authtoken"
}

$_stt = Read-Host "  Enable STT? [yes/no, default: $ENABLE_STT]"
if ($_stt -eq "y") { $_stt = "yes" } elseif ($_stt -eq "n") { $_stt = "no" }
if ($_stt -ne "") { $ENABLE_STT = $_stt }

Write-Host "  TTS: local | remote | no" -ForegroundColor White
Write-Host "    local  = run TTS on this box. NOT RECOMMENDED on Windows: TTS depends on" -ForegroundColor DarkGray
Write-Host "             flashinfer, which has no official Windows build (Linux-only PyPI" -ForegroundColor DarkGray
Write-Host "             wheels). See the CHANGES FROM UPSTREAM note at the top of this file." -ForegroundColor DarkGray
Write-Host "    remote = point at a TTS instance already running on a Linux GPU box" -ForegroundColor DarkGray
$_tts = Read-Host "  TTS mode [default: $ENABLE_TTS]"
if ($_tts -ne "") { $ENABLE_TTS = $_tts }

if ($ENABLE_TTS -eq "remote" -and $TTS_REMOTE_URL -eq "") {
    warn "ENABLE_TTS=remote needs the address of a TTS instance already running elsewhere (e.g. the Sajag Linux GPU box)."
    $TTS_REMOTE_URL = Read-Host "  Remote TTS URL (e.g. ws://100.60.89.2:8002)"
}
if ($ENABLE_TTS -eq "local" -and $HF_TOKEN -eq "") {
    warn "No Hugging Face token found. Get one at: https://huggingface.co/settings/tokens"
    $HF_TOKEN = Read-Host "  Hugging Face token (required for TTS gated model)"
}

Write-Host "  LLM: none | openai | grok" -ForegroundColor White
$_llm = Read-Host "  LLM provider [default: $ENABLE_LLM]"
if ($_llm -ne "") { $ENABLE_LLM = $_llm }

if ($ENABLE_LLM -eq "vllm") {
    err @"
ENABLE_LLM=vllm is not supported by this fork.

On AWS, vLLM-via-WSL2 needs nested virtualization, which AWS only exposes
on bare-metal or the C8i/M8i/R8i family — none of which have a GPU. vLLM
also has no official native-Windows support.

Instead: run vLLM on a separate native Linux GPU box (no WSL2/nested-virt
needed there at all — this is the same pattern the Sajag GPU box already
uses for STT/translation), then set ENABLE_LLM=openai here and point it at
that box's OpenAI-compatible endpoint.
"@
}

if ($ENABLE_LLM -eq "openai" -and $OPENAI_API_KEY -eq "") {
    warn "No OpenAI API key found. Get one at: https://platform.openai.com/api-keys"
    warn "(If you're pointing at a self-hosted OpenAI-compatible endpoint — e.g. a vLLM box — any non-empty value works here; set the base URL in llm_server's config.)"
    $OPENAI_API_KEY = Read-Host "  OpenAI API key"
}
if ($ENABLE_LLM -eq "grok" -and $XAI_API_KEY -eq "") {
    warn "No xAI API key found. Get one at: https://console.x.ai"
    $XAI_API_KEY = Read-Host "  xAI API key"
}

if ($VOBIZ_AUTH_ID -eq "PLACEHOLDER") {
    warn "No Vobiz credentials found. Vobiz is being phased out org-wide in favor of Vodafone Vi Business Managed SIP — treat this as a placeholder."
    warn "Get Vobiz credentials (if still needed) at: https://www.vobiz.in dashboard"
    $v = Read-Host "  Vobiz Auth ID [Enter to skip]"
    if ($v -ne "") { $VOBIZ_AUTH_ID = $v }
}
if ($VOBIZ_AUTH_TOKEN -eq "PLACEHOLDER") {
    $v = Read-Host "  Vobiz Auth Token [Enter to skip]"
    if ($v -ne "") { $VOBIZ_AUTH_TOKEN = $v }
}

Write-Host ""
Write-Host "  STT: $ENABLE_STT  |  TTS: $ENABLE_TTS  |  LLM: $ENABLE_LLM" -ForegroundColor White
$proceed = Read-Host "  Proceed? [Y/n]"
if ($proceed -eq "n") { exit 0 }

$PRIVATE_IP = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -like '172.31.*' -or $_.IPAddress -like '10.*' } |
    Select-Object -First 1).IPAddress
if (-not $PRIVATE_IP) {
    $PRIVATE_IP = (Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -ne '127.0.0.1' } |
        Select-Object -First 1).IPAddress
}
$INTERNAL_KEY = python -c "import secrets; print(secrets.token_urlsafe(32))" 2>$null
if (-not $INTERNAL_KEY) {
    $INTERNAL_KEY = [System.Convert]::ToBase64String([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
}

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 1 — Instance Setup
# ═════════════════════════════════════════════════════════════════════════════
log "Phase 1/3: Instance Setup"

# ── winget packages ──
$pkgs = @(
    @{id="Git.Git";         check={ git --version 2>$null } },
    @{id="Python.Python.3.10"; check={ py -3.10 --version 2>$null } },
    @{id="OpenJS.NodeJS.LTS"; check={ node --version 2>$null } },
    @{id="Gyan.FFmpeg";     check={ ffmpeg -version 2>$null } },
    @{id="aria2.aria2";     check={ aria2c --version 2>$null } }
)
foreach ($pkg in $pkgs) {
    try { & $pkg.check | Out-Null; ok "$($pkg.id) already installed" }
    catch {
        Write-Host "  Installing $($pkg.id)..." -ForegroundColor DarkGray
        winget install --id $pkg.id -e --source winget --accept-package-agreements --accept-source-agreements --silent 2>&1 | Select-Object -Last 2
    }
}
Refresh-Path

# ── PowerShell 7 (install if running on PS5) ──
if ($PSVersionTable.PSVersion.Major -lt 7) {
    warn "PowerShell 5 detected — installing PowerShell 7"
    winget install --id Microsoft.PowerShell --source winget --silent
    warn "Relaunch this script in PowerShell 7 (black window) after install completes."
}

# ── GPU check + driver install ──
# AWS hosts current GPU drivers in a public, unauthenticated S3 bucket for exactly this
# purpose — no AWS CLI or credentials needed. Documented at
# https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/install-nvidia-driver.html
# Silent-install flags confirmed against AWS's own EKS docs for this same driver family:
# https://docs.aws.amazon.com/eks/latest/userguide/ml-eks-windows-optimized-ami.html
try {
    $gpuInfo = nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>$null
    if ($LASTEXITCODE -ne 0) { throw "nvidia-smi not functional" }
    ok "GPU: $gpuInfo"
} catch {
    Write-Host "  NVIDIA driver not found — attempting automated install from AWS's driver bucket..." -ForegroundColor DarkGray
    $driverInstalled = $false
    try {
        $listXml = Invoke-RestMethod "https://ec2-windows-nvidia-drivers.s3.us-east-1.amazonaws.com/?list-type=2&prefix=latest/" -ErrorAction Stop
        $exeKey = ($listXml.ListBucketResult.Contents | Where-Object { $_.Key -like "*.exe" } | Select-Object -First 1).Key
        if (-not $exeKey) { throw "No .exe found in bucket listing" }
        $driverUrl = "https://ec2-windows-nvidia-drivers.s3.us-east-1.amazonaws.com/$exeKey"
        $driverPath = "$env:TEMP\nvidia_driver.exe"
        Write-Host "  Downloading $exeKey (large file, can take a few minutes)..." -ForegroundColor DarkGray
        Invoke-WebRequest $driverUrl -OutFile $driverPath
        Write-Host "  Installing silently..." -ForegroundColor DarkGray
        Start-Process -FilePath $driverPath -ArgumentList "-s -clean -noreboot -noeula" -Wait -NoNewWindow
        $driverInstalled = $true
    } catch {
        Write-Host "  WARN Automated driver download/install failed ($($_.Exception.Message))." -ForegroundColor Yellow
    }

    if ($driverInstalled) {
        Write-Host "  WARN NVIDIA driver installed but needs a REBOOT to load. Reboot this instance, then re-run this script — nvidia-smi should work afterward. STT/TTS will not use the GPU until you do." -ForegroundColor Yellow
    } else {
        Write-Host "  WARN Install manually instead: https://www.nvidia.com/Download/Find.aspx -> Data Center/Tesla, T-Series, Tesla T4 (or your instance's actual GPU), your Windows Server version, Any CUDA Toolkit Version. Reboot after installing, then re-run this script." -ForegroundColor Yellow
    }
}

# ── ngrok ──
# NOTE: ngrok.exe is a very commonly false-positived binary on Windows Defender —
# confirmed live on a Windows Server 2025 AMI, 2026-07-28 ("the file contains a virus
# or potentially unwanted software"). Add an exclusion BEFORE downloading/running it;
# adding one after the fact is less reliable since Defender may have already quarantined
# the specific file. If Defender itself is policy-managed (same as the execution-policy
# GPO override seen elsewhere on managed AMIs), Add-MpPreference may fail too — that's
# handled below as a non-fatal warning with manual remediation steps, not a script-killer.
New-Item -ItemType Directory -Force -Path "C:\ngrok" | Out-Null
try {
    Add-MpPreference -ExclusionPath "C:\ngrok" -ErrorAction Stop
} catch {
    Write-Host "  WARN Could not add a Windows Defender exclusion for C:\ngrok (possibly policy-locked on this AMI). If ngrok gets flagged below, fix manually via Windows Security > Virus & threat protection > Protection history (Allow/Restore the ngrok block), or add the exclusion there under Manage settings > Exclusions, then re-run this script." -ForegroundColor Yellow
}
if (-not (Get-Command ngrok -ErrorAction SilentlyContinue)) {
    Write-Host "  Installing ngrok..." -ForegroundColor DarkGray
    Invoke-WebRequest "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip" -OutFile "$env:TEMP\ngrok.zip"
    Expand-Archive -Path "$env:TEMP\ngrok.zip" -DestinationPath "C:\ngrok\" -Force
    [System.Environment]::SetEnvironmentVariable('PATH', $env:PATH + ";C:\ngrok", [System.EnvironmentVariableTarget]::Machine)
    Refresh-Path
}
try {
    ngrok config add-authtoken $NGROK_TOKEN 2>$null | Out-Null
    ok "ngrok ready"
} catch {
    Write-Host "  WARN ngrok.exe failed to run — almost certainly Windows Defender blocking it as a false positive, not an actual problem with ngrok. This is non-fatal to the rest of setup, but ngrok tunneling won't work until fixed: go to Windows Security > Virus & threat protection > Protection history, find the ngrok block, choose Actions > Allow/Restore (or add C:\ngrok under Exclusions), then re-run this script." -ForegroundColor Yellow
}

# ── cloudflared ──
New-Item -ItemType Directory -Force -Path "C:\cloudflared" | Out-Null
try {
    Add-MpPreference -ExclusionPath "C:\cloudflared" -ErrorAction Stop
} catch {
    Write-Host "  WARN Could not add a Windows Defender exclusion for C:\cloudflared (possibly policy-locked on this AMI). Same remediation as ngrok above if it gets flagged." -ForegroundColor Yellow
}
if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
    Write-Host "  Installing cloudflared..." -ForegroundColor DarkGray
    Invoke-WebRequest "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" `
        -OutFile "C:\cloudflared\cloudflared.exe"
    [System.Environment]::SetEnvironmentVariable('PATH', $env:PATH + ";C:\cloudflared", [System.EnvironmentVariableTarget]::Machine)
    Refresh-Path
}
ok "cloudflared ready"

# ── Clone repo ──
New-Item -ItemType Directory -Force -Path $REPO_DIR | Out-Null
if (-not (Test-Path "$REPO_DIR\.git")) {
    Write-Host "  Cloning VoicEra repository ($VOICERA_REPO_URL, branch $VOICERA_REPO_BRANCH)..." -ForegroundColor DarkGray
    git clone -b $VOICERA_REPO_BRANCH $VOICERA_REPO_URL $REPO_DIR
}
ok "Repository at $REPO_DIR"

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 2 — Application Deploy
# ═════════════════════════════════════════════════════════════════════════════
log "Phase 2/3: Application Deploy"

# ── MongoDB 9.0 nightly ──
$mongoPath = (Get-ChildItem "C:\mongodb9" -Directory -ErrorAction SilentlyContinue | Select-Object -First 1)
if (-not $mongoPath -or -not (Get-Command mongod -ErrorAction SilentlyContinue)) {
    Write-Host "  Downloading MongoDB 9.0 nightly..." -ForegroundColor DarkGray
    $mongoZip = "$env:TEMP\mongodb.zip"
    Invoke-WebRequest "https://downloads.mongodb.org/windows/mongodb-windows-x86_64-latest.zip" -OutFile $mongoZip
    Expand-Archive -Path $mongoZip -DestinationPath "C:\mongodb9\" -Force
    $mongoBin = (Get-ChildItem "C:\mongodb9" -Directory | Select-Object -First 1).FullName + "\bin"
    [System.Environment]::SetEnvironmentVariable('PATH', $env:PATH + ";$mongoBin", [System.EnvironmentVariableTarget]::Machine)
    $env:PATH += ";$mongoBin"
    Refresh-Path
}

# Install mongosh
if (-not (Get-Command mongosh -ErrorAction SilentlyContinue)) {
    winget install MongoDB.Shell --source winget --silent --accept-package-agreements --accept-source-agreements 2>&1 | Select-Object -Last 2
    Refresh-Path
}

New-Item -ItemType Directory -Force -Path "C:\data\mongodb9" | Out-Null
New-Item -ItemType Directory -Force -Path "C:\logs\mongodb" | Out-Null

# Start MongoDB
if (-not (Test-Port 27017)) {
    Start-Process mongod -ArgumentList "--dbpath C:\data\mongodb9 --logpath C:\logs\mongodb\mongod.log --port 27017" -WindowStyle Hidden
    Start-Sleep -Seconds 4
}

# Create admin user (ignore error if already exists)
try {
    mongosh admin --eval "db.createUser({user:'admin',pwd:'admin123',roles:[{role:'root',db:'admin'}]})" 2>$null | Out-Null
} catch {}

# Restart with --auth
Stop-Process -Name mongod -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2
Start-Process mongod -ArgumentList "--dbpath C:\data\mongodb9 --logpath C:\logs\mongodb\mongod.log --port 27017 --auth" -WindowStyle Hidden
Start-Sleep -Seconds 4
ok "MongoDB ready"

# ── MinIO ──
New-Item -ItemType Directory -Force -Path "C:\minio" | Out-Null
New-Item -ItemType Directory -Force -Path "C:\minio-data" | Out-Null
if (-not (Test-Path "C:\minio\minio.exe")) {
    Write-Host "  Downloading MinIO..." -ForegroundColor DarkGray
    Invoke-WebRequest "https://dl.min.io/server/minio/release/windows-amd64/minio.exe" -OutFile "C:\minio\minio.exe"
}
ok "MinIO binary ready"

# ── STT venv ──
if ($ENABLE_STT -eq "yes") {
    $STT_DIR = "$REPO_DIR\ai4bharat_stt_server"
    if (-not (Test-Path "$STT_DIR\venv")) {
        Write-Host "  Creating STT venv..." -ForegroundColor DarkGray
        Set-Location $STT_DIR
        py -3.10 -m venv venv

        # Compatibility patch for NeMo/pytorch_lightning
        $serverPath = "$STT_DIR\server.py"
        $content = Get-Content $serverPath -Raw
        if ($content -notmatch "pytorch_lightning.loggers") {
            $patch = "import sys`nimport pytorch_lightning.loggers`nsys.modules['pytorch_lightning.loggers'].NeptuneLogger = None`n`n"
            Set-Content -Path $serverPath -Value ($patch + $content)
        }

        & "$STT_DIR\venv\Scripts\pip.exe" install -q --upgrade pip 2>&1 | Select-Object -Last 1
        & "$STT_DIR\venv\Scripts\pip.exe" install -q -r requirements.txt 2>&1 | Select-Object -Last 2
        & "$STT_DIR\venv\Scripts\pip.exe" install -q numba ruamel.yaml scikit-learn tensorboard text-unidecode pytorch-lightning 2>&1 | Select-Object -Last 1
        & "$STT_DIR\venv\Scripts\pip.exe" install -q --no-deps "nemo_toolkit[asr] @ git+https://github.com/AI4Bharat/NeMo.git@nemo-v2" 2>&1 | Select-Object -Last 2
    }

    # Download STT checkpoint
    New-Item -ItemType Directory -Force -Path "$STT_DIR\checkpoints" | Out-Null
    if (-not (Test-Path "$STT_DIR\checkpoints\indic_conformer.nemo")) {
        Write-Host "  Downloading STT checkpoint (~2.4 GB)..." -ForegroundColor DarkGray
        aria2c -x 16 -s 16 -k 1M `
            "https://objectstore.e2enetworks.net/indicconformer/models/indicconformer_stt_multi_hybrid_rnnt_600m.nemo" `
            -d "$STT_DIR\checkpoints" -o "indic_conformer.nemo"
    }

    Set-Content -Path "$STT_DIR\.env" -Value @"
PORT=8001
BHILI_ENABLE=no
INDIC_NEMO_PATH=$STT_DIR\checkpoints\indic_conformer.nemo
HF_TOKEN=$HF_TOKEN
"@
    # Verify before declaring success — a failed/partial download should not print "ready".
    $sttCkpt = Get-Item "$STT_DIR\checkpoints\indic_conformer.nemo" -ErrorAction SilentlyContinue
    if ($sttCkpt -and $sttCkpt.Length -gt 500MB) {
        ok "STT ready (checkpoint $([math]::Round($sttCkpt.Length/1GB,2)) GB)"
    } else {
        Write-Host "  WARN STT checkpoint missing or incomplete at $STT_DIR\checkpoints\indic_conformer.nemo — STT will not start correctly. Re-run this script, or download manually and retry." -ForegroundColor Yellow
    }
}

# ── TTS venv ──
if ($ENABLE_TTS -eq "local") {
    $TTS_DIR = "$REPO_DIR\ai4bharat_tts_server"
    Write-Host "  WARN ENABLE_TTS=local: this server needs flashinfer, which has no official" -ForegroundColor Yellow
    Write-Host "       Windows build (Linux-only PyPI wheels). Attempting install anyway in" -ForegroundColor Yellow
    Write-Host "       case an unofficial wheel matches your exact Python/CUDA/torch combo —" -ForegroundColor Yellow
    Write-Host "       expect this to fail on a normal setup. See change #7 at the top of this" -ForegroundColor Yellow
    Write-Host "       file for the recommended alternative (ENABLE_TTS=remote)." -ForegroundColor Yellow
    if (-not (Test-Path "$TTS_DIR\venv")) {
        Write-Host "  Creating TTS venv..." -ForegroundColor DarkGray
        Set-Location $TTS_DIR
        py -3.10 -m venv venv
        & "$TTS_DIR\venv\Scripts\pip.exe" install -q --upgrade pip 2>&1 | Select-Object -Last 1
        & "$TTS_DIR\venv\Scripts\pip.exe" install -q torch transformers==4.46.1 sentencepiece protobuf scipy websockets python-dotenv numpy 2>&1 | Select-Object -Last 2
        & "$TTS_DIR\venv\Scripts\pip.exe" install -q gdown 2>&1 | Select-Object -Last 1
        try {
            & "$TTS_DIR\venv\Scripts\pip.exe" install -q flashinfer-python --no-build-isolation 2>&1 | Select-Object -Last 3
        } catch {
            Write-Host "  WARN flashinfer install failed, as expected on most Windows setups. TTS will not start until this is resolved — see https://github.com/SystemPanic/flashinfer-windows for the unofficial Windows build path, or switch to ENABLE_TTS=remote." -ForegroundColor Yellow
        }
    }

    # Download TTS checkpoints
    New-Item -ItemType Directory -Force -Path "$TTS_DIR\checkpoints" | Out-Null
    $ckptFiles = Get-ChildItem "$TTS_DIR\checkpoints" -ErrorAction SilentlyContinue | Where-Object { $_.Length -gt 100MB }
    if (-not $ckptFiles) {
        Write-Host "  Downloading TTS checkpoints from Google Drive..." -ForegroundColor DarkGray
        # NOTE: --fuzzy is only for extracting a file ID out of a single-file *sharing* URL;
        # it is not valid combined with --folder (which already takes a plain folder URL) and
        # fails with "unrecognized arguments: --fuzzy" if included — confirmed live against a
        # Windows Server 2025 AMI, 2026-07-28. Upstream (PRANABraight/voicera_mono_repository)
        # still has --fuzzy in this call as of that date.
        & "$TTS_DIR\venv\Scripts\python.exe" -m gdown --folder `
            "https://drive.google.com/drive/folders/1qrh56MWXboiBO38gaWEcWhFl0NzlDiaT" `
            -O "$TTS_DIR\checkpoints" 2>&1 | Select-Object -Last 3
        # Flatten nested folder if gdown created one
        $nested = "$TTS_DIR\checkpoints\checkpoints"
        if (Test-Path $nested) {
            Get-ChildItem $nested | Move-Item -Destination "$TTS_DIR\checkpoints\"
            Remove-Item $nested -Force
        }
        $ckptFiles = Get-ChildItem "$TTS_DIR\checkpoints" -ErrorAction SilentlyContinue | Where-Object { $_.Length -gt 100MB }
    }

    Set-Content -Path "$TTS_DIR\.env" -Value @"
CHECKPOINT_PATH_DEFAULT=$TTS_DIR\checkpoints
BHILI_ENABLE=no
PORT=8002
HF_TOKEN=$HF_TOKEN
"@
    # Verify before declaring success — a failed download should not print "ready".
    if ($ckptFiles) {
        ok "TTS ready ($($ckptFiles.Count) checkpoint file(s))"
    } else {
        Write-Host "  WARN No TTS checkpoint files found in $TTS_DIR\checkpoints — the download failed. Retry manually: $TTS_DIR\venv\Scripts\python.exe -m gdown --folder https://drive.google.com/drive/folders/1qrh56MWXboiBO38gaWEcWhFl0NzlDiaT -O $TTS_DIR\checkpoints" -ForegroundColor Yellow
    }
}
if ($ENABLE_TTS -eq "remote") {
    ok "TTS: using remote endpoint $TTS_REMOTE_URL (no local install)"
}

# ── V2V venv ──
$V2V_DIR = "$REPO_DIR\voice_2_voice_server"
if (-not (Test-Path "$V2V_DIR\venv")) {
    Write-Host "  Creating V2V venv..." -ForegroundColor DarkGray
    Set-Location $V2V_DIR
    py -3.10 -m venv venv
    & "$V2V_DIR\venv\Scripts\pip.exe" install -q --upgrade pip 2>&1 | Select-Object -Last 1
    & "$V2V_DIR\venv\Scripts\pip.exe" install -q -r requirements.txt 2>&1 | Select-Object -Last 2
}
ok "V2V venv ready"

# ── Backend venv ──
$BACKEND_DIR = "$REPO_DIR\voicera_backend"
if (-not (Test-Path "$BACKEND_DIR\venv")) {
    Write-Host "  Creating Backend venv..." -ForegroundColor DarkGray
    Set-Location $BACKEND_DIR
    py -3.10 -m venv venv
    & "$BACKEND_DIR\venv\Scripts\pip.exe" install -q --upgrade pip 2>&1 | Select-Object -Last 1
    & "$BACKEND_DIR\venv\Scripts\pip.exe" install -q -r requirements.txt 2>&1 | Select-Object -Last 2
}

# Backend .env
$SECRET_KEY = & "$BACKEND_DIR\venv\Scripts\python.exe" -c "import secrets; print(secrets.token_urlsafe(32))"
Set-Content -Path "$BACKEND_DIR\.env" -Value @"
MONGODB_HOST=localhost
MONGODB_PORT=27017
MONGODB_USER=admin
MONGODB_PASSWORD=admin123
MONGODB_DATABASE=voicera
MONGODB_AUTH_SOURCE=admin
DEBUG=True
SECRET_KEY=$SECRET_KEY
INTERNAL_API_KEY=$INTERNAL_KEY
MAILTRAP_API_TOKEN=placeholder
MAILTRAP_FROM_EMAIL=noreply@voicera.com
MAILTRAP_FROM_NAME=VoicEra
FRONTEND_URL=https://PENDING
MINIO_ENDPOINT=localhost:9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin
VOBIZ_API_BASE_URL=https://api.vobiz.in/v1
VOBIZ_AUTH_ID=$VOBIZ_AUTH_ID
VOBIZ_AUTH_TOKEN=$VOBIZ_AUTH_TOKEN
"@
ok "Backend ready"

# ── Frontend npm install ──
$FRONTEND_DIR = "$REPO_DIR\voicera_frontend"
if (-not (Test-Path "$FRONTEND_DIR\node_modules")) {
    Write-Host "  Installing frontend node_modules..." -ForegroundColor DarkGray
    Set-Location $FRONTEND_DIR
    npm install --silent 2>&1 | Select-Object -Last 3
}
Set-Content -Path "$FRONTEND_DIR\.env.local" -Value 'NEXT_PUBLIC_JOHNAIC_SERVER_URL="https://PENDING"'
ok "Frontend ready"

# ── V2V .env ──
# NOTE: INDIC_STT_SERVER_URL / INDIC_TTS_SERVER_URL are the variable names the
# Python services actually read at runtime. AI4BHARAT_STT_URL/AI4BHARAT_TTS_URL
# are upstream's (documented but not live) names — kept below as a harmless
# fallback only, do not rely on them.
$ttsUrl = switch ($ENABLE_TTS) {
    "local"  { "ws://${PRIVATE_IP}:8002" }
    "remote" { $TTS_REMOTE_URL }
    default  { "ws://PENDING" }
}
Set-Content -Path "$V2V_DIR\.env" -Value @"
VOBIZ_AUTH_ID=$VOBIZ_AUTH_ID
VOBIZ_AUTH_TOKEN=$VOBIZ_AUTH_TOKEN
VOBIZ_API_BASE=https://api.vobiz.in/v1
VOBIZ_CALLER_ID=+91XXXXXXXXXX
PLIVO_AUTH_ID=PLACEHOLDER
PLIVO_AUTH_TOKEN=PLACEHOLDER
JOHNAIC_SERVER_URL=https://PENDING
JOHNAIC_WEBSOCKET_URL=wss://PENDING
VOICERA_BACKEND_URL=http://localhost:8000
INTERNAL_API_KEY=$INTERNAL_KEY
MINIO_ENDPOINT=localhost:9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin
MINIO_SECURE=false
BHASHINI_API_KEY=PLACEHOLDER
BHASHINI_SOCKET_URL=PLACEHOLDER
INDIC_STT_SERVER_URL=http://${PRIVATE_IP}:8001
INDIC_TTS_SERVER_URL=$ttsUrl
AI4BHARAT_STT_URL=http://${PRIVATE_IP}:8001
AI4BHARAT_TTS_URL=$ttsUrl
OPENAI_API_KEY=$OPENAI_API_KEY
XAI_API_KEY=$XAI_API_KEY
"@
ok "V2V .env written (TTS: $ttsUrl)"

# ═════════════════════════════════════════════════════════════════════════════
# PHASE 3 — Go Live
# ═════════════════════════════════════════════════════════════════════════════
log "Phase 3/3: Starting Services"

New-Item -ItemType Directory -Force -Path "C:\logs\voicera" | Out-Null

# Helper: launch a service in a new visible PowerShell window
function Start-Service-Window {
    param([string]$Title, [string]$WorkDir, [string]$Command)
    $args = "-NoExit -Command `"Set-Location '$WorkDir'; $Command`""
    Start-Process powershell -ArgumentList $args -WindowStyle Normal
}

# ── MinIO ──
if (-not (Test-Port 9000)) {
    Start-Process -FilePath "C:\minio\minio.exe" `
        -ArgumentList "server C:\minio-data --console-address :9001" `
        -RedirectStandardOutput "C:\logs\voicera\minio.log" -WindowStyle Minimized
    Start-Sleep -Seconds 3
}
ok "MinIO started (port 9000)"

# ── Backend ──
if (-not (Test-Port 8000)) {
    Start-Service-Window "VoicEra Backend" $BACKEND_DIR `
        "$BACKEND_DIR\venv\Scripts\python.exe run.py"
    Start-Sleep -Seconds 8
}
ok "Backend started (port 8000)"

# ── STT ──
if ($ENABLE_STT -eq "yes" -and -not (Test-Port 8001)) {
    Start-Service-Window "VoicEra STT" "$REPO_DIR\ai4bharat_stt_server" `
        ".\venv\Scripts\python.exe server.py"
    ok "STT started (port 8001) — loading model, takes ~2 min"
}

# ── TTS ──
if ($ENABLE_TTS -eq "local" -and -not (Test-Port 8002)) {
    Start-Service-Window "VoicEra TTS" "$REPO_DIR\ai4bharat_tts_server" `
        ".\venv\Scripts\python.exe server.py"
    ok "TTS started (port 8002)"
}

# NOTE: upstream's "vLLM via WSL2" launch step has been removed in this fork.
# See the CHANGES FROM UPSTREAM note at the top of this file for why.
# If ENABLE_LLM ever reaches this point as "vllm", the config-time check above
# already exited before we got here — this section intentionally has no vLLM
# branch to keep it that way.

# ── Voice2Voice ──
if (-not (Test-Port 7860)) {
    Start-Service-Window "VoicEra V2V" $V2V_DIR `
        "$V2V_DIR\venv\Scripts\python.exe main.py"
    Start-Sleep -Seconds 5
}
ok "V2V started (port 7860)"

# ── Frontend ──
if (-not (Test-Port 3000)) {
    Start-Service-Window "VoicEra Frontend" $FRONTEND_DIR `
        "npm run dev -- --port 3000"
    Start-Sleep -Seconds 8
}
ok "Frontend started (port 3000)"

# ── ngrok ──
if (-not (Test-Port 4040)) {
    Start-Process ngrok -ArgumentList "http 7860" -WindowStyle Minimized
    Start-Sleep -Seconds 5
}
$NGROK_URL = ""
try {
    $tunnels = (Invoke-RestMethod "http://localhost:4040/api/tunnels").tunnels
    $NGROK_URL = ($tunnels | Where-Object { $_.proto -eq "https" } | Select-Object -First 1).public_url
    if (-not $NGROK_URL) { $NGROK_URL = $tunnels[0].public_url }
} catch {}
if ($NGROK_URL) { ok "ngrok started: $NGROK_URL" } else { ok "ngrok started (URL pending)" }

# ── Cloudflare tunnel ──
$CF_LOG = "$env:TEMP\voicera_cf.log"
Remove-Item $CF_LOG -ErrorAction SilentlyContinue
Start-Process cloudflared -ArgumentList "tunnel --url http://localhost:3000 --logfile $CF_LOG" -WindowStyle Minimized
Write-Host "  Waiting for Cloudflare tunnel..." -ForegroundColor DarkGray
$CF_URL = ""
for ($i = 0; $i -lt 12; $i++) {
    Start-Sleep -Seconds 3
    if (Test-Path $CF_LOG) {
        $CF_URL = Select-String -Path $CF_LOG -Pattern 'https://[^ |]+\.trycloudflare\.com' |
            Select-Object -Last 1 | ForEach-Object { $_.Matches[0].Value }
        if ($CF_URL) { break }
    }
}

# ── Update .env files with real tunnel URLs ──
if ($NGROK_URL) {
    $WS_URL = $NGROK_URL -replace "^https://","wss://" -replace "^http://","ws://"
    (Get-Content "$V2V_DIR\.env") -replace "https://PENDING",$NGROK_URL -replace "wss://PENDING",$WS_URL |
        Set-Content "$V2V_DIR\.env"
    (Get-Content "$FRONTEND_DIR\.env.local") -replace "https://PENDING",$NGROK_URL |
        Set-Content "$FRONTEND_DIR\.env.local"
}
if ($CF_URL) {
    (Get-Content "$BACKEND_DIR\.env") -replace "https://PENDING",$CF_URL |
        Set-Content "$BACKEND_DIR\.env"
}

# ── Wait for services to come up (STT loads 2.4 GB model) ──
Write-Host ""
Write-Host "  Waiting for services to come up (STT loads ~2.4 GB model, allow up to 3 min)..." -ForegroundColor DarkGray
for ($i = 1; $i -le 18; $i++) {
    Start-Sleep -Seconds 10
    $sttOk = $false; $v2vOk = $false; $apiOk = $false
    try {
        $sttHealth = Invoke-RestMethod "http://localhost:8001/health" -TimeoutSec 3 -ErrorAction Stop
        $sttOk = $sttHealth.main_loaded -eq $true
    } catch {}
    try {
        $v2vHealth = Invoke-RestMethod "http://localhost:7860/health" -TimeoutSec 3 -ErrorAction Stop
        $v2vOk = $v2vHealth.status -eq "healthy"
    } catch {}
    try {
        $apiHealth = Invoke-RestMethod "http://localhost:8000/health" -TimeoutSec 3 -ErrorAction Stop
        $apiOk = $true
    } catch {}

    $sttSkip = $ENABLE_STT -ne "yes"
    if (($sttOk -or $sttSkip) -and $v2vOk -and $apiOk) { break }
    Write-Host "  ...${i}0s (STT=$(if($sttSkip){'skip'}elseif($sttOk){'ok'}else{'loading'}) V2V=$(if($v2vOk){'ok'}else{'loading'}) API=$(if($apiOk){'ok'}else{'loading'}))" -ForegroundColor DarkGray
}

# ── Final summary ──
Write-Host ""
Write-Host "  ══════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "            VoicEra is Live!" -ForegroundColor Green
Write-Host "  ══════════════════════════════════════════════════════" -ForegroundColor Cyan

if ($NGROK_URL)  { Write-Host "  V2V (ngrok):      $NGROK_URL" -ForegroundColor White }
if ($CF_URL)     { Write-Host "  App (Cloudflare): $CF_URL"    -ForegroundColor White }

Write-Host ""
try {
    $h = Invoke-RestMethod "http://localhost:8001/health" -TimeoutSec 3
    Write-Host "  STT  : $($h.status) | model=$($h.main_loaded)" -ForegroundColor Cyan
} catch { Write-Host "  STT  : loading..." -ForegroundColor DarkGray }

try {
    $h = Invoke-RestMethod "http://localhost:7860/health" -TimeoutSec 3
    Write-Host "  V2V  : $($h.status)" -ForegroundColor Cyan
} catch { Write-Host "  V2V  : loading..." -ForegroundColor DarkGray }

try {
    $h = Invoke-RestMethod "http://localhost:8000/health" -TimeoutSec 3
    Write-Host "  API  : ok" -ForegroundColor Cyan
} catch { Write-Host "  API  : loading..." -ForegroundColor DarkGray }

switch ($ENABLE_TTS) {
    "local"  {
        if (Test-Port 8002) { Write-Host "  TTS  : listening :8002" -ForegroundColor Cyan }
        else { Write-Host "  TTS  : loading..." -ForegroundColor DarkGray }
    }
    "remote" { Write-Host "  TTS  : remote ($TTS_REMOTE_URL)" -ForegroundColor Cyan }
    default  { Write-Host "  TTS  : disabled" -ForegroundColor DarkGray }
}

Write-Host ""
Write-Host "  NOTE: First login requires email verification bypass." -ForegroundColor Yellow
Write-Host "  Run after signup:" -ForegroundColor Yellow
Write-Host '  mongosh "mongodb://admin:admin123@localhost:27017/voicera?authSource=admin" --quiet --eval "db.users.updateOne({email:''your@email.com''},{$set:{is_verified:true}})"' -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Logs: C:\logs\voicera\" -ForegroundColor DarkGray
Write-Host "  ngrok dashboard: http://localhost:4040" -ForegroundColor DarkGray
Write-Host "  MinIO console:   http://localhost:9001  (minioadmin / minioadmin)" -ForegroundColor DarkGray
Write-Host ""
