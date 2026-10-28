targetScope = 'resourceGroup'

@description('Virtual network resource name.')
param name string

@description('Azure region for regional network resources.')
param location string = resourceGroup().location

@description('Resource tags.')
param tags object = {}

@description('Private DNS zones required by the private endpoints, in Blob, Queue, Table, File, and Key Vault order.')
@minLength(5)
@maxLength(5)
param privateDnsZoneNames array

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-07-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.42.0.0/24'
      ]
    }
    enableDdosProtection: false
  }
}

resource appServiceSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' = {
  parent: virtualNetwork
  name: 'snet-app-service'
  properties: {
    addressPrefix: '10.42.0.0/26'
    delegations: [
      {
        name: 'app-service-delegation'
        properties: {
          serviceName: 'Microsoft.Web/serverFarms'
        }
      }
    ]
    privateEndpointNetworkPolicies: 'Enabled'
    privateLinkServiceNetworkPolicies: 'Enabled'
  }
}

resource privateEndpointSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' = {
  parent: virtualNetwork
  name: 'snet-private-endpoints'
  properties: {
    addressPrefix: '10.42.0.64/27'
    delegations: []
    privateEndpointNetworkPolicies: 'Disabled'
    privateLinkServiceNetworkPolicies: 'Enabled'
  }
  dependsOn: [
    appServiceSubnet
  ]
}

resource deploymentScriptPublicIp 'Microsoft.Network/publicIPAddresses@2024-07-01' = {
  name: 'pip-${name}-deployment-script'
  location: location
  sku: {
    name: 'Standard'
  }
  properties: {
    idleTimeoutInMinutes: 4
    publicIPAddressVersion: 'IPv4'
    publicIPAllocationMethod: 'Static'
  }
}

resource deploymentScriptNatGateway 'Microsoft.Network/natGateways@2024-07-01' = {
  name: 'nat-${name}-deployment-script'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    idleTimeoutInMinutes: 4
    publicIpAddresses: [
      {
        id: deploymentScriptPublicIp.id
      }
    ]
  }
}

resource deploymentScriptSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' = {
  parent: virtualNetwork
  name: 'snet-deployment-script'
  properties: {
    addressPrefix: '10.42.0.96/27'
    delegations: [
      {
        name: 'deployment-script-container-instances'
        properties: {
          serviceName: 'Microsoft.ContainerInstance/containerGroups'
        }
      }
    ]
    natGateway: {
      id: deploymentScriptNatGateway.id
    }
    privateEndpointNetworkPolicies: 'Enabled'
    privateLinkServiceNetworkPolicies: 'Enabled'
  }
  dependsOn: [
    privateEndpointSubnet
  ]
}

resource privateDnsZones 'Microsoft.Network/privateDnsZones@2024-06-01' = [
  for zoneName in privateDnsZoneNames: {
    name: zoneName
    location: 'global'
    tags: tags
  }
]

resource privateDnsZoneLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [
  for (zoneName, index) in privateDnsZoneNames: {
    parent: privateDnsZones[index]
    name: '${name}-link'
    location: 'global'
    tags: tags
    properties: {
      registrationEnabled: false
      virtualNetwork: {
        id: virtualNetwork.id
      }
    }
  }
]

output virtualNetworkId string = virtualNetwork.id
output appSubnetId string = appServiceSubnet.id
output privateEndpointSubnetId string = privateEndpointSubnet.id
output deploymentScriptSubnetId string = deploymentScriptSubnet.id
output privateDnsZoneIds array = [
  for (zoneName, index) in privateDnsZoneNames: privateDnsZones[index].id
]
