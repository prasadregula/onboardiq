// OnboardIQ infrastructure (dev by default; prod via a different parameters file).
//   az deployment group create -g rg-onboardiq-dev -f main.bicep -p @params.dev.json -p apiImage=<acr>/onboardiq-api:<tag>
// Cost posture: everything scales to zero or is serverless; AI Search is OFF by default (deploySearch=false) and,
// when on, uses the Free tier unless searchSku says otherwise.
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
@description('Use an EXISTING search service instead (e.g. the one free tier in the subscription, shared with another app). deploy.sh provisions this app\'s own index there and grants roles scoped to that index only. Leave empty to not use one.')
param existingSearchName string = ''
@description('AI Search tier: free (one per subscription, 50 MB, may be removed after long inactivity) or basic (about US$75/month)')
param searchSku string = 'free'
@description('Semantic re-ranking plan: free (limited queries per month), standard, or disabled. The app falls back to hybrid if unavailable.')
param searchSemantic string = 'free'
@description('Deploy Azure SQL serverless (else the API uses SQLite on an ephemeral volume - dev only)')
param deploySql bool = true
@description('Use the Azure SQL Database free offer (100,000 vCore-seconds, 32 GB per month, per subscription, for life) and pause the database when the monthly allowance runs out instead of billing. Applies only when the database is CREATED: an existing paid database cannot be converted (deploy.sh stops and explains)')
param sqlFreeOffer bool = true
@secure()
param sqlAdminPassword string = ''
@description('Entra admin for SQL (object id + login), lets the Container App managed identity sign in with an Entra token')
param sqlAadAdminLogin string = ''
param sqlAadAdminObjectId string = ''

@description('Deploy Azure AI Content Safety for Prompt Shields document-attack screening (T-01). Keyless; F0 is free.')
param deployContentSafety bool = true
@description('Content Safety tier. F0 = free (limited calls/month, one free resource per subscription); use S0 if F0 is taken.')
param contentSafetySku string = 'F0'

@description('AI gateway: put Azure API Management (Consumption tier: no hourly charge, first 1M calls/month free) in front of Azure OpenAI. The agents call the gateway with their own identity; only the gateway can call the model.')
param deployApim bool = false
@description('Required by API Management when deployApim is true: email for service notifications (kept in the gitignored params file)')
param apimPublisherEmail string = ''
param minReplicas int = 0
param maxReplicas int = 3
@description('Daily cap on log ingestion (GB) so a log storm cannot run up a bill. -1 = no cap.')
param logDailyCapGb string = '0.5'

var name = 'onboardiq-${env}'
var tags = { project: 'onboardiq', env: env }

// ------------------------------------------------------------------------------------------ observability
resource logs 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: 'log-${name}'
  location: location
  tags: tags
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30, workspaceCapping: { dailyQuotaGb: json(logDailyCapGb) } }
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

// Serverless General Purpose, 1 vCore max. With the free offer the database costs nothing: compute comes out of the
// monthly free allowance (about 55 hours at 0.5 vCore), it auto-pauses when idle (the offer fixes the delay: setting
// autoPauseDelay is rejected with ProvisioningDisabled), and
// when the allowance is used up it PAUSES until the 1st of next month instead of billing (cost cannot run away).
resource sqlDb 'Microsoft.Sql/servers/databases@2023-08-01-preview' = if (deploySql) {
  parent: sqlServer
  name: 'onboardiq'
  location: location
  tags: tags
  sku: { name: 'GP_S_Gen5', tier: 'GeneralPurpose', family: 'Gen5', capacity: 1 }
  properties: union({ minCapacity: json('0.5'), maxSizeBytes: 34359738368, zoneRedundant: false },
    sqlFreeOffer ? { useFreeLimit: true, freeLimitExhaustionBehavior: 'AutoPause' }
                 : { autoPauseDelay: 60 })
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

// Prompt Shields (T-01): screens document text for injected instructions before the model sees it. Keyless only;
// called by the extraction agent's identity, which is the only identity granted access.
resource contentSafety 'Microsoft.CognitiveServices/accounts@2023-05-01' = if (deployContentSafety) {
  name: 'cs-${name}'
  location: location
  tags: tags
  kind: 'ContentSafety'
  sku: { name: contentSafetySku }
  properties: { customSubDomainName: 'cs-${name}-${take(suffix, 6)}', publicNetworkAccess: 'Enabled', disableLocalAuth: true }
}

resource search 'Microsoft.Search/searchServices@2023-11-01' = if (deploySearch) {
  name: 'srch-${name}'
  location: location
  tags: tags
  sku: { name: searchSku }
  // Keyless: API keys disabled, every caller uses Entra ID + RBAC (risk agent reads, the API's identity writes)
  properties: { replicaCount: 1, partitionCount: 1, hostingMode: 'default', disableLocalAuth: true, semanticSearch: searchSemantic }
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
  { name: 'ONBOARDIQ_AZURE_OPENAI_ENDPOINT', value: deployApim ? apim!.properties.gatewayUrl : openAiEndpoint }
  { name: 'ONBOARDIQ_LLM_GATEWAY', value: deployApim ? 'apim' : '' }
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
        ], !empty(existingSearchName) ? [ { name: 'ONBOARDIQ_SEARCH_ENDPOINT', value: 'https://${existingSearchName}.search.windows.net' }
                                     { name: 'ONBOARDIQ_SEARCH_SEMANTIC', value: searchSemantic == 'disabled' ? 'false' : 'true' }
                                     { name: 'ONBOARDIQ_SEARCH_MANAGE_INDEX', value: 'false' } ]
           : deploySearch ? [ { name: 'ONBOARDIQ_SEARCH_ENDPOINT', value: 'https://srch-${name}.search.windows.net' }
                              { name: 'ONBOARDIQ_SEARCH_SEMANTIC', value: searchSemantic == 'disabled' ? 'false' : 'true' } ] : [],
           deployContentSafety ? [ { name: 'ONBOARDIQ_CONTENT_SAFETY_ENDPOINT', value: 'https://cs-${name}-${take(suffix, 6)}.cognitiveservices.azure.com/' } ] : [])
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
  searchReader: '1407120a-92aa-4202-b7e9-c0e197c71c8f'     // Search Index Data Reader
  searchService: '7ca78c08-252a-4471-8644-bb5ff32d4ba0'    // Search Service Contributor (create/update the index)
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
// Content Safety (Prompt Shields): ONLY the extraction agent, which screens the documents it has just read
resource raCsExtraction 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployContentSafety) {
  name: guid(resourceGroup().id, 'cs', agentId[1].id, roles.cogServicesUser)
  scope: contentSafety
  properties: { principalId: agentId[1].properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.cogServicesUser) }
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
// AI Search: the API's own identity creates the index and uploads policy chunks; ONLY the risk agent may query it
resource raSearch 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deploySearch) {
  name: guid('search', api.id, roles.searchContributor)
  scope: search
  properties: { principalId: api.identity.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchContributor) }
}
resource raSearchService 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deploySearch) {
  name: guid('search', api.id, roles.searchService)
  scope: search
  properties: { principalId: api.identity.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchService) }
}
resource raSearchRisk 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deploySearch) {
  name: guid('search', agentId[3].id, roles.searchReader)
  scope: search
  properties: { principalId: agentId[3].properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchReader) }
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

