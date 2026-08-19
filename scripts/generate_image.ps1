[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Prompt,

    [string[]]$ReferenceImage = @(),

    [string]$OutputDir = ".\outputs",

    [string]$OutputName = "opencc-image.png",

    [ValidateRange(30, 3600)]
    [int]$TimeoutSec = 300,

    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
$Model = "gpt-image-2"
$ExpectedHost = "opencc.yiminju.xyz"
$RetryableStatus = @(502, 503, 504)

function Protect-ErrorText {
    param([string]$Text)

    if ($null -eq $Text) {
        return ""
    }
    $safe = $Text -replace '(?i)bearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [REDACTED]'
    $safe = $safe -replace '(?i)(api[_-]?key|token|authorization)(\s*[:=]\s*)[^\s,;]+', '$1$2[REDACTED]'
    $safe = $safe -replace 'data:image/[^;]+;base64,[A-Za-z0-9+/=]+', 'data:image/[REDACTED]'
    $safe = $safe -replace '(?i)([?&](token|key|signature|sig|auth)=[^&\s]+)', '?[REDACTED]'
    if ($safe.Length -gt 1200) {
        return $safe.Substring(0, 1200)
    }
    return $safe
}

function Get-CodexHome {
    if (-not [string]::IsNullOrWhiteSpace($env:CODEX_HOME)) {
        return [Environment]::ExpandEnvironmentVariables($env:CODEX_HOME)
    }
    if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
        throw "USERPROFILE is unavailable and CODEX_HOME is not configured"
    }
    return Join-Path $env:USERPROFILE ".codex"
}

function Remove-TomlComment {
    param([string]$Value)

    $quote = [char]0
    $escaped = $false
    for ($index = 0; $index -lt $Value.Length; $index++) {
        $character = $Value[$index]
        if ($escaped) {
            $escaped = $false
            continue
        }
        if ($character -eq '\' -and $quote -eq '"') {
            $escaped = $true
            continue
        }
        if ($character -eq '"' -or $character -eq "'") {
            if ($quote -eq [char]0) {
                $quote = $character
            }
            elseif ($quote -eq $character) {
                $quote = [char]0
            }
            continue
        }
        if ($character -eq '#' -and $quote -eq [char]0) {
            return $Value.Substring(0, $index).TrimEnd()
        }
    }
    return $Value.Trim()
}

function ConvertFrom-TomlString {
    param([string]$Value)

    $clean = (Remove-TomlComment -Value $Value).Trim()
    if ($clean.Length -ge 2) {
        $first = $clean.Substring(0, 1)
        $last = $clean.Substring($clean.Length - 1, 1)
        if (($first -eq '"' -or $first -eq "'") -and $first -eq $last) {
            return $clean.Substring(1, $clean.Length - 2)
        }
    }
    throw "Codex config contains a non-string provider setting"
}

function Get-ProviderConfig {
    param([string]$ConfigPath)

    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        throw "Codex config was not found: $ConfigPath"
    }
    $configText = [IO.File]::ReadAllText($ConfigPath, [Text.Encoding]::UTF8)
    $providerMatch = [regex]::Match($configText, '(?m)^\s*model_provider\s*=\s*(.+?)\s*$')
    if (-not $providerMatch.Success) {
        throw "model_provider is missing from Codex config.toml"
    }
    $provider = ConvertFrom-TomlString -Value $providerMatch.Groups[1].Value
    $targetSection = "model_providers.$provider"
    $currentSection = ""
    $baseUrl = ""

    foreach ($rawLine in ($configText -split "`r?`n")) {
        $line = $rawLine.Trim()
        $sectionMatch = [regex]::Match($line, '^\[([^]]+)]\s*(?:#.*)?$')
        if ($sectionMatch.Success) {
            $currentSection = $sectionMatch.Groups[1].Value.Trim().Replace('"', '').Replace("'", '')
            continue
        }
        if ($currentSection -ne $targetSection) {
            continue
        }
        $baseMatch = [regex]::Match($line, '^base_url\s*=\s*(.+?)\s*$')
        if ($baseMatch.Success) {
            $baseUrl = ConvertFrom-TomlString -Value $baseMatch.Groups[1].Value
            break
        }
    }
    if ([string]::IsNullOrWhiteSpace($baseUrl)) {
        throw "base_url is missing for model provider $provider"
    }
    return [PSCustomObject]@{
        Provider = $provider
        BaseUrl = $baseUrl
    }
}

