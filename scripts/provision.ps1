#Requires -Version 7.0

[CmdletBinding()]
param(
    [string]$SubscriptionId = "",
    [ValidatePattern("^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")]
    [string]$EnvironmentName = "receipt-bench",
    [string]$Location = "swedencentral",
    [string]$ResourceGroup = "",
    [string]$ApplicationResourceGroup = "",
    [string]$AccountName = "",
    [string]$ProjectName = "receipt-bench",
    [string]$DeploymentConfigPath = "",
    [ValidateSet("auto", "entra", "key")]
    [string]$AuthMode = "auto",
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
$script:ModelCatalog = $null
$script:QuotaUsage = $null
$script:FoundryApiKey = $null

function Invoke-AzJson {
    param([Parameter(Mandatory)][string[]]$Arguments)

    $output = & az @Arguments --only-show-errors --output json 2>&1
    if ($LASTEXITCODE -ne 0) {
        $detail = ($output | Out-String).Trim()
        throw "Azure CLI command failed: az $($Arguments -join ' '). $detail"
    }
    if (-not $output) {
        return $null
    }
    return $output | ConvertFrom-Json
}

function Get-FoundryApiKey {
    if ($script:FoundryApiKey) {
        return $script:FoundryApiKey
    }
    try {
        $keys = Invoke-AzJson @(
            "cognitiveservices", "account", "keys", "list",
            "--name", $AccountName,
            "--resource-group", $ResourceGroup
        )
    }
    catch {
        throw (
            "Foundry key authentication was requested, but account keys are unavailable. " +
            "If Azure Policy disables local authentication, use -AuthMode entra. $($_.Exception.Message)"
        )
    }
    if (-not $keys.key1) {
        throw "Foundry key authentication was requested, but Azure returned no primary key"
    }
    $script:FoundryApiKey = [string]$keys.key1
    return $script:FoundryApiKey
}

function Get-FoundryBearerToken {
    $output = & az account get-access-token `
        --resource "https://cognitiveservices.azure.com/" `
        --subscription $SubscriptionId `
        --query accessToken `
        --output tsv `
        --only-show-errors 2>&1
    if ($LASTEXITCODE -ne 0 -or -not $output) {
        $detail = ($output | Out-String).Trim()
        throw (
            "Unable to acquire a Microsoft Entra token for Foundry. Run az login " +
            "for the resource tenant and ensure the caller has Cognitive Services User " +
            "on the Foundry account. Azure CLI: $detail"
        )
    }
    return ($output | Out-String).Trim()
}

function Get-EntraPrincipal {
    param([Parameter(Mandatory)][string]$Token)

    $segments = $Token.Split(".")
    if ($segments.Count -lt 2) {
        throw "Microsoft Entra returned a token with an unexpected format"
    }
    $payload = $segments[1].Replace("-", "+").Replace("_", "/")
    while ($payload.Length % 4) {
        $payload += "="
    }
    try {
        $claims = [Text.Encoding]::UTF8.GetString(
            [Convert]::FromBase64String($payload)
        ) | ConvertFrom-Json
    }
    catch {
        throw "Microsoft Entra token claims could not be decoded"
    }
    if (-not $claims.oid) {
        throw "Microsoft Entra token does not contain an object ID claim"
    }
    return [pscustomobject]@{
        ObjectId = [string]$claims.oid
        PrincipalType = if ($claims.idtyp -eq "app") {
            "ServicePrincipal"
        }
        else {
            "User"
        }
    }
}

function Grant-FoundryOperatorAccess {
    param([Parameter(Mandatory)][string]$Token)

    $principal = Get-EntraPrincipal -Token $Token
    $output = & az role assignment create `
        --assignee-object-id $principal.ObjectId `
        --assignee-principal-type $principal.PrincipalType `
        --role "Cognitive Services User" `
        --scope $foundryAccount.id `
        --subscription $SubscriptionId `
        --only-show-errors `
        --output none 2>&1
    if ($LASTEXITCODE -ne 0) {
        $detail = ($output | Out-String).Trim()
        if ($detail -match "RoleAssignmentExists") {
            return
        }
        throw (
            "Could not assign Cognitive Services User to the deployment operator " +
            "at $($foundryAccount.id). The deployment identity needs " +
            "Microsoft.Authorization/roleAssignments/write. Azure CLI: $detail"
        )
    }
}

function Test-AzCommand {
    param([Parameter(Mandatory)][string[]]$Arguments)

    & az @Arguments --only-show-errors --output none 2>$null
    return $LASTEXITCODE -eq 0
}

function Assert-ModelAvailable {
    param(
        [Parameter(Mandatory)][string]$ModelName,
        [Parameter(Mandatory)][string]$ModelVersion,
        [Parameter(Mandatory)][string]$ModelFormat,
        [Parameter(Mandatory)][string]$SkuName
    )

    if ($null -eq $script:ModelCatalog) {
        $script:ModelCatalog = Invoke-AzJson @(
            "cognitiveservices", "model", "list",
            "--location", $Location,
            "--subscription", $SubscriptionId
        )
    }
    $match = $script:ModelCatalog | Where-Object {
        $_.model.name -eq $ModelName -and
        $_.model.version -eq $ModelVersion -and
        $_.model.format -eq $ModelFormat -and
        $_.model.skus.name -contains $SkuName
    } | Select-Object -First 1

    if (-not $match) {
        throw "Model is not deployable as $SkuName in ${Location}: ${ModelFormat}/${ModelName}/${ModelVersion}"
    }
}

function Assert-QuotaAvailable {
    param(
        [Parameter(Mandatory)][string]$ModelName,
        [Parameter(Mandatory)][string]$ModelFormat,
        [Parameter(Mandatory)][string]$SkuName,
        [Parameter(Mandatory)][int]$Capacity
    )

    $prefix = if ($ModelFormat -eq "OpenAI") { "OpenAI" } else { "AIServices" }
    $quotaName = "$prefix.$SkuName.$ModelName"
    if ($null -eq $script:QuotaUsage) {
        $script:QuotaUsage = Invoke-AzJson @(
            "cognitiveservices", "usage", "list",
            "--location", $Location,
            "--subscription", $SubscriptionId
        )
    }
    $entry = $script:QuotaUsage | Where-Object {
        $_.name.value -eq $quotaName
    } | Select-Object -First 1
    if (-not $entry) {
        throw "Quota entry not found: $quotaName"
    }
    $available = [double]$entry.limit - [double]$entry.currentValue
    if ($available -lt $Capacity) {
        throw "Insufficient quota for ${ModelName}: requested $Capacity, available $available"
    }
}

function Wait-Deployment {
    param([Parameter(Mandatory)][string]$DeploymentName)

    $deadline = (Get-Date).AddMinutes(10)
    do {
        $deployment = Invoke-AzJson @(
            "cognitiveservices", "account", "deployment", "show",
            "--name", $AccountName,
            "--resource-group", $ResourceGroup,
            "--deployment-name", $DeploymentName
        )
        $state = $deployment.properties.provisioningState
        if ($state -eq "Succeeded") {
            return
        }
        if ($state -in "Failed", "Canceled") {
            throw "Deployment entered terminal state ${state}: $DeploymentName"
        }
        Start-Sleep -Seconds 5
    } while ((Get-Date) -lt $deadline)

    throw "Timed out waiting for deployment: $DeploymentName"
}

function Get-CompatibleDeployment {
    param(
        [Parameter(Mandatory)][object]$Definition
    )

    $exists = Test-AzCommand @(
        "cognitiveservices", "account", "deployment", "show",
        "--name", $AccountName,
        "--resource-group", $ResourceGroup,
        "--deployment-name", $Definition.DeploymentName
    )

    if (-not $exists) {
        return $null
    }
    $current = Invoke-AzJson @(
        "cognitiveservices", "account", "deployment", "show",
        "--name", $AccountName,
        "--resource-group", $ResourceGroup,
        "--deployment-name", $Definition.DeploymentName
    )
    $matchesIdentity = (
        $current.properties.model.name -eq $Definition.ModelName -and
        $current.properties.model.version -eq $Definition.ModelVersion -and
        $current.properties.model.format -eq $Definition.ModelFormat -and
        $current.sku.name -eq $Definition.SkuName
    )
    if (-not $matchesIdentity) {
        throw "Deployment exists with different settings: $($Definition.DeploymentName)"
    }
    return $current
}

function New-ModelDeployment {
    param(
        [Parameter(Mandatory)][object]$Definition
    )

    $current = Get-CompatibleDeployment -Definition $Definition
    $capacityNeeded = $Definition.Capacity
    if ($null -ne $current) {
        if ($current.sku.capacity -ge $Definition.Capacity) {
            Write-Host "Verified existing deployment $($Definition.DeploymentName) at capacity $($current.sku.capacity)"
            return
        }
        $capacityNeeded = $Definition.Capacity - $current.sku.capacity
    }
    Assert-ModelAvailable `
        -ModelName $Definition.ModelName `
        -ModelVersion $Definition.ModelVersion `
        -ModelFormat $Definition.ModelFormat `
        -SkuName $Definition.SkuName
    Assert-QuotaAvailable `
        -ModelName $Definition.ModelName `
        -ModelFormat $Definition.ModelFormat `
        -SkuName $Definition.SkuName `
        -Capacity $capacityNeeded

    $properties = @{
        model = @{
            format = $Definition.ModelFormat
            name = $Definition.ModelName
            version = $Definition.ModelVersion
        }
    }
    if ($Definition.ModelFormat -eq "OpenAI") {
        $properties.raiPolicyName = "Microsoft.DefaultV2"
        $properties.versionUpgradeOption = "NoAutoUpgrade"
        $properties.dynamicThrottlingEnabled = $false
    }
    if ($Definition.ModelFormat -eq "Anthropic") {
        $properties.modelProviderData = @{
            industry = "technology"
            countryCode = "US"
            organizationName = "Contoso"
        }
    }

    $body = @{
        sku = @{
            name = $Definition.SkuName
            capacity = $Definition.Capacity
        }
        properties = $properties
    } | ConvertTo-Json -Depth 8 -Compress

    # 2026-05-01 is the minimum API version that accepts modelProviderData for
    # Anthropic deployments; use it for every model to keep one deployment path.
    $url = "https://management.azure.com/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.CognitiveServices/accounts/$AccountName/deployments/$($Definition.DeploymentName)?api-version=2026-05-01"
    $bodyFile = New-TemporaryFile
    try {
        Set-Content -LiteralPath $bodyFile.FullName -Value $body -Encoding utf8NoBOM
        & az rest `
            --method put `
            --url $url `
            --headers "Content-Type=application/json" `
            --body "@$($bodyFile.FullName)" `
            --only-show-errors `
            --output none
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to create deployment: $($Definition.DeploymentName)"
        }
    } finally {
        Remove-Item -LiteralPath $bodyFile.FullName -Force -ErrorAction SilentlyContinue
    }
    Wait-Deployment -DeploymentName $Definition.DeploymentName
    Write-Host "Created deployment $($Definition.DeploymentName)"
}

