targetScope = 'resourceGroup'

param cosmosAccountName string
param databaseName string = 'incident-memory'
param storageAccountName string
param blobContainerName string = 'incident-evidence'
param inferenceAccountName string
param foundryAccountName string
param foundryProjectName string
param runtimePrincipalId string
@allowed(['User', 'ServicePrincipal'])
param runtimePrincipalType string

// Resolve all Azure RBAC GUIDs using az role definition list --name.
// No management-plane Contributor or Owner is granted to the runtime.
param embeddingUserRoleId string
param blobDataContributorRoleId string
param blobDelegatorRoleId string
param openAiUserRoleId string
param foundryUserRoleId string

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2025-04-15' existing = {
  name: cosmosAccountName
}
resource storage 'Microsoft.Storage/storageAccounts@2025-06-01' existing = {
  name: storageAccountName
}
resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2025-06-01' existing = {
  parent: storage
  name: 'default'
}
resource images 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-06-01' existing = {
  parent: blobs
  name: blobContainerName
}
resource inference 'Microsoft.InferenceService/inferenceAccounts@2026-08-01-preview' existing = {
  name: inferenceAccountName
}
resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}
resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: foundry
  name: foundryProjectName
}

// Native Cosmos role definition ID is the documented built-in Data Contributor.
var cosmosContributorRoleId = '${cosmos.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
resource cosmosData 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2025-04-15' = {
  parent: cosmos
  name: guid(cosmos.id, databaseName, runtimePrincipalId, cosmosContributorRoleId)
  properties: {
    principalId: runtimePrincipalId
    roleDefinitionId: cosmosContributorRoleId
    scope: '${cosmos.id}/dbs/${databaseName}'
  }
}
resource embeddingUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: inference
  name: guid(inference.id, runtimePrincipalId, embeddingUserRoleId)
  properties: {
    principalId: runtimePrincipalId
    principalType: runtimePrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', embeddingUserRoleId)
  }
}
resource imageContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: images
  name: guid(images.id, runtimePrincipalId, blobDataContributorRoleId)
  properties: {
    principalId: runtimePrincipalId
    principalType: runtimePrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobDataContributorRoleId)
  }
}
resource imageDelegator 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, runtimePrincipalId, blobDelegatorRoleId)
  properties: {
    principalId: runtimePrincipalId
    principalType: runtimePrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobDelegatorRoleId)
  }
}
resource openAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundry
  name: guid(foundry.id, runtimePrincipalId, openAiUserRoleId)
  properties: {
    principalId: runtimePrincipalId
    principalType: runtimePrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRoleId)
  }
}
resource foundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: project
  name: guid(project.id, runtimePrincipalId, foundryUserRoleId)
  properties: {
    principalId: runtimePrincipalId
    principalType: runtimePrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRoleId)
  }
}
