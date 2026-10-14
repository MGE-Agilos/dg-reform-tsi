# Liste les fichiers tsi-* du workspace qui n'existent plus dans la nouvelle release (a supprimer dans *Files*).
# Avec le SDK (OVERWRITE), les fichiers modifies sont remplaces, mais un document disparu du corpus reste dans le
# workspace et dans l'index : ce script donne leurs noms. Il ne supprime rien et n'appelle aucun service.
#
# -Current : ce qui est actuellement dans le workspace, au choix
#            - un fichier texte avec un nom de fichier par ligne (liste copiee depuis *Files*, ou export de l'API) ;
#            - le dossier des fichiers de la release precedente (celle qui est en ligne).
# -New     : dossier des fichiers de la nouvelle release (defaut : deploy\prod\build\files).
#
#   .\deploy\prod\obsolete-files.ps1 -Current C:\tsi-release-old\files
#   .\deploy\prod\obsolete-files.ps1 -Current workspace-files.txt -New C:\tsi-release\files -Out a-supprimer.txt
#
# Compatible Windows PowerShell 5.1.

param(
    [Parameter(Mandatory = $true)][string]$Current,
    [string]$New = "",
    [string]$Out = ""
)

$ErrorActionPreference = "Stop"
if (-not $New) { $New = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "build\files" }
if (-not (Test-Path $New -PathType Container)) { throw "dossier de la nouvelle release introuvable : $New (passer -New)" }

function Get-DocNames([string[]]$names) {
    # Documents seulement : les .meta.json accompagnent leur fichier et ne sont pas des documents du workspace.
    $names | ForEach-Object { $_.Trim() } | Where-Object { $_ -like "tsi-*" -and $_ -notlike "*.meta.json" }
}

if (Test-Path $Current -PathType Container) {
    $currentNames = Get-DocNames (Get-ChildItem $Current -File | ForEach-Object { $_.Name })
} elseif (Test-Path $Current -PathType Leaf) {
    $currentNames = Get-DocNames (Get-Content $Current -Encoding utf8)
} else {
    throw "introuvable : $Current (fichier texte d'un nom par ligne, ou dossier de la release precedente)"
}
$newNames = @{}
foreach ($n in (Get-DocNames (Get-ChildItem $New -File | ForEach-Object { $_.Name }))) { $newNames[$n] = $true }
if ($newNames.Count -eq 0) { throw "aucun fichier tsi-* dans $New" }

$obsolete = @($currentNames | Sort-Object -Unique | Where-Object { -not $newNames.ContainsKey($_) })
Write-Host "Workspace actuel : $(@($currentNames).Count) fichiers tsi-* ; nouvelle release : $($newNames.Count) ; a supprimer : $($obsolete.Count)"
if ($obsolete.Count -gt 0) {
    $obsolete | ForEach-Object { Write-Output $_ }
    if ($Out) {
        $obsolete | Set-Content -Path $Out -Encoding utf8
        Write-Host "Liste ecrite dans $Out"
    }
}