$account = Invoke-AzJson @("account", "show")
if (-not $SubscriptionId) {
    $SubscriptionId = $account.id
}
if ($account.id -ne $SubscriptionId) {
    & az account set --subscription $SubscriptionId --only-show-errors
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to select Azure subscription $SubscriptionId"
    }
}

if (-not $ResourceGroup) {
    $ResourceGroup = "rg-$EnvironmentName"
}
if (-not $ApplicationResourceGroup) {
    $ApplicationResourceGroup = $ResourceGroup
}
$generatedAccountName = -not $AccountName
if ($generatedAccountName) {
    $nameMaterial = [Text.Encoding]::UTF8.GetBytes(
        "$SubscriptionId|$EnvironmentName|$Location"
    )
    $digest = [Security.Cryptography.SHA256]::HashData($nameMaterial)
    $suffix = [Convert]::ToHexString($digest).ToLowerInvariant().Substring(0, 10)
    $AccountName = "airb$suffix"
}
if (
    ($generatedAccountName -and $AccountName -notmatch "^[a-z0-9]{2,24}$") -or
    (-not $generatedAccountName -and $AccountName -notmatch "^(?=.{2,64}$)[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])$")
) {
    throw "Foundry account name must be 2-64 letters, digits, or internal hyphens"
}
if ($ResourceGroup.Length -gt 90) {
    throw "Resource group name must be no longer than 90 characters"
}

