// OnboardIQ infrastructure (dev by default; prod via a different parameters file).
//   az deployment group create -g rg-onboardiq-dev -f main.bicep -p @params.dev.json -p apiImage=<acr>/onboardiq-api:<tag>
// Cost posture: everything scales to zero or is serverless; AI Search is OFF by default (deploySearch=false).
targetScope = 'resourceGroup'

@description('Short environment name (dev|test|prod)')
param env string = 'dev'
param location string = resourceGroup().location
param suffix string = uniqueString(resourceGroup().id)
@description('Container image for the API')
param apiImage string
@description('ACR login server, e.g. acronboardiqdev.azurecr.io')
param acrLoginServer string
@description('Resource id of the user-assigned identity used ONLY to pull images (AcrPull is granted by deploy.sh before this deployment, so the first revision can pull)')
param pullIdentityId string
@description('Existing Azure OpenAI (Foundry) resource id (role assignment is done by deploy.sh - cross-RG)')
param openAiResourceId string
@description('Azure OpenAI endpoint, e.g. https://<resource>.openai.azure.com/')
param openAiEndpoint string
param chatDeployment string = 'gpt-4.1-mini'
param embeddingDeployment string = 'text-embedding-3-small'

@description('Entra ID: tenant, API app registration client id (audience api://<id>)')
param entraTenantId string = subscription().tenantId
param entraApiClientId string
@description('Origins allowed to call the API (Static Web App host, localhost for dev)')
param corsOrigins string = 'http://localhost:5180'

@description('Deploy Azure AI Search (Basic, ~$75/mo). Local policy index is used when false.')
param deploySearch bool = false
@description('Deploy Azure SQL serverless (else the API uses SQLite on an ephemeral volume - dev only)')
param deploySql bool = true
@secure()
param sqlAdminPassword string = ''
@description('Entra admin for SQL (object id + login), lets the Container App managed identity sign in with an Entra token')
param sqlAadAdminLogin string = ''
param sqlAadAdminObjectId string = ''

param minReplicas int = 0
param maxReplicas int = 3

var name = 'onboardiq-${env}'
var tags = { project: 'onboardiq', env: env }

// ------------------------------------------------------------------------------------------ observability
resource logs 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: 'log-${name}'
  location: location
  tags: tags
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}

resource appi 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${name}'
  location: location
  tags: tags
  kind: 'web'
  properties: { Application_Type: 'web', WorkspaceResourceId: logs.id }
}

// ------------------------------------------------------------------------------------------ data
resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: 'stonboardiq${env}${take(suffix, 6)}'
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    allowSharedKeyAccess: true // needed for the Azure Files mount (OFAC lists); blob access from the API uses managed identity
    supportsHttpsTrafficOnly: true
  }
}

resource blobSvc 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storage
  name: 'default'
  properties: { deleteRetentionPolicy: { enabled: true, days: 30 }, containerDeleteRetentionPolicy: { enabled: true, days: 30 } }
}

resource docs 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobSvc
  name: 'documents'
  properties: { publicAccess: 'None' }
}

resource ofacShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-01-01' = {
  name: '${storage.name}/default/ofac'
  properties: { shareQuota: 1 }
}

resource sqlServer 'Microsoft.Sql/servers@2023-05-01-preview' = if (deploySql) {
  name: 'sql-${name}-${take(suffix, 5)}'
  location: location
  tags: tags
  properties: {
    administratorLogin: 'onboardiqadmin'
    administratorLoginPassword: sqlAdminPassword
    minimalTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
    administrators: empty(sqlAadAdminObjectId) ? null : {
      administratorType: 'ActiveDirectory'
      principalType: 'User'
      login: sqlAadAdminLogin
      sid: sqlAadAdminObjectId
      tenantId: entraTenantId
      azureADOnlyAuthentication: false
    }
  }
}

resource sqlDb 'Microsoft.Sql/servers/databases@2023-05-01-preview' = if (deploySql) {
  parent: sqlServer
  name: 'onboardiq'
  location: location
  tags: tags
  sku: { name: 'GP_S_Gen5', tier: 'GeneralPurpose', family: 'Gen5', capacity: 1 }
  properties: { autoPauseDelay: 60, minCapacity: json('0.5'), maxSizeBytes: 34359738368, zoneRedundant: false }
}

