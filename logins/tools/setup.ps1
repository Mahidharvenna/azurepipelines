<#
.SYNOPSIS
    One-shot bootstrap for the login-history database, run from the pipeline.

.DESCRIPTION
    Does the setup that would otherwise need sqlcmd on someone's laptop:
    pre-flight checks, schema, grants, and verification.

    Uses System.Data.SqlClient (built into Windows PowerShell) rather than
    sqlcmd, so the agent needs nothing installed. That means this script has to
    do two things sqlcmd does for free:
      * split the file on GO -- a batch separator, not T-SQL, so the server
        rejects it
      * expand :setvar / $(TOKEN) -- a sqlcmd construct the server never sees

    Configuration comes from environment variables set by the pipeline, so no
    secret is ever passed as an argument (arguments are echoed in the log).

.PARAMETER Action
    check   pre-flight only. Touches nothing. The default, deliberately.
    schema  apply sql/schema.sql (idempotent)
    grants  apply sql/grants.sql
    verify  run sql/verify.sql and print every result set
    all     check, schema, grants, verify -- in that order
#>
[CmdletBinding()]
param(
    [ValidateSet('check', 'schema', 'grants', 'verify', 'all')]
    [string]$Action = 'check',

    [string]$CollectorLogin = '',
    [string]$GrafanaLogin = '',

    [string]$SqlRoot = 'logins/sql'
)

$ErrorActionPreference = 'Stop'
$script:SoftFailures = 0

function Write-Section($text) {
    Write-Host ''
    Write-Host ('=' * 72)
    Write-Host "  $text"
    Write-Host ('=' * 72)
}

function Write-Ok($text)   { Write-Host "  [ OK ] $text" }
function Write-Info($text) { Write-Host "         $text" }

function Write-Soft($text) {
    $script:SoftFailures++
    Write-Host "##vso[task.logissue type=warning]$text"
}

function Get-EnvOrDefault([string]$name, [string]$default = '') {
    $v = [Environment]::GetEnvironmentVariable($name)
    # A variable not defined in the group arrives as the literal "$(NAME)".
    if ([string]::IsNullOrWhiteSpace($v)) { return $default }
    if ($v.Trim().StartsWith('$(') -and $v.Trim().EndsWith(')')) { return $default }
    return $v
}

function Get-EnvBool([string]$name, [bool]$default) {
    $v = (Get-EnvOrDefault $name '').Trim().ToLower()
    if ($v -eq '') { return $default }
    return @('true', '1', 'yes', 'y') -contains $v
}

# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
$DbServer   = Get-EnvOrDefault 'DB_SERVER'
$DbName     = Get-EnvOrDefault 'DB_NAME'
$DbUser     = Get-EnvOrDefault 'DB_USER'
$DbPass     = Get-EnvOrDefault 'DB_PASS'
$DbTrusted  = Get-EnvBool 'DB_TRUSTED_CONNECTION' $false
$DbEncrypt  = Get-EnvBool 'DB_ENCRYPT' $true
$DbTrustCrt = Get-EnvBool 'DB_TRUST_SERVER_CERT' $false

$LokiUrl     = Get-EnvOrDefault 'LOKI_URL'
$LokiVerify  = Get-EnvBool 'LOKI_VERIFY_TLS' $true
$BypassProxy = Get-EnvBool 'BYPASS_PROXY' $true

if (-not $DbServer -or -not $DbName) {
    throw "DB_SERVER and DB_NAME must be set. Check the variable group is linked to this pipeline."
}
if (-not $CollectorLogin) { $CollectorLogin = $DbUser }

function Get-ConnectionString {
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append("Server=$DbServer;Database=$DbName;")
    if ($DbTrusted) {
        [void]$sb.Append("Integrated Security=True;")
    } else {
        if (-not $DbUser) { throw "Set DB_USER/DB_PASS, or DB_TRUSTED_CONNECTION=true." }
        [void]$sb.Append("User ID=$DbUser;Password=$DbPass;")
    }
    [void]$sb.Append("Encrypt=$(if ($DbEncrypt) {'True'} else {'False'});")
    if ($DbTrustCrt) { [void]$sb.Append("TrustServerCertificate=True;") }
    [void]$sb.Append("Connect Timeout=30;Application Name=GW-Login-Setup;")
    return $sb.ToString()
}

function New-SqlConnection {
    $cn = New-Object System.Data.SqlClient.SqlConnection (Get-ConnectionString)
    # Surface PRINT / RAISERROR(...,10,...) output the way sqlcmd would.
    $cn.add_InfoMessage({ param($sender, $e) Write-Host "         $($e.Message)" })
    $cn.Open()
    return $cn
}

