// ============================================================================
// PyRIT GUI — Azure Container Apps Deployment (Security-Hardened)
//
// Community entry point composing shared infrastructure and application phases.
// Prerequisites: an Entra ID app registration, a versioned ACR image, and an
// existing Azure SQL server/database and Key Vault. Grant the managed identity
// the required roles before the first application revision (see infra/README.md).
// ============================================================================

@description('Name for the Container App and related resources')
@minLength(2)
@maxLength(32)
param appName string = 'pyrit-gui'

@description('Azure region for all resources')
param location string = resourceGroup().location

@description('Container image, required when deployApp is true — must use a unique tag (commit SHA) or digest, never :latest. Enforce in CI pipeline.')
@metadata({ example: 'myacr.azurecr.io/pyrit:a1b2c3d or myacr.azurecr.io/pyrit@sha256:...' })
param containerImage string = ''

@description('Reconcile shared infrastructure. False requires an existing environment, managed identity, and registry.')
param deployInfra bool = true

@description('Deploy the Container App image and configuration. False leaves the existing application untouched.')
param deployApp bool = true

@description('Entra ID tenant ID')
param entraTenantId string

@description('Entra ID app registration client ID (no secrets needed)')
param entraClientId string

@description('Comma-separated object IDs of Entra security groups allowed to access the GUI. Find each ID in Azure Portal → Entra ID → Groups → your group → Object ID.')
@minLength(1)
param allowedGroupObjectIds string

@description('Object ID of the Entra security group allowed to manage backend configuration')
@minLength(1)
param adminGroupObjectId string

@description('CIDR range allowed to reach ACA directly. Empty = unrestricted. Must be empty when Front Door is enabled because ACA sees Front Door backend IPs, not client IPs.')
param allowedCidr string = ''

@description('Human-readable description for the IP restriction rule')
param allowedCidrDescription string = 'Allowed IP range'

@description('Azure SQL server FQDN (e.g., myserver.database.windows.net)')
param sqlServerFqdn string

@description('Azure SQL database name')
param sqlDatabaseName string

@description('Comma-separated PyRIT initializers to run. Defaults register target configs and attack techniques.')
param pyritInitializer string = 'target,technique'

@secure()
@description('Optional Azure Blob HTTPS URI for the backend .pyrit_conf. The deployment helper accepts credential-free managed-identity URIs only. When empty, start.sh generates config from the SQL and initializer parameters.')
param pyritConfigFileUri string = ''

@description('Key Vault secret name containing the .env file contents. Used as env_akv_ref when envFileContents is empty.')
param envSecretName string = 'env-global'

@secure()
@description('Optional raw .env file contents. If provided, this is used directly instead of reading from Key Vault.')
param envFileContents string = ''

@description('Container CPU cores')
param cpuCores string = '1.0'

@description('Container memory in GB')
param memoryGb string = '2.0'

@description('Minimum number of replicas')
param minReplicas int = 1

@description('Maximum number of replicas')
param maxReplicas int = 1

@description('Azure Container Registry name (for managed identity pull). Used if acrResourceId is not provided.')
param acrName string = ''

@description('Virtual network address prefix')
param vnetAddressPrefix string = '10.0.0.0/16'

@description('Dedicated ACA infrastructure subnet prefix')
param infrastructureSubnetAddressPrefix string = '10.0.1.0/26'

@description('Existing Azure Policy IP tags to preserve when adopting a reserved egress public IP')
param egressPublicIpTags array = []

@description('Protect the static egress public IP from accidental deletion')
param protectEgressPublicIp bool = false

@description('Log Analytics retention in days (used only when creating a new workspace)')
param logRetentionDays int = 90

@description('Resource ID of an existing Log Analytics workspace. If provided, you must also provide logAnalyticsCustomerId. Recommended for orgs with a central governance workspace.')
param logAnalyticsWorkspaceId string = ''

@description('Customer ID of an existing Log Analytics workspace (required if logAnalyticsWorkspaceId is provided)')
param logAnalyticsCustomerId string = ''

@secure()
@description('Shared key of an existing Log Analytics workspace (required if logAnalyticsWorkspaceId is provided). This is used only for ACA log ingestion config.')
param logAnalyticsSharedKey string = ''

@description('Resource ID of an existing Key Vault (required). Use your org\'s governed vault to avoid soft-delete/purge-protection issues on redeployment.')
param keyVaultResourceId string

@description('Resource ID of the Azure Container Registry (for AcrPull role assignment). Recommended over acrName for IaC-managed access.')
param acrResourceId string = ''

