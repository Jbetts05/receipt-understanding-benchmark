from __future__ import annotations

import json
import re
import runpy
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra"
STAGE_SCRIPT = INFRA / "scripts" / "stage-package.py"


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _all_bicep() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in INFRA.rglob("*.bicep"))


def _stage_function() -> Any:
    return runpy.run_path(str(STAGE_SCRIPT), run_name="infra_stage_package")["stage_package"]


def _stage_module() -> dict[str, Any]:
    return runpy.run_path(str(STAGE_SCRIPT), run_name="infra_stage_package")


@pytest.fixture
def stage_workspace() -> Iterator[Path]:
    workspace = Path(__file__).parent / ".stage-test-work"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir()
    try:
        yield workspace
    finally:
        if workspace.exists():
            shutil.rmtree(workspace)


def _write_package_fixture(
    root: Path,
    *,
    dependencies: set[str],
    include_factory: bool = True,
) -> None:
    files = {
        "LICENSE": "fixture\n",
        "README.md": "fixture\n",
        "pyproject.toml": '[project]\nname = "fixture"\nversion = "0.0.0"\n'
        'requires-python = ">=3.12"\n',
        "requirements.lock": "\n".join(f"{name}==1.0.0" for name in sorted(dependencies)),
        "configs/app.yaml": "{}\n",
        "frontend/package.json": '{"scripts":{"build":"echo build"}}\n',
        "frontend/package-lock.json": '{"lockfileVersion":3}\n',
        "frontend/src/main.ts": "export {};\n",
        "frontend/src/main.test.ts": "throw new Error('must not ship');\n",
        "frontend/dist/stale.js": "must not ship\n",
        "frontend/.env.production": "SECRET=must-not-ship\n",
        "schemas/app.schema.json": "{}\n",
        "src/receipt_bench/__init__.py": "",
        "src/receipt_bench/web/__init__.py": "",
        "src/receipt_bench/web/app.py": (
            "def create_app():\n    return object()\n"
            if include_factory
            else "def another_factory():\n    return object()\n"
        ),
        "src/receipt_bench/demo/static/index.html": "<!doctype html>\n",
        "src/receipt_bench/web/static/stale.js": "must not ship\n",
        "src/receipt_bench/web/test_app.py": "SECRET = 'must-not-ship'\n",
        "src/receipt_bench/web/__pycache__/app.pyc": "must not ship\n",
        "src/receipt_bench/restricted/private.jpg": "must not ship\n",
        "src/receipt_bench/results/output.json": "must not ship\n",
        "src/receipt_bench/secrets/credentials.json": "must not ship\n",
        "data/manifests/benchmark.jsonl": "{}\n",
        "prompts/example.json": "{}\n",
        "infra/scripts/oryx-pre-build.sh": "#!/usr/bin/env bash\n",
    }
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_azd_targets_one_clean_source_deployed_app_service() -> None:
    config = yaml.safe_load(_read("azure.yaml"))
    assert config["name"] == "receipt-bench"
    assert config["resourceGroup"] == "${AZURE_RESOURCE_GROUP}"
    assert config["infra"] == {"provider": "bicep", "path": "./infra", "module": "main"}
    assert config["requiredVersions"]["azd"] == ">= 1.27.0"
    assert set(config["services"]) == {"web"}

    web = config["services"]["web"]
    assert {key: value for key, value in web.items() if key != "hooks"} == {
        "project": "./infra/package",
        "language": "python",
        "host": "appservice",
        "resourceName": "${AZURE_WEB_APP_NAME}",
    }
    assert "docker" not in web
    assert "remoteBuild" not in web

    assert set(config["hooks"]) == {"prerestore"}
    root_hook_commands = [
        config["hooks"]["prerestore"]["windows"]["run"],
        config["hooks"]["prerestore"]["posix"]["run"],
    ]
    assert all("stage-package.py" in command for command in root_hook_commands)
    assert config["hooks"]["prerestore"]["windows"]["continueOnError"] is False
    assert config["hooks"]["prerestore"]["posix"]["continueOnError"] is False

    assert set(web["hooks"]) == {"prepackage"}
    service_hook = web["hooks"]["prepackage"]
    assert service_hook["windows"]["run"] == (
        r"python ..\scripts\stage-package.py --in-place"
    )
    assert service_hook["posix"]["run"] == (
        "python3 ../scripts/stage-package.py --in-place"
    )
    assert service_hook["windows"]["continueOnError"] is False
    assert service_hook["posix"]["continueOnError"] is False


def test_oryx_remote_build_uses_the_authoritative_locks() -> None:
    build_script = _read("infra/scripts/oryx-pre-build.sh")
    assert "set -euo pipefail" in build_script
    assert "expected = (3, 12)" in build_script
    assert "sys.exit(" in build_script
    assert "raise SystemExit" not in build_script
    assert "cp requirements.lock requirements.txt" in build_script
    assert "printf '\\n.\\n' >> requirements.txt" in build_script
    assert "npm --prefix" not in build_script
    assert "test -f src/receipt_bench/demo/static/index.html" in build_script

    app = _read("infra/modules/app-service.bicep")
    assert "ENABLE_ORYX_BUILD: 'true'" in app
    assert "SCM_DO_BUILD_DURING_DEPLOYMENT: 'true'" in app
    assert "PRE_BUILD_COMMAND: 'bash infra/scripts/oryx-pre-build.sh'" in app
    assert "WEBSITE_RUN_FROM_PACKAGE" not in app


