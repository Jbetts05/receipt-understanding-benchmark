targetScope = 'resourceGroup'

@description('Globally unique Key Vault name.')
@minLength(3)
@maxLength(24)
param name string

@description('Azure region for Key Vault and its private endpoint.')
param location string = resourceGroup().location

@description('Resource tags.')
param tags object = {}

@description('Resource ID of the dedicated private endpoint subnet.')
param privateEndpointSubnetId string

@description('Resource ID of the delegated subnet used by the private deployment script ACI.')
param deploymentScriptSubnetId string

@description('Resource ID of privatelink.file.core.windows.net for deployment-script storage.')
param deploymentScriptStoragePrivateDnsZoneId string

@description('Resource ID of privatelink.vaultcore.azure.net.')
param privateDnsZoneId string

@description('Stable Key Vault secret names. Values are never returned by this module.')
param secretNames object

@description('Forces the idempotent private secret writer to run on every infrastructure deployment.')
param deploymentRunTag string = utcNow()

@description('Whether the Foundry API key is present and should be written to Key Vault.')
param writeFoundryApiKey bool = false

@secure()
param foundryApiKey string = ''

@secure()
param foundryOpenAiEndpoint string

@secure()
param foundryModelsEndpoint string

@secure()
param anthropicEndpoint string

@secure()
param mistralOcrEndpoint string

@secure()
param contentUnderstandingEndpoint string

@secure()
param documentIntelligenceEndpoint string

@secure()
param visionEndpoint string

var deploymentScriptStorageName = 'stds${uniqueString(resourceGroup().id, name)}'
var keyVaultSecretsOfficerRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
)
var storageFileDataPrivilegedContributorRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '69566ab7-960f-475b-8e7c-b3118f30c6bd'
)
var foundryApiKeyEnvironmentVariables = writeFoundryApiKey
  ? [
      {
        name: 'FOUNDRY_API_KEY'
        secureValue: foundryApiKey
      }
    ]
  : []

resource keyVault 'Microsoft.KeyVault/vaults@2024-11-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    accessPolicies: []
    enablePurgeProtection: true
    enableRbacAuthorization: true
    enableSoftDelete: true
    enabledForDeployment: false
    enabledForDiskEncryption: false
    enabledForTemplateDeployment: false
    networkAcls: {
      bypass: 'None'
      defaultAction: 'Deny'
      ipRules: []
      virtualNetworkRules: []
    }
    publicNetworkAccess: 'Disabled'
    sku: {
      family: 'A'
      name: 'standard'
    }
    softDeleteRetentionInDays: 90
    tenantId: subscription().tenantId
  }
}

// Azure Container Instances mounts the deployment-script file share with local
// authentication. The dedicated account has no public route and stores no app data.
resource deploymentScriptStorage 'Microsoft.Storage/storageAccounts@2025-01-01' = {
  name: deploymentScriptStorageName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    allowBlobPublicAccess: false
    allowCrossTenantReplication: false
    allowSharedKeyAccess: true
    defaultToOAuthAuthentication: true
    dnsEndpointType: 'Standard'
    isHnsEnabled: false
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Disabled'
    supportsHttpsTrafficOnly: true
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
      ipRules: []
      resourceAccessRules: []
      virtualNetworkRules: []
    }
    encryption: {
      keySource: 'Microsoft.Storage'
      services: {
        file: {
          enabled: true
          keyType: 'Account'
        }
      }
    }
  }
}

resource secretWriterIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${name}-secret-writer'
  location: location
  tags: tags
}

resource secretWriterRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, secretWriterIdentity.id, keyVaultSecretsOfficerRoleId)
  scope: keyVault
  properties: {
    description: 'Write application secrets from the private deployment script.'
    principalId: secretWriterIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: keyVaultSecretsOfficerRoleId
  }
}

resource deploymentScriptStorageRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(deploymentScriptStorage.id, secretWriterIdentity.id, storageFileDataPrivilegedContributorRoleId)
  scope: deploymentScriptStorage
  properties: {
    description: 'Access the private Azure Files share required by the deployment script.'
    principalId: secretWriterIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: storageFileDataPrivilegedContributorRoleId
  }
}

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2024-07-01' = {
  name: 'pe-${name}-vault'
  location: location
  tags: tags
  properties: {
    customNetworkInterfaceName: 'nic-pe-${name}-vault'
    privateLinkServiceConnections: [
      {
        name: 'plsc-${name}-vault'
        properties: {
          groupIds: [
            'vault'
          ]
          privateLinkServiceId: keyVault.id
        }
      }
    ]
    subnet: {
      id: privateEndpointSubnetId
    }
  }
}

