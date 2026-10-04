# sharing - onboard this repo as a device of {{PUBLIC_URL}}
# Usage (from the web UI, Onboard device -> Windows):
#   & ([scriptblock]::Create((irm {{PUBLIC_URL}}/onboarding.ps1))) 'shr1.XXXXXXXXXXXXXXXXXXXXXX' [-Device NAME] [-Project NAME] [-Force] [-Yes]
# Runs on Windows PowerShell 5.1 and PowerShell 7+. Kept to 5.1 syntax on purpose (no null-coalescing,
# no ternary, no pipeline chains). Never uses `exit`, because this block runs inside YOUR session;
# failures throw instead. The onboarding code is never printed or written to disk.
# After a failure, $LASTEXITCODE holds the bash installer's exit status: the handshake's own code
# (3 = rejected, still pending or code refused), 5 = integrity, 6 = local guard, else 1.
# $env:Path changes (to find a freshly installed uv) last only for this run.
param(
  [Parameter(Position = 0)][string]$Code = '',
  [string]$Device = '',
  [string]$Project = '',
  [switch]$Force,
  [switch]$Yes
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-StrictMode -Version 3

$SkillFiles = @('SKILL.md', 'tickets-SKILL.md', 'sharing.py', 'sharing.cmd', 'sharing')

function Say([string]$Message) { Write-Host "sharing: $Message" }
function Fail([string]$Message, [int]$ExitCode = 1) {
  # throw always ends powershell.exe -Command with 1; $LASTEXITCODE carries the bash-equivalent code
  # (5 = integrity, 6 = local guard, or the handshake's own code) for the caller's session.
  $global:LASTEXITCODE = $ExitCode
  throw "sharing: $Message"
}

function Invoke-Native {
  # Windows PowerShell 5.1 turns a redirected native stderr line into a terminating error under
  # 'Stop', so native calls whose output we capture run with 'Continue' and we check the exit code.
  param([string]$FilePath, [string[]]$Arguments)
  $saved = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $out = & $FilePath @Arguments 2>&1
    $rc = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $saved
  }
  [pscustomobject]@{ Exit = $rc; Output = @($out | ForEach-Object { "$_" }) }
}

function ConvertTo-Slug([string]$Value) {
  $s = $Value.ToLowerInvariant() -replace '[^a-z0-9._-]', '-'
  $s = $s -replace '^[-.]+', '' -replace '-+$', ''
  if ($s.Length -gt 64) { $s = $s.Substring(0, 64) }
  return $s
}

function Confirm-Yes([string]$Question) {
  if ($Yes) { return $true }
  if ([Console]::IsInputRedirected -or -not [Environment]::UserInteractive) { return $false }
  $answer = Read-Host "sharing: $Question [y/N]"
  return ($answer -match '^(y|yes)$')
}

function Update-SessionPath {
  # Picks up uv from a fresh install without dropping what this session already added to PATH.
  # Only for this run: the outer finally puts the caller's $env:Path back.
  $user = [Environment]::GetEnvironmentVariable('Path', 'User')
  $local = Join-Path $env:USERPROFILE '.local\bin'
  $env:Path = (@($local, $user, $env:Path) | Where-Object { $_ }) -join ';'
}