def test_runtime_startup_and_scale_match_the_python_312_plan() -> None:
    app = _read("infra/modules/app-service.bicep")
    main = _read("infra/main.bicep")
    parameters = json.loads(_read("infra/main.parameters.json"))["parameters"]

    assert "@allowed([\n  '3.12'\n])" in main
    assert "param pythonRuntimeVersion string = '3.12'" in main
    assert parameters["pythonRuntimeVersion"]["value"] == "3.12"
    assert "linuxFxVersion: 'PYTHON|${pythonRuntimeVersion}'" in app
    assert (
        "python -m uvicorn receipt_bench.web.app:create_app --factory "
        "--host 0.0.0.0 --port $PORT --workers 1 --no-access-log"
    ) in main
    assert "param appStartupCommand" not in main
    assert "--reload" not in main
    assert "PORT: '8000'" in app

    assert "name: 'B1'" in app
    assert "tier: 'Basic'" in app
    assert "capacity: 1" in app
    assert "perSiteScaling: false" in app
    assert "numberOfWorkers: 1" in app
    assert "alwaysOn: true" in app
    assert "healthCheckPath: '/api/ready'" in app
    assert "WEBSITES_CONTAINER_START_TIME_LIMIT: '1800'" in app
    assert "WEBSITE_HEALTHCHECK_MAXPINGFAILURES: '10'" in app
    assert "WEBSITE_WARMUP_PATH: '/api/health'" in app
    assert "WEBSITE_WARMUP_STATUSES: '200'" in app


def test_app_service_transport_identity_and_public_ingress_contract() -> None:
    app = _read("infra/modules/app-service.bicep")
    assert "httpsOnly: true" in app
    assert "http20Enabled: true" in app
    assert "minTlsVersion: '1.2'" in app
    assert "scmMinTlsVersion: '1.2'" in app
    assert "ftpsState: 'Disabled'" in app
    assert "clientAffinityEnabled: false" in app
    assert "type: 'SystemAssigned'" in app
    assert "publicNetworkAccess: 'Enabled'" in app


def test_network_uses_exact_subnets_private_dns_and_split_routing() -> None:
    network = _read("infra/modules/network.bicep")
    app = _read("infra/modules/app-service.bicep")
    storage = _read("infra/modules/storage.bicep")
    vault = _read("infra/modules/key-vault.bicep")
    main = _read("infra/main.bicep")

    assert "10.42.0.0/24" in network
    assert "10.42.0.0/26" in network
    assert "Microsoft.Web/serverFarms" in network
    assert "10.42.0.64/27" in network
    assert "privateEndpointNetworkPolicies: 'Disabled'" in network
    for zone in (
        "privatelink.blob.core.windows.net",
        "privatelink.queue.core.windows.net",
        "privatelink.table.core.windows.net",
        "privatelink.file.core.windows.net",
        "privatelink.vaultcore.azure.net",
    ):
        assert zone in main
    assert "blob: network.outputs.privateDnsZoneIds[0]" in main
    assert "queue: network.outputs.privateDnsZoneIds[1]" in main
    assert "table: network.outputs.privateDnsZoneIds[2]" in main
    assert "deploymentScriptStoragePrivateDnsZoneId: network.outputs.privateDnsZoneIds[3]" in main
    assert "privateDnsZoneId: network.outputs.privateDnsZoneIds[4]" in main
    assert "registrationEnabled: false" in network
    assert "10.42.0.96/27" in network
    assert "Microsoft.ContainerInstance/containerGroups" in network
    assert "Microsoft.Network/publicIPAddresses@2024-07-01" in network
    assert "Microsoft.Network/natGateways@2024-07-01" in network
    assert "natGateway: {" in network
    assert "id: deploymentScriptNatGateway.id" in network
    assert "deploymentScriptSubnetId string = deploymentScriptSubnet.id" in network
    assert "allTraffic: false" in app
    assert "applicationTraffic: false" in app
    assert "backupRestoreTraffic: false" in app
    assert "contentShareTraffic: false" in app
    assert "imagePullTraffic: false" in app
    assert "virtualNetworkSubnetId: appSubnetId" in app
    assert "groupId: 'blob'" in storage
    assert "groupId: 'queue'" in storage
    assert "groupId: 'table'" in storage
    assert "groupIds: [\n            'vault'\n          ]" in vault
    assert "privateDnsZoneGroups" in storage
    assert "privateDnsZoneGroup" in vault