@description('Optional existing user-assigned managed identity resource ID. Empty creates a new identity using the existing naming behavior.')
param existingManagedIdentityResourceId string = ''

@description('Resource tags applied to all resources (ownership + data classification)')
param tags object = {
  Service: 'pyrit-gui'
  Owner: '<your-team>'
  DataClass: '<your-data-classification>'
}

@description('Enable OpenTelemetry managed agent for audit logging. Creates Application Insights and wires the ACA managed OTel collector.')
param enableOtel bool = false

@description('Create Azure Front Door Premium as the public application endpoint')
param enableFrontDoor bool = false

@description('Connect Azure Front Door Premium to the ACA environment through Private Link')
param enableFrontDoorPrivateLink bool = false

@description('Deterministic message used to discover and approve the ACA Private Link request')
param frontDoorPrivateLinkRequestMessage string = 'Azure Front Door private access to ${appName}'

@description('Disable the ACA environment public endpoint after Front Door Private Link is configured')
param disableContainerAppsPublicAccess bool = false

module infrastructure './infrastructure.bicep' = if (deployInfra) {
  name: '${appName}-infrastructure'
  params: {
    appName: appName
    location: location
    vnetAddressPrefix: vnetAddressPrefix
    infrastructureSubnetAddressPrefix: infrastructureSubnetAddressPrefix
    egressPublicIpTags: egressPublicIpTags
    protectEgressPublicIp: protectEgressPublicIp
    logRetentionDays: logRetentionDays
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
    logAnalyticsCustomerId: logAnalyticsCustomerId
    logAnalyticsSharedKey: logAnalyticsSharedKey
    acrResourceId: acrResourceId
    acrName: acrName
    existingManagedIdentityResourceId: existingManagedIdentityResourceId
    enableOtel: enableOtel
    enableFrontDoor: enableFrontDoor
    enableFrontDoorPrivateLink: enableFrontDoorPrivateLink
    frontDoorPrivateLinkRequestMessage: frontDoorPrivateLinkRequestMessage
    disableContainerAppsPublicAccess: disableContainerAppsPublicAccess
    tags: tags
  }
}

module application './application.bicep' = if (deployApp) {
  name: '${appName}-application'
  params: {
    appName: appName
    location: location
    containerImage: containerImage
    entraTenantId: entraTenantId
    entraClientId: entraClientId
    allowedGroupObjectIds: allowedGroupObjectIds
    adminGroupObjectId: adminGroupObjectId
    sqlServerFqdn: sqlServerFqdn
    sqlDatabaseName: sqlDatabaseName
    pyritInitializer: pyritInitializer
    pyritConfigFileUri: pyritConfigFileUri
    envSecretName: envSecretName
    envFileContents: envFileContents
    cpuCores: cpuCores
    memoryGb: memoryGb
    minReplicas: minReplicas
    maxReplicas: maxReplicas
    allowedCidr: allowedCidr
    allowedCidrDescription: allowedCidrDescription
    keyVaultResourceId: keyVaultResourceId
    acrResourceId: acrResourceId
    acrName: deployInfra ? split(infrastructure!.outputs.acrLoginServer, '.')[0] : acrName
    existingManagedIdentityResourceId: deployInfra
      ? infrastructure!.outputs.managedIdentityResourceId
      : existingManagedIdentityResourceId
    enableOtel: enableOtel
    enableFrontDoor: enableFrontDoor
    tags: tags
  }
}

// Existing reads retain the public outputs when either phase is skipped.
resource existingContainerApp 'Microsoft.App/containerApps@2024-03-01' existing = if (!deployApp) {
  name: appName
}

resource existingAcaEnvironment 'Microsoft.App/managedEnvironments@2024-10-02-preview' existing = if (!deployInfra) {
  name: '${appName}-env'
}

resource existingFrontDoorEndpoint 'Microsoft.Cdn/profiles/afdEndpoints@2024-09-01' existing = if (!deployInfra && enableFrontDoor) {
  name: '${appName}-afd/${appName}-${take(uniqueString(subscription().id, resourceGroup().id, appName), 8)}'
}

resource existingEgressPublicIp 'Microsoft.Network/publicIPAddresses@2024-05-01' existing = if (!deployInfra) {
  name: '${appName}-egress-pip'
}

resource existingAppInsights 'Microsoft.Insights/components@2020-02-02' existing = if (!deployInfra && enableOtel) {
  name: '${appName}-ai'
}

var requiredExistingManagedIdentityId = !deployInfra && empty(existingManagedIdentityResourceId)
  ? fail('App-only deployment requires existingManagedIdentityResourceId')
  : existingManagedIdentityResourceId