resource sqlFwAzure 'Microsoft.Sql/servers/firewallRules@2023-05-01-preview' = if (deploySql) {
  parent: sqlServer
  name: 'AllowAzureServices'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

// ------------------------------------------------------------------------------------------ AI services
resource docIntel 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: 'di-${name}'
  location: location
  tags: tags
  kind: 'FormRecognizer'
  sku: { name: env == 'prod' ? 'S0' : 'F0' }
  identity: { type: 'SystemAssigned' }
  properties: { customSubDomainName: 'di-${name}-${take(suffix, 6)}', publicNetworkAccess: 'Enabled', disableLocalAuth: true }
}

resource search 'Microsoft.Search/searchServices@2023-11-01' = if (deploySearch) {
  name: 'srch-${name}'
  location: location
  tags: tags
  sku: { name: 'basic' }
  identity: { type: 'SystemAssigned' }
  properties: { replicaCount: 1, partitionCount: 1, hostingMode: 'default', authOptions: { aadOrApiKey: { aadAuthFailureMode: 'http401WithBearerChallenge' } } }
}

// ------------------------------------------------------------------------------------------ secrets
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv-${name}-${take(suffix, 5)}'
  location: location
  tags: tags
  properties: { tenantId: subscription().tenantId, sku: { family: 'A', name: 'standard' }, enableRbacAuthorization: true, enablePurgeProtection: env == 'prod' ? true : null, softDeleteRetentionInDays: 30 }
}

// ------------------------------------------------------------------------------------------ compute
resource caEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${name}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: { destination: 'log-analytics', logAnalyticsConfiguration: { customerId: logs.properties.customerId, sharedKey: logs.listKeys().primarySharedKey } }
  }
}

resource caEnvStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: caEnv
  name: 'ofac'
  properties: { azureFile: { accountName: storage.name, accountKey: storage.listKeys().keys[0].value, shareName: 'ofac', accessMode: 'ReadWrite' } }
}

var sqlUrl = deploySql ? 'mssql+pyodbc://@${sqlServer!.properties.fullyQualifiedDomainName}/onboardiq?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&Connection+Timeout=60' : 'sqlite:////app/data/onboardiq.db'

// ------------------------------------------------------------------------------------------ per-agent identities (w3-1)
// One user-assigned managed identity per agent role; the app picks the identity per tool call (app/identity.py).
var agentRoles = [ 'orchestrator', 'extraction', 'screening', 'risk', 'decision' ]
resource agentId 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [for r in agentRoles: {
  name: 'id-${name}-${r}'
  location: location
  tags: union(tags, { 'agent-role': r })
}]
// index order matches agentRoles: 0 orchestrator, 1 extraction, 2 screening, 3 risk, 4 decision

// On-behalf-of (w3-3): the API app registration trusts THIS identity via a federated identity credential, so the OBO
// token exchange needs no client secret. It holds no Azure roles - it can only prove "I am OnboardIQ-API".
resource oboId 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${name}-obo'
  location: location
  tags: union(tags, { purpose: 'obo-assertion' })
}