def test_application_storage_and_key_vault_block_public_and_shared_key_access() -> None:
    storage = _read("infra/modules/storage.bicep")
    vault = _read("infra/modules/key-vault.bicep")
    bicep = _all_bicep()

    assert "kind: 'StorageV2'" in storage
    assert "name: 'Standard_LRS'" in storage
    assert "accessTier: 'Hot'" in storage
    assert "allowBlobPublicAccess: false" in storage
    assert "allowCrossTenantReplication: false" in storage
    assert "allowSharedKeyAccess: false" in storage
    assert "defaultToOAuthAuthentication: true" in storage
    assert "minimumTlsVersion: 'TLS1_2'" in storage
    assert "supportsHttpsTrafficOnly: true" in storage
    assert "publicNetworkAccess: 'Disabled'" in storage
    assert "enableRbacAuthorization: true" in vault
    assert "enablePurgeProtection: true" in vault
    assert "softDeleteRetentionInDays: 90" in vault
    assert vault.count("publicNetworkAccess: 'Disabled'") == 2
    assert "publicNetworkAccess: 'Enabled'" not in vault
    assert vault.count("bypass: 'AzureServices'") == 1
    assert vault.count("bypass: 'None'") == 1
    assert "AZURE_STORAGE_CONNECTION_STRING" not in bicep
    assert "listKeys(" not in bicep
    assert "SharedAccessSignature" not in bicep


def test_data_plane_resources_and_passwordless_settings_match_the_plan() -> None:
    main = _read("infra/main.bicep")
    app = _read("infra/modules/app-service.bicep")
    for resource_name in (
        "'artifacts'",
        "'extraction-tasks'",
        "'extraction-tasks-poison'",
        "'Projects'",
        "'Runs'",
        "'Tasks'",
    ):
        assert resource_name in main
    for setting in (
        "AZURE_STORAGE_ACCOUNT_NAME",
        "AZURE_STORAGE_BLOB_ENDPOINT",
        "AZURE_STORAGE_QUEUE_ENDPOINT",
        "AZURE_STORAGE_TABLE_ENDPOINT",
        "AZURE_STORAGE_ARTIFACT_CONTAINER",
        "AZURE_STORAGE_TASK_QUEUE",
        "AZURE_STORAGE_POISON_QUEUE",
        "AZURE_STORAGE_PROJECTS_TABLE",
        "AZURE_STORAGE_RUNS_TABLE",
        "AZURE_STORAGE_TASKS_TABLE",
    ):
        assert setting in app
    assert "AZURE_STORAGE_AUTH_MODE: 'managed-identity'" in app


def test_hosted_factory_receives_cloud_context_and_lifecycle_gates() -> None:
    app = _read("infra/modules/app-service.bicep")
    for setting in (
        "AZURE_SUBSCRIPTION_ID: subscription().subscriptionId",
        "AZURE_RESOURCE_GROUP: resourceGroup().name",
        "AZURE_LOCATION: location",
        "RECEIPT_WEB_PROFILE: 'hosted'",
        "RECEIPT_STORAGE_BACKEND: 'azure'",
        "RECEIPT_EXECUTION_MODE: 'azure'",
        "RECEIPT_SERVER_WORKERS: '1'",
        "RECEIPT_WORKER_ENABLED: 'true'",
        "RECEIPT_WEB_ALLOWED_HOSTS: webApp.properties.defaultHostName",
        "RECEIPT_SEED_BUILTIN_PROJECTS: 'true'",
        "RECEIPT_SEED_CORD: 'true'",
        "RECEIPT_WEB_SROIE_RIGHTS_EVIDENCE: sroieRightsEvidence",
    ):
        assert setting in app
    assert "RECEIPT_SEED_SROIE: empty(trim(sroieRightsEvidence)) ? 'false' : 'true'" in app
    assert "RECEIPT_RUNTIME_PROFILE" not in app
    assert "RECEIPT_SROIE_RIGHTS_EVIDENCE:" not in app

    lifecycle_scripts = _read("azure.yaml") + _read("infra/scripts/oryx-pre-build.sh")
    assert "az storage" not in lifecycle_scripts.casefold()
    assert "seed" not in lifecycle_scripts.casefold()


def test_hosted_azure_ai_auth_is_entra_first_and_explicit() -> None:
    main = _read("infra/main.bicep")
    app = _read("infra/modules/app-service.bicep")
    parameters = json.loads(_read("infra/main.parameters.json"))["parameters"]

    assert "@allowed([\n  'entra'\n  'key'\n  'auto'\n])" in main
    assert "param azureAiAuthMode string = 'entra'" in main
    assert parameters["azureAiAuthMode"]["value"] == "${AZURE_AI_AUTH_MODE=entra}"
    assert "AZURE_AI_AUTH_MODE: azureAiAuthMode" in app
    assert "AZURE_AI_CREDENTIAL_KIND: 'managed_identity'" in app
    assert "azureAiAuthMode: azureAiAuthMode" in main
    assert "output AZURE_AI_AUTH_MODE string = azureAiAuthMode" in main


