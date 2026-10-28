targetScope = 'subscription'

@description('Azure Developer CLI environment name. It participates in deterministic resource naming.')
@minLength(1)
@maxLength(64)
param environmentName string

@description('Azure region confirmed by the operator after model and service availability checks.')
@minLength(1)
param location string = 'swedencentral'

@description('Resource group created by the guided deployment script for application resources.')
@minLength(1)
param resourceGroupName string

@description('Resource group containing the existing Microsoft Foundry account. Defaults to the application resource group.')
@minLength(1)
param foundryResourceGroupName string = resourceGroupName

@description('Microsoft Foundry account created or verified by the guided deployment script.')
@minLength(2)
param foundryAccountName string

@description('Custom subdomain used by the existing Foundry account. Empty preserves compatibility when it matches the ARM account name.')
param foundryCustomSubdomainName string = ''

@description('Microsoft Foundry project created or verified by the guided deployment script.')
@minLength(1)
param foundryProjectName string = 'receipt-bench'

@description('Authentication mode used by Azure AI provider clients.')
@allowed([
  'entra'
  'key'
  'auto'
])
param azureAiAuthMode string = 'entra'

@description('Current App Service Python runtime. Override only with a version returned by az webapp list-runtimes --os linux.')
@allowed([
  '3.12'
])
param pythonRuntimeVersion string = '3.12'

@description('Reference to actual SROIE rights-holder authorization evidence. Empty keeps SROIE hosted seeding disabled.')
@maxLength(512)
param sroieRightsEvidence string = ''

@secure()
param foundryApiKey string = ''

@secure()
@minLength(1)
param foundryOpenAiEndpoint string

@secure()
@minLength(1)
param foundryModelsEndpoint string

@secure()
@minLength(1)
param anthropicEndpoint string

@secure()
@minLength(1)
param mistralOcrEndpoint string

@secure()
@minLength(1)
param contentUnderstandingEndpoint string

@secure()
@minLength(1)
param documentIntelligenceEndpoint string

@secure()
@minLength(1)
param visionEndpoint string

var resourceToken = uniqueString(subscription().id, resourceGroupName, environmentName, location)
var resourceNames = {
  appServicePlan: 'asp-receipt-${resourceToken}'
  webApp: 'app-receipt-${resourceToken}'
  storageAccount: 'streceipt${resourceToken}'
  keyVault: 'kv-receipt-${take(resourceToken, 10)}'
  virtualNetwork: 'vnet-receipt-${resourceToken}'
  logAnalytics: 'log-receipt-${resourceToken}'
  applicationInsights: 'appi-receipt-${resourceToken}'
}
var tags = {
  'azd-env-name': environmentName
  application: 'receipt-bench'
  environment: environmentName
  'managed-by': 'azd-bicep'
}
var privateDnsZoneNames = [
  #disable-next-line no-hardcoded-env-urls
  'privatelink.blob.core.windows.net'
  #disable-next-line no-hardcoded-env-urls
  'privatelink.queue.core.windows.net'
  #disable-next-line no-hardcoded-env-urls
  'privatelink.table.core.windows.net'
  #disable-next-line no-hardcoded-env-urls
  'privatelink.file.core.windows.net'
  'privatelink.vaultcore.azure.net'
]
var blobContainerNames = [
  'artifacts'
]
var queueNames = [
  'extraction-tasks'
  'extraction-tasks-poison'
]
var tableNames = [
  'Projects'
  'Runs'
  'Tasks'
]
var secretNames = {
  foundryApiKey: 'foundry-api-key'
  foundryOpenAiEndpoint: 'foundry-openai-endpoint'
  foundryModelsEndpoint: 'foundry-models-endpoint'
  anthropicEndpoint: 'anthropic-endpoint'
  mistralOcrEndpoint: 'mistral-ocr-endpoint'
  contentUnderstandingEndpoint: 'content-understanding-endpoint'
  documentIntelligenceEndpoint: 'document-intelligence-endpoint'
  visionEndpoint: 'vision-endpoint'
}
var writeFoundryApiKey = azureAiAuthMode == 'key' && !empty(trim(foundryApiKey))
var resolvedFoundryCustomSubdomainName = empty(trim(foundryCustomSubdomainName))
  ? foundryAccountName
  : foundryCustomSubdomainName
var appStartupCommand = 'python -m uvicorn receipt_bench.web.app:create_app --factory --host 0.0.0.0 --port $PORT --workers 1 --no-access-log'

resource targetResourceGroup 'Microsoft.Resources/resourceGroups@2024-03-01' existing = {
  name: resourceGroupName
}

resource foundryResourceGroup 'Microsoft.Resources/resourceGroups@2024-03-01' existing = {
  name: foundryResourceGroupName
}

module network './modules/network.bicep' = {
  name: 'network-${resourceToken}'
  scope: targetResourceGroup
  params: {
    name: resourceNames.virtualNetwork
    location: location
    tags: tags
    privateDnsZoneNames: privateDnsZoneNames
  }
}

