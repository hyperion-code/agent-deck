[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$entryName = 'Codex AgentDeck'

if (Get-ItemProperty -Path $runKey -Name $entryName -ErrorAction SilentlyContinue) {
    Remove-ItemProperty -Path $runKey -Name $entryName
    Write-Host "Removed the $entryName Windows startup entry."
}
else {
    Write-Host "$entryName is not registered for Windows startup."
}
