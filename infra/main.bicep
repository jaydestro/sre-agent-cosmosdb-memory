targetScope = 'resourceGroup'

@description('Deploy into a new, dedicated resource group. Its Owner tag is set by the caller.')
param location string
param ownerTagValue string
param cosmosAccountName string
param storageAccountName string
param inferenceAccountName string
@description('Set false on subsequent deployments: this preview inference RP rejects a repeated create PUT with Conflict.')
param createInferenceAccount bool = true
param foundryAccountName string
param foundryProjectName string
param blobContainerName string = 'incident-evidence'

@description('Only source IPv4 addresses verified from the actual SDK runtime. Empty means network disabled, never allow-all.')
param verifiedRuntimeSourceIps array = []

var ownerTags = {
  Owner: ownerTagValue
}

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2025-04-15' = {
  name: cosmosAccountName
  location: location
  tags: ownerTags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    disableLocalAuth: true
    disableKeyBasedMetadataWriteAccess: true
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    enableAutomaticFailover: false
    enableMultipleWriteLocations: false
    enableFreeTier: false
    capabilities: [
      { name: 'EnableNoSQLVectorSearch' }
    ]
    publicNetworkAccess: empty(verifiedRuntimeSourceIps) ? 'Disabled' : 'Enabled'
    ipRules: [for ip in verifiedRuntimeSourceIps: { ipAddressOrRange: ip }]
    isVirtualNetworkFilterEnabled: false
    networkAclBypass: 'None'
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2025-06-01' = {
  name: storageAccountName
  location: location
  tags: ownerTags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    // ME1 fetches the caller's user-delegation SAS over HTTPS.
    // Private container, not a private endpoint. SAS does not bypass a firewall.
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'None'
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2025-06-01' = {
  parent: storage
  name: 'default'
  properties: {}
}

// Blob services/containers do not have a tags field in the ARM schema.
resource imageContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-06-01' = {
  parent: blobService
  name: blobContainerName
  properties: {
    publicAccess: 'None'
  }
}

// Preview schema is documented in the ME1 generate-embedding guide.
// RP metadata must confirm SupportsTags; no presumed Foundry model deployment.
resource inference 'Microsoft.InferenceService/inferenceAccounts@2026-08-01-preview' = if (createInferenceAccount) {
  name: inferenceAccountName
  location: location
  tags: ownerTags
  properties: {}
}

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: foundryAccountName
  location: location
  tags: ownerTags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: foundryAccountName
    allowProjectManagement: true
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundry
  name: foundryProjectName
  location: location
  tags: ownerTags
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'Incident memory'
    description: 'Isolated local incident-memory demonstration'
  }
}