resource privateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-07-01' = {
  parent: privateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'vault-zone'
        properties: {
          privateDnsZoneId: privateDnsZoneId
        }
      }
    ]
  }
}

resource deploymentScriptStoragePrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-07-01' = {
  name: 'pe-${deploymentScriptStorage.name}-file'
  location: location
  tags: tags
  properties: {
    customNetworkInterfaceName: 'nic-pe-${deploymentScriptStorage.name}-file'
    privateLinkServiceConnections: [
      {
        name: 'plsc-${deploymentScriptStorage.name}-file'
        properties: {
          groupIds: [
            'file'
          ]
          privateLinkServiceId: deploymentScriptStorage.id
        }
      }
    ]
    subnet: {
      id: privateEndpointSubnetId
    }
  }
}

resource deploymentScriptStoragePrivateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' existing = {
  name: last(split(deploymentScriptStoragePrivateDnsZoneId, '/'))
}

resource deploymentScriptStoragePrivateDnsRecord 'Microsoft.Network/privateDnsZones/A@2024-06-01' = {
  parent: deploymentScriptStoragePrivateDnsZone
  name: deploymentScriptStorage.name
  properties: {
    ttl: 10
    aRecords: [
      {
        ipv4Address: first(first(deploymentScriptStoragePrivateEndpoint.properties.customDnsConfigs)!.ipAddresses)
      }
    ]
  }
}

