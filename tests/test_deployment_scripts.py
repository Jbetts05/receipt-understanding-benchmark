from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_provider_registration_failure_explains_subscription_boundary() -> None:
    deploy = _read("scripts/deploy.ps1")

    assert "Register-RequiredProvider" in deploy
    assert '"Microsoft.ContainerInstance"' in deploy
    assert "Microsoft.Resources/subscriptions/resourceProviders/register/action" in deploy
    assert "subscription Owner or" in deploy
    assert "Azure CLI: $detail" in deploy
    assert "2>&1" in deploy


def test_guided_deployment_threads_auth_mode_and_foundry_resource_group() -> None:
    deploy = _read("scripts/deploy.ps1")

    assert '[ValidateSet("auto", "entra", "key")]' in deploy
    assert "AuthMode = $AuthMode" in deploy
    assert '"env", "set", "AZURE_AI_AUTH_MODE", $AuthMode' in deploy
    assert '"env", "set", "AZURE_FOUNDRY_RESOURCE_GROUP", $FoundryResourceGroup' in deploy
    assert '"env", "set", "--file", $envPath' in deploy
    assert "Get-Content -LiteralPath $envPath" not in deploy
    assert "$resourceGroupExists = & az group exists" in deploy
    assert '$sroieRightsSpecified = $PSBoundParameters.ContainsKey("SroieRightsEvidence")' in deploy
    assert "if ($sroieRightsSpecified)" in deploy
    assert "azd env get-value SROIE_RIGHTS_EVIDENCE" in deploy
    assert "if ($LASTEXITCODE -ne 0)" not in deploy.split(
        '& "$PSScriptRoot\\provision.ps1" @validationArguments', 1
    )[1].split("if ($ValidateOnly)", 1)[0]


def test_provisioning_uses_entra_first_and_only_reads_keys_on_fallback() -> None:
    provision = _read("scripts/provision.ps1")

    assert "Get-FoundryBearerToken" in provision
    assert "Get-FoundryApiKey" in provision
    assert '"https://cognitiveservices.azure.com/"' in provision
    assert '$resolvedAuthMode = if ($AuthMode -eq "key") { "key" } else { "entra" }' in provision
    assert "$foundryAccount.properties.disableLocalAuth" in provision
    assert '"AZURE_AI_AUTH_MODE=$resolvedAuthMode"' in provision
    assert '"AZURE_AI_CREDENTIAL_KIND=default"' in provision
    assert '"AZURE_FOUNDRY_API_KEY=$apiKey"' in provision
    assert "HTTP (401|403)" in provision
    assert "Cognitive Services User" in provision
    assert "Grant-FoundryOperatorAccess" in provision
    assert "Microsoft.Authorization/roleAssignments/write" in provision
    assert "RoleAssignmentExists" in provision
    assert "properties.customSubDomainName" in provision
    assert '"AZURE_FOUNDRY_CUSTOM_SUBDOMAIN=$customSubdomain"' in provision
    assert "AddMinutes(5)" in provision
    assert "did not propagate within five minutes" in provision
    assert provision.index("Configure-ReceiptAnalyzer") < provision.rindex(
        "$envLines = @("
    )
    assert '$resourceGroupExists = Test-AzCommand @(' in provision
    assert "if (-not $resourceGroupExists)" in provision


def test_content_understanding_bootstrap_accepts_exactly_one_credential() -> None:
    configure = _read("scripts/configure-content-understanding.ps1")

    assert '[string]$ApiKey = ""' in configure
    assert '[string]$BearerToken = ""' in configure
    assert "Provide exactly one of -ApiKey or -BearerToken" in configure
    assert "$useBearerToken = -not [string]::IsNullOrWhiteSpace($BearerToken)" in configure
    assert 'Authorization = "Bearer $BearerToken"' in configure
    assert '"Ocp-Apim-Subscription-Key" = $ApiKey' in configure


def test_deployment_docs_describe_entra_and_provider_registration() -> None:
    deployment = _read("docs/deployment.md")
    readme = _read("README.md")

    assert "Microsoft Entra ID by default" in deployment
    assert "disableLocalAuth=true" in deployment
    assert "resourceProviders/register/action" in deployment
    assert "managed identity by default" in readme