def test_infrastructure_settings_match_the_app_owned_hosted_startup_contract() -> None:
    app_settings = _read("infra/modules/app-service.bicep")
    runtime_source = _read("src/receipt_bench/web/app.py") + _read(
        "src/receipt_bench/web/settings.py"
    )
    for setting in (
        "AZURE_STORAGE_AUTH_MODE",
        "AZURE_STORAGE_BLOB_ENDPOINT",
        "AZURE_STORAGE_QUEUE_ENDPOINT",
        "AZURE_STORAGE_TABLE_ENDPOINT",
        "AZURE_STORAGE_ARTIFACT_CONTAINER",
        "AZURE_STORAGE_TASK_QUEUE",
        "AZURE_STORAGE_POISON_QUEUE",
        "AZURE_STORAGE_PROJECTS_TABLE",
        "AZURE_STORAGE_RUNS_TABLE",
        "AZURE_STORAGE_TASKS_TABLE",
        "RECEIPT_WEB_PROFILE",
        "RECEIPT_WEB_ALLOWED_HOSTS",
        "RECEIPT_WEB_SROIE_RIGHTS_EVIDENCE",
        "RECEIPT_EXECUTION_MODE",
        "RECEIPT_STORAGE_BACKEND",
        "RECEIPT_SERVER_WORKERS",
        "RECEIPT_WORKER_ENABLED",
        "RECEIPT_SEED_BUILTIN_PROJECTS",
        "RECEIPT_SEED_CORD",
        "RECEIPT_SEED_SROIE",
    ):
        assert f"{setting}:" in app_settings
        assert f'"{setting}"' in runtime_source

    assert "BuiltInProjectSeeder" in runtime_source
    assert "await startup_value" in runtime_source


def test_managed_identity_roles_are_deterministic_and_narrow() -> None:
    rbac = _read("infra/modules/rbac.bicep")
    for role_id in (
        "ba92f5b4-2d11-453d-a403-e96b0029c9fe",
        "974c5e8b-45b9-4653-ba55-5f855dd0fb88",
        "0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3",
        "4633458b-17de-408a-b874-0445c86b69e6",
    ):
        assert role_id in rbac
    assert rbac.count("principalType: 'ServicePrincipal'") == 4
    assert "name: guid(" in rbac
    assert "scope: blobContainers[index]" in rbac
    assert "scope: queues[index]" in rbac
    assert "scope: tables[index]" in rbac
    assert "scope: keyVault" in rbac


def test_foundry_role_is_scoped_to_the_existing_cross_resource_group_account() -> None:
    main = _read("infra/main.bicep")
    app = _read("infra/modules/app-service.bicep")
    foundry_rbac = _read("infra/modules/foundry-rbac.bicep")
    parameters = json.loads(_read("infra/main.parameters.json"))["parameters"]

    assert "param foundryResourceGroupName string = resourceGroupName" in main
    assert "param foundryCustomSubdomainName string = ''" in main
    assert parameters["foundryResourceGroupName"]["value"] == (
        "${AZURE_FOUNDRY_RESOURCE_GROUP}"
    )
    assert parameters["foundryCustomSubdomainName"]["value"] == (
        "${AZURE_FOUNDRY_CUSTOM_SUBDOMAIN=}"
    )
    assert (
        "resource foundryResourceGroup "
        "'Microsoft.Resources/resourceGroups@2024-03-01' existing"
    ) in main
    assert "name: foundryResourceGroupName" in main
    assert "scope: foundryResourceGroup" in main
    assert "foundryResourceGroupName: foundryResourceGroupName" in main
    assert (
        "foundryCustomSubdomainName: resolvedFoundryCustomSubdomainName"
        in main
    )
    assert "AZURE_FOUNDRY_RESOURCE_GROUP: foundryResourceGroupName" in app
    assert (
        "https://${foundryCustomSubdomainName}.services.ai.azure.com/"
        "api/projects/${foundryProjectName}"
    ) in app
    assert (
        "resource foundryAccount "
        "'Microsoft.CognitiveServices/accounts@2024-10-01' existing"
    ) in foundry_rbac
    assert "a97b65f3-24c7-4388-baec-2e87135dc908" in foundry_rbac
    assert "scope: foundryAccount" in foundry_rbac
    assert (
        "name: guid(foundryAccount.id, principalId, cognitiveServicesUserRoleId)"
        in foundry_rbac
    )
    assert "principalType: 'ServicePrincipal'" in foundry_rbac


