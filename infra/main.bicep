// ─────────────────────────────────────────────────────────────────────────────
// infra/main.bicep — Ingestion Pipeline Azure Infrastructure
//
// Provisions:
//   Azure Container Registry (ACR)
//   Azure Container Apps Environment (shared with retrieval pipeline)
//   Container Apps:
//     - rag-ingestion   (HTTP, external — receives SharePoint webhooks)
//     - rag-processing  (no ingress — SB listener only)
//     - rag-embedding   (no ingress — SB listener only)
//   Azure Container Apps Job:
//     - rag-reindex-job (manual trigger — full folder reindex)
//   Azure Blob Storage (raw + processed containers)
//   Azure Service Bus (3 queues)
//   Managed Identity with RBAC assignments
// ─────────────────────────────────────────────────────────────────────────────

targetScope = 'resourceGroup'

@description('Environment name: prod / staging / dev')
param environmentName string = 'prod'

param location string = resourceGroup().location

@description('Existing AI Foundry project endpoint')
param azureFoundryProjectEndpoint string

@description('Embedding deployment name')
param embeddingDeployment string = 'text-embedding-ada-002'

@description('Light LLM deployment name for page cleaning')
param lightLlmDeployment string = 'gpt-4o-mini'

@description('Azure AI Search endpoint (shared with retrieval pipeline)')
param searchEndpoint string

@secure()
param searchApiKey string

param searchIndex string = 'idx-rag'
param searchSemanticConfig string = 'rag-semantic-config'

@description('SharePoint / Graph API app registration')
param sharepointTenantId string
param sharepointClientId string

@secure()
param sharepointClientSecret string

param sharepointWebhookSecret string

@description('Site→domain mapping e.g. site-id-1:hr,site-id-2:legal')
param siteDomainMap string = ''

@description('App Insights connection string (from retrieval pipeline stack)')
param appInsightsConnectionString string = ''

var prefix = 'rag-${environmentName}'
var tags   = { environment: environmentName, project: 'rag-ingestion' }

// ── Log Analytics ─────────────────────────────────────────────────────────────
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${prefix}-ingest-logs'
  location: location
  tags: tags
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}

// ── ACR ───────────────────────────────────────────────────────────────────────
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: replace('${prefix}ingestacr', '-', '')
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

// ── Blob Storage ──────────────────────────────────────────────────────────────
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: replace('${prefix}ingest', '-', '')
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: { accessTier: 'Hot', allowBlobPublicAccess: false }
}

resource rawContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  name: '${storageAccount.name}/default/raw-documents'
  properties: { publicAccess: 'None' }
}

resource processedContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  name: '${storageAccount.name}/default/processed-chunks'
  properties: { publicAccess: 'None' }
}

// ── Service Bus ───────────────────────────────────────────────────────────────
resource sbNamespace 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: '${prefix}-ingest-sb'
  location: location
  tags: tags
  sku: { name: 'Standard', tier: 'Standard' }
}

resource sbIngestion  'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  parent: sbNamespace
  name: 'ingestion-tasks'
  properties: { maxDeliveryCount: 3, deadLetteringOnMessageExpiration: true, lockDuration: 'PT2M' }
}

resource sbProcessing 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  parent: sbNamespace
  name: 'processing-tasks'
  properties: { maxDeliveryCount: 3, deadLetteringOnMessageExpiration: true, lockDuration: 'PT5M' }
}

resource sbEmbedding  'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  parent: sbNamespace
  name: 'embedding-tasks'
  properties: { maxDeliveryCount: 3, deadLetteringOnMessageExpiration: true, lockDuration: 'PT5M' }
}

// ── Managed Identity ──────────────────────────────────────────────────────────
resource managedId 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-ingest-id'
  location: location
  tags: tags
}

// ACR Pull
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, managedId.id, 'AcrPull')
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: managedId.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Storage Blob Data Contributor
resource blobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, managedId.id, 'BlobContributor')
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
    principalId: managedId.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Service Bus Data Owner
resource sbRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(sbNamespace.id, managedId.id, 'SBDataOwner')
  scope: sbNamespace
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '090c5cfd-751d-490a-894a-3ce6f1109419')
    principalId: managedId.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Cognitive Services OpenAI User (for Foundry)
resource cogRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, managedId.id, 'CogUser-ingest')
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908')
    principalId: managedId.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ── ACA Environment ───────────────────────────────────────────────────────────
resource acaEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${prefix}-ingest-env'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// ── Common env vars ───────────────────────────────────────────────────────────
var commonEnv = [
  { name: 'RUNNING_IN_AZURE', value: 'true' }
  { name: 'AZURE_FOUNDRY_PROJECT_ENDPOINT', value: azureFoundryProjectEndpoint }
  { name: 'AZURE_OPENAI_EMBEDDING_DEPLOYMENT', value: embeddingDeployment }
  { name: 'AZURE_OPENAI_LIGHT_LLM_DEPLOYMENT', value: lightLlmDeployment }
  { name: 'AZURE_STORAGE_ACCOUNT_NAME', value: storageAccount.name }
  { name: 'AZURE_SEARCH_ENDPOINT', value: searchEndpoint }
  { name: 'AZURE_SEARCH_API_KEY', secretRef: 'search-key' }
  { name: 'AZURE_SEARCH_INDEX', value: searchIndex }
  { name: 'AZURE_SEARCH_SEMANTIC_CONFIG', value: searchSemanticConfig }
  { name: 'AZURE_SERVICE_BUS_NAMESPACE', value: '${sbNamespace.name}.servicebus.windows.net' }
  { name: 'SHAREPOINT_TENANT_ID', value: sharepointTenantId }
  { name: 'SHAREPOINT_CLIENT_ID', value: sharepointClientId }
  { name: 'SHAREPOINT_CLIENT_SECRET', secretRef: 'sp-secret' }
  { name: 'SHAREPOINT_WEBHOOK_SECRET', value: sharepointWebhookSecret }
  { name: 'SITE_DOMAIN_MAP', value: siteDomainMap }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
  { name: 'LOG_LEVEL', value: 'INFO' }
]

