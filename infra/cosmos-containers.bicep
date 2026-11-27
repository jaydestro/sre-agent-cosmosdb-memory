targetScope = 'resourceGroup'

param location string
param ownerTagValue string
param cosmosAccountName string
param databaseName string = 'incident-memory'
param operationsContainerName string = 'operations'
param evidenceContainerName string = 'evidence'
param memoryContainerName string = 'agent-memory'
param counterContainerName string = 'counter'

@minValue(1000)
@description('Dedicated autoscale maximum on every container, never shared database throughput.')
param autoscaleMaxRu int = 1000

var ownerTags = { Owner: ownerTagValue }
// Generic ARM schemas allow tags here, but current Cosmos SQL children are virtual:
// service readback does not retain independent tags, and the tag extension targets
// the account. These fields must not be reported as inherited or verified child tags.
var dedicatedAutoscale = {
  autoscaleSettings: { maxThroughput: autoscaleMaxRu }
}
var operationalPartition = {
  paths: ['/scope_id']
  kind: 'Hash'
  version: 2
}
var nativeHierarchicalPartition = {
  paths: ['/user_id', '/thread_id']
  kind: 'MultiHash'
  version: 2
}
var dense1536Cosine = {
  vectorEmbeddings: [
    {
      path: '/embedding'
      dataType: 'float32'
      dimensions: 1536
      distanceFunction: 'cosine'
    }
  ]
}
var scalarIndex = {
  indexingMode: 'consistent'
  automatic: true
  includedPaths: [{ path: '/*' }]
  excludedPaths: [{ path: '/"_etag"/?' }]
}
var evidenceIndex = union(scalarIndex, {
  excludedPaths: [
    { path: '/"_etag"/?' }
    { path: '/embedding/*' }
  ]
  vectorIndexes: [{ path: '/embedding', type: 'quantizedFlat' }]
})

// Pinned public AgentMemoryToolkit v0.3.0b2 _utils._container_policies.
// Native text embeddings are a separate space from ME1 evidence embeddings.
var nativeIndex = {
  indexingMode: 'consistent'
  automatic: true
  includedPaths: [{ path: '/*' }]
  excludedPaths: [
    { path: '/source_memory_ids/*' }
    { path: '/supersedes_ids/*' }
    { path: '/"_etag"/?' }
  ]
  vectorIndexes: [{ path: '/embedding', type: 'quantizedFlat' }]
  fullTextIndexes: [{ path: '/content' }]
}
var nativeFullText = {
  defaultLanguage: 'en-US'
  fullTextPaths: [{ path: '/content', language: 'en-US' }]
}
var summariesIndex = union(nativeIndex, {
  compositeIndexes: [
    [
      { path: '/user_id', order: 'ascending' }
      { path: '/thread_id', order: 'ascending' }
      { path: '/version', order: 'descending' }
    ]
  ]
})

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2025-04-15' existing = {
  name: cosmosAccountName
}
resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2025-04-15' = {
  parent: cosmos
  name: databaseName
  location: location
  tags: ownerTags
  properties: {
    resource: { id: databaseName }
    options: {}
  }
}

var containerDefinitions = [
  {
    id: operationsContainerName
    partitionKey: operationalPartition
    indexingPolicy: scalarIndex
  }
  {
    id: evidenceContainerName
    partitionKey: operationalPartition
    indexingPolicy: evidenceIndex
    vectorEmbeddingPolicy: dense1536Cosine
  }
  {
    id: memoryContainerName
    partitionKey: nativeHierarchicalPartition
    indexingPolicy: nativeIndex
    vectorEmbeddingPolicy: dense1536Cosine
    fullTextPolicy: nativeFullText
    defaultTtl: -1
  }
  {
    id: '${memoryContainerName}-turns'
    partitionKey: nativeHierarchicalPartition
    indexingPolicy: nativeIndex
    vectorEmbeddingPolicy: dense1536Cosine
    fullTextPolicy: nativeFullText
    defaultTtl: 2592000
  }
  {
    id: '${memoryContainerName}-summaries'
    partitionKey: nativeHierarchicalPartition
    indexingPolicy: summariesIndex
    vectorEmbeddingPolicy: dense1536Cosine
    fullTextPolicy: nativeFullText
    defaultTtl: -1
  }
  // SDK add_memory touches this even with all cadence thresholds disabled.
  // No lease container: this demo does not run a change-feed processor.
  {
    id: counterContainerName
    partitionKey: nativeHierarchicalPartition
    indexingPolicy: scalarIndex
  }
]

resource containers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2025-04-15' = [for definition in containerDefinitions: {
  parent: database
  name: definition.id
  location: location
  tags: ownerTags
  properties: {
    resource: definition
    options: dedicatedAutoscale
  }
}]