module monitoring './modules/monitoring.bicep' = {
  name: 'monitoring-${resourceToken}'
  scope: targetResourceGroup
  params: {
    name: resourceNames.applicationInsights
    workspaceName: resourceNames.logAnalytics
    location: location
    tags: tags
  }
}

module storage './modules/storage.bicep' = {
  name: 'storage-${resourceToken}'
  scope: targetResourceGroup
  params: {
    name: resourceNames.storageAccount
    location: location
    tags: tags
    privateEndpointSubnetId: network.outputs.privateEndpointSubnetId
    privateDnsZoneIds: {
      blob: network.outputs.privateDnsZoneIds[0]
      queue: network.outputs.privateDnsZoneIds[1]
      table: network.outputs.privateDnsZoneIds[2]
    }
    blobContainerNames: blobContainerNames
    queueNames: queueNames
    tableNames: tableNames
  }
}

module keyVault './modules/key-vault.bicep' = {
  name: 'key-vault-${resourceToken}'
  scope: targetResourceGroup
  params: {
    name: resourceNames.keyVault
    location: location
    tags: tags
    privateEndpointSubnetId: network.outputs.privateEndpointSubnetId
    deploymentScriptSubnetId: network.outputs.deploymentScriptSubnetId
    deploymentScriptStoragePrivateDnsZoneId: network.outputs.privateDnsZoneIds[3]
    privateDnsZoneId: network.outputs.privateDnsZoneIds[4]
    secretNames: secretNames
    foundryApiKey: foundryApiKey
    writeFoundryApiKey: writeFoundryApiKey
    foundryOpenAiEndpoint: foundryOpenAiEndpoint
    foundryModelsEndpoint: foundryModelsEndpoint
    anthropicEndpoint: anthropicEndpoint
    mistralOcrEndpoint: mistralOcrEndpoint
    contentUnderstandingEndpoint: contentUnderstandingEndpoint
    documentIntelligenceEndpoint: documentIntelligenceEndpoint
    visionEndpoint: visionEndpoint
  }
}

module appService './modules/app-service.bicep' = {
  name: 'app-service-${resourceToken}'
  scope: targetResourceGroup
  params: {
    name: resourceNames.webApp
    planName: resourceNames.appServicePlan
    location: location
    tags: tags
    serviceName: 'web'
    pythonRuntimeVersion: pythonRuntimeVersion
    startupCommand: appStartupCommand
    appSubnetId: network.outputs.appSubnetId
    applicationInsightsConnectionString: monitoring.outputs.applicationInsightsConnectionString
    storageAccountName: storage.outputs.storageAccountName
    storageBlobEndpoint: storage.outputs.blobEndpoint
    storageQueueEndpoint: storage.outputs.queueEndpoint
    storageTableEndpoint: storage.outputs.tableEndpoint
    keyVaultName: keyVault.outputs.keyVaultName
    keyVaultUri: keyVault.outputs.keyVaultUri
    azureAiAuthMode: azureAiAuthMode
    foundryAccountName: foundryAccountName
    foundryCustomSubdomainName: resolvedFoundryCustomSubdomainName
    foundryResourceGroupName: foundryResourceGroupName
    foundryProjectName: foundryProjectName
    includeFoundryApiKeySecretName: writeFoundryApiKey
    secretNames: secretNames
    blobContainerNames: blobContainerNames
    queueNames: queueNames
    tableNames: tableNames
    sroieRightsEvidence: sroieRightsEvidence
    environmentName: environmentName
  }
}

module rbac './modules/rbac.bicep' = {
  name: 'rbac-${resourceToken}'
  scope: targetResourceGroup
  params: {
    name: 'receipt-app-data-access'
    location: location
    tags: tags
    principalId: appService.outputs.principalId
    storageAccountName: storage.outputs.storageAccountName
    keyVaultName: keyVault.outputs.keyVaultName
    blobContainerNames: blobContainerNames
    queueNames: queueNames
    tableNames: tableNames
  }
}

module foundryRbac './modules/foundry-rbac.bicep' = {
  name: 'foundry-rbac-${resourceToken}'
  scope: foundryResourceGroup
  params: {
    foundryAccountName: foundryAccountName
    principalId: appService.outputs.principalId
  }
}

output AZURE_RESOURCE_GROUP string = targetResourceGroup.name
output AZURE_FOUNDRY_RESOURCE_GROUP string = foundryResourceGroup.name
output AZURE_FOUNDRY_CUSTOM_SUBDOMAIN string = resolvedFoundryCustomSubdomainName
output AZURE_AI_AUTH_MODE string = azureAiAuthMode
output AZURE_WEB_APP_NAME string = appService.outputs.appName
output AZURE_STORAGE_ACCOUNT_NAME string = storage.outputs.storageAccountName
output AZURE_KEY_VAULT_NAME string = keyVault.outputs.keyVaultName
output AZURE_LOG_ANALYTICS_WORKSPACE_ID string = monitoring.outputs.logAnalyticsWorkspaceId
output AZURE_PYTHON_RUNTIME_VERSION string = pythonRuntimeVersion
output SERVICE_WEB_URI string = appService.outputs.appUrl
output WEB_URL string = appService.outputs.appUrl
