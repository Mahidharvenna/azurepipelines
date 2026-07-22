#Requires -Version 5.1
<#
    Monthly Guidewire login report: Loki -> Excel -> email.

    Pure PowerShell -- no Python, no modules, no Excel install. The .xlsx is
    written directly as OOXML, which matters on a locked-down build agent.
    All configuration comes from environment variables.
#>

$ErrorActionPreference = 'Stop'

# ---- config ----
function Get-EnvOr { param([string]$Name, [string]$Default = '')
    $v = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($v)) { return $Default }
    return $v
}
function Get-EnvBool { param([string]$Name, [bool]$Default = $false)
    $v = (Get-EnvOr $Name).Trim().ToLower()
    if ($v -eq '') { return $Default }
    return @('true', '1', 'yes') -contains $v
}
function Split-List { param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return @() }
    return @($Value -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

$LokiUrl     = (Get-EnvOr 'LOKI_URL' 'https://your-loki-host.example.com:3100').TrimEnd('/')
$LokiProject = Get-EnvOr 'LOKI_PROJECT' 'myproject'
# Loki caps a single query_range span (max_query_length). A month can exceed
# it, so the window is fetched in chunks and stitched together.
$ChunkDays   = [int](Get-EnvOr 'LOKI_MAX_QUERY_DAYS' '7')
$VerifyTls   = Get-EnvBool 'LOKI_VERIFY_TLS' $true
# Loki is an internal host, so the corporate proxy should not be in the path.
# Agents differ: some have a proxy configured and return a Squid "Access Denied"
# page instead of reaching it. Set false if your Loki genuinely sits behind one.
$BypassProxy = Get-EnvBool 'BYPASS_PROXY' $true

$SmtpHost = Get-EnvOr 'SMTP_HOST'
$SmtpPort = [int](Get-EnvOr 'SMTP_PORT' '25')
$SmtpTls  = Get-EnvBool 'SMTP_TLS' $false
$SmtpUser = Get-EnvOr 'SMTP_USER'
$SmtpPass = Get-EnvOr 'SMTP_PASS'

$FromAddr = Get-EnvOr 'FROM_ADDR'
# @() at the CALL SITE is required: a single-element array gets unwrapped to a
# scalar on return, and indexing a scalar string yields a [char].
$ToAddrs  = @(Split-List (Get-EnvOr 'TO_ADDRS'))
# A comma-separated list, or ALL to discover every env with logs in the window.
$Envs        = @(Split-List (Get-EnvOr 'ENVS' 'DEV1'))
$EnvsExclude = @(Split-List (Get-EnvOr 'ENVS_EXCLUDE') | ForEach-Object { $_.ToUpper() })
$Products = @(Split-List (Get-EnvOr 'PRODUCTS' 'pc') | ForEach-Object { $_.ToLower() })
$OutDir   = Get-EnvOr 'OUTPUT_DIR' '.'

# Heads the email, subject line and the workbook's Summary sheet.
$ReportTitle  = Get-EnvOr 'REPORT_TITLE' 'Guidewire Login Report'
$FilePrefix   = Get-EnvOr 'REPORT_FILE_PREFIX' 'gw-logins'

# The username must be parsed out of the log line, and that format is
# site-specific. LOGIN_USER_REGEX needs a named group 'user'. For columnar logs
# use a positional pattern, e.g. field 2: ^\s*\S+\s+(?<user>\S+)
# The run prints unmatched samples split into numbered fields to help.
$IncludeUsers  = Get-EnvBool 'INCLUDE_USER_DETAIL' $true
$UserRegex     = Get-EnvOr 'LOGIN_USER_REGEX' '(?i)User\s+Login\s*[:=\-]?\s*(?<user>[A-Za-z0-9._\\@-]+)'
$LogLimit      = [int](Get-EnvOr 'LOKI_LOG_LIMIT' '5000')   # Loki's per-query entry cap
$MaxDetailRows = [int](Get-EnvOr 'MAX_DETAIL_ROWS' '50000')
$ReportTz      = Get-EnvOr 'REPORT_TIMEZONE'                # e.g. 'Eastern Standard Time'; blank = UTC

$tzInfo = $null
if ($ReportTz) {
    try   { $tzInfo = [TimeZoneInfo]::FindSystemTimeZoneById($ReportTz) }
    catch { Write-Host "##vso[task.logissue type=warning]Unknown REPORT_TIMEZONE '$ReportTz' -- timestamps stay in UTC." }
}
$tzLabel = if ($tzInfo) { $ReportTz } else { 'UTC' }

foreach ($req in @{ SMTP_HOST = $SmtpHost; FROM_ADDR = $FromAddr }.GetEnumerator()) {
    if ([string]::IsNullOrWhiteSpace($req.Value)) { throw "$($req.Key) is not set. Add it to the variable group." }
}
if ($ToAddrs.Count -eq 0) { throw "TO_ADDRS is not set. Add it to the variable group." }

# Confirm job names in Grafana -> Explore -> Label browser before using BC/CC/CM.
$ProductMeta = @{
    pc = @{ job = 'pclogs'; frag = 'pc'; label = 'PolicyCenter'   }
    bc = @{ job = 'bclogs'; frag = 'bc'; label = 'BillingCenter'  }
    cc = @{ job = 'cclogs'; frag = 'cc'; label = 'ClaimCenter'    }
    cm = @{ job = 'cmlogs'; frag = 'cm'; label = 'ContactManager' }
}
foreach ($p in $Products) {
    if (-not $ProductMeta.ContainsKey($p)) { throw "Unknown product '$p' in PRODUCTS. Expected any of: pc, bc, cc, cm." }
}

# ---- date window (UTC) ----
# TEST_MONTH=YYYY-MM for a specific month, blank for the current one. The window
# ends at the first of the NEXT month, so a mid-month run reports the month so
# far rather than failing -- which also means it is a partial figure.
$testMonth = Get-EnvOr 'TEST_MONTH'
if ($testMonth) {
    $start = [datetime]::SpecifyKind([datetime]::ParseExact("$testMonth-01", 'yyyy-MM-dd', $null), 'Utc')
    Write-Host "Period source    : TEST_MONTH override"
} else {
    $utcNow = [datetime]::UtcNow
    $start  = [datetime]::SpecifyKind([datetime]::new($utcNow.Year, $utcNow.Month, 1), 'Utc')
    Write-Host "Period source    : current month"
}
$end        = $start.AddMonths(1)
$monthLabel = $start.ToString('MMMM yyyy')
$genStamp   = [datetime]::UtcNow.ToString('yyyy-MM-dd HH:mm') + ' UTC'

Write-Host "Reporting period : $($start.ToString('yyyy-MM-dd')) to $($end.AddDays(-1).ToString('yyyy-MM-dd'))  ($monthLabel)"
Write-Host "Environments     : $($Envs -join ', ')"
Write-Host "Centres          : $($Products -join ', ')"
Write-Host ""

# ---- loki ----
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
if ($BypassProxy) {
    # Windows PowerShell 5.1 has no -NoProxy on Invoke-RestMethod; clearing the
    # default proxy is the equivalent and also covers later .NET web calls.
    [System.Net.WebRequest]::DefaultWebProxy = $null
    Write-Host "Proxy            : bypassed (BYPASS_PROXY=true)"
} else {
    Write-Host "Proxy            : system default"
}
if (-not $VerifyTls) {
    Write-Host "LOKI_VERIFY_TLS is false -- certificate validation disabled for this run."
    if (-not ('TrustAllCertsPolicy' -as [type])) {
        Add-Type @'
using System.Net;
using System.Security.Cryptography.X509Certificates;
public class TrustAllCertsPolicy : ICertificatePolicy {
    public bool CheckValidationResult(ServicePoint sp, X509Certificate cert, WebRequest req, int problem) { return true; }
}
'@
    }
    [Net.ServicePointManager]::CertificatePolicy = New-Object TrustAllCertsPolicy
}

# Single-quoted so the backticks around `User Login` stay literal; {{ }} escape
# the LogQL braces for the -f operator.
$QueryTemplate = 'sum(count_over_time({{project="{0}", job="{1}", env="{2}", filename=~".*{3}.log"}} |= `User Login` [1d]))'

function Get-UnixNanos { param([datetime]$T)
    return [long]([DateTimeOffset]::new($T).ToUnixTimeSeconds()) * 1000000000L
}

# Same selector without the aggregation, so the raw lines come back.
$LogSelectorTemplate = '{{project="{0}", job="{1}", env="{2}", filename=~".*{3}.log"}} |= `User Login`'

function ConvertFrom-UnixNanos { param([long]$Nanos)
    $utc = [DateTimeOffset]::FromUnixTimeMilliseconds([long]($Nanos / 1000000)).UtcDateTime
    if ($tzInfo) { return [TimeZoneInfo]::ConvertTimeFromUtc($utc, $tzInfo) }
    return $utc
}

function Get-LoginEvents {
    param([string]$EnvLabel, [string]$Product)
    $meta   = $ProductMeta[$Product]
    $sel    = $LogSelectorTemplate -f $LokiProject, $meta.job, $EnvLabel, $meta.frag
    $uri    = "$LokiUrl/loki/api/v1/query_range"
    $events = New-Object System.Collections.ArrayList
    $samples = New-Object System.Collections.ArrayList
    $unparsed = 0
    $hitLimit = $false

    $chunkFrom = $start
    while ($chunkFrom -lt $end) {
        $chunkTo = $chunkFrom.AddDays($ChunkDays)
        if ($chunkTo -gt $end) { $chunkTo = $end }

        $body = @{
            query     = $sel
            start     = (Get-UnixNanos $chunkFrom).ToString()
            end       = (Get-UnixNanos $chunkTo).ToString()
            limit     = $LogLimit
            direction = 'forward'
        }
        try {
            $resp = Invoke-RestMethod -Uri $uri -Method Get -Body $body -TimeoutSec 180
        } catch {
            throw "Loki log query failed for $EnvLabel/$Product ($($chunkFrom.ToString('yyyy-MM-dd')) to $($chunkTo.ToString('yyyy-MM-dd'))): $_`nSelector: $sel"
        }

        $inChunk = 0
        foreach ($stream in $resp.data.result) {
            foreach ($entry in $stream.values) {
                $inChunk++
                $line = [string]$entry[1]
                $when = ConvertFrom-UnixNanos ([long]$entry[0])

                $user = ''
                $m = [regex]::Match($line, $UserRegex)
                if ($m.Success -and $m.Groups['user'].Success) {
                    $user = $m.Groups['user'].Value
                } else {
                    $unparsed++
                    if ($samples.Count -lt 3) { [void]$samples.Add($line) }
                }
                [void]$events.Add([pscustomobject]@{ When = $when; User = $user; Line = $line })
            }
        }
        # Loki caps entries per query; hitting it means this chunk was truncated.
        if ($inChunk -ge $LogLimit) { $hitLimit = $true }
        $chunkFrom = $chunkTo
    }

    if ($hitLimit) {
        Write-Host "##vso[task.logissue type=warning]$EnvLabel/$Product hit Loki's $LogLimit-entry cap in at least one chunk -- the user detail is incomplete. Lower LOKI_MAX_QUERY_DAYS to fetch smaller windows."
    }
    if ($unparsed -gt 0) {
        Write-Host "##vso[task.logissue type=warning]$EnvLabel/$Product : $unparsed line(s) did not match LOGIN_USER_REGEX -- those users are blank."
        Write-Host "  Sample lines that did not match, with their whitespace-delimited fields:"
        foreach ($smp in $samples) {
            Write-Host "    $smp"
            $fields = @($smp -split '\s+' | Where-Object { $_ })
            $show   = [Math]::Min(5, $fields.Count)
            $hint   = @()
            for ($i = 0; $i -lt $show; $i++) { $hint += "[$($i + 1)] $($fields[$i])" }
            Write-Host "      fields: $($hint -join '   ')"
        }
        Write-Host "  Set LOGIN_USER_REGEX in the variable group to one of these:"
        Write-Host "    username is field 2 ->  ^\s*\S+\s+(?<user>\S+)"
        Write-Host "    username is field 3 ->  ^\s*(\S+\s+){2}(?<user>\S+)"
        Write-Host "    username is field N ->  ^\s*(\S+\s+){N-1}(?<user>\S+)" 
    }
    return $events
}

function Get-DailyCounts {
    param([string]$EnvLabel, [string]$Product)
    $meta  = $ProductMeta[$Product]
    $query = $QueryTemplate -f $LokiProject, $meta.job, $EnvLabel, $meta.frag
    $uri   = "$LokiUrl/loki/api/v1/query_range"
    $daily = @{}

    # Each sample covers the PRECEDING range: the one stamped 02 Jul with [1d]
    # counts 01 Jul. So query from start+1d and label samples timestamp-1d, or
    # every figure lands a day early.
    $queryStart = $start.AddDays(1)
    $chunkFrom  = $queryStart
    $chunks     = 0

    while ($chunkFrom -lt $end) {
        $chunkTo = $chunkFrom.AddDays($ChunkDays)
        if ($chunkTo -gt $end) { $chunkTo = $end }
        $chunks++

        $body = @{
            query = $query
            start = (Get-UnixNanos $chunkFrom).ToString()
            end   = (Get-UnixNanos $chunkTo).ToString()
            step  = '1d'
        }
        try {
            $resp = Invoke-RestMethod -Uri $uri -Method Get -Body $body -TimeoutSec 120
        } catch {
            $detail = $_.ToString()
            if ($detail -match 'exceeds the limit') {
                throw "Loki rejected the query window for $EnvLabel/$Product. Lower LOKI_MAX_QUERY_DAYS (currently $ChunkDays).`nLoki said: $detail"
            }
            throw "Loki query failed for $EnvLabel/$Product ($($chunkFrom.ToString('yyyy-MM-dd')) to $($chunkTo.ToString('yyyy-MM-dd'))):`n$(Format-LokiError $detail $uri)`nQuery: $query"
        }

        $result = $resp.data.result
        if ($result -and $result.Count -gt 0) {
            foreach ($pair in $result[0].values) {
                $sampleAt = [DateTimeOffset]::FromUnixTimeSeconds([long][double]$pair[0]).UtcDateTime
                $day      = $sampleAt.AddDays(-1).Date          # the day the sample covers
                if ($day -ge $start.Date -and $day -lt $end.Date) {
                    $daily[$day] = [int][double]$pair[1]        # assign, so chunk overlaps cannot double count
                }
            }
        }
        $chunkFrom = $chunkTo
    }
    Write-Verbose "  ($chunks chunk(s) of up to $ChunkDays days)"
    return $daily
}

# ---- environment discovery ----
function Format-LokiError {
    param([string]$Detail, [string]$Uri)
    if ($Detail -match 'squid|Access Denied|cache administrator|could not be retrieved') {
        return @"
A proxy refused the request to $Uri.

The agent is routing internal traffic through the corporate proxy. Either:
  * leave BYPASS_PROXY=true (the default) so the proxy is skipped, or
  * pin this pipeline to an agent that reaches Loki directly.

Proxy response: $Detail
"@
    }
    return $Detail
}

function Get-DiscoveredEnvs {
    # Chunked like the data queries -- this endpoint has the same length limit.
    # Windows are unioned, so a briefly-active env is still found.
    $uri   = "$LokiUrl/loki/api/v1/label/env/values"
    $job   = $ProductMeta[$Products[0]].job
    $found = New-Object 'System.Collections.Generic.HashSet[string]'

    $chunkFrom = $start
    while ($chunkFrom -lt $end) {
        $chunkTo = $chunkFrom.AddDays($ChunkDays)
        if ($chunkTo -gt $end) { $chunkTo = $end }

        $body = @{
            start = (Get-UnixNanos $chunkFrom).ToString()
            end   = (Get-UnixNanos $chunkTo).ToString()
            # Scoping by selector needs Loki 2.8+. Older versions ignore it and
            # return every env label, which the empty-environment drop cleans up.
            query = ('{{project="{0}", job="{1}"}}' -f $LokiProject, $job)
        }
        try {
            $resp = Invoke-RestMethod -Uri $uri -Method Get -Body $body -TimeoutSec 60
        } catch {
            $detail = $_.ToString()
            if ($detail -match 'exceeds the limit') {
                throw "Loki rejected the discovery window. Lower LOKI_MAX_QUERY_DAYS (currently $ChunkDays).`nLoki said: $detail"
            }
            throw "Could not discover environments from Loki ($uri):`n$(Format-LokiError $detail $uri)"
        }
        foreach ($v in @($resp.data)) {
            if ($v) { [void]$found.Add([string]$v) }
        }
        $chunkFrom = $chunkTo
    }
    return @($found)
}

function Sort-EnvNatural { param([string[]]$Names)
    # Plain alphabetical puts QA10 before QA2; sort on the letter prefix, then
    # the numeric suffix.
    return @($Names | Sort-Object `
        @{ Expression = { ($_ -replace '\d', '') } }, `
        @{ Expression = { $d = ($_ -replace '\D', ''); if ($d) { [int]$d } else { 0 } } })
}

$discovered = $false
if ($Envs.Count -eq 1 -and ([string]$Envs[0]).ToUpper() -eq 'ALL') {
    $discovered = $true
    $found = @(Get-DiscoveredEnvs)
    if ($found.Count -eq 0) {
        throw "ENVS=ALL found no 'env' label values in Loki for the reporting window. Check LOKI_PROJECT and the job label."
    }
    $Envs = @(Sort-EnvNatural @($found | Where-Object { $EnvsExclude -notcontains ([string]$_).ToUpper() }))
    Write-Host "Discovered envs  : $($found.Count) found, $($Envs.Count) after exclusions"
    Write-Host "                   $($Envs -join ', ')"
    Write-Host ""
}

$data = @{}
foreach ($e in $Envs) {
    foreach ($p in $Products) {
        $daily = Get-DailyCounts -EnvLabel $e -Product $p
        $total = ($daily.Values | Measure-Object -Sum).Sum
        if ($null -eq $total) { $total = 0 }
        $events = @()
        if ($IncludeUsers) { $events = @(Get-LoginEvents -EnvLabel $e -Product $p) }
        $data["$e|$p"] = @{ daily = $daily; total = [int]$total; events = $events }
        $distinct = @($events | Where-Object { $_.User } | Select-Object -ExpandProperty User -Unique).Count
        if ($IncludeUsers) {
            Write-Host ("  {0,-8} {1,-4} {2,8:N0} logins   {3,5:N0} distinct users" -f $e, $p.ToUpper(), $total, $distinct)
        } else {
            Write-Host ("  {0,-8} {1,-4} {2,8:N0} logins" -f $e, $p.ToUpper(), $total)
        }
    }
}
Write-Host ""

if ($discovered) {
    # Discovery returns every env with any log line. Drop the idle ones --
    # an explicitly listed env keeps its zero row, a discovered one does not.
    $keep = @()
    foreach ($e in $Envs) {
        $sum = 0
        foreach ($p in $Products) { $sum += $data["$e|$p"].total }
        if ($sum -gt 0) { $keep += $e }
    }
    $dropped = $Envs.Count - $keep.Count
    if ($dropped -gt 0) {
        Write-Host "Omitted $dropped discovered env(s) with no logins in this period."
    }
    if ($keep.Count -eq 0) {
        throw "None of the $($Envs.Count) discovered environments had any logins in this period."
    }
    $Envs = @($keep)
    Write-Host ""
}

# ---- xlsx writer: OOXML by hand, so no module or Excel install is needed ----
Add-Type -AssemblyName System.IO.Compression | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem | Out-Null

function ConvertTo-ColumnName { param([int]$Index)
    $name = ''
    while ($Index -gt 0) {
        $rem = ($Index - 1) % 26
        $name = [char](65 + $rem) + $name
        $Index = [int](($Index - $rem - 1) / 26)
    }
    return $name
}

function ConvertTo-XmlText { param([string]$Text)
    return ($Text -replace '&', '&amp;' -replace '<', '&lt;' -replace '>', '&gt;' -replace '"', '&quot;')
}

# Styles: 0 normal, 1 bold, 2 number, 3 header, 4 date, 5 date+time.
function New-SheetXml {
    param([object[]]$Rows, [int]$HeaderRow = -1, [string]$DateStyle = '4')
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
    [void]$sb.Append('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>')
    for ($r = 0; $r -lt $Rows.Count; $r++) {
        $cells = @($Rows[$r])
        [void]$sb.Append("<row r=`"$($r + 1)`">")
        for ($c = 0; $c -lt $cells.Count; $c++) {
            $v = $cells[$c]
            if ($null -eq $v) { continue }
            $ref = (ConvertTo-ColumnName ($c + 1)) + ($r + 1)
            if ($v -is [int] -or $v -is [long] -or $v -is [double] -or $v -is [decimal]) {
                [void]$sb.Append("<c r=`"$ref`" s=`"2`"><v>$v</v></c>")
            } elseif ($v -is [datetime]) {
                # TotalDays: the fraction carries the time of day.
                $serial = ($v - [datetime]'1899-12-30').TotalDays
                [void]$sb.Append("<c r=`"$ref`" s=`"$DateStyle`"><v>$serial</v></c>")
            } else {
                $style = if ($r -eq $HeaderRow) { '3' } else { '0' }
                $text  = ConvertTo-XmlText ([string]$v)
                [void]$sb.Append("<c r=`"$ref`" t=`"inlineStr`" s=`"$style`"><is><t xml:space=`"preserve`">$text</t></is></c>")
            }
        }
        [void]$sb.Append('</row>')
    }
    [void]$sb.Append('</sheetData></worksheet>')
    return $sb.ToString()
}

function New-XlsxFile {
    param([string]$Path, [object[]]$Sheets)   # @{ Name; Rows; HeaderRow; DateStyle }

    if (Test-Path $Path) { Remove-Item $Path -Force }
    $zip = [System.IO.Compression.ZipFile]::Open($Path, 'Create')
    try {
        function Add-Part { param($Zip, [string]$Name, [string]$Content)
            $entry  = $Zip.CreateEntry($Name)
            $writer = New-Object System.IO.StreamWriter($entry.Open(), (New-Object System.Text.UTF8Encoding($false)))
            $writer.Write($Content); $writer.Flush(); $writer.Dispose()
        }

        $overrides = ''
        $sheetRefs = ''
        $relEntries = ''
        for ($i = 0; $i -lt $Sheets.Count; $i++) {
            $n = $i + 1
            $overrides  += "<Override PartName=`"/xl/worksheets/sheet$n.xml`" ContentType=`"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml`"/>"
            $sheetRefs  += "<sheet name=`"$(ConvertTo-XmlText $Sheets[$i].Name)`" sheetId=`"$n`" r:id=`"rId$n`"/>"
            $relEntries += "<Relationship Id=`"rId$n`" Type=`"http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet`" Target=`"worksheets/sheet$n.xml`"/>"
        }
        $styleRelId = $Sheets.Count + 1

        Add-Part $zip '[Content_Types].xml' (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
            '<Default Extension="xml" ContentType="application/xml"/>' +
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' +
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' +
            $overrides + '</Types>')

        Add-Part $zip '_rels/.rels' (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>' +
            '</Relationships>')

        Add-Part $zip 'xl/workbook.xml' (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" ' +
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">' +
            "<sheets>$sheetRefs</sheets></workbook>")

        Add-Part $zip 'xl/_rels/workbook.xml.rels' (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
            $relEntries +
            "<Relationship Id=`"rId$styleRelId`" Type=`"http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles`" Target=`"styles.xml`"/>" +
            '</Relationships>')

        # numFmtId 3 = #,##0, 14 = date, 22 = date+time (all built in).
        Add-Part $zip 'xl/styles.xml' (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' +
            '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>' +
            '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts>' +
            '<fills count="3"><fill><patternFill patternType="none"/></fill>' +
            '<fill><patternFill patternType="gray125"/></fill>' +
            '<fill><patternFill patternType="solid"><fgColor rgb="FF305496"/><bgColor indexed="64"/></patternFill></fill></fills>' +
            '<borders count="1"><border/></borders>' +
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>' +
            '<cellXfs count="6">' +
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>' +
            '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>' +
            '<xf numFmtId="3" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>' +
            '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>' +
            '<xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>' +
            '<xf numFmtId="22" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>' +
            '</cellXfs>' +
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>' +
            '</styleSheet>')

        for ($i = 0; $i -lt $Sheets.Count; $i++) {
            $hdr = if ($null -ne $Sheets[$i].HeaderRow) { $Sheets[$i].HeaderRow } else { -1 }
            $ds  = if ($Sheets[$i].DateStyle) { $Sheets[$i].DateStyle } else { '4' }
            Add-Part $zip "xl/worksheets/sheet$($i + 1).xml" (New-SheetXml -Rows $Sheets[$i].Rows -HeaderRow $hdr -DateStyle $ds)
        }
    } finally {
        $zip.Dispose()
    }
}

# ---- workbook ----
$summaryRows = New-Object System.Collections.ArrayList
[void]$summaryRows.Add(@($ReportTitle))
[void]$summaryRows.Add(@("Period: $($start.ToString('yyyy-MM-dd')) to $($end.AddDays(-1).ToString('yyyy-MM-dd'))  ($monthLabel)"))
[void]$summaryRows.Add(@("Source: Loki $LokiUrl  |  Match: |= ""User Login""  |  Generated: $genStamp"))
[void]$summaryRows.Add(@(''))
[void]$summaryRows.Add(@('Environment', 'Centre', 'Total Logins'))
$headerRowIndex = $summaryRows.Count - 1

$grand = 0
foreach ($e in $Envs) {
    foreach ($p in $Products) {
        $t = $data["$e|$p"].total
        $grand += $t
        [void]$summaryRows.Add(@($e, $ProductMeta[$p].label, [int]$t))
    }
}
[void]$summaryRows.Add(@('TOTAL', '', [int]$grand))

# Daily sheet: one row per day, one column per env/centre
$combos = @()
foreach ($e in $Envs) { foreach ($p in $Products) { $combos += ,@($e, $p) } }

$allDates = @()
foreach ($k in $data.Keys) { $allDates += $data[$k].daily.Keys }
$allDates = @($allDates | Sort-Object -Unique)

$dailyRows = New-Object System.Collections.ArrayList
[void]$dailyRows.Add(@('Daily Login Counts'))
[void]$dailyRows.Add(@(''))
$hdr = @('Date')
foreach ($cb in $combos) {
    $hdr += $(if ($Products.Count -eq 1) { [string]$cb[0] } else { "$([string]$cb[0])/$(([string]$cb[1]).ToUpper())" })
}
[void]$dailyRows.Add($hdr)
$dailyHeaderIndex = $dailyRows.Count - 1

foreach ($d in $allDates) {
    $row = @($d)
    foreach ($cb in $combos) {
        $v = $data["$($cb[0])|$($cb[1])"].daily[$d]
        $row += $(if ($null -ne $v) { [int]$v } else { [int]0 })
    }
    [void]$dailyRows.Add($row)
}

# One row per user per env/centre, busiest first.
$userRows = New-Object System.Collections.ArrayList
$detailRows = New-Object System.Collections.ArrayList
$userHeaderIndex = -1
$detailHeaderIndex = -1

if ($IncludeUsers) {
    [void]$userRows.Add(@('Logins by User'))
    [void]$userRows.Add(@("$monthLabel   |   timestamps in $tzLabel"))
    [void]$userRows.Add(@(''))
    [void]$userRows.Add(@('User', 'Environment', 'Centre', 'Logins', 'First Login', 'Last Login'))
    $userHeaderIndex = $userRows.Count - 1

    foreach ($e in $Envs) {
        foreach ($p in $Products) {
            $evts = $data["$e|$p"].events
            $grouped = $evts | Where-Object { $_.User } | Group-Object -Property User |
                       Sort-Object -Property @{ Expression = { $_.Count }; Descending = $true }, Name
            foreach ($g in $grouped) {
                $times = $g.Group | Select-Object -ExpandProperty When | Sort-Object
                [void]$userRows.Add(@($g.Name, $e, $ProductMeta[$p].label, [int]$g.Count, $times[0], $times[-1]))
            }
            $blank = @($evts | Where-Object { -not $_.User }).Count
            if ($blank -gt 0) {
                [void]$userRows.Add(@('(unparsed)', $e, $ProductMeta[$p].label, [int]$blank, $null, $null))
            }
        }
    }
    if ($userRows.Count -eq ($userHeaderIndex + 1)) { [void]$userRows.Add(@('(no login events found)')) }

    # Detail sheet: every login event.
    [void]$detailRows.Add(@('Login Detail'))
    [void]$detailRows.Add(@("$monthLabel   |   timestamps in $tzLabel"))
    [void]$detailRows.Add(@(''))
    [void]$detailRows.Add(@('Timestamp', 'Environment', 'Centre', 'User'))
    $detailHeaderIndex = $detailRows.Count - 1

    $all = @()
    foreach ($e in $Envs) {
        foreach ($p in $Products) {
            foreach ($ev in $data["$e|$p"].events) {
                $all += [pscustomobject]@{ When = $ev.When; Env = $e; Centre = $ProductMeta[$p].label; User = $ev.User }
            }
        }
    }
    $all = @($all | Sort-Object When)
    if ($all.Count -gt $MaxDetailRows) {
        Write-Host "##vso[task.logissue type=warning]Login detail truncated to $MaxDetailRows of $($all.Count) rows (MAX_DETAIL_ROWS)."
        $all = @($all[0..($MaxDetailRows - 1)])
    }
    foreach ($row in $all) {
        [void]$detailRows.Add(@($row.When, $row.Env, $row.Centre, $(if ($row.User) { $row.User } else { '(unparsed)' })))
    }
    if ($all.Count -eq 0) { [void]$detailRows.Add(@('(no login events found)')) }
    Write-Host "Detail rows      : $($all.Count)"
}

$fileName = "$FilePrefix-$($start.ToString('yyyy-MM')).xlsx"
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$xlsxPath = Join-Path $OutDir $fileName

$sheetSpecs = @(
    @{ Name = 'Summary'; Rows = $summaryRows.ToArray(); HeaderRow = $headerRowIndex },
    @{ Name = 'Daily';   Rows = $dailyRows.ToArray();   HeaderRow = $dailyHeaderIndex }
)
if ($IncludeUsers) {
    $sheetSpecs += @{ Name = 'Users';  Rows = $userRows.ToArray();   HeaderRow = $userHeaderIndex;   DateStyle = '5' }
    $sheetSpecs += @{ Name = 'Detail'; Rows = $detailRows.ToArray(); HeaderRow = $detailHeaderIndex; DateStyle = '5' }
}
New-XlsxFile -Path $xlsxPath -Sheets $sheetSpecs
Write-Host "Wrote $xlsxPath ($([math]::Round((Get-Item $xlsxPath).Length / 1KB, 1)) KB)"

# ---- email ----
# A set, not a sum: someone active in two envs counts once.
$allUsers    = New-Object 'System.Collections.Generic.HashSet[string]'
$grandLogins = 0
$rowsHtml    = ''
$rowIndex    = 0

foreach ($e in $Envs) {
    foreach ($p in $Products) {
        $d = $data["$e|$p"]
        $grandLogins += $d.total

        if ($IncludeUsers) {
            $users = @($d.events | Where-Object { $_.User } | Select-Object -ExpandProperty User -Unique)
            foreach ($u in $users) { [void]$allUsers.Add($u) }
            $userCell = '{0:N0}' -f $users.Count
        } else {
            $userCell = '&mdash;'
        }

        $bg = if ($rowIndex % 2 -eq 1) { ' background:#f7f9fc;' } else { '' }
        $rowsHtml += (
            "<tr style=`"$bg`">" +
            "<td style=`"padding:7px 12px;border-bottom:1px solid #e4e8ee;`">$e</td>" +
            "<td style=`"padding:7px 12px;border-bottom:1px solid #e4e8ee;`">$($ProductMeta[$p].label)</td>" +
            "<td style=`"padding:7px 12px;border-bottom:1px solid #e4e8ee;text-align:right;`">$('{0:N0}' -f $d.total)</td>" +
            "<td style=`"padding:7px 12px;border-bottom:1px solid #e4e8ee;text-align:right;`">$userCell</td>" +
            "</tr>")
        $rowIndex++
    }
}

$grandUserCell = if ($IncludeUsers) { '{0:N0}' -f $allUsers.Count } else { '&mdash;' }
$totalRow =
    "<tr style=`"font-weight:bold;background:#eef2f8;`">" +
    "<td style=`"padding:8px 12px;border-top:2px solid #305496;`" colspan=`"2`">TOTAL</td>" +
    "<td style=`"padding:8px 12px;border-top:2px solid #305496;text-align:right;`">$('{0:N0}' -f $grandLogins)</td>" +
    "<td style=`"padding:8px 12px;border-top:2px solid #305496;text-align:right;`">$grandUserCell</td>" +
    "</tr>"


$topHtml = ''
if ($IncludeUsers) {
    $everyEvent = @()
    foreach ($e in $Envs) { foreach ($p in $Products) { $everyEvent += $data["$e|$p"].events } }
    $top = @($everyEvent | Where-Object { $_.User } | Group-Object -Property User |
             Sort-Object -Property @{ Expression = { $_.Count }; Descending = $true }, Name |
             Select-Object -First 5)
    if ($top.Count -gt 0) {
        $lis = ($top | ForEach-Object {
            "<li style=`"margin:2px 0;`"><b>$($_.Name)</b> &mdash; $('{0:N0}' -f $_.Count) logins</li>"
        }) -join ''
        $topHtml =
            "<p style=`"margin:22px 0 6px;font-size:13px;color:#333;`"><b>Busiest users</b></p>" +
            "<ol style=`"margin:0;padding-left:22px;font-size:13px;color:#333;`">$lis</ol>"
    }
}

$periodText = "$($start.ToString('d MMM yyyy')) &ndash; $($end.AddDays(-1).ToString('d MMM yyyy'))"
$sheetNote  = if ($IncludeUsers) { 'Summary, Daily, Users and Detail sheets' } else { 'Summary and Daily sheets' }

$html = @"
<html><body style="margin:0;padding:0;background:#ffffff;">
<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;max-width:720px;padding:4px 2px;">

  <h2 style="margin:0 0 2px;font-size:19px;color:#1f3864;">$ReportTitle</h2>
  <p style="margin:0 0 18px;font-size:13px;color:#666;">
    $monthLabel &nbsp;&middot;&nbsp; $periodText &nbsp;&middot;&nbsp; times in $tzLabel
  </p>

  <table cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:13px;color:#222;min-width:460px;">
    <thead>
      <tr style="background:#305496;color:#ffffff;text-align:left;">
        <th style="padding:8px 12px;font-weight:600;">Environment</th>
        <th style="padding:8px 12px;font-weight:600;">Centre</th>
        <th style="padding:8px 12px;font-weight:600;text-align:right;">Logins</th>
        <th style="padding:8px 12px;font-weight:600;text-align:right;">Distinct users</th>
      </tr>
    </thead>
    <tbody>
      $rowsHtml
      $totalRow
    </tbody>
  </table>

  <p style="margin:8px 0 0;font-size:11px;color:#888;">
    Distinct users are counted once across the whole report, so the total is not the sum of the column.
  </p>

  $topHtml

  <p style="margin:22px 0 4px;font-size:13px;color:#333;">
    Full detail is in the attached workbook ($sheetNote).
  </p>

  <p style="margin:18px 0 0;padding-top:10px;border-top:1px solid #e4e8ee;font-size:11px;color:#999;">
    Source: Loki at $LokiUrl &nbsp;&middot;&nbsp; match <code>|= "User Login"</code><br/>
    Generated $genStamp &nbsp;&middot;&nbsp; automated, do not reply
  </p>

</div></body></html>
"@

$msg = New-Object System.Net.Mail.MailMessage
$msg.From = New-Object System.Net.Mail.MailAddress($FromAddr)
foreach ($t in $ToAddrs) { $msg.To.Add($t) }
$msg.Subject    = "$ReportTitle - $monthLabel"
$msg.IsBodyHtml = $true
$msg.Body       = $html
$attachment     = New-Object System.Net.Mail.Attachment($xlsxPath)
$msg.Attachments.Add($attachment)

$smtp = New-Object System.Net.Mail.SmtpClient($SmtpHost, $SmtpPort)
$smtp.EnableSsl = $SmtpTls
if ($SmtpUser) { $smtp.Credentials = New-Object System.Net.NetworkCredential($SmtpUser, $SmtpPass) }

try {
    $smtp.Send($msg)
    Write-Host "Sent '$fileName' to $($ToAddrs.Count) recipient(s): $($ToAddrs -join ', ')"
} catch {
    throw "SMTP send failed via $SmtpHost`:$SmtpPort -- $_`nIf this is 'relay denied', the agent's IP is probably not allowlisted on the relay."
} finally {
    $attachment.Dispose()
    $msg.Dispose()
    $smtp.Dispose()
}