$provider = Invoke-AzJson @(
    "provider", "show",
    "--namespace", "Microsoft.CognitiveServices"
)
if ($provider.registrationState -ne "Registered") {
    throw "Microsoft.CognitiveServices is not registered in subscription $SubscriptionId"
}

if (-not $DeploymentConfigPath) {
    $DeploymentConfigPath = Join-Path $PSScriptRoot "..\configs\azure-deployments.json"
}
if (-not (Test-Path -LiteralPath $DeploymentConfigPath -PathType Leaf)) {
    throw "Deployment configuration not found: $DeploymentConfigPath"
}
$deployments = @(
    Get-Content -LiteralPath $DeploymentConfigPath -Raw | ConvertFrom-Json
)
if ($deployments.Count -eq 0) {
    throw "Deployment configuration cannot be empty"
}
$deploymentNames = @($deployments.deploymentName)
if (@($deploymentNames | Sort-Object -Unique).Count -ne $deploymentNames.Count) {
    throw "Deployment configuration contains duplicate deployment names"
}

if ($ValidateOnly) {
    $sameName = Invoke-AzJson @(
        "cognitiveservices", "account", "list",
        "--subscription", $SubscriptionId
    ) | Where-Object { $_.name -eq $AccountName }
    if ($sameName -and $sameName.resourceGroup -ne $ResourceGroup) {
        throw "Account name $AccountName is already used in resource group $($sameName.resourceGroup)"
    }

    foreach ($deployment in $deployments) {
        $current = Get-CompatibleDeployment -Definition $deployment
        $capacityNeeded = $deployment.Capacity
        if ($null -ne $current) {
            if ($current.sku.capacity -ge $deployment.Capacity) {
                continue
            }
            $capacityNeeded = $deployment.Capacity - $current.sku.capacity
        }
        Assert-ModelAvailable `
            -ModelName $deployment.ModelName `
            -ModelVersion $deployment.ModelVersion `
            -ModelFormat $deployment.ModelFormat `
            -SkuName $deployment.SkuName
        Assert-QuotaAvailable `
            -ModelName $deployment.ModelName `
            -ModelFormat $deployment.ModelFormat `
            -SkuName $deployment.SkuName `
            -Capacity $capacityNeeded
    }

    $deployments | Select-Object DeploymentName, ModelName, ModelVersion, ModelFormat, SkuName, Capacity |
        Format-Table -AutoSize
    Write-Host "Read-only Azure validation succeeded."
    return
}