// ------------------------------------------------------------------------------------------ AI gateway (optional)
// Azure API Management, Consumption tier. Agents call https://<apim>.azure-api.net/openai/... with their own managed
// identity token; the policy (apim-openai-policy.xml) admits only the orchestrator and risk agents, swaps in the
// gateway's identity for the call to Azure OpenAI and records token usage per agent. deploy.sh grants the gateway
// identity on Azure OpenAI (cross resource group) and revokes the agents' direct access.
var openAiBase = endsWith(openAiEndpoint, '/') ? openAiEndpoint : '${openAiEndpoint}/'

resource apim 'Microsoft.ApiManagement/service@2023-09-01-preview' = if (deployApim) {
  name: 'apim-${name}-${take(suffix, 5)}'
  location: location
  tags: tags
  sku: { name: 'Consumption', capacity: 0 }
  identity: { type: 'SystemAssigned' }
  properties: { publisherEmail: apimPublisherEmail, publisherName: 'OnboardIQ' }
}

resource apimLogger 'Microsoft.ApiManagement/service/loggers@2023-09-01-preview' = if (deployApim) {
  parent: apim
  name: 'appi'
  properties: { loggerType: 'applicationInsights', credentials: { connectionString: appi.properties.ConnectionString } }
}

resource apimOpenAi 'Microsoft.ApiManagement/service/apis@2023-09-01-preview' = if (deployApim) {
  parent: apim
  name: 'openai'
  properties: {
    displayName: 'Azure OpenAI (OnboardIQ agents)'
    path: 'openai'
    protocols: [ 'https' ]
    serviceUrl: '${openAiBase}openai'
    subscriptionRequired: false   // callers authenticate with Entra tokens, validated by the policy (no keys)
  }
}

resource apimOpPost 'Microsoft.ApiManagement/service/apis/operations@2023-09-01-preview' = if (deployApim) {
  parent: apimOpenAi
  name: 'post-any'
  properties: { displayName: 'POST (chat completions, embeddings)', method: 'POST', urlTemplate: '/*' }
}

resource apimPolicy 'Microsoft.ApiManagement/service/apis/policies@2023-09-01-preview' = if (deployApim) {
  parent: apimOpenAi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: replace(replace(replace(loadTextContent('apim-openai-policy.xml'),
      '__TENANT_ID__', entraTenantId),
      '__ORCHESTRATOR_APPID__', agentId[0].properties.clientId),
      '__RISK_APPID__', agentId[3].properties.clientId)
  }
}

// Application Insights logging for the gateway: request metadata and the per-agent token metric. No bodies.
resource apimDiag 'Microsoft.ApiManagement/service/apis/diagnostics@2023-09-01-preview' = if (deployApim) {
  parent: apimOpenAi
  name: 'applicationinsights'
  properties: {
    loggerId: apimLogger.id
    alwaysLog: 'allErrors'
    sampling: { samplingType: 'fixed', percentage: 100 }
    metrics: true
    verbosity: 'information'
    frontend: { request: { body: { bytes: 0 } }, response: { body: { bytes: 0 } } }
    backend: { request: { body: { bytes: 0 } }, response: { body: { bytes: 0 } } }
  }
}

output apimName string = deployApim ? apim.name : ''
output apimGatewayUrl string = deployApim ? apim!.properties.gatewayUrl : ''
output apimPrincipalId string = deployApim ? apim!.identity.principalId : ''