def test_secrets_use_a_private_managed_identity_deployment_script() -> None:
    main = _read("infra/main.bicep")
    vault = _read("infra/modules/key-vault.bicep")
    app = _read("infra/modules/app-service.bicep")
    assert main.count("@secure()") == 8
    assert vault.count("@secure()") == 8
    assert "Microsoft.KeyVault/vaults/secrets" not in _all_bicep()
    assert "Microsoft.Resources/deploymentScripts@2023-08-01" in vault
    assert "Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31" in vault
    assert vault.count("secureValue:") == 8
    assert "azPowerShellVersion: '14.0'" in vault
    assert "id: deploymentScriptSubnetId" in vault
    assert "storageAccountName: deploymentScriptStorage.name" in vault
    assert "storageAccountKey:" not in vault
    assert "forceUpdateTag: deploymentRunTag" in vault
    assert "Set-AzKeyVaultSecret `" in vault
    assert "-SecretValue $secureValue `" in vault
    assert "Set-AzKeyVaultSecret `\n" in vault
    assert "| Out-Null" in vault
    assert "$DeploymentScriptOutputs" not in vault
    assert "Write-Host" not in vault
    assert "Write-Output" not in vault

    assert "b86a8fe4-44ce-4948-aee5-eccb2c155cd7" in vault
    assert "69566ab7-960f-475b-8e7c-b3118f30c6bd" in vault
    assert "scope: keyVault" in vault
    assert "scope: deploymentScriptStorage" in vault

    assert "resource deploymentScriptStorage " in vault
    assert "allowSharedKeyAccess: true" in vault
    assert vault.count("publicNetworkAccess: 'Disabled'") == 2
    assert vault.count("bypass: 'None'") == 1
    assert "publicNetworkAccess: 'Enabled'" not in vault
    assert vault.count("bypass: 'AzureServices'") == 1
    assert "groupIds: [\n            'file'\n          ]" in vault
    assert "deploymentScriptStoragePrivateDnsZoneId" in vault
    assert "Microsoft.Network/privateDnsZones/A@2024-06-01" in vault
    assert "deploymentScriptStoragePrivateEndpoint.properties.customDnsConfigs" in vault

    script_declaration = vault.split("resource secretWriter ", maxsplit=1)[1]
    for dependency in (
        "deploymentScriptStoragePrivateDnsRecord",
        "deploymentScriptStorageRoleAssignment",
        "privateDnsZoneGroup",
        "secretWriterRoleAssignment",
    ):
        assert dependency in script_declaration

    assert "@Microsoft.KeyVault(" not in app
    assert "AZURE_KEY_VAULT_URI" in app
    assert "AZURE_KEY_VAULT_NAME" in app
    assert app.count("_SECRET_NAME") == 8
    assert "output foundry" not in main.casefold()
    assert "output foundry" not in vault.casefold()


def test_foundry_api_key_secret_is_only_wired_for_key_mode_with_a_key() -> None:
    main = _read("infra/main.bicep")
    vault = _read("infra/modules/key-vault.bicep")
    app = _read("infra/modules/app-service.bicep")
    parameters = json.loads(_read("infra/main.parameters.json"))["parameters"]

    assert "param foundryApiKey string = ''" in main
    assert parameters["foundryApiKey"]["value"] == "${AZURE_FOUNDRY_API_KEY=}"
    assert (
        "var writeFoundryApiKey = "
        "azureAiAuthMode == 'key' && !empty(trim(foundryApiKey))"
    ) in main
    assert "writeFoundryApiKey: writeFoundryApiKey" in main
    assert "includeFoundryApiKeySecretName: writeFoundryApiKey" in main

    assert "param writeFoundryApiKey bool = false" in vault
    assert "param foundryApiKey string = ''" in vault
    assert "var foundryApiKeyEnvironmentVariables = writeFoundryApiKey" in vault
    assert "? [" in vault
    assert "environmentVariables: concat(" in vault
    assert "foundryApiKeyEnvironmentVariables," in vault
    assert "value: string(writeFoundryApiKey)" in vault
    assert "if ($env:WRITE_FOUNDRY_API_KEY -eq 'true') {" in vault
    assert "Remove-PrivateSecret -Name $env:FOUNDRY_API_KEY_NAME" in vault
    assert "Undo-AzKeyVaultSecretRemoval" in vault
    assert "FOUNDRY_API_KEY_NAME" in vault

    assert "var foundryApiKeyAppSetting = includeFoundryApiKeySecretName" in app
    assert "properties: union(" in app
    assert app.count("foundryApiKeyAppSetting") == 2


def test_parameters_are_azd_placeholders_not_secret_values() -> None:
    parameters = json.loads(_read("infra/main.parameters.json"))["parameters"]
    assert parameters["sroieRightsEvidence"]["value"] == "${SROIE_RIGHTS_EVIDENCE=}"
    assert "@maxLength(512)\nparam sroieRightsEvidence string = ''" in _read(
        "infra/main.bicep"
    )
    assert parameters["foundryApiKey"]["value"] == "${AZURE_FOUNDRY_API_KEY=}"
    secure_variables = {
        "foundryOpenAiEndpoint": "AZURE_FOUNDRY_OPENAI_ENDPOINT",
        "foundryModelsEndpoint": "AZURE_FOUNDRY_MODELS_ENDPOINT",
        "anthropicEndpoint": "AZURE_ANTHROPIC_ENDPOINT",
        "mistralOcrEndpoint": "AZURE_MISTRAL_OCR_ENDPOINT",
        "contentUnderstandingEndpoint": "AZURE_CONTENT_UNDERSTANDING_ENDPOINT",
        "documentIntelligenceEndpoint": "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT",
        "visionEndpoint": "AZURE_VISION_ENDPOINT",
    }
    for parameter_name, variable_name in secure_variables.items():
        assert parameters[parameter_name]["value"] == f"${{{variable_name}}}"