function Expand-SqlcmdTokens([string]$text, [hashtable]$overrides) {
    $vars = @{}
    foreach ($line in ($text -split "`r?`n")) {
        if ($line -match '^\s*:setvar\s+(\w+)\s+"?([^"]*)"?\s*$') {
            $vars[$Matches[1]] = $Matches[2]
        }
    }
    foreach ($k in $overrides.Keys) {
        if ($overrides[$k]) { $vars[$k] = $overrides[$k] }
    }
    # Drop the :setvar lines themselves -- the server has never heard of them.
    $kept = ($text -split "`r?`n") | Where-Object { $_ -notmatch '^\s*:setvar\s' }
    $out = ($kept -join "`n")
    foreach ($k in $vars.Keys) {
        $out = $out.Replace('$' + '(' + $k + ')', $vars[$k])
        Write-Info ":setvar $k = $($vars[$k])"
    }
    return $out
}

function Invoke-SqlFile {
    param(
        [Parameter(Mandatory)][string]$Path,
        [hashtable]$Vars = @{},
        [switch]$ShowResults
    )
    if (-not (Test-Path $Path)) { throw "SQL file not found: $Path" }
    Write-Info "file: $Path"

    $text = Get-Content -Raw -Path $Path
    $text = Expand-SqlcmdTokens $text $Vars

    # GO is a batch separator understood by sqlcmd/SSMS, not by the server.
    $batches = [regex]::Split($text, '(?im)^[ \t]*GO[ \t]*$') |
               Where-Object { $_.Trim().Length -gt 0 }
    Write-Info "$($batches.Count) batch(es)"

    $cn = New-SqlConnection
    try {
        $i = 0
        foreach ($batch in $batches) {
            $i++
            $cmd = $cn.CreateCommand()
            $cmd.CommandText = $batch
            $cmd.CommandTimeout = 600
            try {
                if ($ShowResults) {
                    $da = New-Object System.Data.SqlClient.SqlDataAdapter $cmd
                    $ds = New-Object System.Data.DataSet
                    [void]$da.Fill($ds)
                    foreach ($t in $ds.Tables) {
                        if ($t.Rows.Count -eq 0) {
                            Write-Host "         (no rows)"
                        } else {
                            ($t | Format-Table -AutoSize | Out-String).TrimEnd() -split "`n" |
                                ForEach-Object { Write-Host "         $_" }
                        }
                    }
                } else {
                    [void]$cmd.ExecuteNonQuery()
                }
            } catch {
                $preview = ($batch.Trim() -split "`n" | Select-Object -First 3) -join ' / '
                throw "Batch $i of $Path failed: $($_.Exception.Message)`n  near: $preview"
            }
        }
    } finally {
        $cn.Close()
    }
    Write-Ok "applied $Path"
}

function Invoke-SqlScalar([string]$query) {
    $cn = New-SqlConnection
    try {
        $cmd = $cn.CreateCommand()
        $cmd.CommandText = $query
        $cmd.CommandTimeout = 60
        return $cmd.ExecuteScalar()
    } finally { $cn.Close() }
}

