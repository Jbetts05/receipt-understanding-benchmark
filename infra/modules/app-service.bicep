targetScope = 'resourceGroup'

@description('Globally unique Linux web app name.')
param name string

@description('Linux App Service plan name.')
param planName string

@description('Azure region for App Service resources.')
param location string = resourceGroup().location

@description('Resource tags.')
param tags object = {}

@description('Azure Developer CLI service name.')
param serviceName string

@description('Python runtime version supported by App Service.')
param pythonRuntimeVersion string

@description('One-process FastAPI and queue-worker startup command.')
param startupCommand string

@description('Resource ID of the dedicated /26 regional VNet integration subnet.')
param appSubnetId string

@description('Workspace-based Application Insights connection string. Microsoft documents this value as non-secret.')
param applicationInsightsConnectionString string

@description('Storage account name.')
param storageAccountName string

@description('Standard Blob service endpoint. Private DNS resolves it to the private endpoint in the integration VNet.')
param storageBlobEndpoint string

@description('Standard Queue service endpoint. Private DNS resolves it to the private endpoint in the integration VNet.')
param storageQueueEndpoint string

@description('Standard Table service endpoint. Private DNS resolves it to the private endpoint in the integration VNet.')
param storageTableEndpoint string

@description('Key Vault name.')
param keyVaultName string

@description('Key Vault data-plane URI.')
param keyVaultUri string

@description('Existing Foundry account name.')
param foundryAccountName string

@description('Custom subdomain used by the existing Foundry account endpoints.')
param foundryCustomSubdomainName string

@description('Resource group containing the existing Foundry account.')
param foundryResourceGroupName string

@description('Existing Foundry project name.')
param foundryProjectName string

@description('Authentication mode used by Azure AI provider clients.')
@allowed([
  'entra'
  'key'
  'auto'
])
param azureAiAuthMode string

@description('Whether the hosted app should resolve the Foundry API key from Key Vault.')
param includeFoundryApiKeySecretName bool

@description('Key Vault secret names for Foundry credentials and endpoints.')
param secretNames object

@description('Blob container names. The first container holds immutable project and run artifacts.')
param blobContainerNames array

@description('Queue names in work/poison order.')
param queueNames array

@description('Table names in projects/runs/tasks/results order.')
param tableNames array

@description('Reference to rights-holder evidence. Empty disables hosted SROIE seeding.')
param sroieRightsEvidence string

@description('Environment name used in telemetry resource attributes.')
param environmentName string

var foundryApiKeyAppSetting = includeFoundryApiKeySecretName
  ? {
      AZURE_FOUNDRY_API_KEY_SECRET_NAME: secretNames.foundryApiKey
    }
  : {}

resource appServicePlan 'Microsoft.Web/serverfarms@2024-11-01' = {
  name: planName
  location: location
  tags: tags
  kind: 'linux'
  sku: {
    capacity: 1
    family: 'B'
    name: 'B1'
    size: 'B1'
    tier: 'Basic'
  }
  properties: {
    perSiteScaling: false
    reserved: true
    zoneRedundant: false
  }
}

resource webApp 'Microsoft.Web/sites@2024-11-01' = {
  name: name
  location: location
  tags: union(tags, {
    'azd-service-name': serviceName
  })
  kind: 'app,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    clientAffinityEnabled: false
    clientCertEnabled: false
    httpsOnly: true
    outboundVnetRouting: {
      allTraffic: false
      applicationTraffic: false
      backupRestoreTraffic: false
      contentShareTraffic: false
      imagePullTraffic: false
    }
    publicNetworkAccess: 'Enabled'
    serverFarmId: appServicePlan.id
    siteConfig: {
      alwaysOn: true
      appCommandLine: startupCommand
      detailedErrorLoggingEnabled: false
      ftpsState: 'Disabled'
      healthCheckPath: '/api/ready'
      http20Enabled: true
      httpLoggingEnabled: true
      linuxFxVersion: 'PYTHON|${pythonRuntimeVersion}'
      loadBalancing: 'LeastRequests'
      minTlsVersion: '1.2'
      numberOfWorkers: 1
      remoteDebuggingEnabled: false
      requestTracingEnabled: false
      scmMinTlsVersion: '1.2'
      use32BitWorkerProcess: false
      webSocketsEnabled: false
    }
    virtualNetworkSubnetId: appSubnetId
  }
}

