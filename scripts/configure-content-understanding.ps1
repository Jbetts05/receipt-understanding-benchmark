#Requires -Version 7.0

[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Endpoint,
    [string]$ApiKey = "",
    [string]$BearerToken = "",
    [Parameter(Mandatory)][string]$AnalyzerDefinitionPath,
    [string]$AnalyzerId = "expense_receipt_v1",
    [string]$ApiVersion = "2025-11-01"
)

$ErrorActionPreference = "Stop"

$useApiKey = -not [string]::IsNullOrWhiteSpace($ApiKey)
$useBearerToken = -not [string]::IsNullOrWhiteSpace($BearerToken)
if ($useApiKey -eq $useBearerToken) {
    throw "Provide exactly one of -ApiKey or -BearerToken"
}
if (-not (Test-Path -LiteralPath $AnalyzerDefinitionPath -PathType Leaf)) {
    throw "Analyzer definition not found: $AnalyzerDefinitionPath"
}

$headers = if ($useBearerToken) {
    @{
        Authorization = "Bearer $BearerToken"
    }
}
else {
    @{
        "Ocp-Apim-Subscription-Key" = $ApiKey
    }
}
$baseEndpoint = $Endpoint.TrimEnd("/")

function Get-FirstHeaderValue {
    param(
        [Parameter(Mandatory)][object]$Response,
        [Parameter(Mandatory)][string]$Name
    )

    $value = $Response.Headers[$Name]
    if ($null -eq $value) {
        return $null
    }
    if ($value -is [string]) {
        return $value
    }
    $values = @($value)
    if ($values.Count -eq 0) {
        return $null
    }
    return [string]$values[0]
}

function Invoke-ContentUnderstanding {
    param(
        [Parameter(Mandatory)][ValidateSet("GET", "PUT", "PATCH")][string]$Method,
        [Parameter(Mandatory)][string]$Uri,
        [string]$Body,
        [string]$ContentType = "application/json"
    )

    $parameters = @{
        Method = $Method
        Uri = $Uri
        Headers = $headers
        SkipHttpErrorCheck = $true
    }
    if ($Body) {
        $parameters["ContentType"] = $ContentType
        $parameters["Body"] = $Body
    }
    return Invoke-WebRequest @parameters
}

function Wait-ContentUnderstandingOperation {
    param([Parameter(Mandatory)][string]$OperationLocation)

    $deadline = (Get-Date).AddMinutes(10)
    do {
        $response = Invoke-ContentUnderstanding -Method GET -Uri $OperationLocation
        if ($response.StatusCode -notin 200, 202) {
            throw "Content Understanding operation polling failed with HTTP $($response.StatusCode)"
        }
        $payload = $response.Content | ConvertFrom-Json
        switch ($payload.status) {
            "Succeeded" {
                return
            }
            "Failed" {
                throw "Content Understanding operation failed: $($response.Content)"
            }
            "Canceled" {
                throw "Content Understanding operation was canceled: $($response.Content)"
            }
            { $_ -in "NotStarted", "Running", "Creating" } {
                break
            }
            default {
                throw "Unexpected Content Understanding operation status: $($payload.status)"
            }
        }
        $retryAfter = Get-FirstHeaderValue -Response $response -Name "Retry-After"
        $delay = if ($retryAfter) {
            [int]$retryAfter
        } else {
            2
        }
        Start-Sleep -Seconds $delay
    } while ((Get-Date) -lt $deadline)

    throw "Timed out waiting for Content Understanding operation"
}

$defaultsBody = @{
    modelDeployments = @{
        "gpt-5.4-mini" = "receipt-gpt-54-mini"
        "gpt-5.2" = "receipt-gpt-52-cu"
        "text-embedding-3-large" = "receipt-embedding-3-large"
        "prebuilt-analyzer-completion" = "receipt-gpt-52-cu"
        "prebuilt-analyzer-completion-mini" = "receipt-gpt-54-mini"
        "prebuilt-analyzer-embedding" = "receipt-embedding-3-large"
    }
} | ConvertTo-Json -Depth 5 -Compress

$defaultsUri = "$baseEndpoint/contentunderstanding/defaults?api-version=$ApiVersion"
$defaultsResponse = Invoke-ContentUnderstanding `
    -Method PATCH `
    -Uri $defaultsUri `
    -Body $defaultsBody `
    -ContentType "application/merge-patch+json"
if ($defaultsResponse.StatusCode -notin 200, 201, 202, 204) {
    throw "Failed to configure Content Understanding defaults: HTTP $($defaultsResponse.StatusCode) $($defaultsResponse.Content)"
}
$defaultsOperation = Get-FirstHeaderValue -Response $defaultsResponse -Name "Operation-Location"
if ($defaultsOperation) {
    Wait-ContentUnderstandingOperation -OperationLocation $defaultsOperation
}

$definitionHash = (Get-FileHash -LiteralPath $AnalyzerDefinitionPath -Algorithm SHA256).Hash.ToLowerInvariant()
$definitionObject = Get-Content -LiteralPath $AnalyzerDefinitionPath -Raw |
    ConvertFrom-Json -AsHashtable
$definitionObject["tags"] = @{
    definitionSha256 = $definitionHash
}
$definition = $definitionObject | ConvertTo-Json -Depth 30 -Compress

$analyzerUri = "$baseEndpoint/contentunderstanding/analyzers/${AnalyzerId}?api-version=$ApiVersion"
$existing = Invoke-ContentUnderstanding -Method GET -Uri $analyzerUri
if ($existing.StatusCode -eq 200) {
    $current = $existing.Content | ConvertFrom-Json
    if (
        $current.baseAnalyzerId -ne $definitionObject["baseAnalyzerId"] -or
        $current.models.completion -ne $definitionObject["models"]["completion"] -or
        $current.tags.definitionSha256 -ne $definitionHash
    ) {
        throw "Analyzer $AnalyzerId already exists with settings outside the approved plan"
    }
    Write-Host "Verified existing Content Understanding analyzer $AnalyzerId"
    return
}
if ($existing.StatusCode -ne 404) {
    throw "Unable to inspect Content Understanding analyzer: HTTP $($existing.StatusCode) $($existing.Content)"
}

$createResponse = Invoke-ContentUnderstanding `
    -Method PUT `
    -Uri $analyzerUri `
    -Body $definition
if ($createResponse.StatusCode -notin 200, 201, 202) {
    throw "Failed to create Content Understanding analyzer: HTTP $($createResponse.StatusCode) $($createResponse.Content)"
}

$operationLocation = Get-FirstHeaderValue -Response $createResponse -Name "Operation-Location"
if ($operationLocation) {
    Wait-ContentUnderstandingOperation -OperationLocation $operationLocation
}
Write-Host "Created Content Understanding analyzer $AnalyzerId"