function Get-ApiKey {
    param([string]$AuthPath)

    if (-not (Test-Path -LiteralPath $AuthPath -PathType Leaf)) {
        throw "Codex auth file was not found: $AuthPath"
    }
    try {
        $authText = [IO.File]::ReadAllText($AuthPath, [Text.Encoding]::UTF8)
        $auth = $authText | ConvertFrom-Json
    }
    catch {
        throw "Codex auth.json is not valid JSON"
    }
    $apiKey = $auth.OPENAI_API_KEY
    if ([string]::IsNullOrWhiteSpace([string]$apiKey)) {
        throw "OPENAI_API_KEY is missing from Codex auth.json"
    }
    return ([string]$apiKey).Trim()
}

function Get-Endpoint {
    param([string]$BaseUrl)

    try {
        $uri = [Uri]$BaseUrl.Trim()
    }
    catch {
        throw "The configured OpenCC base_url is invalid"
    }
    if ($uri.Scheme -ne "https" -or $uri.Host.ToLowerInvariant() -ne $ExpectedHost) {
        throw "Active provider must use https://$ExpectedHost"
    }
    if (-not [string]::IsNullOrWhiteSpace($uri.UserInfo) -or -not [string]::IsNullOrWhiteSpace($uri.Query) -or -not [string]::IsNullOrWhiteSpace($uri.Fragment)) {
        throw "OpenCC base_url must not contain credentials, query parameters, or fragments"
    }
    $path = $uri.AbsolutePath.TrimEnd('/')
    if ($path.EndsWith('/v1')) {
        $path = "$path/chat/completions"
    }
    else {
        $path = "$path/v1/chat/completions"
    }
    return "https://$ExpectedHost$path"
}

function Get-ImageType {
    param([byte[]]$Bytes)

    if ($Bytes.Length -ge 8 -and
        $Bytes[0] -eq 0x89 -and $Bytes[1] -eq 0x50 -and $Bytes[2] -eq 0x4E -and $Bytes[3] -eq 0x47 -and
        $Bytes[4] -eq 0x0D -and $Bytes[5] -eq 0x0A -and $Bytes[6] -eq 0x1A -and $Bytes[7] -eq 0x0A) {
        return [PSCustomObject]@{ Mime = "image/png"; Extension = ".png" }
    }
    if ($Bytes.Length -ge 3 -and $Bytes[0] -eq 0xFF -and $Bytes[1] -eq 0xD8 -and $Bytes[2] -eq 0xFF) {
        return [PSCustomObject]@{ Mime = "image/jpeg"; Extension = ".jpg" }
    }
    if ($Bytes.Length -ge 12 -and
        [Text.Encoding]::ASCII.GetString($Bytes, 0, 4) -eq "RIFF" -and
        [Text.Encoding]::ASCII.GetString($Bytes, 8, 4) -eq "WEBP") {
        return [PSCustomObject]@{ Mime = "image/webp"; Extension = ".webp" }
    }
    if ($Bytes.Length -ge 6) {
        $header = [Text.Encoding]::ASCII.GetString($Bytes, 0, 6)
        if ($header -eq "GIF87a" -or $header -eq "GIF89a") {
            return [PSCustomObject]@{ Mime = "image/gif"; Extension = ".gif" }
        }
    }
    throw "A supplied or returned file is not a supported PNG, JPEG, WebP, or GIF image"
}

function Get-ReferenceContent {
    param([string[]]$Paths)

    $items = [Collections.ArrayList]::new()
    foreach ($path in $Paths) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Reference image was not found: $path"
        }
        $bytes = [IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $path).Path)
        if ($bytes.Length -eq 0) {
            throw "Reference image is empty: $path"
        }
        $imageType = Get-ImageType -Bytes $bytes
        $encoded = [Convert]::ToBase64String($bytes)
        [void]$items.Add([ordered]@{
            type = "image_url"
            image_url = [ordered]@{
                url = "data:$($imageType.Mime);base64,$encoded"
            }
        })
    }
    return ,$items
}

function New-RequestPayload {
    param(
        [string]$ImagePrompt,
        [Collections.ArrayList]$References
    )

    if ([string]::IsNullOrWhiteSpace($ImagePrompt)) {
        throw "The image prompt cannot be empty"
    }
    $content = $ImagePrompt.Trim()
    if ($References.Count -gt 0) {
        $parts = [Collections.ArrayList]::new()
        [void]$parts.Add([ordered]@{ type = "text"; text = $ImagePrompt.Trim() })
        foreach ($reference in $References) {
            [void]$parts.Add($reference)
        }
        $content = $parts
    }
    return [ordered]@{
        model = $Model
        messages = @(
            [ordered]@{
                role = "user"
                content = $content
            }
        )
        stream = $false
    }
}