function Test-Command([string]$Name) {
  return ($null -ne (Get-Command $Name -ErrorAction SilentlyContinue))
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
  [IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function Test-PendingConfig([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path)) { return $false }
  try {
    $c = Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
    return ($null -ne $c.PSObject.Properties['pending_privkey'])
  } catch {
    return $false
  }
}

function Invoke-SharingInstall {
  $server = $env:SHARING_SERVER
  if (-not $server) { $server = '{{PUBLIC_URL}}' }
  $server = $server.TrimEnd('/')

  # 1. Free local checks.
  if (-not $Code) { Fail 'missing onboarding code. Copy the whole command from the web UI (Onboard device -> Windows).' }
  if ($Code -notmatch '^shr1\.[A-Za-z0-9_-]{22}$') { Fail 'that is not a valid onboarding code - copy the whole command again from the web UI' }
  if (($server -notmatch '^https://') -and ($server -notmatch '^http://(127\.0\.0\.1|localhost)(:\d+)?$')) {
    Fail "refusing server $server - only https (or http on localhost, for tests) is allowed"
  }
  [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

  # 2. Preflight: everything that can fail runs BEFORE the one-time code is spent.
  if (-not (Test-Command 'git')) { Fail 'git is required (https://git-scm.com/download/win)' }
  $top = Invoke-Native 'git' @('rev-parse', '--show-toplevel')
  $inGit = ($top.Exit -eq 0) -and ($top.Output.Count -gt 0)
  if ($inGit) {
    $root = $top.Output[0].Trim()
  } elseif ($Project) {
    $root = (Get-Location).ProviderPath
  } else {
    Fail 'run this inside a git repository (or pass -Project NAME to onboard this directory)' 6
  }
  $root = [IO.Path]::GetFullPath($root)
  Set-Location -LiteralPath $root

  $dev = $Device
  if (-not $dev) { $dev = $env:COMPUTERNAME }
  $proj = $Project
  if (-not $proj) { $proj = Split-Path -Leaf $root }
  $dev = ConvertTo-Slug $dev
  $proj = ConvertTo-Slug $proj
  if (-not $dev) { Fail 'could not derive a device name; pass -Device NAME' }
  if (-not $proj) { Fail 'could not derive a project name; pass -Project NAME' }

  $dest = Join-Path $root '.claude\skills\sharing'
  $cfgPath = Join-Path $dest 'config.json'
  if ((Test-Path -LiteralPath $cfgPath) -and -not $Force) {
    Fail "this repo is already onboarded ($cfgPath). Re-run with -Force to replace that identity." 6
  }

  if ($inGit) {
    # The handshake re-checks this with git_tracked; failing here keeps the code unspent.
    $tracked = Invoke-Native 'git' @('ls-files', '--', '.claude/skills/sharing')
    if ($tracked.Exit -ne 0) { Fail 'could not ask git whether .claude/skills/sharing is tracked; fix git, then run the command again' 6 }
    if (@($tracked.Output | Where-Object { $_.Trim() }).Count -gt 0) {
      Fail 'git tracks files under .claude/skills/sharing - the device key would be committed. Remove them from git (git rm -r --cached .claude/skills/sharing) and run the command again.' 6
    }
  }

  $installedUv = $false
  if (-not (Test-Command 'uv')) { Update-SessionPath }
  if (-not (Test-Command 'uv')) {
    if (Confirm-Yes "uv (https://docs.astral.sh/uv/) is required. Install it now?") {
      Say 'installing uv...'
      $inst = Invoke-Native 'powershell.exe' @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', '[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; irm https://astral.sh/uv/install.ps1 | iex')
      if ($inst.Exit -ne 0) { Fail ("installing uv failed:`n" + ($inst.Output -join "`n")) }
      $installedUv = $true
      Update-SessionPath
      if (-not (Test-Command 'uv')) { Fail 'uv was installed but is not on PATH; open a new terminal and run the command again' }
    } else {
      Fail 'uv is required. Install it (https://docs.astral.sh/uv/) or re-run with -Yes'
    }
  }

  Say "checking that encryption works (the first run downloads 'cryptography')..."
  $probe = 'from cryptography.hazmat.primitives.ciphers.aead import AESGCM; from cryptography.hazmat.primitives.asymmetric import ec; AESGCM(bytes(32)); ec.generate_private_key(ec.SECP256R1())'
  $chk = Invoke-Native 'uv' @('run', '--quiet', '--no-project', '--with', 'cryptography>=43', 'python', '-c', $probe)
  if ($chk.Exit -ne 0) {
    Fail ("could not load 'cryptography' through uv - nothing was installed and the code was not used`n" + ($chk.Output -join "`n")) 5
  }

  # 3. Download the skill into a private temp dir and verify every file against the manifest.
  $tmp = Join-Path ([IO.Path]::GetTempPath()) ('sharing.' + [guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $tmp | Out-Null
  try {
    $manifest = Invoke-RestMethod -UseBasicParsing -Uri "$server/skill/manifest.json"
    if (($null -eq $manifest) -or ($null -eq $manifest.PSObject.Properties['files'])) {
      Fail "the server did not return a skill manifest ($server/skill/manifest.json)"
    }
    foreach ($f in $SkillFiles) {
      $want = $manifest.files.PSObject.Properties[$f]
      if ($null -eq $want) { Fail "the server's skill manifest does not list $f; nothing was installed and the code was not used" 5 }
      $target = Join-Path $tmp $f
      Invoke-WebRequest -UseBasicParsing -Uri "$server/skill/$f" -OutFile $target
      $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash.ToLowerInvariant()
      if ($got -ne ([string]$want.Value).ToLowerInvariant()) {
        Fail "download of $f is corrupt (sha256 mismatch); nothing was installed and the code was not used" 5
      }
    }

    # 4. Stage: the folders become self-ignoring BEFORE any secret can land in them.
    #    config.json (an existing identity under -Force) is left alone; the handshake moves it aside.
    $fresh = -not (Test-Path -LiteralPath $dest)
    $share = Join-Path $root 'share'
    $shareFresh = -not (Test-Path -LiteralPath $share)
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    New-Item -ItemType Directory -Force -Path $share | Out-Null
    Write-Utf8NoBom (Join-Path $dest '.gitignore') "*`n"
    Write-Utf8NoBom (Join-Path $share '.gitignore') "*`n"
    foreach ($f in $SkillFiles) {
      Move-Item -Force -LiteralPath (Join-Path $tmp $f) -Destination (Join-Path $dest $f)
    }

    # 5. Handshake: this spends the code. It travels over stdin, never in argv or on screen.
    #    The CLI prints the fingerprint and waits for approval in the web UI.
    $hs = @('run', '--quiet', '--script', (Join-Path $dest 'sharing.py'), '_handshake', '--code', '-',
            '--server', $server, '--repo', $root, '--skill-dir', $dest,
            '--device', $dev, '--project', $proj, '--hostname', $env:COMPUTERNAME, '--platform', 'windows')
    if ($Force) { $hs += '--force' }
    Say "contacting $server ..."
    $hsExit = 1
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $Code | & uv @hs; $hsExit = $LASTEXITCODE } finally { $ErrorActionPreference = $saved }

    if ($hsExit -ne 0) {
      if (Test-PendingConfig $cfgPath) {
        Fail "not approved yet. Approve the fingerprint in the web UI, then run: $dest\sharing.cmd wait" $hsExit
      }
      # Remove only what this run created; a folder that held an identity before (-Force) stays.
      if ($fresh -and (Test-Path -LiteralPath $dest)) { Remove-Item -Recurse -Force -LiteralPath $dest }
      if ($shareFresh -and (Test-Path -LiteralPath $share)) { Remove-Item -Recurse -Force -LiteralPath $share }
      Fail "onboarding failed (exit $hsExit, see the message above)" $hsExit
    }
  } finally {
    if (Test-Path -LiteralPath $tmp) { Remove-Item -Recurse -Force -LiteralPath $tmp }
  }

  # 6. Prove it works; show what was written (nothing secret).
  & (Join-Path $dest 'sharing.cmd') whoami
  $whoamiExit = $LASTEXITCODE
  if ($whoamiExit -ne 0) { Fail "installed, but 'sharing whoami' failed (exit $whoamiExit)" $whoamiExit }
  Say "installed skill: $dest"
  Say "device config:   $cfgPath (owner-only; .gitignore '*' keeps this folder out of every commit)"
  if ($installedUv) { Say 'uv was installed for your user; open a NEW terminal so sharing.cmd finds it on PATH.' }
  Say 'done. Start a NEW Claude Code session in this repo, then ask e.g. "get me FILE1" or "share notes.md to the remote".'
}

$savedPath = $env:Path
Push-Location
try {
  Invoke-SharingInstall
} finally {
  Pop-Location
  $env:Path = $savedPath
}