resource secretWriter 'Microsoft.Resources/deploymentScripts@2023-08-01' = {
  name: 'ds-${name}-secrets'
  location: location
  tags: tags
  kind: 'AzurePowerShell'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${secretWriterIdentity.id}': {}
    }
  }
  properties: {
    azPowerShellVersion: '14.0'
    cleanupPreference: 'Always'
    containerSettings: {
      subnetIds: [
        {
          id: deploymentScriptSubnetId
        }
      ]
    }
    environmentVariables: concat(
      [
        {
          name: 'KEY_VAULT_NAME'
          value: keyVault.name
        }
        {
          name: 'WRITE_FOUNDRY_API_KEY'
          value: string(writeFoundryApiKey)
        }
        {
          name: 'FOUNDRY_API_KEY_NAME'
          value: secretNames.foundryApiKey
        }
      ],
      foundryApiKeyEnvironmentVariables,
      [
        {
          name: 'FOUNDRY_OPENAI_ENDPOINT_NAME'
          value: secretNames.foundryOpenAiEndpoint
        }
        {
          name: 'FOUNDRY_OPENAI_ENDPOINT'
          secureValue: foundryOpenAiEndpoint
        }
        {
          name: 'FOUNDRY_MODELS_ENDPOINT_NAME'
          value: secretNames.foundryModelsEndpoint
        }
        {
          name: 'FOUNDRY_MODELS_ENDPOINT'
          secureValue: foundryModelsEndpoint
        }
        {
          name: 'ANTHROPIC_ENDPOINT_NAME'
          value: secretNames.anthropicEndpoint
        }
        {
          name: 'ANTHROPIC_ENDPOINT'
          secureValue: anthropicEndpoint
        }
        {
          name: 'MISTRAL_OCR_ENDPOINT_NAME'
          value: secretNames.mistralOcrEndpoint
        }
        {
          name: 'MISTRAL_OCR_ENDPOINT'
          secureValue: mistralOcrEndpoint
        }
        {
          name: 'CONTENT_UNDERSTANDING_ENDPOINT_NAME'
          value: secretNames.contentUnderstandingEndpoint
        }
        {
          name: 'CONTENT_UNDERSTANDING_ENDPOINT'
          secureValue: contentUnderstandingEndpoint
        }
        {
          name: 'DOCUMENT_INTELLIGENCE_ENDPOINT_NAME'
          value: secretNames.documentIntelligenceEndpoint
        }
        {
          name: 'DOCUMENT_INTELLIGENCE_ENDPOINT'
          secureValue: documentIntelligenceEndpoint
        }
        {
          name: 'VISION_ENDPOINT_NAME'
          value: secretNames.visionEndpoint
        }
        {
          name: 'VISION_ENDPOINT'
          secureValue: visionEndpoint
        }
      ]
    )
    forceUpdateTag: deploymentRunTag
    retentionInterval: 'P1D'
    scriptContent: '''
      $ErrorActionPreference = 'Stop'
      $InformationPreference = 'SilentlyContinue'
      $ProgressPreference = 'SilentlyContinue'
      $WarningPreference = 'SilentlyContinue'

      function Set-PrivateSecret {
        param(
          [Parameter(Mandatory = $true)]
          [string] $Name,
          [Parameter(Mandatory = $true)]
          [string] $Value
        )

        $removed = Get-AzKeyVaultSecret `
          -VaultName $env:KEY_VAULT_NAME `
          -Name $Name `
          -InRemovedState `
          -ErrorAction SilentlyContinue
        if ($null -ne $removed) {
          Undo-AzKeyVaultSecretRemoval `
            -VaultName $env:KEY_VAULT_NAME `
            -Name $Name | Out-Null
          Start-Sleep -Seconds 10
        }

        for ($attempt = 1; $attempt -le 60; $attempt++) {
          try {
            $secureValue = ConvertTo-SecureString $Value -AsPlainText -Force
            Set-AzKeyVaultSecret `
              -VaultName $env:KEY_VAULT_NAME `
              -Name $Name `
              -SecretValue $secureValue `
              -ContentType 'text/plain' | Out-Null
            return
          }
          catch {
            if ($attempt -eq 60) {
              throw "Failed to set Key Vault secret '$Name' after $attempt attempts."
            }
            Start-Sleep -Seconds 10
          }
          finally {
            $secureValue = $null
          }
        }
      }

      function Remove-PrivateSecret {
        param(
          [Parameter(Mandatory = $true)]
          [string] $Name
        )

        for ($attempt = 1; $attempt -le 60; $attempt++) {
          try {
            Remove-AzKeyVaultSecret `
              -VaultName $env:KEY_VAULT_NAME `
              -Name $Name `
              -Force `
              -ErrorAction Stop
            return
          }
          catch {
            if ($_.Exception.Message -match '(?i)(SecretNotFound|not found)') {
              return
            }
            if ($attempt -eq 60) {
              throw "Failed to remove Key Vault secret '$Name' after $attempt attempts."
            }
            Start-Sleep -Seconds 10
          }
        }
      }

      if ($env:WRITE_FOUNDRY_API_KEY -eq 'true') {
        Set-PrivateSecret -Name $env:FOUNDRY_API_KEY_NAME -Value $env:FOUNDRY_API_KEY
      }
      else {
        Remove-PrivateSecret -Name $env:FOUNDRY_API_KEY_NAME
      }
      Set-PrivateSecret -Name $env:FOUNDRY_OPENAI_ENDPOINT_NAME -Value $env:FOUNDRY_OPENAI_ENDPOINT
      Set-PrivateSecret -Name $env:FOUNDRY_MODELS_ENDPOINT_NAME -Value $env:FOUNDRY_MODELS_ENDPOINT
      Set-PrivateSecret -Name $env:ANTHROPIC_ENDPOINT_NAME -Value $env:ANTHROPIC_ENDPOINT
      Set-PrivateSecret -Name $env:MISTRAL_OCR_ENDPOINT_NAME -Value $env:MISTRAL_OCR_ENDPOINT
      Set-PrivateSecret -Name $env:CONTENT_UNDERSTANDING_ENDPOINT_NAME -Value $env:CONTENT_UNDERSTANDING_ENDPOINT
      Set-PrivateSecret -Name $env:DOCUMENT_INTELLIGENCE_ENDPOINT_NAME -Value $env:DOCUMENT_INTELLIGENCE_ENDPOINT
      Set-PrivateSecret -Name $env:VISION_ENDPOINT_NAME -Value $env:VISION_ENDPOINT
    '''
    storageAccountSettings: {
      storageAccountName: deploymentScriptStorage.name
    }
    timeout: 'PT20M'
  }
  dependsOn: [
    deploymentScriptStoragePrivateDnsRecord
    deploymentScriptStorageRoleAssignment
    privateDnsZoneGroup
    secretWriterRoleAssignment
  ]
}

output keyVaultId string = keyVault.id
output keyVaultName string = keyVault.name
output keyVaultUri string = keyVault.properties.vaultUri
