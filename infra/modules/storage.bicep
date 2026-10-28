targetScope = 'resourceGroup'

@description('Globally unique Storage account name.')
@minLength(3)
@maxLength(24)
param name string

@description('Azure region for the Storage account and private endpoints.')
param location string = resourceGroup().location

@description('Resource tags.')
param tags object = {}

@description('Resource ID of the dedicated private endpoint subnet.')
param privateEndpointSubnetId string

@description('Private DNS zone resource IDs keyed by blob, queue, and table.')
param privateDnsZoneIds object

@description('Private Blob container names.')
param blobContainerNames array

@description('Queue names, including the poison queue.')
param queueNames array

@description('Table names.')
param tableNames array

resource storageAccount 'Microsoft.Storage/storageAccounts@2025-01-01' = {
  name: name
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowCrossTenantReplication: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    dnsEndpointType: 'Standard'
    isHnsEnabled: false
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Disabled'
    supportsHttpsTrafficOnly: true
    networkAcls: {
      bypass: 'None'
      defaultAction: 'Deny'
      ipRules: []
      resourceAccessRules: []
      virtualNetworkRules: []
    }
    encryption: {
      keySource: 'Microsoft.Storage'
      services: {
        blob: {
          enabled: true
          keyType: 'Account'
        }
        file: {
          enabled: true
          keyType: 'Account'
        }
      }
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2025-01-01' = {
  parent: storageAccount
  name: 'default'
  properties: {
    automaticSnapshotPolicyEnabled: false
    changeFeed: {
      enabled: false
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: 30
    }
    deleteRetentionPolicy: {
      allowPermanentDelete: false
      enabled: true
      days: 30
    }
    isVersioningEnabled: true
  }
}

resource blobContainers 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' = [
  for containerName in blobContainerNames: {
    parent: blobService
    name: containerName
    properties: {
      defaultEncryptionScope: '$account-encryption-key'
      denyEncryptionScopeOverride: true
      publicAccess: 'None'
    }
  }
]

resource queueService 'Microsoft.Storage/storageAccounts/queueServices@2025-01-01' = {
  parent: storageAccount
  name: 'default'
  properties: {}
}

resource queues 'Microsoft.Storage/storageAccounts/queueServices/queues@2025-01-01' = [
  for queueName in queueNames: {
    parent: queueService
    name: queueName
    properties: {
      metadata: {}
    }
  }
]

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2025-01-01' = {
  parent: storageAccount
  name: 'default'
  properties: {}
}

resource tables 'Microsoft.Storage/storageAccounts/tableServices/tables@2025-01-01' = [
  for tableName in tableNames: {
    parent: tableService
    name: tableName
    properties: {}
  }
]

var privateEndpointDefinitions = [
  {
    groupId: 'blob'
    privateDnsZoneId: privateDnsZoneIds.blob
  }
  {
    groupId: 'queue'
    privateDnsZoneId: privateDnsZoneIds.queue
  }
  {
    groupId: 'table'
    privateDnsZoneId: privateDnsZoneIds.table
  }
]

resource privateEndpoints 'Microsoft.Network/privateEndpoints@2024-07-01' = [
  for definition in privateEndpointDefinitions: {
    name: 'pe-${name}-${definition.groupId}'
    location: location
    tags: tags
    properties: {
      customNetworkInterfaceName: 'nic-pe-${name}-${definition.groupId}'
      privateLinkServiceConnections: [
        {
          name: 'plsc-${name}-${definition.groupId}'
          properties: {
            groupIds: [
              definition.groupId
            ]
            privateLinkServiceId: storageAccount.id
          }
        }
      ]
      subnet: {
        id: privateEndpointSubnetId
      }
    }
  }
]

resource privateDnsZoneGroups 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-07-01' = [
  for (definition, index) in privateEndpointDefinitions: {
    parent: privateEndpoints[index]
    name: 'default'
    properties: {
      privateDnsZoneConfigs: [
        {
          name: '${definition.groupId}-zone'
          properties: {
            privateDnsZoneId: definition.privateDnsZoneId
          }
        }
      ]
    }
  }
]

output storageAccountId string = storageAccount.id
output storageAccountName string = storageAccount.name
output blobEndpoint string = 'https://${storageAccount.name}.blob.${environment().suffixes.storage}'
output queueEndpoint string = 'https://${storageAccount.name}.queue.${environment().suffixes.storage}'
output tableEndpoint string = 'https://${storageAccount.name}.table.${environment().suffixes.storage}'
