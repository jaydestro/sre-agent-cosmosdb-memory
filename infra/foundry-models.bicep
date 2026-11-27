targetScope = 'resourceGroup'

param ownerTagValue string
param foundryAccountName string
param reasoningDeploymentName string
param reasoningModelName string
param reasoningModelVersion string
param embeddingDeploymentName string
param embeddingModelName string
param embeddingModelVersion string

@description('Choose only after actual region catalog/quota verification; Standard keeps processing regional.')
@allowed(['Standard'])
param deploymentSku string = 'Standard'
@minValue(1)
param reasoningCapacity int = 20
@minValue(1)
param embeddingCapacity int = 10

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}
var modelDefinitions = [
  {
    name: reasoningDeploymentName
    model: reasoningModelName
    version: reasoningModelVersion
    capacity: reasoningCapacity
  }
  {
    name: embeddingDeploymentName
    model: embeddingModelName
    version: embeddingModelVersion
    capacity: embeddingCapacity
  }
]
// Deployment children support tags in the 2025-06-01 schema.
// The provider can lock the shared account during child updates.
@batchSize(1)
resource deployments 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = [for definition in modelDefinitions: {
  parent: foundry
  name: definition.name
  tags: { Owner: ownerTagValue }
  sku: {
    name: deploymentSku
    capacity: definition.capacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: definition.model
      version: definition.version
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}]
