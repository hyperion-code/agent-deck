[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$entryName = 'Codex AgentDeck'
$scriptPath = Join-Path $PSScriptRoot 'agent_deck.py'

if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "AgentDeck script not found: $scriptPath"
}

$pythonw = (Get-Command pythonw.exe -ErrorAction Stop).Source
$command = '"{0}" "{1}"' -f $pythonw, $scriptPath

New-Item -Path $runKey -Force | Out-Null
New-ItemProperty `
    -Path $runKey `
    -Name $entryName `
    -PropertyType String `
    -Value $command `
    -Force | Out-Null

$savedCommand = Get-ItemPropertyValue -Path $runKey -Name $entryName
if ($savedCommand -ne $command) {
    throw "The Windows startup entry could not be verified."
}

Write-Host "$entryName will start automatically when this user signs in."
Write-Host $savedCommand
