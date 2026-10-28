#Requires -Version 7.0

[CmdletBinding()]
param(
    [ValidatePattern("^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")]
    [string]$EnvironmentName = "receipt-bench",
    [string]$SubscriptionId = "",
    [string]$Location = "swedencentral",
    [string]$ResourceGroup = "",
    [string]$FoundryResourceGroup = "",
    [string]$FoundryAccount = "",
    [string]$FoundryProject = "receipt-bench",
    [ValidateSet("auto", "entra", "key")]
    [string]$AuthMode = "auto",
    [string]$SroieRightsEvidence = "",
    [switch]$ValidateOnly,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$sroieRightsSpecified = $PSBoundParameters.ContainsKey("SroieRightsEvidence")

function Assert-Command {
    param([Parameter(Mandatory)][string]$Name)

    if ($null -eq (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found. See docs/deployment.md."
    }
}

function Invoke-Native {
    param(
        [Parameter(Mandatory)][string]$Command,
        [Parameter(Mandatory)][string[]]$Arguments,
        [switch]$RedactArguments
    )

    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        $display = if ($RedactArguments) {
            "$Command $($Arguments[0])"
        }
        else {
            "$Command $($Arguments -join ' ')"
        }
        throw "$display failed"
    }
}

function Get-ProviderRegistrationState {
    param([Parameter(Mandatory)][string]$Namespace)

    $output = & az provider show `
        --namespace $Namespace `
        --subscription $SubscriptionId `
        --query registrationState `
        --output tsv `
        --only-show-errors 2>&1
    if ($LASTEXITCODE -ne 0) {
        $detail = ($output | Out-String).Trim()
        throw (
            "Unable to inspect Azure resource provider $Namespace in subscription " +
            "$SubscriptionId. Verify that the subscription is visible to the current " +
            "Azure CLI identity. Azure CLI: $detail"
        )
    }
    return ($output | Out-String).Trim()
}

function Register-RequiredProvider {
    param([Parameter(Mandatory)][string]$Namespace)

    Write-Host "Registering required Azure resource provider $Namespace..."
    $output = & az provider register `
        --namespace $Namespace `
        --subscription $SubscriptionId `
        --wait `
        --only-show-errors 2>&1
    if ($LASTEXITCODE -ne 0) {
        $detail = ($output | Out-String).Trim()
        throw @"
Receipt Bench cannot register the required Azure resource provider $Namespace.

Provider registration is a subscription bootstrap permission and happens before
Foundry runtime authentication is configured. Ask a subscription Owner or
Contributor (or a custom role holder) to register $Namespace, or grant
Microsoft.Resources/subscriptions/resourceProviders/register/action at the
subscription scope. Then rerun this command.

Azure CLI: $detail
"@
    }
    $state = Get-ProviderRegistrationState -Namespace $Namespace
    if ($state -ne "Registered") {
        throw "Azure resource provider $Namespace remained in state $state after registration"
    }
}

Assert-Command -Name "az"
Assert-Command -Name "azd"

$accountJson = & az account show --only-show-errors --output json
if ($LASTEXITCODE -ne 0 -or -not $accountJson) {
    throw "Azure CLI is not authenticated. Run 'az login' first."
}
$account = $accountJson | ConvertFrom-Json
if (-not $SubscriptionId) {
    $SubscriptionId = $account.id
}
if ($account.id -ne $SubscriptionId) {
    Invoke-Native -Command "az" -Arguments @(
        "account", "set", "--subscription", $SubscriptionId, "--only-show-errors"
    )
}
if (-not $ResourceGroup) {
    $ResourceGroup = "rg-$EnvironmentName"
}
if (-not $FoundryResourceGroup) {
    $FoundryResourceGroup = $ResourceGroup
}
if (-not $FoundryAccount) {
    $nameMaterial = [Text.Encoding]::UTF8.GetBytes(
        "$SubscriptionId|$EnvironmentName|$Location"
    )
    $digest = [Security.Cryptography.SHA256]::HashData($nameMaterial)
    $suffix = [Convert]::ToHexString($digest).ToLowerInvariant().Substring(0, 10)
    $FoundryAccount = "airb$suffix"
}

& azd auth login --check-status | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Azure Developer CLI is not authenticated. Run 'azd auth login' first."
}

$knownEnvironments = @(& azd env list --output json | ConvertFrom-Json)
if ($EnvironmentName -in @($knownEnvironments.Name)) {
    Invoke-Native -Command "azd" -Arguments @("env", "select", $EnvironmentName)
}
else {
    Invoke-Native -Command "azd" -Arguments @(
        "env", "new", $EnvironmentName, "--no-prompt"
    )
}

Invoke-Native -Command "azd" -Arguments @(
    "env", "set", "AZURE_SUBSCRIPTION_ID", $SubscriptionId
)
Invoke-Native -Command "azd" -Arguments @(
    "env", "set", "AZURE_LOCATION", $Location
)
Invoke-Native -Command "azd" -Arguments @(
    "env", "set", "AZURE_RESOURCE_GROUP", $ResourceGroup
)
Invoke-Native -Command "azd" -Arguments @(
    "env", "set", "AZURE_FOUNDRY_ACCOUNT", $FoundryAccount
)
Invoke-Native -Command "azd" -Arguments @(
    "env", "set", "AZURE_FOUNDRY_PROJECT", $FoundryProject
)
Invoke-Native -Command "azd" -Arguments @(
    "env", "set", "AZURE_FOUNDRY_RESOURCE_GROUP", $FoundryResourceGroup
)
Invoke-Native -Command "azd" -Arguments @(
    "env", "set", "AZURE_AI_AUTH_MODE", $AuthMode
)

$requiredProviders = @(
    "Microsoft.ContainerInstance",
    "Microsoft.CognitiveServices",
    "Microsoft.Insights",
    "Microsoft.KeyVault",
    "Microsoft.ManagedIdentity",
    "Microsoft.Network",
    "Microsoft.OperationalInsights",
    "Microsoft.Storage",
    "Microsoft.Web"
)
$missingProviders = @(
    foreach ($provider in $requiredProviders) {
        $state = Get-ProviderRegistrationState -Namespace $provider
        if ($state -ne "Registered") {
            $provider
        }
    }
)
if ($ValidateOnly -and $missingProviders.Count -gt 0) {
    $commands = ($missingProviders | ForEach-Object {
        "az provider register --namespace $_ --subscription $SubscriptionId --wait"
    }) -join [Environment]::NewLine
    throw (
        "Required Azure providers are not registered: " +
        ($missingProviders -join ", ") + ". Provider registration requires " +
        "Microsoft.Resources/subscriptions/resourceProviders/register/action at " +
        "subscription scope. Ask a subscription administrator to run:" +
        [Environment]::NewLine + $commands
    )
}
foreach ($provider in $missingProviders) {
    Register-RequiredProvider -Namespace $provider
}

$provisionArguments = @{
    SubscriptionId = $SubscriptionId
    EnvironmentName = $EnvironmentName
    Location = $Location
    ResourceGroup = $FoundryResourceGroup
    ApplicationResourceGroup = $ResourceGroup
    AccountName = $FoundryAccount
    ProjectName = $FoundryProject
    AuthMode = $AuthMode
}
$validationArguments = $provisionArguments.Clone()
$validationArguments.ValidateOnly = $true

& "$PSScriptRoot\provision.ps1" @validationArguments

if ($ValidateOnly) {
    Write-Host "Azure model availability and quota validation succeeded."
    return
}

$resourceGroupExists = & az group exists `
    --name $ResourceGroup `
    --subscription $SubscriptionId `
    --only-show-errors
if ($LASTEXITCODE -ne 0) {
    throw "Unable to inspect application resource group $ResourceGroup"
}
if (($resourceGroupExists | Out-String).Trim() -ne "true") {
    Invoke-Native -Command "az" -Arguments @(
        "group", "create",
        "--name", $ResourceGroup,
        "--location", $Location,
        "--subscription", $SubscriptionId,
        "--tags",
        "application=receipt-bench",
        "environment=$EnvironmentName",
        "managed-by=receipt-bench",
        "--only-show-errors",
        "--output", "none"
    )
}

& "$PSScriptRoot\provision.ps1" @provisionArguments

$envPath = Join-Path $repoRoot ".env"
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    throw "Provisioning completed without writing the expected .env file"
}
Invoke-Native `
    -Command "azd" `
    -Arguments @("env", "set", "--file", $envPath) `
    -RedactArguments
if ($sroieRightsSpecified) {
    if ($SroieRightsEvidence) {
        Invoke-Native -Command "azd" -Arguments @(
            "env", "set", "SROIE_RIGHTS_EVIDENCE", $SroieRightsEvidence
        )
    }
    else {
        Invoke-Native -Command "azd" -Arguments @(
            "env", "set", "SROIE_RIGHTS_EVIDENCE="
        )
    }
}
else {
    & azd env get-value SROIE_RIGHTS_EVIDENCE --no-prompt *> $null
    if ($LASTEXITCODE -ne 0) {
        Invoke-Native -Command "azd" -Arguments @(
            "env", "set", "SROIE_RIGHTS_EVIDENCE="
        )
    }
}

Invoke-Native -Command "azd" -Arguments @(
    "provision", "--preview", "--no-prompt"
)
if (-not $Yes) {
    $confirmation = Read-Host "Apply this preview? Type 'deploy' to continue"
    if ($confirmation -ne "deploy") {
        Write-Host "Deployment cancelled; no application infrastructure was changed."
        return
    }
}
Invoke-Native -Command "azd" -Arguments @("up", "--no-prompt")

$webUrlOutput = & azd env get-value WEB_URL 2>&1
if ($LASTEXITCODE -ne 0 -or -not $webUrlOutput) {
    throw "Deployment succeeded but WEB_URL was not returned"
}
$webUrl = ($webUrlOutput | Out-String).Trim()
Write-Host "Receipt Bench deployed: $webUrl"
