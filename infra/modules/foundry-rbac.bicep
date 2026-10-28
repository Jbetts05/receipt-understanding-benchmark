targetScope = 'resourceGroup'

@description('Existing Microsoft Foundry account name.')
param foundryAccountName string

@description('Object ID of the App Service system-assigned managed identity.')
param principalId string

var cognitiveServicesUserRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'a97b65f3-24c7-4388-baec-2e87135dc908'
)

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: foundryAccountName
}

resource cognitiveServicesUserRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount.id, principalId, cognitiveServicesUserRoleId)
  scope: foundryAccount
  properties: {
    description: 'Use Foundry models with the App Service system-assigned managed identity.'
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: cognitiveServicesUserRoleId
  }
}
