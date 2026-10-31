#Requires -Version 7
<#
.SYNOPSIS
  Tâche planifiée Windows « nc-autotag-nuit » (séance de nuit quotidienne à 22:30).
.EXAMPLE
  pwsh -NoProfile -File scripts\install-taches.ps1 -Status
  pwsh -NoProfile -File scripts\install-taches.ps1 -Install
  pwsh -NoProfile -File scripts\install-taches.ps1 -Uninstall
.NOTES
  Pause temporaire sans désinstaller : Disable-ScheduledTask -TaskName nc-autotag-nuit
  (puis Enable-ScheduledTask -TaskName nc-autotag-nuit pour reprendre).
#>
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Status
)

$ErrorActionPreference = 'Stop'
$taskName = 'nc-autotag-nuit'
$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root 'scripts\nuit.ps1'

if (@($Install, $Uninstall, $Status).Where({ $_ }).Count -ne 1) {
    Write-Host 'Indiquer exactement une option : -Install, -Uninstall ou -Status.'
    exit 1
}

function Get-Task {
    Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
}

if ($Status) {
    $task = Get-Task
    if (-not $task) {
        Write-Host "Tâche $taskName : non installée."
        exit 0
    }
    $info = Get-ScheduledTaskInfo -TaskName $taskName
    Write-Host "Tâche $taskName : $($task.State)"
    Write-Host "  Prochain lancement : $($info.NextRunTime)"
    Write-Host "  Dernier lancement  : $($info.LastRunTime) (résultat $($info.LastTaskResult))"
    Write-Host "  Action             : $($task.Actions[0].Execute) $($task.Actions[0].Arguments)"
    exit 0
}

if ($Uninstall) {
    if (-not (Get-Task)) {
        Write-Host "Tâche $taskName : déjà absente."
        exit 0
    }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Tâche $taskName supprimée."
    exit 0
}

if (-not (Test-Path $script)) {
    Write-Host "Script introuvable : $script"
    exit 1
}
$pwsh = (Get-Command pwsh -ErrorAction Stop).Source
$action = New-ScheduledTaskAction -Execute $pwsh `
    -Argument "-NoProfile -File `"$script`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At '22:30'
$settings = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6) -MultipleInstances IgnoreNew
$user = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description 'nc-autotag : séance de nuit (incrémental puis /Local), arrêt à 02:30' | Out-Null
Write-Host "Tâche $taskName installée : tous les jours à 22:30, session de $user ouverte."
