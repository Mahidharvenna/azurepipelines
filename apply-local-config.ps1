<#
    apply-local-config.ps1

    Re-applies your site-specific values to the pipeline templates after you
    pull a new version of them.

    The templates ship with placeholders so they can live in a shared/public
    repo. Every environment has to swap those for real hostnames, paths, schema
    and mail domains. Doing that by hand means redoing ~9 edits on every update
    and quietly getting one wrong; this script makes it repeatable.

    USAGE
      1. Fill in the CONFIG block below, once.
      2. Copy the new templates/, pipelines/ and config/ over your working copy.
      3. Run:  pwsh -File apply-local-config.ps1
      4. Review 'git diff', then commit.

    Safe to run repeatedly -- it reports any placeholder it could not find, so a
    silently-missed substitution shows up instead of reaching a deploy.
#>

# =============================== CONFIG ======================================
# Replace each value with yours. Everything here is site-specific, not secret --
# passwords and webhook URLs stay in the variable group.

$AdoCollectionUrl = 'http://your-tfs-host:8080/tfs/DefaultCollection'
$ProjectName      = 'YourProject'          # ADO project; also the REST path segment
$BuildPrefix      = 'YourOrg'              # run-name prefix, e.g. YourOrg-DEV1-20260101.1
$VariableGroup    = 'Your Variable Group'  # the Library group holding DB + userpass secrets
$DbSchema         = 'dbo'                  # schema owning ReleaseNotes / CurrentBuild
$GwCorePath       = 'D:\PATH\TO\GWCORE'    # on-agent Guidewire core root (no trailing slash)

# Prod mail domains to STRIP in non-prod, so a lower env cannot email customers.
# List every one your org uses -- a missed domain means live mail from DEV/QA.
$ProdMailDomains  = @('yourdomain.example', 'yourotherdomain.example')
# =============================================================================

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$edits = 0
$misses = @()

function Swap {
    param([string]$RelPath, [string]$Find, [string]$Replace, [string]$Label)
    $path = Join-Path $root $RelPath
    if (-not (Test-Path $path)) { $script:misses += "$RelPath (file not found)"; return }
    $text = Get-Content $path -Raw
    if ($text -notlike "*$Find*") {
        # Either already applied, or the template changed shape.
        $script:misses += "$Label -- placeholder '$Find' not found in $RelPath"
        return
    }
    ($text -replace [regex]::Escape($Find), $Replace) | Set-Content $path -NoNewline
    Write-Host ("  {0,-28} {1}" -f $Label, $RelPath)
    $script:edits++
}

Write-Host "Applying local configuration..." -ForegroundColor Cyan
Write-Host ""

Swap 'templates/tier-orchestrator.yml' "'deployment-secrets'" "'$VariableGroup'" 'variable group'
Swap 'templates/tier-orchestrator.yml' "default: 'dbo'"       "default: '$DbSchema'" 'db schema'
Swap 'templates/gw-build.yml'          'C:\path\to\gw-core'   $GwCorePath            'gw core path'
Swap 'templates/gw-deploy.yml'         'https://your-ado-host.example.com/your-collection' $AdoCollectionUrl 'ado url'
Swap 'templates/gw-deploy.yml'         '$projectName              = "MyOrg"' "`$projectName              = `"$ProjectName`"" 'rest project name'

# Mail domains: rebuild the sed expression from the list, however many there are.
$sedArgs = ($ProdMailDomains | ForEach-Object { "-e 's/@$_//g'" }) -join ' '
Swap 'templates/gw-deploy.yml' "-e 's/@example.com//g' -e 's/@example.net//g'" $sedArgs 'prod mail domains'

foreach ($w in @('release-dev','release-qa','release-uat')) {
    Swap "pipelines/$w.yaml" "name: 'MyOrg-" "name: '$BuildPrefix-" "run name ($w)"
}

Write-Host ""
if ($misses.Count -gt 0) {
    Write-Host "Not applied:" -ForegroundColor Yellow
    $misses | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
    Write-Host ""
    Write-Host "A placeholder is missing either because it is already substituted" -ForegroundColor Yellow
    Write-Host "or because the template changed. Check 'git diff' before committing." -ForegroundColor Yellow
} else {
    Write-Host "All placeholders substituted." -ForegroundColor Green
}
Write-Host ""
Write-Host "$edits edit(s) made. Review with 'git diff', then commit."