var commonSecrets = [
  { name: 'search-key', value: searchApiKey }
  { name: 'sp-secret', value: sharepointClientSecret }
]

var acrBase = acr.properties.loginServer

// ── Ingestion Agent (external HTTP — receives webhooks) ───────────────────────
resource ingestionApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${prefix}-ingestion'
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${managedId.id}': {} } }
  properties: {
    managedEnvironmentId: acaEnv.id
    configuration: {
      registries: [{ server: acr.properties.loginServer, identity: managedId.id }]
      secrets: commonSecrets
      ingress: { external: true, targetPort: 8010, transport: 'http' }
    }
    template: {
      containers: [{
        name: 'ingestion'
        image: '${acrBase}/rag-ingestion:latest'
        resources: { cpu: json('0.5'), memory: '1Gi' }
        env: union(commonEnv, [{ name: 'AGENT_PORT', value: '8010' }])
        probes: [{ type: 'Liveness', httpGet: { path: '/health', port: 8010 }, initialDelaySeconds: 15, periodSeconds: 30 }]
      }]
      scale: { minReplicas: 1, maxReplicas: 3 }
    }
  }
}

// ── Processing Agent (internal — SB listener, no HTTP ingress) ────────────────
resource processingApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${prefix}-processing'
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${managedId.id}': {} } }
  properties: {
    managedEnvironmentId: acaEnv.id
    configuration: {
      registries: [{ server: acr.properties.loginServer, identity: managedId.id }]
      secrets: commonSecrets
      ingress: { external: false, targetPort: 8011, transport: 'http' }
    }
    template: {
      containers: [{
        name: 'processing'
        image: '${acrBase}/rag-processing:latest'
        resources: { cpu: json('2.0'), memory: '4Gi' }   // DI + LLM calls need more memory
        env: union(commonEnv, [{ name: 'AGENT_PORT', value: '8011' }])
        probes: [{ type: 'Liveness', httpGet: { path: '/health', port: 8011 }, initialDelaySeconds: 15, periodSeconds: 30 }]
      }]
      scale: { minReplicas: 1, maxReplicas: 5 }
    }
  }
}

// ── Embedding Agent (internal — SB listener, no HTTP ingress) ─────────────────
resource embeddingApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${prefix}-embedding'
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${managedId.id}': {} } }
  properties: {
    managedEnvironmentId: acaEnv.id
    configuration: {
      registries: [{ server: acr.properties.loginServer, identity: managedId.id }]
      secrets: commonSecrets
      ingress: { external: false, targetPort: 8012, transport: 'http' }
    }
    template: {
      containers: [{
        name: 'embedding'
        image: '${acrBase}/rag-embedding:latest'
        resources: { cpu: json('1.0'), memory: '2Gi' }
        env: union(commonEnv, [{ name: 'AGENT_PORT', value: '8012' }])
        probes: [{ type: 'Liveness', httpGet: { path: '/health', port: 8012 }, initialDelaySeconds: 15, periodSeconds: 30 }]
      }]
      scale: { minReplicas: 1, maxReplicas: 5 }
    }
  }
}

// ── Container Apps Job — Manual reindex (runs to completion, not a daemon) ────
// Use this to trigger a full folder reindex on demand:
//   az containerapp job start --name <job-name> --resource-group <rg>
resource reindexJob 'Microsoft.App/jobs@2024-03-01' = {
  name: '${prefix}-reindex-job'
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${managedId.id}': {} } }
  properties: {
    environmentId: acaEnv.id
    configuration: {
      triggerType: 'Manual'          // triggered via az cli / API, not on schedule
      replicaTimeout: 3600           // 1 hour max per run
      replicaRetryLimit: 1
      registries: [{ server: acr.properties.loginServer, identity: managedId.id }]
      secrets: commonSecrets
    }
    template: {
      containers: [{
        name: 'reindex'
        image: '${acrBase}/rag-ingestion:latest'
        resources: { cpu: json('1.0'), memory: '2Gi' }
        env: union(commonEnv, [
          { name: 'AGENT_PORT', value: '8010' }
          // Job runs ingestion_agent as a one-shot HTTP call to itself
          // Override CMD to call ingest/folder directly
        ])
        // The job image uses the same ingestion agent image but runs
        // scripts/run_reindex_job.py as its entrypoint
        command: ['python', 'scripts/run_reindex_job.py']
      }]
      scale: { minExecutions: 0, maxExecutions: 1 }
    }
  }
}

// ── Outputs ───────────────────────────────────────────────────────────────────
output acrLoginServer string = acr.properties.loginServer
output ingestionFqdn string = ingestionApp.properties.configuration.ingress.fqdn
output storageAccountName string = storageAccount.name
output serviceBusNamespace string = sbNamespace.name
output reindexJobName string = reindexJob.name