resource appSettings 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: webApp
  name: 'appsettings'
  properties: union(
    {
      APPLICATIONINSIGHTS_CONNECTION_STRING: applicationInsightsConnectionString
      AZURE_AI_AUTH_MODE: azureAiAuthMode
      AZURE_AI_CREDENTIAL_KIND: 'managed_identity'
      AZURE_LOCATION: location
      AZURE_RESOURCE_GROUP: resourceGroup().name
      AZURE_SUBSCRIPTION_ID: subscription().subscriptionId
      AZURE_FOUNDRY_ACCOUNT: foundryAccountName
      AZURE_FOUNDRY_MODELS_ENDPOINT_SECRET_NAME: secretNames.foundryModelsEndpoint
      AZURE_FOUNDRY_OPENAI_ENDPOINT_SECRET_NAME: secretNames.foundryOpenAiEndpoint
      AZURE_FOUNDRY_PROJECT: foundryProjectName
      AZURE_FOUNDRY_PROJECT_ENDPOINT: 'https://${foundryCustomSubdomainName}.services.ai.azure.com/api/projects/${foundryProjectName}'
      AZURE_FOUNDRY_RESOURCE_GROUP: foundryResourceGroupName
      AZURE_ANTHROPIC_ENDPOINT_SECRET_NAME: secretNames.anthropicEndpoint
      AZURE_CONTENT_UNDERSTANDING_ENDPOINT_SECRET_NAME: secretNames.contentUnderstandingEndpoint
      AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT_SECRET_NAME: secretNames.documentIntelligenceEndpoint
      AZURE_KEY_VAULT_NAME: keyVaultName
      AZURE_KEY_VAULT_URI: keyVaultUri
      AZURE_MISTRAL_OCR_ENDPOINT_SECRET_NAME: secretNames.mistralOcrEndpoint
      AZURE_STORAGE_ACCOUNT_NAME: storageAccountName
      AZURE_STORAGE_AUTH_MODE: 'managed-identity'
      AZURE_STORAGE_BLOB_ENDPOINT: storageBlobEndpoint
      AZURE_STORAGE_POISON_QUEUE: queueNames[1]
      AZURE_STORAGE_ARTIFACT_CONTAINER: blobContainerNames[0]
      AZURE_STORAGE_PROJECTS_TABLE: tableNames[0]
      AZURE_STORAGE_QUEUE_ENDPOINT: storageQueueEndpoint
      AZURE_STORAGE_RUNS_TABLE: tableNames[1]
      AZURE_STORAGE_TABLE_ENDPOINT: storageTableEndpoint
      AZURE_STORAGE_TASK_QUEUE: queueNames[0]
      AZURE_STORAGE_TASKS_TABLE: tableNames[2]
      AZURE_VISION_ENDPOINT_SECRET_NAME: secretNames.visionEndpoint
      ENABLE_ORYX_BUILD: 'true'
      OTEL_RESOURCE_ATTRIBUTES: 'deployment.environment.name=${environmentName},service.namespace=receipt-bench'
      OTEL_SERVICE_NAME: 'receipt-bench-web'
      PORT: '8000'
      PRE_BUILD_COMMAND: 'bash infra/scripts/oryx-pre-build.sh'
      PYTHONUNBUFFERED: '1'
      RECEIPT_EXECUTION_MODE: 'azure'
      RECEIPT_SEED_BUILTIN_PROJECTS: 'true'
      RECEIPT_SEED_CORD: 'true'
      RECEIPT_SEED_SROIE: empty(trim(sroieRightsEvidence)) ? 'false' : 'true'
      RECEIPT_SERVER_WORKERS: '1'
      RECEIPT_STORAGE_BACKEND: 'azure'
      RECEIPT_WEB_ALLOWED_HOSTS: webApp.properties.defaultHostName
      RECEIPT_WEB_PROFILE: 'hosted'
      RECEIPT_WEB_SROIE_RIGHTS_EVIDENCE: sroieRightsEvidence
      RECEIPT_WORKER_ENABLED: 'true'
      SCM_DO_BUILD_DURING_DEPLOYMENT: 'true'
      WEBSITES_CONTAINER_START_TIME_LIMIT: '1800'
      WEBSITE_HEALTHCHECK_MAXPINGFAILURES: '10'
      WEBSITE_HTTPLOGGING_RETENTION_DAYS: '7'
      WEBSITE_WARMUP_PATH: '/api/health'
      WEBSITE_WARMUP_STATUSES: '200'
    },
    foundryApiKeyAppSetting
  )
}

resource logConfig 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: webApp
  name: 'logs'
  properties: {
    applicationLogs: {
      fileSystem: {
        level: 'Off'
      }
    }
    detailedErrorMessages: {
      enabled: false
    }
    failedRequestsTracing: {
      enabled: false
    }
    httpLogs: {
      fileSystem: {
        enabled: true
        retentionInDays: 7
        retentionInMb: 35
      }
    }
  }
}

output appId string = webApp.id
output appName string = webApp.name
output appUrl string = 'https://${webApp.properties.defaultHostName}'
output principalId string = webApp.identity.principalId