function Invoke-OpenCCRequest {
    param(
        [string]$Endpoint,
        [string]$ApiKey,
        [string]$JsonBody,
        [int]$Timeout
    )

    Add-Type -AssemblyName System.Net.Http
    $delays = @(0, 5, 10)
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        if ($delays[$attempt - 1] -gt 0) {
            Start-Sleep -Seconds $delays[$attempt - 1]
        }
        $client = [Net.Http.HttpClient]::new()
        $client.Timeout = [TimeSpan]::FromSeconds($Timeout)
        $request = [Net.Http.HttpRequestMessage]::new([Net.Http.HttpMethod]::Post, $Endpoint)
        $request.Headers.Authorization = [Net.Http.Headers.AuthenticationHeaderValue]::new("Bearer", $ApiKey)
        $request.Headers.UserAgent.ParseAdd("opencc-image2-skill/1.0.0")
        $request.Content = [Net.Http.StringContent]::new($JsonBody, [Text.Encoding]::UTF8, "application/json")
        try {
            $response = $client.SendAsync($request).GetAwaiter().GetResult()
            $responseText = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        }
        catch {
            $connectionMessage = "OpenCC connection failed: $(Protect-ErrorText -Text $_.Exception.Message)"
            if ($attempt -eq 3) {
                throw $connectionMessage
            }
            continue
        }
        finally {
            if ($null -ne $request) { $request.Dispose() }
            if ($null -ne $client) { $client.Dispose() }
        }

        $status = [int]$response.StatusCode
        if ($response.IsSuccessStatusCode) {
            try {
                $parsed = $responseText | ConvertFrom-Json
            }
            catch {
                throw "OpenCC returned a non-JSON response"
            }
            return [PSCustomObject]@{
                Response = $parsed
                Attempts = $attempt
            }
        }
        $httpMessage = "OpenCC request failed with HTTP ${status}: $(Protect-ErrorText -Text $responseText)"
        if ($RetryableStatus -notcontains $status -or $attempt -eq 3) {
            throw $httpMessage
        }
    }
    throw "OpenCC request failed"
}

function Get-CandidateFromString {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }
    $dataMatch = [regex]::Match($Value, 'data:image/[^;\s]+;base64,[A-Za-z0-9+/=]+')
    if ($dataMatch.Success) {
        return [PSCustomObject]@{ Kind = "data"; Value = $dataMatch.Value }
    }
    $markdownMatch = [regex]::Match($Value, '!\[[^]]*]\((https://[^)\s]+)\)')
    if ($markdownMatch.Success) {
        return [PSCustomObject]@{ Kind = "url"; Value = $markdownMatch.Groups[1].Value }
    }
    $urlMatch = [regex]::Match($Value, 'https://[^\s<>''"]+')
    if ($urlMatch.Success) {
        return [PSCustomObject]@{ Kind = "url"; Value = $urlMatch.Value.TrimEnd(')', '.', ',', ';') }
    }
    return $null
}

function Get-ImageCandidate {
    param($Response)

    if ($null -ne $Response.data -and @($Response.data).Count -gt 0) {
        $first = @($Response.data)[0]
        if (-not [string]::IsNullOrWhiteSpace([string]$first.url)) {
            return [PSCustomObject]@{ Kind = "url"; Value = [string]$first.url }
        }
        if (-not [string]::IsNullOrWhiteSpace([string]$first.b64_json)) {
            return [PSCustomObject]@{ Kind = "base64"; Value = [string]$first.b64_json }
        }
    }
    if ($null -eq $Response.choices -or @($Response.choices).Count -eq 0) {
        throw "OpenCC response did not contain image output"
    }
    $message = @($Response.choices)[0].message
    foreach ($image in @($message.images)) {
        if ($null -eq $image -or $null -eq $image.image_url) { continue }
        $value = if ($image.image_url -is [string]) { [string]$image.image_url } else { [string]$image.image_url.url }
        $candidate = Get-CandidateFromString -Value $value
        if ($null -ne $candidate) { return $candidate }
    }
    $content = $message.content
    if ($content -is [string]) {
        $candidate = Get-CandidateFromString -Value $content
        if ($null -ne $candidate) { return $candidate }
    }
    else {
        foreach ($item in @($content)) {
            if ($null -ne $item.image_url) {
                $value = if ($item.image_url -is [string]) { [string]$item.image_url } else { [string]$item.image_url.url }
                $candidate = Get-CandidateFromString -Value $value
                if ($null -ne $candidate) { return $candidate }
            }
            if (-not [string]::IsNullOrWhiteSpace([string]$item.url)) {
                $candidate = Get-CandidateFromString -Value ([string]$item.url)
                if ($null -ne $candidate) { return $candidate }
            }
            if (-not [string]::IsNullOrWhiteSpace([string]$item.b64_json)) {
                return [PSCustomObject]@{ Kind = "base64"; Value = [string]$item.b64_json }
            }
            if (-not [string]::IsNullOrWhiteSpace([string]$item.text)) {
                $candidate = Get-CandidateFromString -Value ([string]$item.text)
                if ($null -ne $candidate) { return $candidate }
            }
        }
    }
    throw "OpenCC response did not contain a downloadable image"
}

