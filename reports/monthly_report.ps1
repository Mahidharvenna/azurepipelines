#Requires -Version 5.1
<#
    monthly_report.ps1

    Monthly Guidewire login report:  Loki  ->  Excel  ->  email.

    Pure PowerShell. No Python, no pip, no PSGallery module, and no Excel
    install on the agent -- an .xlsx is just a zip of OOXML parts, which .NET
    can write directly. That matters on a locked-down build agent where
    installing a runtime or reaching the PowerShell Gallery may not be an option.

    All configuration comes from environment variables, supplied by the
    pipeline's variable group. Nothing site-specific is hard-coded.
#>

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# CONFIG (from environment)
# ---------------------------------------------------------------------------
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
$VerifyTls   = Get-EnvBool 'LOKI_VERIFY_TLS' $true

$SmtpHost = Get-EnvOr 'SMTP_HOST'
$SmtpPort = [int](Get-EnvOr 'SMTP_PORT' '25')
$SmtpTls  = Get-EnvBool 'SMTP_TLS' $false
$SmtpUser = Get-EnvOr 'SMTP_USER'
$SmtpPass = Get-EnvOr 'SMTP_PASS'

$FromAddr = Get-EnvOr 'FROM_ADDR'
$ToAddrs  = Split-List (Get-EnvOr 'TO_ADDRS')
$Envs     = Split-List (Get-EnvOr 'ENVS' 'DEV1')
$Products = @(Split-List (Get-EnvOr 'PRODUCTS' 'pc') | ForEach-Object { $_.ToLower() })
$OutDir   = Get-EnvOr 'OUTPUT_DIR' '.'

foreach ($req in @{ SMTP_HOST = $SmtpHost; FROM_ADDR = $FromAddr }.GetEnumerator()) {
    if ([string]::IsNullOrWhiteSpace($req.Value)) { throw "$($req.Key) is not set. Add it to the variable group." }
}
if ($ToAddrs.Count -eq 0) { throw "TO_ADDRS is not set. Add it to the variable group." }

# job label + filename fragment per centre. Confirm these against
# Grafana -> Explore -> Label browser -> job before enabling BC/CC/CM.
$ProductMeta = @{
    pc = @{ job = 'pclogs'; frag = 'pc'; label = 'PolicyCenter'   }
    bc = @{ job = 'bclogs'; frag = 'bc'; label = 'BillingCenter'  }
    cc = @{ job = 'cclogs'; frag = 'cc'; label = 'ClaimCenter'    }
    cm = @{ job = 'cmlogs'; frag = 'cm'; label = 'ContactManager' }
}
foreach ($p in $Products) {
    if (-not $ProductMeta.ContainsKey($p)) { throw "Unknown product '$p' in PRODUCTS. Expected any of: pc, bc, cc, cm." }
}

# ---------------------------------------------------------------------------
# DATE WINDOW (UTC)
#
#   TEST_MONTH=YYYY-MM  -> exactly that month (back-fill / re-issue)
#   blank               -> the CURRENT month
#
# Blank is the normal case, including every scheduled run. Scheduling is managed
# in the ADO UI, so running on the last day of the month reports that month.
#
# Note the window always ends at the first of the NEXT month, so a run partway
# through reports the month so far rather than failing -- useful for a mid-month
# spot check, but it does mean a run before month-end is a partial figure.
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# LOKI
# ---------------------------------------------------------------------------
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
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

function Get-DailyCounts {
    param([string]$EnvLabel, [string]$Product)
    $meta  = $ProductMeta[$Product]
    $query = $QueryTemplate -f $LokiProject, $meta.job, $EnvLabel, $meta.frag
    $uri   = "$LokiUrl/loki/api/v1/query_range"
    $body  = @{
        query = $query
        start = (Get-UnixNanos $start).ToString()
        end   = (Get-UnixNanos $end).ToString()
        step  = '1d'
    }
    try {
        $resp = Invoke-RestMethod -Uri $uri -Method Get -Body $body -TimeoutSec 120
    } catch {
        throw "Loki query failed for $EnvLabel/$Product : $_`nQuery: $query"
    }
    $result = $resp.data.result
    $daily  = @{}
    if ($result -and $result.Count -gt 0) {
        foreach ($pair in $result[0].values) {
            $day = [DateTimeOffset]::FromUnixTimeSeconds([long][double]$pair[0]).UtcDateTime.Date
            $daily[$day] = [int][double]$pair[1]
        }
    }
    return $daily
}

