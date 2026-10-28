targetScope = 'resourceGroup'

@description('Logical role-assignment module name.')
param name string

@description('Accepted for a consistent module interface; role assignments are global resources.')
param location string = resourceGroup().location

@description('Accepted for a consistent module interface; role assignments do not support tags.')
param tags object = {}

@description('Object ID of the App Service system-assigned managed identity.')
param principalId string

@description('Storage account name.')
param storageAccountName string

@description('Key Vault name.')
param keyVaultName string

@description('Blob container names.')
param blobContainerNames array

@description('Queue names.')
param queueNames array

@description('Table names.')
param tableNames array

var storageBlobDataContributorRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
)
var storageQueueDataContributorRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
)
var storageTableDataContributorRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
)
var keyVaultSecretsUserRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '4633458b-17de-408a-b874-0445c86b69e6'
)
var assignmentContext = '${name}; ${location}; tag-count=${length(items(tags))}'

resource storageAccount 'Microsoft.Storage/storageAccounts@2025-01-01' existing = {
  name: storageAccountName
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2025-01-01' existing = {
  parent: storageAccount
  name: 'default'
}

resource blobContainers 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' existing = [
  for containerName in blobContainerNames: {
    parent: blobService
    name: containerName
  }
]

resource queueService 'Microsoft.Storage/storageAccounts/queueServices@2025-01-01' existing = {
  parent: storageAccount
  name: 'default'
}

resource queues 'Microsoft.Storage/storageAccounts/queueServices/queues@2025-01-01' existing = [
  for queueName in queueNames: {
    parent: queueService
    name: queueName
  }
]

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2025-01-01' existing = {
  parent: storageAccount
  name: 'default'
}

resource tables 'Microsoft.Storage/storageAccounts/tableServices/tables@2025-01-01' existing = [
  for tableName in tableNames: {
    parent: tableService
    name: tableName
  }
]

resource keyVault 'Microsoft.KeyVault/vaults@2024-11-01' existing = {
  name: keyVaultName
}

resource blobRoleAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for (containerName, index) in blobContainerNames: {
    name: guid(blobContainers[index].id, principalId, storageBlobDataContributorRoleId)
    scope: blobContainers[index]
    properties: {
      description: '${assignmentContext}; Blob data access for ${containerName}'
      principalId: principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: storageBlobDataContributorRoleId
    }
  }
]

resource queueRoleAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for (queueName, index) in queueNames: {
    name: guid(queues[index].id, principalId, storageQueueDataContributorRoleId)
    scope: queues[index]
    properties: {
      description: '${assignmentContext}; Queue data access for ${queueName}'
      principalId: principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: storageQueueDataContributorRoleId
    }
  }
]

resource tableRoleAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for (tableName, index) in tableNames: {
    name: guid(tables[index].id, principalId, storageTableDataContributorRoleId)
    scope: tables[index]
    properties: {
      description: '${assignmentContext}; Table data access for ${tableName}'
      principalId: principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: storageTableDataContributorRoleId
    }
  }
]

resource keyVaultRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, principalId, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    description: '${assignmentContext}; access to Foundry secrets'
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: keyVaultSecretsUserRoleId
  }
}
