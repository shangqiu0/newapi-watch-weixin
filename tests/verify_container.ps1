param([string]$Image = 'newapi-watch:local')
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$artifacts = Join-Path $root 'artifacts'
New-Item -ItemType Directory -Force -Path $artifacts | Out-Null
$id = [Guid]::NewGuid().ToString('N')
$container = "newapi-watch-check-$id"
$volume = "newapi-watch-check-$id"
$key = [Guid]::NewGuid().ToString('N') + [Guid]::NewGuid().ToString('N')
$createdVolume = $false
$createdContainer = $false
$result = [ordered]@{ image = $Image; container = $container; passed = $false }

function Invoke-Docker {
    param([string[]]$Arguments)
    $output = & docker @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed (exit $LASTEXITCODE): $($output | Out-String)"
    }
    return $output
}

function Wait-Ready {
    param([string]$BaseUrl)
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    $last = 'not started'
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-WebRequest "$BaseUrl/health/ready" -TimeoutSec 2 -SkipHttpErrorCheck
            if ($response.StatusCode -eq 200) { return }
            $last = "HTTP $($response.StatusCode)"
        } catch { $last = $_.Exception.GetType().Name }
        Wait-Event -Timeout 1 | Out-Null
    }
    throw "Container did not become ready: $last"
}