def test_existing_foundry_attachment_is_configuration_only() -> None:
    main = _read("infra/main.bicep")
    app = _read("infra/modules/app-service.bicep")
    foundry_rbac = _read("infra/modules/foundry-rbac.bicep")
    assert "@minLength(2)\nparam foundryAccountName string" in main
    assert "param foundryProjectName string = 'receipt-bench'" in main
    assert re.search(r"^param foundryAccountName string\s*$", main, re.MULTILINE)
    assert "param foundryResourceGroupName string = resourceGroupName" in main
    assert "AZURE_FOUNDRY_ACCOUNT: foundryAccountName" in app
    assert "AZURE_FOUNDRY_PROJECT: foundryProjectName" in app
    assert "AZURE_FOUNDRY_PROJECT_ENDPOINT:" in app
    assert (
        "resource foundryAccount "
        "'Microsoft.CognitiveServices/accounts@2024-10-01' existing"
    ) in foundry_rbac
    assert "sku:" not in foundry_rbac
    assert "kind:" not in foundry_rbac
    assert "/deployments" not in _all_bicep()


def test_guided_deployment_has_no_owner_specific_defaults() -> None:
    deployment = _read("scripts/deploy.ps1")
    provision = _read("scripts/provision.ps1")
    parameters = json.loads(_read("infra/main.parameters.json"))["parameters"]

    assert "scripts/deploy.ps1" in _read("README.md")
    assert "provision\", \"--preview\"" in deployment
    assert "up\", \"--no-prompt\"" in deployment
    assert '"env", "set", "--file", $envPath' in deployment
    assert "Apply this preview? Type 'deploy' to continue" in deployment
    assert "[switch]$Yes" in deployment
    assert "FoundryResourceGroup" in deployment
    assert "ApplicationResourceGroup" in provision
    assert "$validationArguments.ValidateOnly = $true" in deployment
    assert deployment.index("$validationArguments.ValidateOnly = $true") < deployment.index(
        '"group", "create"'
    )
    assert parameters["resourceGroupName"]["value"] == "${AZURE_RESOURCE_GROUP}"
    assert parameters["foundryResourceGroupName"]["value"] == (
        "${AZURE_FOUNDRY_RESOURCE_GROUP}"
    )
    assert parameters["foundryAccountName"]["value"] == "${AZURE_FOUNDRY_ACCOUNT}"
    assert parameters["azureAiAuthMode"]["value"] == "${AZURE_AI_AUTH_MODE=entra}"
    for script, parameter_names in (
        (deployment, ("SubscriptionId", "ResourceGroup", "FoundryResourceGroup", "FoundryAccount")),
        (provision, ("SubscriptionId", "ResourceGroup", "ApplicationResourceGroup", "AccountName")),
    ):
        parameter_block = script.split("\n)", maxsplit=1)[0]
        for parameter_name in parameter_names:
            assert re.search(
                rf'^\s*\[string\]\${parameter_name}\s*=\s*""\s*,?\s*$',
                parameter_block,
                re.MULTILINE,
            )


def test_module_output_dependencies_are_explicit_in_the_data_flow() -> None:
    main = _read("infra/main.bicep")
    assert "privateEndpointSubnetId: network.outputs.privateEndpointSubnetId" in main
    assert "deploymentScriptSubnetId: network.outputs.deploymentScriptSubnetId" in main
    assert (
        "deploymentScriptStoragePrivateDnsZoneId: "
        "network.outputs.privateDnsZoneIds[3]"
    ) in main
    assert "privateDnsZoneId: network.outputs.privateDnsZoneIds[4]" in main
    assert "storageAccountName: storage.outputs.storageAccountName" in main
    assert "keyVaultUri: keyVault.outputs.keyVaultUri" in main
    assert (
        "applicationInsightsConnectionString: "
        "monitoring.outputs.applicationInsightsConnectionString"
    ) in main
    assert "principalId: appService.outputs.principalId" in main
    assert "keyVaultName: keyVault.outputs.keyVaultName" in main


def test_resource_names_are_environment_scoped_and_length_safe() -> None:
    main = _read("infra/main.bicep")
    assert (
        "uniqueString(subscription().id, resourceGroupName, environmentName, location)"
        in main
    )
    assert "storageAccount: 'streceipt${resourceToken}'" in main
    assert "keyVault: 'kv-receipt-${take(resourceToken, 10)}'" in main
    assert "'azd-env-name': environmentName" in main
    assert "'azd-service-name': serviceName" in _read("infra/modules/app-service.bicep")