var existingManagedIdentitySegments = split(requiredExistingManagedIdentityId, '/')
resource referencedManagedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = if (!deployInfra) {
  name: deployInfra ? '' : last(existingManagedIdentitySegments)
  scope: resourceGroup(
    deployInfra ? subscription().subscriptionId : existingManagedIdentitySegments[2],
    deployInfra ? resourceGroup().name : existingManagedIdentitySegments[4]
  )
}

var appHostName = deployApp
  ? application!.outputs.appFqdn
  : existingContainerApp!.properties.configuration.ingress.fqdn
var frontDoorHostName = enableFrontDoor
  ? (deployInfra ? infrastructure!.outputs.frontDoorFqdn : existingFrontDoorEndpoint!.properties.hostName)
  : ''
var existingAcrName = acrName != ''
  ? acrName
  : (acrResourceId != ''
    ? last(split(acrResourceId, '/'))
    : (deployInfra ? '' : fail('App-only deployment requires an existing registry')))

@description('The generated ACA FQDN; inaccessible when ACA public network access is disabled')
output appFqdn string = appHostName

@description('The Azure Front Door managed HTTPS hostname')
output frontDoorFqdn string = frontDoorHostName

@description('The Azure Front Door public URL')
output frontDoorUrl string = enableFrontDoor ? 'https://${frontDoorHostName}' : ''

@description('The deterministic ACA Private Link approval request message; empty when Private Link is disabled')
output frontDoorPrivateLinkRequestMessage string = deployInfra
  ? infrastructure!.outputs.frontDoorPrivateLinkRequestMessage
  : ''

@description('ACA environment public network access state')
output containerAppsPublicNetworkAccess string = deployInfra
  ? infrastructure!.outputs.containerAppsPublicNetworkAccess
  : existingAcaEnvironment!.properties.publicNetworkAccess

@description('The public application FQDN selected for this deployment')
output publicFqdn string = enableFrontDoor ? frontDoorHostName : appHostName

@description('The default domain of the ACA environment')
output environmentDefaultDomain string = deployInfra
  ? infrastructure!.outputs.environmentDefaultDomain
  : existingAcaEnvironment!.properties.defaultDomain

@description('Static outbound IPv4 address')
output egressPublicIpAddress string = deployInfra
  ? infrastructure!.outputs.egressPublicIpAddress
  : existingEgressPublicIp!.properties.ipAddress

@description('NAT Gateway resource ID')
output natGatewayId string = deployInfra
  ? infrastructure!.outputs.natGatewayId
  : resourceId('Microsoft.Network/natGateways', '${appName}-nat')

@description('ACA infrastructure subnet resource ID')
output acaInfrastructureSubnetId string = deployInfra
  ? infrastructure!.outputs.acaInfrastructureSubnetId
  : resourceId('Microsoft.Network/virtualNetworks/subnets', '${appName}-vnet', '${appName}-aca-subnet')

@description('The principal ID of the user-assigned managed identity — grant this Cognitive Services OpenAI User on your AOAI instances and db_datareader/db_datawriter on Azure SQL')
output managedIdentityPrincipalId string = deployInfra
  ? infrastructure!.outputs.managedIdentityPrincipalId
  : referencedManagedIdentity!.properties.principalId

@description('The resource ID of the user-assigned managed identity')
output managedIdentityResourceId string = deployInfra
  ? infrastructure!.outputs.managedIdentityResourceId
  : referencedManagedIdentity!.id

@description('IMPORTANT: Create an Azure AD contained user in the target database for this managed identity. See README post-deployment steps.')
output sqlAadSetupRequired string = deployInfra && empty(existingManagedIdentityResourceId)
  ? 'Run CREATE USER [${appName}-identity] FROM EXTERNAL PROVIDER on database ${sqlDatabaseName}'
  : 'Verify the existing managed identity has the required contained user and database roles on ${sqlDatabaseName}'

@description('Key Vault name (existing)')
output keyVaultName string = last(split(keyVaultResourceId, '/'))

@description('ACR login server')
output acrLoginServer string = deployInfra ? infrastructure!.outputs.acrLoginServer : '${existingAcrName}.azurecr.io'

@description('Virtual network name')
output vnetName string = deployInfra ? infrastructure!.outputs.vnetName : '${appName}-vnet'

@description('Application Insights connection string (if OTel enabled)')
output appInsightsConnectionString string = enableOtel
  ? (deployInfra ? infrastructure!.outputs.appInsightsConnectionString : existingAppInsights!.properties.ConnectionString)
  : 'N/A (OTel disabled)'