var commonEnv = [
  { name: 'ONBOARDIQ_ENV', value: env }
  { name: 'ONBOARDIQ_LOG_JSON', value: 'true' }
  { name: 'ONBOARDIQ_AUTH_MODE', value: 'entra' }
  { name: 'ONBOARDIQ_ENTRA_TENANT_ID', value: entraTenantId }
  { name: 'ONBOARDIQ_ENTRA_AUDIENCE', value: 'api://${entraApiClientId}' }
  { name: 'ONBOARDIQ_CORS_ORIGINS', value: corsOrigins }
  { name: 'ONBOARDIQ_DATABASE_URL', value: sqlUrl }
  { name: 'ONBOARDIQ_STORAGE_PROVIDER', value: 'azure' }
  { name: 'ONBOARDIQ_STORAGE_ACCOUNT_URL', value: storage.properties.primaryEndpoints.blob }
  { name: 'ONBOARDIQ_ORCHESTRATOR_MODE', value: 'llm' }
  { name: 'ONBOARDIQ_LLM_PROVIDER', value: 'azure' }
  { name: 'ONBOARDIQ_AZURE_OPENAI_ENDPOINT', value: openAiEndpoint }
  { name: 'ONBOARDIQ_AZURE_OPENAI_CHAT_DEPLOYMENT', value: chatDeployment }
  { name: 'ONBOARDIQ_AZURE_OPENAI_EMBEDDING_DEPLOYMENT', value: embeddingDeployment }
  { name: 'ONBOARDIQ_EXTRACTION_PROVIDER', value: 'azure' }
  { name: 'ONBOARDIQ_DOC_INTEL_ENDPOINT', value: docIntel.properties.endpoint }
  { name: 'ONBOARDIQ_SCREENING_PROVIDER', value: 'ofac' }
  // dev only: fictional demo sanctions entries alongside the real OFAC lists, so golden cases exercise every gate
  { name: 'ONBOARDIQ_SCREENING_DEMO_ENTRIES', value: env == 'prod' ? 'false' : 'true' }
  { name: 'ONBOARDIQ_OFAC_DATA_DIR', value: '/mnt/ofac' }
  { name: 'ONBOARDIQ_RAG_INDEX_PATH', value: '/mnt/ofac/policy_index.json' }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appi.properties.ConnectionString }
]

resource api 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-${name}-api'
  location: location
  tags: tags
  // System-assigned identity = what the app does (OpenAI, SQL, Blob, DI). User-assigned = image pull only.
  // System-assigned = API itself (SQL, uploads). User-assigned = image pull + one identity per agent role.
  identity: { type: 'SystemAssigned,UserAssigned', userAssignedIdentities: {
    '${pullIdentityId}': {}
    '${agentId[0].id}': {}
    '${agentId[1].id}': {}
    '${agentId[2].id}': {}
    '${agentId[3].id}': {}
    '${agentId[4].id}': {}
    '${oboId.id}': {}
  } }
  properties: {
    managedEnvironmentId: caEnv.id
    configuration: {
      registries: [ { server: acrLoginServer, identity: pullIdentityId } ]
      ingress: {
        external: true
        targetPort: 8080
        transport: 'http'
        corsPolicy: { allowedOrigins: split(corsOrigins, ','), allowedHeaders: [ '*' ], allowedMethods: [ '*' ], exposeHeaders: [ 'X-Request-Id' ] }
      }
    }
    template: {
      containers: [ {
        name: 'api'
        image: apiImage
        resources: { cpu: json('1.0'), memory: '2Gi' }
        env: concat(commonEnv, [
          { name: 'ONBOARDIQ_IDENTITY_ORCHESTRATOR', value: agentId[0].properties.clientId }
          { name: 'ONBOARDIQ_IDENTITY_EXTRACTION', value: agentId[1].properties.clientId }
          { name: 'ONBOARDIQ_IDENTITY_SCREENING', value: agentId[2].properties.clientId }
          { name: 'ONBOARDIQ_IDENTITY_RISK', value: agentId[3].properties.clientId }
          { name: 'ONBOARDIQ_IDENTITY_DECISION', value: agentId[4].properties.clientId }
          { name: 'ONBOARDIQ_OBO_CLIENT_ID', value: entraApiClientId }
          { name: 'ONBOARDIQ_OBO_ASSERTION_IDENTITY', value: oboId.properties.clientId }
        ], deploySearch ? [ { name: 'ONBOARDIQ_SEARCH_ENDPOINT', value: 'https://${search.name}.search.windows.net' } ] : [])
        volumeMounts: [ { volumeName: 'ofac', mountPath: '/mnt/ofac' } ]
        probes: [
          // Startup probe: allow up to 5 min for first boot (SQL wake-up, migrations, policy index, OFAC load) before
          // liveness checks begin - otherwise a slow cold start is killed and restarts forever.
          { type: 'Startup', httpGet: { path: '/health', port: 8080 }, periodSeconds: 10, failureThreshold: 30, timeoutSeconds: 5 }
          { type: 'Liveness', httpGet: { path: '/health', port: 8080 }, periodSeconds: 30, timeoutSeconds: 5 }
          { type: 'Readiness', httpGet: { path: '/ready', port: 8080 }, periodSeconds: 10, initialDelaySeconds: 10 }
        ]
      } ]
      volumes: [ { name: 'ofac', storageType: 'AzureFile', storageName: caEnvStorage.name } ]
      scale: { minReplicas: minReplicas, maxReplicas: maxReplicas, rules: [ { name: 'http', http: { metadata: { concurrentRequests: '20' } } } ] }
    }
  }
}