$resourceGroupExists = Test-AzCommand @(
    "group", "show",
    "--name", $ResourceGroup,
    "--subscription", $SubscriptionId
)
if (-not $resourceGroupExists) {
    Invoke-AzJson @(
        "group", "create",
        "--name", $ResourceGroup,
        "--location", $Location,
        "--tags",
        "application=receipt-bench",
        "environment=$EnvironmentName",
        "managed-by=receipt-bench"
    ) | Out-Null
}

$accountExists = Test-AzCommand @(
    "cognitiveservices", "account", "show",
    "--name", $AccountName,
    "--resource-group", $ResourceGroup
)
if (-not $accountExists) {
    Invoke-AzJson @(
        "cognitiveservices", "account", "create",
        "--name", $AccountName,
        "--resource-group", $ResourceGroup,
        "--kind", "AIServices",
        "--sku", "S0",
        "--location", $Location,
        "--custom-domain", $AccountName,
        "--assign-identity",
        "--allow-project-management", "true",
        "--tags",
        "application=receipt-bench",
        "environment=$EnvironmentName",
        "managed-by=receipt-bench",
        "--yes"
    ) | Out-Null
}

$foundryAccount = Invoke-AzJson @(
    "cognitiveservices", "account", "show",
    "--name", $AccountName,
    "--resource-group", $ResourceGroup
)
if (
    $foundryAccount.kind -ne "AIServices" -or
    $foundryAccount.location -ne $Location -or
    $foundryAccount.properties.provisioningState -ne "Succeeded"
) {
    throw "Foundry account does not match the approved plan"
}

