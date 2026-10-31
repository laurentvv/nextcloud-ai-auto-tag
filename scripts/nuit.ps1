#Requires -Version 7
<#
.SYNOPSIS
  Séance de nuit nc-autotag : passe incrémentale (hors /Local, avec écriture) puis analyse de /Local (dry-run).
.PARAMETER Until
  Heure d'arrêt HH:MM (défaut 02:30) ; la fenêtre sans écriture du serveur commence à 03:00.
.PARAMETER Mode
  incremental | local | both (défaut).
.PARAMETER Chunk
  Fichiers analysés par tour et par compte pour /Local (les comptes alternent).
.PARAMETER Force
  Autorise un lancement dont l'heure d'arrêt est à plus de 8 h (lancement manuel en journée).
.NOTES
  Codes de sortie : 0 ok, 1 erreur, 2 séance déjà en cours.
#>
param(
    [string]$Until = '02:30',
    [ValidateSet('incremental', 'local', 'both')][string]$Mode = 'both',
    [int]$Chunk = 400,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'

$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ('nuit-{0:yyyyMMdd}.log' -f (Get-Date))
$lockFile = Join-Path $logDir 'nuit.lock'

function Log([string]$Message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $Message
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Host $line
}

function Invoke-Nc([string[]]$NcArgs) {
    Log ('> nc-autotag ' + ($NcArgs -join ' '))
    $lines = [System.Collections.Generic.List[string]]::new()
    & uv run nc-autotag @NcArgs 2>&1 | ForEach-Object {
        $text = "$_"
        $lines.Add($text)
        Log "  $text"
    }
    [pscustomobject]@{ Code = $LASTEXITCODE; Text = ($lines -join "`n") }
}

function Get-Remaining($Result) {
    if ($Result.Text -match 'Reste en attente : (\d+)') { return [int]$Matches[1] }
    return $null
}

function Get-Processed($Result) {
    if ($Result.Text -match '(\d+) traités, (\d+) ignorés, (\d+) en erreur') {
        return [int]$Matches[1] + [int]$Matches[2] + [int]$Matches[3]
    }
    return 0
}

function Test-Llama([int]$Port) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3 -SkipHttpErrorCheck
        return $r.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Invoke-Session {
    if ($Until -notmatch '^([01]\d|2[0-3]):[0-5]\d$') {
        Log "Heure d'arrêt invalide : $Until (attendu HH:MM)"
        return 1
    }
    $now = Get-Date
    $hour, $minute = $Until.Split(':') | ForEach-Object { [int]$_ }
    $deadline = $now.Date.AddHours($hour).AddMinutes($minute)
    if ($deadline -le $now) { $deadline = $deadline.AddDays(1) }
    if (-not $Force -and ($deadline - $now).TotalHours -gt 8) {
        Log "Heure d'arrêt $Until dans plus de 8 h : séance ignorée (utiliser -Force pour un lancement manuel)."
        return 0
    }

    $cfgResult = & uv run nc-autotag config-show --json
    if ($LASTEXITCODE -ne 0) {
        Log 'Lecture de config/settings.toml impossible (config-show).'
        return 1
    }
    $cfg = ($cfgResult -join "`n") | ConvertFrom-Json
    $users = @($cfg.users)
    $port = [int]$cfg.llama.port
    $code = 0

    Log "Séance de nuit : mode $Mode, arrêt à $($deadline.ToString('dd/MM HH:mm')), comptes $($users -join ', ')."

    $llama = $null
    if (Test-Llama $port) {
        Log "llama-server déjà prêt sur le port $port."
    } else {
        $ub = [string]$cfg.llama.ub
        $llamaArgs = @(
            '-m', "`"$($cfg.llama.model)`"", '--mmproj', "`"$($cfg.llama.mmproj)`"",
            '--host', '127.0.0.1', '--port', "$port",
            '-b', $ub, '-ub', $ub, '-ngl', '99'
        )
        Log 'Démarrage de llama-server (masqué).'
        $llama = Start-Process -FilePath $cfg.llama.server_exe -ArgumentList $llamaArgs `
            -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $logDir 'llama-server-nuit.log') `
            -RedirectStandardError (Join-Path $logDir 'llama-server-nuit.err.log')
        $ready = $false
        for ($i = 0; $i -lt 60; $i++) {
            if ($llama.HasExited) { break }
            if (Test-Llama $port) { $ready = $true; break }
            Start-Sleep -Seconds 3
        }
        if (-not $ready) {
            Log 'llama-server non prêt après 3 min (voir logs/llama-server-nuit.err.log).'
            return 1
        }
        Log 'llama-server prêt.'
    }

    try {
        $inv = Invoke-Nc @('inventory')
        $inventoryOk = $inv.Code -eq 0
        if (-not $inventoryOk) {
            Log 'Inventaire en échec : passe incrémentale ignorée.'
            $code = 1
        }

        if (($Mode -in 'incremental', 'both') -and $inventoryOk) {
            foreach ($u in $users) {
                if ((Get-Date) -ge $deadline) { break }
                $r = Invoke-Nc @('run', '--user', $u, '--exclude-path', '/Local', '--apply',
                    '--no-delete-tag', '--until', $Until)
                if ($null -eq (Get-Remaining $r)) {
                    Log "Passe incrémentale en échec pour $u."
                    $code = 1
                }
            }
        }

        if ($Mode -in 'local', 'both') {
            $active = [System.Collections.Generic.List[string]]::new($users)
            $failed = $false
            while ($active.Count -gt 0 -and -not $failed -and (Get-Date) -lt $deadline) {
                foreach ($u in @($active)) {
                    if ((Get-Date) -ge $deadline) { break }
                    $r = Invoke-Nc @('run', '--user', $u, '--path', "/Local/$u",
                        '--limit', "$Chunk", '--until', $Until)
                    $left = Get-Remaining $r
                    if ($null -eq $left) {
                        Log "Analyse de /Local/$u en échec : séance interrompue."
                        $code = 1
                        $failed = $true
                        break
                    }
                    if ($left -eq 0) {
                        Log "/Local/$u : terminé."
                        [void]$active.Remove($u)
                    } elseif ((Get-Processed $r) -eq 0) {
                        Log "/Local/$u : aucun fichier traité, séance interrompue."
                        $code = 1
                        $failed = $true
                        break
                    }
                }
            }
        }
    } finally {
        if ($llama) {
            Log 'Arrêt de llama-server (démarré par la séance).'
            Stop-Process -Id $llama.Id -Force -ErrorAction SilentlyContinue
        }
    }

    try {
        $counts = (& uv run nc-autotag status --json) -join "`n" | ConvertFrom-Json
        $parts = foreach ($p in $counts.PSObject.Properties) {
            $c = $p.Value
            '{0} : {1} en attente, {2} traités, {3} en erreur, {4} ignorés' -f $p.Name, $c.pending, $c.done, $c.error, $c.skipped
        }
        Log ('Résumé : ' + ($parts -join ' | '))
    } catch {
        Log 'Résumé indisponible (status --json).'
    }
    Log "Fin de séance (code $code)."
    return $code
}

if (Test-Path $lockFile) {
    $other = (Get-Content $lockFile -ErrorAction SilentlyContinue | Select-Object -First 1) -as [int]
    $alive = $other -and (Get-Process -Id $other -ErrorAction SilentlyContinue)
    if ($alive) {
        Log "Une séance est déjà en cours (PID $other)."
        exit 2
    }
    Log 'Verrou périmé supprimé.'
    Remove-Item $lockFile -Force
}
try {
    "$PID" | Out-File -FilePath $lockFile -NoClobber -Encoding ascii
} catch {
    Log 'Verrou pris par une autre séance.'
    exit 2
}

Add-Type -Namespace NcAutotag -Name Power -MemberDefinition '[System.Runtime.InteropServices.DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint flags);'
[void][NcAutotag.Power]::SetThreadExecutionState([uint32]2147483649)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED : pas de veille pendant la séance
$exitCode = 1
try {
    $exitCode = Invoke-Session
} catch {
    Log "Erreur : $($_.Exception.Message)"
    $exitCode = 1
} finally {
    [void][NcAutotag.Power]::SetThreadExecutionState([uint32]2147483648)
    Remove-Item $lockFile -Force -ErrorAction SilentlyContinue
}
exit $exitCode