// Daily OFAC list refresh: downloads to the shared file volume, then tells the API to reload.
resource ofacJob 'Microsoft.App/jobs@2024-03-01' = {
  name: 'caj-${name}-ofac'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned,UserAssigned', userAssignedIdentities: { '${pullIdentityId}': {} } }
  properties: {
    environmentId: caEnv.id
    configuration: {
      triggerType: 'Schedule'
      scheduleTriggerConfig: { cronExpression: '0 6 * * *', parallelism: 1, replicaCompletionCount: 1 }
      replicaTimeout: 900
      replicaRetryLimit: 1
      registries: [ { server: acrLoginServer, identity: pullIdentityId } ]
    }
    template: {
      containers: [ {
        name: 'refresh'
        image: apiImage
        command: [ 'python', '-m', 'scripts.refresh_ofac', '--dir', '/mnt/ofac' ]
        resources: { cpu: json('0.25'), memory: '0.5Gi' }
        volumeMounts: [ { volumeName: 'ofac', mountPath: '/mnt/ofac' } ]
      } ]
      volumes: [ { name: 'ofac', storageType: 'AzureFile', storageName: caEnvStorage.name } ]
    }
  }
}

// ------------------------------------------------------------------------------------------ RBAC for the API identity
var roles = {
  cogServicesUser: 'a97b65f3-24c7-4388-baec-2e87135dc908'  // Cognitive Services User (Document Intelligence)
  blobContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'  // Storage Blob Data Contributor
  searchContributor: '8ebe5a00-799e-43f5-93ac-243d3dce84a7' // Search Index Data Contributor
  kvSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'    // Key Vault Secrets User
  blobReader: '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'       // Storage Blob Data Reader
}

// Document Intelligence: ONLY the extraction agent (the API's own identity no longer has it - see deploy.sh revoke step)
resource raDiExtraction 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(docIntel.id, agentId[1].id, roles.cogServicesUser)
  scope: docIntel
  properties: { principalId: agentId[1].properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.cogServicesUser) }
}
// Blob: extraction agent may READ documents, never write or delete
resource raBlobExtraction 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, agentId[1].id, roles.blobReader)
  scope: storage
  properties: { principalId: agentId[1].properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.blobReader) }
}
// screening and decision agents: deliberately NO Azure role assignments
resource raBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, api.id, roles.blobContributor)
  scope: storage
  properties: { principalId: api.identity.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.blobContributor) }
}
resource raKv 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, api.id, roles.kvSecretsUser)
  scope: kv
  properties: { principalId: api.identity.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.kvSecretsUser) }
}
resource raSearch 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deploySearch) {
  name: guid('search', api.id, roles.searchContributor)
  scope: search
  properties: { principalId: api.identity.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchContributor) }
}

output apiUrl string = 'https://${api.properties.configuration.ingress.fqdn}'
output apiPrincipalId string = api.identity.principalId
output apiPrincipalName string = api.name
output docIntelEndpoint string = docIntel.properties.endpoint
output storageAccount string = storage.name
output sqlServerFqdn string = deploySql ? sqlServer!.properties.fullyQualifiedDomainName : ''
output keyVault string = kv.name
output openAiResourceId string = openAiResourceId
output orchestratorPrincipalId string = agentId[0].properties.principalId
output riskPrincipalId string = agentId[3].properties.principalId
output docIntelId string = docIntel.id
output oboPrincipalId string = oboId.properties.principalId