def test_resource_api_versions_are_stable_and_non_preview() -> None:
    bicep = _all_bicep()
    expected = {
        "Microsoft.Authorization/roleAssignments@2022-04-01",
        "Microsoft.CognitiveServices/accounts@2024-10-01",
        "Microsoft.Insights/components@2020-02-02",
        "Microsoft.KeyVault/vaults@2024-11-01",
        "Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31",
        "Microsoft.Network/privateDnsZones@2024-06-01",
        "Microsoft.Network/privateDnsZones/A@2024-06-01",
        "Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01",
        "Microsoft.Network/privateEndpoints@2024-07-01",
        "Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-07-01",
        "Microsoft.Network/natGateways@2024-07-01",
        "Microsoft.Network/publicIPAddresses@2024-07-01",
        "Microsoft.Network/virtualNetworks@2024-07-01",
        "Microsoft.Network/virtualNetworks/subnets@2024-07-01",
        "Microsoft.OperationalInsights/workspaces@2023-09-01",
        "Microsoft.Resources/deploymentScripts@2023-08-01",
        "Microsoft.Resources/resourceGroups@2024-03-01",
        "Microsoft.Storage/storageAccounts@2025-01-01",
        "Microsoft.Storage/storageAccounts/blobServices@2025-01-01",
        "Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01",
        "Microsoft.Storage/storageAccounts/queueServices@2025-01-01",
        "Microsoft.Storage/storageAccounts/queueServices/queues@2025-01-01",
        "Microsoft.Storage/storageAccounts/tableServices@2025-01-01",
        "Microsoft.Storage/storageAccounts/tableServices/tables@2025-01-01",
        "Microsoft.Web/serverfarms@2024-11-01",
        "Microsoft.Web/sites@2024-11-01",
        "Microsoft.Web/sites/config@2024-11-01",
    }
    actual = set(
        re.findall(
            r"'(Microsoft\.[^']+@\d{4}-\d{2}-\d{2}(?:-preview)?)'",
            bicep,
        )
    )
    assert actual == expected
    assert "-preview" not in bicep.casefold()


def test_outputs_include_absolute_https_app_url_and_resource_names() -> None:
    main = _read("infra/main.bicep")
    app = _read("infra/modules/app-service.bicep")
    for output_name in (
        "AZURE_RESOURCE_GROUP",
        "AZURE_FOUNDRY_RESOURCE_GROUP",
        "AZURE_AI_AUTH_MODE",
        "AZURE_WEB_APP_NAME",
        "AZURE_STORAGE_ACCOUNT_NAME",
        "AZURE_KEY_VAULT_NAME",
        "AZURE_LOG_ANALYTICS_WORKSPACE_ID",
        "AZURE_PYTHON_RUNTIME_VERSION",
        "SERVICE_WEB_URI",
        "WEB_URL",
    ):
        assert f"output {output_name} string" in main
    assert "output appUrl string = 'https://${webApp.properties.defaultHostName}'" in app


def test_stage_package_is_clean_atomic_and_complete(stage_workspace: Path) -> None:
    stage_package = _stage_function()
    required_dependencies = {
        "aiohttp",
        "azure-data-tables",
        "azure-identity",
        "azure-keyvault-secrets",
        "azure-storage-blob",
        "azure-storage-queue",
        "python-multipart",
    }
    repository = stage_workspace / "repository"
    _write_package_fixture(repository, dependencies=required_dependencies)
    stage = repository / "infra" / "package"

    stage_package(repository, stage)
    assert (stage / "src/receipt_bench/web/app.py").is_file()
    assert (stage / "frontend/src/main.ts").is_file()
    assert (stage / "src/receipt_bench/demo/static/index.html").is_file()
    assert not (stage / "data/manifests/benchmark.jsonl").exists()
    assert (stage / "infra/scripts/oryx-pre-build.sh").is_file()
    assert b"\r\n" not in (stage / "infra/scripts/oryx-pre-build.sh").read_bytes()
    assert not (stage / "infra/package").exists()
    assert not (stage / "infra/scripts/stage-package.py").exists()
    assert (stage / ".gitignore").read_text(encoding="utf-8") == "*\n!.gitignore\n"
    for forbidden in (
        "frontend/src/main.test.ts",
        "frontend/dist/stale.js",
        "frontend/.env.production",
        "src/receipt_bench/web/static/stale.js",
        "src/receipt_bench/web/test_app.py",
        "src/receipt_bench/web/__pycache__/app.pyc",
        "src/receipt_bench/restricted/private.jpg",
        "src/receipt_bench/results/output.json",
        "src/receipt_bench/secrets/credentials.json",
    ):
        assert not (stage / forbidden).exists()

    stale = stage / "stale-output.txt"
    stale.write_text("stale\n", encoding="utf-8")
    stage_package(repository, stage)
    assert not stale.exists()