$data = @{}
foreach ($e in $Envs) {
    foreach ($p in $Products) {
        $daily = Get-DailyCounts -EnvLabel $e -Product $p
        $total = ($daily.Values | Measure-Object -Sum).Sum
        if ($null -eq $total) { $total = 0 }
        $data["$e|$p"] = @{ daily = $daily; total = [int]$total }
        Write-Host ("  {0,-8} {1,-4} {2,8:N0} logins" -f $e, $p.ToUpper(), $total)
    }
}
Write-Host ""

# ---------------------------------------------------------------------------
# XLSX WRITER -- OOXML by hand, so no module or Excel install is required
# ---------------------------------------------------------------------------
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

# Style ids defined in styles.xml below: 0 normal, 1 bold, 2 number (#,##0),
# 3 bold on a fill (header), 4 date.
function New-SheetXml {
    param([object[]]$Rows, [int]$HeaderRow = -1)
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
                $serial = ($v - [datetime]'1899-12-30').Days
                [void]$sb.Append("<c r=`"$ref`" s=`"4`"><v>$serial</v></c>")
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
    param([string]$Path, [object[]]$Sheets)   # each: @{ Name; Rows; HeaderRow }

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

        # numFmtId 3 = #,##0 ; 14 = short date. Both are built in.
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
            '<cellXfs count="5">' +
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>' +
            '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>' +
            '<xf numFmtId="3" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>' +
            '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>' +
            '<xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>' +
            '</cellXfs>' +
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>' +
            '</styleSheet>')

        for ($i = 0; $i -lt $Sheets.Count; $i++) {
            $hdr = if ($null -ne $Sheets[$i].HeaderRow) { $Sheets[$i].HeaderRow } else { -1 }
            Add-Part $zip "xl/worksheets/sheet$($i + 1).xml" (New-SheetXml -Rows $Sheets[$i].Rows -HeaderRow $hdr)
        }
    } finally {
        $zip.Dispose()
    }
}

# ---------------------------------------------------------------------------
# BUILD THE WORKBOOK
# ---------------------------------------------------------------------------
$summaryRows = New-Object System.Collections.ArrayList
[void]$summaryRows.Add(@('Guidewire Login Report'))
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
    $hdr += $(if ($Products.Count -eq 1) { $cb[0] } else { "$($cb[0])/$($cb[1].ToUpper())" })
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

$fileName = "gw-logins-$($start.ToString('yyyy-MM')).xlsx"
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$xlsxPath = Join-Path $OutDir $fileName

New-XlsxFile -Path $xlsxPath -Sheets @(
    @{ Name = 'Summary'; Rows = $summaryRows.ToArray(); HeaderRow = $headerRowIndex },
    @{ Name = 'Daily';   Rows = $dailyRows.ToArray();   HeaderRow = $dailyHeaderIndex }
)
Write-Host "Wrote $xlsxPath ($([math]::Round((Get-Item $xlsxPath).Length / 1KB, 1)) KB)"

# ---------------------------------------------------------------------------
# EMAIL
# ---------------------------------------------------------------------------
$items = ''
foreach ($e in $Envs) {
    foreach ($p in $Products) {
        $items += "<li><b>$e $($ProductMeta[$p].label)</b>: {0:N0} logins</li>" -f $data["$e|$p"].total
    }
}
$html = @"
<html><body style="font-family:sans-serif">
<p>Hi team,</p>
<p>Attached is the Guidewire login report for <b>$monthLabel</b>.</p>
<ul>$items</ul>
<p style="color:#888;font-size:0.85em">
Source: Loki at $LokiUrl. Match: <code>|= "User Login"</code>.
Generated $genStamp (automated).
</p>
</body></html>
"@

$msg = New-Object System.Net.Mail.MailMessage
$msg.From = New-Object System.Net.Mail.MailAddress($FromAddr)
foreach ($t in $ToAddrs) { $msg.To.Add($t) }
$msg.Subject    = "Guidewire Login Report - $monthLabel"
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