function Get-ImageBytes {
    param(
        $Candidate,
        [int]$Timeout
    )

    if ($Candidate.Kind -eq "base64") {
        try { return ,([Convert]::FromBase64String($Candidate.Value)) }
        catch { throw "OpenCC returned invalid Base64 image data" }
    }
    if ($Candidate.Kind -eq "data") {
        $match = [regex]::Match($Candidate.Value, '^data:image/[^;]+;base64,(.+)$')
        if (-not $match.Success) { throw "OpenCC returned an invalid image Data URL" }
        try { return ,([Convert]::FromBase64String($match.Groups[1].Value)) }
        catch { throw "OpenCC returned invalid Base64 image data" }
    }
    try {
        $uri = [Uri]$Candidate.Value
        if ($uri.Scheme -ne "https") { throw "OpenCC returned a non-HTTPS image URL" }
        Add-Type -AssemblyName System.Net.Http
        $client = [Net.Http.HttpClient]::new()
        $client.Timeout = [TimeSpan]::FromSeconds($Timeout)
        try {
            return ,($client.GetByteArrayAsync($uri).GetAwaiter().GetResult())
        }
        finally {
            $client.Dispose()
        }
    }
    catch {
        throw "Generated image download failed: $(Protect-ErrorText -Text $_.Exception.Message)"
    }
}

function Get-CollisionFreePath {
    param(
        [string]$Directory,
        [string]$Name,
        [string]$Extension
    )

    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
        [void](New-Item -ItemType Directory -Path $Directory -Force)
    }
    $leaf = [IO.Path]::GetFileName($Name)
    if ([string]::IsNullOrWhiteSpace($leaf)) {
        $leaf = "opencc-image$Extension"
    }
    $stem = [IO.Path]::GetFileNameWithoutExtension($leaf)
    if ([string]::IsNullOrWhiteSpace($stem)) { $stem = "opencc-image" }
    $candidate = Join-Path $Directory "$stem$Extension"
    $version = 2
    while (Test-Path -LiteralPath $candidate) {
        $candidate = Join-Path $Directory "$stem-v$version$Extension"
        $version++
    }
    return $candidate
}

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $codexHome = Get-CodexHome
    $providerConfig = Get-ProviderConfig -ConfigPath (Join-Path $codexHome "config.toml")
    $apiKey = Get-ApiKey -AuthPath (Join-Path $codexHome "auth.json")
    $endpoint = Get-Endpoint -BaseUrl $providerConfig.BaseUrl
    $references = Get-ReferenceContent -Paths $ReferenceImage
    $payload = New-RequestPayload -ImagePrompt $Prompt -References $references

    if ($ValidateOnly) {
        [ordered]@{
            ok = $true
            validate_only = $true
            provider = $providerConfig.Provider
            provider_host = $ExpectedHost
            endpoint_path = ([Uri]$endpoint).AbsolutePath
            model = $Model
            reference_count = $references.Count
        } | ConvertTo-Json -Compress
        exit 0
    }

    $jsonBody = $payload | ConvertTo-Json -Depth 12 -Compress
    $requestResult = Invoke-OpenCCRequest -Endpoint $endpoint -ApiKey $apiKey -JsonBody $jsonBody -Timeout $TimeoutSec
    $candidate = Get-ImageCandidate -Response $requestResult.Response
    $bytes = Get-ImageBytes -Candidate $candidate -Timeout $TimeoutSec
    if ($bytes.Length -eq 0) { throw "OpenCC returned an empty image" }
    $imageType = Get-ImageType -Bytes $bytes
    $outputPath = Get-CollisionFreePath -Directory $OutputDir -Name $OutputName -Extension $imageType.Extension
    [IO.File]::WriteAllBytes($outputPath, $bytes)

    [ordered]@{
        ok = $true
        provider = $providerConfig.Provider
        provider_host = $ExpectedHost
        endpoint_path = ([Uri]$endpoint).AbsolutePath
        model = $Model
        output_path = $outputPath
        size_bytes = $bytes.Length
        attempts = $requestResult.Attempts
        reference_count = $references.Count
    } | ConvertTo-Json -Compress
    exit 0
}
catch {
    $errorJson = [ordered]@{
        ok = $false
        error = Protect-ErrorText -Text $_.Exception.Message
    } | ConvertTo-Json -Compress
    [Console]::Error.WriteLine($errorJson)
    exit 1
}