def test_stage_package_can_refresh_from_an_azd_service_working_directory(
    stage_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies = {
        "aiohttp",
        "azure-data-tables",
        "azure-identity",
        "azure-keyvault-secrets",
        "azure-storage-blob",
        "azure-storage-queue",
        "python-multipart",
    }
    repository = stage_workspace / "repository"
    _write_package_fixture(repository, dependencies=dependencies)
    stage = repository / "infra" / "package"
    stage_package = _stage_function()
    stage_package(repository, stage)

    (stage / "stale-output.txt").write_text("stale\n", encoding="utf-8")
    (repository / "configs" / "current.yaml").write_text("current: true\n", encoding="utf-8")
    monkeypatch.chdir(stage)

    stage_package(repository, stage, in_place=True)

    assert Path.cwd() == stage
    assert not (stage / "stale-output.txt").exists()
    assert (stage / "configs" / "current.yaml").read_text(encoding="utf-8") == (
        "current: true\n"
    )


def test_stage_package_restores_the_previous_package_if_swap_fails(
    stage_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies = {
        "aiohttp",
        "azure-data-tables",
        "azure-identity",
        "azure-keyvault-secrets",
        "azure-storage-blob",
        "azure-storage-queue",
        "python-multipart",
    }
    repository = stage_workspace / "repository"
    _write_package_fixture(repository, dependencies=dependencies)
    stage = repository / "infra" / "package"
    stage.mkdir(parents=True)
    previous = stage / "previous.txt"
    previous.write_text("preserve me\n", encoding="utf-8")
    original_replace = Path.replace

    def fail_new_package_swap(source: Path, target: Path) -> Path:
        if source.name == ".package-staging" and target == stage:
            raise OSError("simulated package swap failure")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_new_package_swap)
    with pytest.raises(OSError, match="simulated package swap failure"):
        _stage_function()(repository, stage)

    assert previous.read_text(encoding="utf-8") == "preserve me\n"
    assert not (repository / "infra" / ".package-staging").exists()
    assert not (repository / "infra" / ".package-backup").exists()


def test_generated_package_directory_is_source_control_ignored() -> None:
    assert "infra/package/" in _read(".gitignore").splitlines()
    assert _read("infra/.gitignore") == (
        ".package-staging/\n.package-backup/\npackage/*\n!package/.gitignore\n"
    )


def test_authoritative_lock_contains_every_hosted_sdk_dependency() -> None:
    stage_module = _stage_module()
    locked_packages = stage_module["_locked_packages"](ROOT / "requirements.lock")
    assert stage_module["REQUIRED_HOSTED_DEPENDENCIES"] <= locked_packages


def test_stage_package_fails_clearly_for_missing_parent_dependencies(
    stage_workspace: Path,
) -> None:
    repository = stage_workspace / "repository"
    _write_package_fixture(repository, dependencies={"azure-identity"})
    with pytest.raises(
        RuntimeError,
        match=r"requirements\.lock is missing hosted Azure SDK dependencies: .*"
        r"azure-keyvault-secrets.*parent/API track",
    ):
        _stage_function()(repository, repository / "infra" / "package", dry_run=True)
    assert not (repository / "infra" / ".package-staging").exists()


def test_stage_package_rejects_a_python_runtime_mismatch(
    stage_workspace: Path,
) -> None:
    dependencies = {
        "aiohttp",
        "azure-data-tables",
        "azure-identity",
        "azure-keyvault-secrets",
        "azure-storage-blob",
        "azure-storage-queue",
        "python-multipart",
    }
    repository = stage_workspace / "repository"
    _write_package_fixture(repository, dependencies=dependencies)
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "0.0.0"\n'
        'requires-python = ">=3.13"\n',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match=r"Python >=3\.12"):
        _stage_function()(repository, repository / "infra" / "package", dry_run=True)


def test_stage_package_fails_clearly_for_missing_parent_startup_factory(
    stage_workspace: Path,
) -> None:
    dependencies = {
        "aiohttp",
        "azure-data-tables",
        "azure-identity",
        "azure-keyvault-secrets",
        "azure-storage-blob",
        "azure-storage-queue",
        "python-multipart",
    }
    repository = stage_workspace / "repository"
    _write_package_fixture(
        repository,
        dependencies=dependencies,
        include_factory=False,
    )
    with pytest.raises(
        RuntimeError,
        match=r"startup target is missing: receipt_bench\.web\.app:create_app",
    ):
        _stage_function()(repository, repository / "infra" / "package", dry_run=True)
    assert not (repository / "infra" / ".package-staging").exists()


def test_stage_package_rejects_an_incomplete_deployment_layout(
    stage_workspace: Path,
) -> None:
    dependencies = {
        "aiohttp",
        "azure-data-tables",
        "azure-identity",
        "azure-keyvault-secrets",
        "azure-storage-blob",
        "azure-storage-queue",
        "python-multipart",
    }
    repository = stage_workspace / "repository"
    _write_package_fixture(repository, dependencies=dependencies)
    (repository / "frontend" / "package-lock.json").unlink()

    with pytest.raises(
        RuntimeError,
        match=r"Deployment package is incomplete; missing: frontend/package-lock\.json",
    ):
        _stage_function()(repository, repository / "infra" / "package", dry_run=True)
    assert not (repository / "infra" / ".package-staging").exists()


def test_stage_package_rejects_factory_with_required_arguments(
    stage_workspace: Path,
) -> None:
    dependencies = {
        "aiohttp",
        "azure-data-tables",
        "azure-identity",
        "azure-keyvault-secrets",
        "azure-storage-blob",
        "azure-storage-queue",
        "python-multipart",
    }
    repository = stage_workspace / "repository"
    _write_package_fixture(repository, dependencies=dependencies)
    (repository / "src/receipt_bench/web/app.py").write_text(
        "def create_app(required):\n    return required\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="must be callable without arguments"):
        _stage_function()(repository, repository / "infra" / "package", dry_run=True)