$projectExists = Test-AzCommand @(
    "cognitiveservices", "account", "project", "show",
    "--name", $AccountName,
    "--resource-group", $ResourceGroup,
    "--project-name", $ProjectName
)
if (-not $projectExists) {
    Invoke-AzJson @(
        "cognitiveservices", "account", "project", "create",
        "--name", $AccountName,
        "--resource-group", $ResourceGroup,
        "--project-name", $ProjectName,
        "--location", $Location
    ) | Out-Null
}

$existingDeployments = Invoke-AzJson @(
    "cognitiveservices", "account", "deployment", "list",
    "--name", $AccountName,
    "--resource-group", $ResourceGroup
)
$newDeploymentCount = @(
    $deployments | Where-Object {
        $_.DeploymentName -notin @($existingDeployments.name)
    }
).Count
if (@($existingDeployments).Count + $newDeploymentCount -gt 32) {
    throw "The deployment would exceed the 32-model limit for a Foundry resource"
}

foreach ($deployment in $deployments) {
    New-ModelDeployment -Definition $deployment
}

$customSubdomain = [string]$foundryAccount.properties.customSubDomainName
if ([string]::IsNullOrWhiteSpace($customSubdomain)) {
    throw (
        "Foundry account $AccountName has no custom subdomain. Microsoft Entra " +
        "authentication, private endpoints, and the Foundry model routes require " +
        "properties.customSubDomainName. Use a compatible Foundry account."
    )
}
$cognitiveEndpoint = ([string]$foundryAccount.properties.endpoint).TrimEnd("/")
if ([string]::IsNullOrWhiteSpace($cognitiveEndpoint)) {
    $cognitiveEndpoint = "https://$customSubdomain.cognitiveservices.azure.com"
}
$serviceEndpoint = "https://$customSubdomain.services.ai.azure.com"
$openAiEndpoint = "https://$customSubdomain.openai.azure.com/openai/v1/"
$localAuthDisabled = $foundryAccount.properties.disableLocalAuth -eq $true
$resolvedAuthMode = if ($AuthMode -eq "key") { "key" } else { "entra" }
$apiKey = ""
$bearerToken = ""

if ($resolvedAuthMode -eq "key") {
    if ($localAuthDisabled) {
        throw (
            "Foundry account $AccountName has local authentication disabled by policy. " +
            "Key authentication cannot be used; rerun with -AuthMode entra or auto."
        )
    }
    $apiKey = Get-FoundryApiKey
}
else {
    try {
        $bearerToken = Get-FoundryBearerToken
    }
    catch {
        if ($AuthMode -eq "auto" -and -not $localAuthDisabled) {
            Write-Warning (
                "Microsoft Entra token acquisition failed; falling back to a Foundry " +
                "account key because local authentication is enabled. $($_.Exception.Message)"
            )
            $resolvedAuthMode = "key"
            $apiKey = Get-FoundryApiKey
        }
        else {
            throw
        }
    }
}