# ---------------------------------------------------------------------------
# pre-flight
# ---------------------------------------------------------------------------
function Invoke-Preflight {
    Write-Section 'Pre-flight'

    # --- 1. TCP reachability -------------------------------------------------
    $sqlHost = ($DbServer -split ',')[0]
    $sqlPort = 1433
    if ($DbServer -match ',(\d+)$') { $sqlPort = [int]$Matches[1] }

    $t = Test-NetConnection -ComputerName $sqlHost -Port $sqlPort -WarningAction SilentlyContinue
    if ($t.TcpTestSucceeded) { Write-Ok "SQL Server reachable: ${sqlHost}:${sqlPort}" }
    else { throw "Cannot reach ${sqlHost}:${sqlPort} from this agent. Wrong host, firewall, or the collector needs a different agent pool." }

    if ($LokiUrl) {
        $u = [Uri]$LokiUrl
        $t2 = Test-NetConnection -ComputerName $u.Host -Port $u.Port -WarningAction SilentlyContinue
        if ($t2.TcpTestSucceeded) { Write-Ok "Loki reachable: $($u.Host):$($u.Port)" }
        else { Write-Soft "Cannot reach Loki at $($u.Host):$($u.Port). The collector will fail even though the database is fine." }
    } else {
        Write-Soft "LOKI_URL is not set -- skipping the Loki probe."
    }

    # --- 2. Loki actually answers -------------------------------------------
    if ($LokiUrl) {
        try {
            [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
            if (-not $LokiVerify) {
                [System.Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
                Write-Info "LOKI_VERIFY_TLS is false -- certificate validation disabled."
            }
            $wc = New-Object System.Net.WebClient
            if ($BypassProxy) { $wc.Proxy = $null }
            $body = $wc.DownloadString("$LokiUrl/loki/api/v1/labels")
            $labels = (ConvertFrom-Json $body).data
            Write-Ok "Loki answered. $($labels.Count) label(s): $((($labels | Select-Object -First 8) -join ', '))"
            if ($labels -notcontains 'env')     { Write-Soft "Loki has no 'env' label -- ENVS will not match anything." }
            if ($labels -notcontains 'project') { Write-Soft "Loki has no 'project' label -- check LOKI_PROJECT." }
        } catch {
            Write-Soft "Loki HTTP probe failed: $($_.Exception.Message). Check TLS trust or BYPASS_PROXY."
        }
    }

    # --- 3. Database engine and rights ---------------------------------------
    $version = Invoke-SqlScalar 'SELECT @@VERSION'
    $firstLine = ($version -split "`n")[0].Trim()
    Write-Ok "Connected. $firstLine"
    if ($firstLine -notmatch 'Microsoft SQL Server') {
        throw "This is not Microsoft SQL Server. The schema and the dashboard panels are T-SQL and need a dialect pass first."
    }

    $canCreate = Invoke-SqlScalar "SELECT HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','CREATE TABLE')"
    if ($canCreate -eq 1) { Write-Ok "'$DbUser' can CREATE TABLE in [$DbName]" }
    else { Write-Soft "'$DbUser' cannot CREATE TABLE in [$DbName]. A DBA must run schema.sql once; after that this account only needs the DML in grants.sql." }

    # --- 4. What already exists ----------------------------------------------
    $existing = Invoke-SqlScalar "SELECT COUNT(*) FROM sys.objects WHERE name LIKE 'gw_login%'"
    Write-Info "existing gw_login* objects: $existing (7 once schema.sql has run)"

    # --- 5. Collector prerequisites ------------------------------------------
    $py = $null
    foreach ($c in @('python', 'python3', 'py')) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) {
            $ver = (& $c --version 2>&1) -join ' '
            if ($ver -match 'Python 3') { $py = $cmd.Source; Write-Ok "Python: $ver ($py)"; break }
        }
    }
    if (-not $py) { Write-Soft "No Python 3 on this agent. The collector pipeline will fail until one is installed and the agent service restarted." }

    $drivers = @()
    try { $drivers = (Get-OdbcDriver -ErrorAction SilentlyContinue | Where-Object { $_.Name -like '*SQL Server*' }).Name } catch { }
    if ($drivers) { Write-Ok "ODBC drivers: $($drivers -join '; ')" }
    else { Write-Soft "No SQL Server ODBC driver found. pyodbc needs Microsoft's ODBC Driver 17 or 18 installed on this machine." }

    if ($py) {
        try {
            & $py -m pip --version 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { Write-Ok "pip is available" } else { Write-Soft "pip is not usable by $py." }
        } catch { Write-Soft "pip check failed: $($_.Exception.Message)" }
    }
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
Write-Host "Action           : $Action"
Write-Host "Database         : $DbServer / $DbName"
Write-Host "Auth             : $(if ($DbTrusted) {'Windows integrated'} else {"SQL login '$DbUser'"})"
Write-Host "Encrypt          : $DbEncrypt  (TrustServerCertificate=$DbTrustCrt)"

if ($Action -in @('check', 'all')) { Invoke-Preflight }

if ($Action -in @('schema', 'all')) {
    Write-Section 'Apply schema'
    Invoke-SqlFile -Path (Join-Path $SqlRoot 'schema.sql')
    $n = Invoke-SqlScalar "SELECT COUNT(*) FROM sys.objects WHERE name LIKE 'gw_login%'"
    if ($n -lt 7) { throw "Expected 7 gw_login* objects after schema.sql, found $n." }
    Write-Ok "$n objects present"
}

if ($Action -in @('grants', 'all')) {
    Write-Section 'Apply grants'
    if (-not $GrafanaLogin) {
        throw "grants needs -GrafanaLogin. Create a read-only SQL login for Grafana first; it must never be the collector's account."
    }
    if ($GrafanaLogin -eq $CollectorLogin) {
        throw "GrafanaLogin and CollectorLogin are the same account. Grafana must be read-only -- panels run ad-hoc SQL that any dashboard editor can change, and these tables are the only copy of the history."
    }
    Invoke-SqlFile -Path (Join-Path $SqlRoot 'grants.sql') -Vars @{
        CollectorLogin = $CollectorLogin
        GrafanaLogin   = $GrafanaLogin
    }
}

if ($Action -in @('verify', 'all')) {
    Write-Section 'Verify'
    Invoke-SqlFile -Path (Join-Path $SqlRoot 'verify.sql') -ShowResults
    Write-Info ''
    Write-Info "Sections 3 (gaps) and 4 (per-user reconciliation) must be empty."
    Write-Info "Before the first backfill, every section being empty is expected."
}

Write-Section 'Done'
if ($script:SoftFailures -gt 0) {
    Write-Host "Completed with $($script:SoftFailures) warning(s) -- see the warnings above."
} else {
    Write-Host "No warnings."
}
exit 0