try {
    Invoke-Docker @('volume', 'create', '--label', 'newapi-watch.validation=true', $volume) | Out-Null
    $createdVolume = $true
    $arguments = @('run', '-d', '--name', $container, '--label', 'newapi-watch.validation=true',
        '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
        '--tmpfs', '/tmp:size=134217728,mode=1777', '--mount', "type=volume,source=$volume,target=/app/data",
        '-p', '127.0.0.1::8088', '-e', 'WATCH_DEMO=true', '-e', 'WATCH_DATABASE=data/demo.sqlite3',
        '-e', "WATCH_API_KEY=$key", '-e', 'WATCH_REPORT_ENABLED=false', $Image,
        'serve', '--host', '0.0.0.0', '--port', '8088')
    Invoke-Docker $arguments | Out-Null
    $createdContainer = $true
    $binding = (Invoke-Docker @('port', $container, '8088/tcp') | Out-String).Trim()
    $port = $binding.Split(':')[-1]
    $base = "http://127.0.0.1:$port"
    $headers = @{ 'X-API-Key' = $key }
    Wait-Ready $base

    $uid = (Invoke-Docker @('exec', $container, 'id', '-u') | Out-String).Trim()
    if ($uid -ne '10001') { throw 'Container is not running as the intended non-root user' }
    Invoke-Docker @('exec', $container, 'sh', '-c', 'test ! -w /app && test -w /app/data && test -w /tmp') | Out-Null
    $result.non_root_uid = $uid
    $result.read_only_filesystem = $true

    $unauthorized = Invoke-WebRequest "$base/api/v1/channels" -SkipHttpErrorCheck
    if ($unauthorized.StatusCode -ne 401) { throw 'Unauthenticated API access was not rejected' }
    $channels = Invoke-RestMethod "$base/api/v1/channels" -Headers $headers
    if ($channels.data.total -ne 2) { throw 'Demo channel collection failed' }
    $result.channels = $channels.data.total
    $query = @{ text = '用户 demo' } | ConvertTo-Json -Compress
    $users = Invoke-RestMethod "$base/api/v1/commands" -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($query))
    if ($users.data.text -notmatch 'demo') { throw 'User query failed' }
    $query = @{ text = '图表 今天' } | ConvertTo-Json -Compress
    $report = Invoke-RestMethod "$base/api/v1/commands" -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($query))
    if (-not $report.data.has_image) { throw 'Chart generation failed' }
    $day = [regex]::Match($report.data.text, '\d{4}-\d{2}-\d{2}').Value
    if (-not $day) { throw 'Report date missing' }
    $image = Join-Path $artifacts 'container-report.png'
    Invoke-WebRequest "$base/api/v1/reports/$day/chart" -Headers $headers -OutFile $image
    $bytes = [IO.File]::ReadAllBytes($image)
    if ([BitConverter]::ToString($bytes[0..7]) -ne '89-50-4E-47-0D-0A-1A-0A') { throw 'Invalid PNG' }
    $result.png_bytes = $bytes.Length

    $periodStart = ([DateTime]::ParseExact($day, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)).AddDays(-6).ToString('yyyy-MM-dd')
    $statistics = Invoke-RestMethod "$base/api/v1/statistics?start=$periodStart&end=$day&dimension=model" -Headers $headers
    if ($statistics.data.query.dimension -ne 'model') { throw 'v0.2 statistics endpoint failed' }
    $rankingImage = Join-Path $artifacts 'container-ranking.png'
    Invoke-WebRequest "$base/api/v1/statistics/chart?start=$periodStart&end=$day&dimension=model&kind=ranking" -Headers $headers -OutFile $rankingImage
    $rankingBytes = [IO.File]::ReadAllBytes($rankingImage)
    if ([BitConverter]::ToString($rankingBytes[0..7]) -ne '89-50-4E-47-0D-0A-1A-0A') { throw 'v0.2 ranking PNG failed' }
    $audits = Invoke-RestMethod "$base/api/v1/audits?page_size=1" -Headers $headers
    if ($audits.data.total -lt 1) { throw 'v0.2 command auditing failed' }
    $silences = Invoke-RestMethod "$base/api/v1/silences" -Headers $headers
    if ($silences.code -ne 0) { throw 'v0.2 silence endpoint failed' }
    $result.statistics = $true
    $result.ranking_png_bytes = $rankingBytes.Length
    $result.command_audits = $true


    # This command only changes the isolated demo database. Its receipt proves restart persistence.
    $mutationHeaders = @{ 'X-API-Key' = $key; 'Idempotency-Key' = [Guid]::NewGuid().ToString('N') }
    $query = @{ text = '告警 静默 渠道1 1h' } | ConvertTo-Json -Compress
    $body = [Text.Encoding]::UTF8.GetBytes($query)
    Invoke-RestMethod "$base/api/v1/commands" -Method Post -Headers $mutationHeaders -ContentType 'application/json; charset=utf-8' -Body $body | Out-Null
    Invoke-Docker @('restart', '--time', '10', $container) | Out-Null
    # Docker can assign a new host port after restarting an ephemeral port binding.
    $binding = (Invoke-Docker @('port', $container, '8088/tcp') | Out-String).Trim()
    $port = $binding.Split(':')[-1]
    $base = "http://127.0.0.1:$port"
    Wait-Ready $base
    $duplicate = Invoke-WebRequest "$base/api/v1/commands" -Method Post -Headers $mutationHeaders -ContentType 'application/json; charset=utf-8' -Body $body -SkipHttpErrorCheck
    if ($duplicate.StatusCode -ne 409) { throw 'Idempotency receipt did not survive container restart' }
    $result.restart_persistence = $true

    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    do {
        $status = (Invoke-Docker @('inspect', '--format', '{{.State.Health.Status}}', $container) | Out-String).Trim()
        if ($status -eq 'healthy') { break }
        Wait-Event -Timeout 1 | Out-Null
    } while ([DateTime]::UtcNow -lt $deadline)
    if ($status -ne 'healthy') { throw "Docker HEALTHCHECK status: $status" }
    $result.healthcheck = $status
    $result.passed = $true
    $result.verified_at = [DateTime]::UtcNow.ToString('o')
} finally {
    if ($createdContainer) {
        & docker logs $container 2>&1 | Set-Content -Encoding utf8 (Join-Path $artifacts 'container-logs.txt')
        & docker rm -f $container | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Warning "Could not remove validation container: $container" }
    }
    if ($createdVolume) {
        & docker volume rm $volume | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Warning "Could not remove validation volume: $volume" }
    }
    $result | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $artifacts 'container-verification.json')
}
$result | ConvertTo-Json