function Configure-ReceiptAnalyzer {
    param(
        [Parameter(Mandatory)][string]$DefinitionPath,
        [Parameter(Mandatory)][string]$AnalyzerId
    )

    $arguments = @{
        Endpoint = $serviceEndpoint
        AnalyzerDefinitionPath = $DefinitionPath
        AnalyzerId = $AnalyzerId
    }
    if ($resolvedAuthMode -eq "entra") {
        $arguments.BearerToken = $bearerToken
    }
    else {
        $arguments.ApiKey = $apiKey
    }

    try {
        & "$PSScriptRoot\configure-content-understanding.ps1" @arguments
        return
    }
    catch {
        $entraError = $_.Exception.Message
    }

    $networkDenied = $entraError -match "(?i)(firewall|network ACL|private endpoint|public network)"
    $authorizationDenied = (
        $resolvedAuthMode -eq "entra" -and
        -not $networkDenied -and
        $entraError -match "HTTP (401|403)"
    )
    if (-not $authorizationDenied) {
        throw $entraError
    }

    try {
        Grant-FoundryOperatorAccess -Token $bearerToken
        $propagationDeadline = (Get-Date).AddMinutes(5)
        do {
            try {
                & "$PSScriptRoot\configure-content-understanding.ps1" @arguments
                return
            }
            catch {
                if ($_.Exception.Message -notmatch "HTTP (401|403)") {
                    throw
                }
            }
            $remainingSeconds = [Math]::Floor(
                ($propagationDeadline - (Get-Date)).TotalSeconds
            )
            if ($remainingSeconds -le 0) {
                break
            }
            Start-Sleep -Seconds ([Math]::Min(10, $remainingSeconds))
        } while ((Get-Date) -lt $propagationDeadline)
        throw "Cognitive Services User assignment did not propagate within five minutes"
    }
    catch {
        $entraSetupError = $_.Exception.Message
    }

    if ($AuthMode -eq "auto" -and -not $localAuthDisabled) {
        Write-Warning (
            "Microsoft Entra analyzer configuration was unavailable; falling back " +
            "to an account key because local authentication is enabled."
        )
        try {
            $script:resolvedAuthMode = "key"
            $script:apiKey = Get-FoundryApiKey
            $arguments.Remove("BearerToken")
            $arguments.ApiKey = $script:apiKey
            & "$PSScriptRoot\configure-content-understanding.ps1" @arguments
            return
        }
        catch {
            throw (
                "Microsoft Entra analyzer configuration failed: $entraError " +
                "Role setup failed: $entraSetupError " +
                "The account-key fallback also failed: $($_.Exception.Message)"
            )
        }
    }

    throw (
        "Microsoft Entra authentication reached Foundry but was not authorized. " +
        "Assign Cognitive Services User to the signed-in operator on account " +
        "$AccountName, wait for RBAC propagation, and rerun. Initial error: " +
        "$entraError Role setup: $entraSetupError"
    )
}

Configure-ReceiptAnalyzer `
    -DefinitionPath "$PSScriptRoot\..\configs\content-understanding\expense_receipt_v1.json" `
    -AnalyzerId "expense_receipt_v1"
Configure-ReceiptAnalyzer `
    -DefinitionPath "$PSScriptRoot\..\configs\content-understanding\cord_receipt_v1.json" `
    -AnalyzerId "cord_receipt_v1"

$envLines = @(
    "AZURE_SUBSCRIPTION_ID=$SubscriptionId",
    "AZURE_RESOURCE_GROUP=$ApplicationResourceGroup",
    "AZURE_FOUNDRY_RESOURCE_GROUP=$ResourceGroup",
    "AZURE_LOCATION=$Location",
    "AZURE_FOUNDRY_ACCOUNT=$AccountName",
    "AZURE_FOUNDRY_CUSTOM_SUBDOMAIN=$customSubdomain",
    "AZURE_FOUNDRY_PROJECT=$ProjectName",
    "AZURE_AI_AUTH_MODE=$resolvedAuthMode",
    "AZURE_AI_CREDENTIAL_KIND=default",
    "AZURE_FOUNDRY_API_KEY=$apiKey",
    "AZURE_FOUNDRY_OPENAI_ENDPOINT=$openAiEndpoint",
    "AZURE_FOUNDRY_MODELS_ENDPOINT=$serviceEndpoint/openai/v1/",
    "AZURE_ANTHROPIC_ENDPOINT=$serviceEndpoint/anthropic",
    "AZURE_MISTRAL_OCR_ENDPOINT=$serviceEndpoint",
    "AZURE_CONTENT_UNDERSTANDING_ENDPOINT=$serviceEndpoint",
    "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT=$cognitiveEndpoint",
    "AZURE_VISION_ENDPOINT=$cognitiveEndpoint"
)
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$envPath = Join-Path $repoRoot ".env"
$envLines | Set-Content -LiteralPath $envPath -Encoding utf8NoBOM

$summary = $deployments | ForEach-Object {
    [pscustomobject]@{
        Deployment = $_.DeploymentName
        Model = $_.ModelName
        Version = $_.ModelVersion
        Format = $_.ModelFormat
    }
}
$summary | Format-Table -AutoSize
Write-Host "Foundry project: $serviceEndpoint/api/projects/$ProjectName"
Write-Host "Foundry resource group: $ResourceGroup"
Write-Host "Application resource group: $ApplicationResourceGroup"
Write-Host "Foundry account: $AccountName"
Write-Host "Foundry runtime authentication: $resolvedAuthMode"
Write-Host "Runtime configuration written to the ignored .env file."
