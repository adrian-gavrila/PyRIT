# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
"""Compile the deployment phases and verify their public-NAT contract."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
MAIN_BICEP = REPO_ROOT / "infra" / "main.bicep"
INFRASTRUCTURE_BICEP = REPO_ROOT / "infra" / "infrastructure.bicep"
APPLICATION_BICEP = REPO_ROOT / "infra" / "application.bicep"
NETWORK_BICEP = REPO_ROOT / "infra" / "modules" / "aca_nat_network.bicep"
FRONT_DOOR_BICEP = REPO_ROOT / "infra" / "modules" / "aca_front_door.bicep"
PRIVATE_ENDPOINT_APPROVAL_BICEP = REPO_ROOT / "infra" / "modules" / "aca_private_endpoint_approval.bicep"
BICEP_TOPOLOGY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "bicep_topology.yml"


def _find_bicep_cli() -> str | None:
    """Return an already-installed Bicep binary without triggering a download."""
    path_binary = shutil.which("bicep")
    if path_binary:
        return path_binary

    azure_config_directory = Path(os.environ.get("AZURE_CONFIG_DIR", Path.home() / ".azure"))
    managed_binary = azure_config_directory / "bin" / ("bicep.exe" if os.name == "nt" else "bicep")
    return str(managed_binary) if managed_binary.is_file() else None


BICEP_CLI = _find_bicep_cli()
BICEP_REQUIRED = os.environ.get("PYRIT_REQUIRE_BICEP", "").strip().casefold() == "true"


def _compile_bicep(source: Path, output: Path) -> dict[str, Any]:
    """Compile one Bicep file and return its generated ARM template."""
    if BICEP_CLI is None:
        raise RuntimeError("Bicep CLI is required to compile infrastructure topology tests")
    command = [BICEP_CLI, "build", str(source), "--outfile", str(output)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(output.read_text(encoding="utf-8"))


def _resources(template: dict[str, Any], resource_type: str) -> list[dict[str, Any]]:
    """Return resources of one ARM type from a compiled template."""
    return [resource for resource in template["resources"] if resource["type"] == resource_type]


class TestBicepCiContract(unittest.TestCase):
    """Keep one fail-closed CI path for compiling the infrastructure templates."""

    def test_workflow_installs_bicep_and_requires_topology_tests(self):
        workflow = BICEP_TOPOLOGY_WORKFLOW.read_text(encoding="utf-8")

        assert "bicep-topology:" in workflow
        assert "PYRIT_REQUIRE_BICEP: 'true'" in workflow
        assert workflow.count("AZURE_CONFIG_DIR: ${{ runner.temp }}/.azure") == 2
        assert "az bicep install --version v0.46.1" in workflow
        assert "python tests/unit/infra/test_bicep_topology.py -v" in workflow
        assert workflow.count("'infra/**/*.bicep'") == 2
        assert workflow.count("'tests/unit/infra/**'") == 2
        assert workflow.count("'.github/workflows/bicep_topology.yml'") == 2
        assert "merge_group:" in workflow
        assert "workflow_dispatch:" in workflow


@unittest.skipIf(BICEP_CLI is None and not BICEP_REQUIRED, "Bicep CLI is not already installed")
class TestBicepTopology(unittest.TestCase):
    """Verify the only supported public ACA topology with fixed NAT egress."""

    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.output_directory = Path(self._temporary_directory.name)

    def test_main_has_one_public_nat_topology(self) -> None:
        template = _compile_bicep(MAIN_BICEP, self.output_directory / "main.json")

        expected_defaults = {
            "appName": "pyrit-gui",
            "location": "[resourceGroup().location]",
            "containerImage": "",
            "deployInfra": True,
            "deployApp": True,
            "allowedCidr": "",
            "allowedCidrDescription": "Allowed IP range",
            "pyritInitializer": "target,technique",
            "pyritConfigFileUri": "",
            "envSecretName": "env-global",
            "envFileContents": "",
            "cpuCores": "1.0",
            "memoryGb": "2.0",
            "minReplicas": 1,
            "maxReplicas": 1,
            "acrName": "",
            "vnetAddressPrefix": "10.0.0.0/16",
            "infrastructureSubnetAddressPrefix": "10.0.1.0/26",
            "egressPublicIpTags": [],
            "protectEgressPublicIp": False,
            "logRetentionDays": 90,
            "logAnalyticsWorkspaceId": "",
            "logAnalyticsCustomerId": "",
            "logAnalyticsSharedKey": "",
            "acrResourceId": "",
            "existingManagedIdentityResourceId": "",
            "tags": {
                "Service": "pyrit-gui",
                "Owner": "<your-team>",
                "DataClass": "<your-data-classification>",
            },
            "enableOtel": False,
            "enableFrontDoor": False,
            "enableFrontDoorPrivateLink": False,
            "frontDoorPrivateLinkRequestMessage": (
                "[format('Azure Front Door private access to {0}', parameters('appName'))]"
            ),
            "disableContainerAppsPublicAccess": False,
        }
        required_parameters = {
            "entraTenantId",
            "entraClientId",
            "allowedGroupObjectIds",
            "adminGroupObjectId",
            "sqlServerFqdn",
            "sqlDatabaseName",
            "keyVaultResourceId",
        }
        assert set(template["parameters"]) == set(expected_defaults) | required_parameters
        for name, expected in expected_defaults.items():
            assert template["parameters"][name]["defaultValue"] == expected, name
        for name in required_parameters:
            assert "defaultValue" not in template["parameters"][name], name

        phases = _resources(template, "Microsoft.Resources/deployments")
        assert len(phases) == len(template["resources"]) == 2
        infrastructure = next(phase for phase in phases if "-infrastructure" in phase["name"])
        application = next(phase for phase in phases if "-application" in phase["name"])
        infrastructure_template = infrastructure["properties"]["template"]
        application_template = application["properties"]["template"]
        resource_contracts = {
            "Microsoft.ContainerRegistry/registries": ("2023-08-01-preview", "[variables('generatedAcrName')]"),
            "Microsoft.OperationalInsights/workspaces": (
                "2023-09-01",
                "[format('{0}-logs', parameters('appName'))]",
            ),
            "Microsoft.Insights/components": ("2020-02-02", "[format('{0}-ai', parameters('appName'))]"),
            "Microsoft.ManagedIdentity/userAssignedIdentities": (
                "2023-01-31",
                "[format('{0}-identity', parameters('appName'))]",
            ),
            "Microsoft.App/managedEnvironments": ("2024-10-02-preview", "[format('{0}-env', parameters('appName'))]"),
        }
        for resource_type, (api_version, name) in resource_contracts.items():
            resources = _resources(infrastructure_template, resource_type)
            assert len(resources) == 1
            assert resources[0]["apiVersion"] == api_version
            assert resources[0]["name"] == name
            assert resources[0]["location"] == "[parameters('location')]"
            assert resources[0]["tags"] == "[parameters('tags')]"
            assert "scope" not in resources[0]
        created_identity = _resources(infrastructure_template, "Microsoft.ManagedIdentity/userAssignedIdentities")[0]
        assert created_identity["condition"] == "[variables('createManagedIdentity')]"
        assert infrastructure_template["variables"]["createManagedIdentity"] == (
            "[empty(parameters('existingManagedIdentityResourceId'))]"
        )
        created_acr = _resources(infrastructure_template, "Microsoft.ContainerRegistry/registries")[0]
        assert created_acr["condition"] == "[variables('createAcr')]"
        assert "parameters('acrName')" in infrastructure_template["variables"]["createAcr"]
        assert "parameters('acrResourceId')" in infrastructure_template["variables"]["createAcr"]
        assert created_acr["sku"]["name"] == "Standard"
        assert created_acr["properties"]["adminUserEnabled"] is False
        log_analytics = _resources(infrastructure_template, "Microsoft.OperationalInsights/workspaces")[0]
        assert log_analytics["condition"] == "[variables('createLogAnalytics')]"
        assert log_analytics["properties"]["retentionInDays"] == "[parameters('logRetentionDays')]"
        app_insights = _resources(infrastructure_template, "Microsoft.Insights/components")[0]
        assert app_insights["condition"] == "[parameters('enableOtel')]"

        modules = _resources(infrastructure_template, "Microsoft.Resources/deployments")
        assert len(modules) == 2
        network_module = next(module for module in modules if "aca-nat-network" in module["name"])
        front_door_module = next(module for module in modules if "aca-front-door" in module["name"])
        assert "condition" not in network_module
        assert front_door_module["condition"] == "[parameters('enableFrontDoor')]"
        assert (
            "effectiveFrontDoorPrivateLink"
            in front_door_module["properties"]["parameters"]["enablePrivateLink"]["value"]
        )
        assert (
            "Microsoft.App/managedEnvironments"
            in front_door_module["properties"]["parameters"]["originResourceId"]["value"]
        )
        assert (
            "frontDoorPrivateLinkRequestMessage"
            in front_door_module["properties"]["parameters"]["privateLinkRequestMessage"]["value"]
        )

        environment = _resources(infrastructure_template, "Microsoft.App/managedEnvironments")[0]
        environment_properties = environment["properties"]
        assert "condition" not in environment
        assert environment_properties["publicNetworkAccess"] == "[variables('effectiveContainerAppsPublicAccess')]"
        effective_public_access = infrastructure_template["variables"]["effectiveContainerAppsPublicAccess"]
        assert "disableContainerAppsPublicAccess" in effective_public_access
        assert "effectiveFrontDoorPrivateLink" in effective_public_access
        assert "fail(" in effective_public_access
        assert "existingAcaEnvironment" not in effective_public_access
        assert environment_properties["vnetConfiguration"]["internal"] is False
        assert (
            environment_properties["appLogsConfiguration"]["logAnalyticsConfiguration"]["dynamicJsonColumns"] is False
        )
        assert environment_properties["peerAuthentication"]["mtls"]["enabled"] is False
        assert environment_properties["peerTrafficConfiguration"]["encryption"]["enabled"] is False
        assert (
            "outputs.infrastructureSubnetId.value"
            in environment_properties["vnetConfiguration"]["infrastructureSubnetId"]
        )

        container_app = _resources(application_template, "Microsoft.App/containerApps")[0]
        assert "condition" not in container_app
        assert container_app["apiVersion"] == "2024-03-01"
        assert container_app["name"] == "[parameters('appName')]"
        assert container_app["location"] == "[parameters('location')]"
        assert container_app["tags"] == "[parameters('tags')]"
        assert "scope" not in container_app
        assert container_app["properties"]["managedEnvironmentId"] == (
            "[resourceId('Microsoft.App/managedEnvironments', format('{0}-env', parameters('appName')))]"
        )
        assert container_app["properties"]["configuration"]["activeRevisionsMode"] == "Single"
        ingress = container_app["properties"]["configuration"]["ingress"]
        assert ingress["external"] is True
        assert ingress["targetPort"] == 8000
        assert ingress["transport"] == "http"
        assert ingress["allowInsecure"] is False
        assert container_app["properties"]["template"]["containers"][0]["resources"] == {
            "cpu": "[json(parameters('cpuCores'))]",
            "memory": "[format('{0}Gi', parameters('memoryGb'))]",
        }
        assert container_app["properties"]["template"]["scale"] == {
            "minReplicas": "[parameters('minReplicas')]",
            "maxReplicas": "[parameters('maxReplicas')]",
        }
        assert container_app["properties"]["configuration"]["registries"][0]["identity"] == (
            "[variables('effectiveManagedIdentityId')]"
        )

    def test_main_gates_application_and_infrastructure_independently(self) -> None:
        template = _compile_bicep(MAIN_BICEP, self.output_directory / "app-only.json")

        phases = _resources(template, "Microsoft.Resources/deployments")
        assert len(phases) == len(template["resources"]) == 2
        infrastructure = next(phase for phase in phases if "-infrastructure" in phase["name"])
        application = next(phase for phase in phases if "-application" in phase["name"])
        assert infrastructure["condition"] == "[parameters('deployInfra')]"
        assert application["condition"] == "[parameters('deployApp')]"
        assert any("-infrastructure" in dependency for dependency in application["dependsOn"])
        application_parameters = application["properties"]["parameters"]
        identity = application_parameters["existingManagedIdentityResourceId"]
        assert identity.startswith("[if(parameters('deployInfra'),")
        assert "outputs.managedIdentityResourceId.value" in identity
        assert identity.endswith(", createObject('value', parameters('existingManagedIdentityResourceId')))]")
        registry = application_parameters["acrName"]
        assert registry.startswith("[if(parameters('deployInfra'),")
        assert "outputs.acrLoginServer.value" in registry
        assert registry.endswith(", createObject('value', parameters('acrName')))]")
        for name, value in application_parameters.items():
            if name not in {"acrName", "existingManagedIdentityResourceId"}:
                assert "reference(" not in value["value"], name
        assert not infrastructure.get("dependsOn")

        outputs = template["outputs"]
        assert set(outputs) == {
            "appFqdn",
            "frontDoorFqdn",
            "frontDoorUrl",
            "frontDoorPrivateLinkRequestMessage",
            "containerAppsPublicNetworkAccess",
            "publicFqdn",
            "environmentDefaultDomain",
            "egressPublicIpAddress",
            "natGatewayId",
            "acaInfrastructureSubnetId",
            "managedIdentityPrincipalId",
            "managedIdentityResourceId",
            "sqlAadSetupRequired",
            "keyVaultName",
            "acrLoginServer",
            "vnetName",
            "appInsightsConnectionString",
        }
        assert all(output["type"] == "string" for output in outputs.values())
        assert outputs["egressPublicIpAddress"]["value"].startswith("[if(parameters('deployInfra'),")
        assert "Microsoft.Network/publicIPAddresses" in outputs["egressPublicIpAddress"]["value"]
        assert "Microsoft.Cdn/profiles/afdEndpoints" in outputs["frontDoorFqdn"]["value"]
        assert "parameters('deployInfra')" in outputs["frontDoorFqdn"]["value"]
        assert outputs["appFqdn"]["value"].startswith("[if(parameters('deployApp'),")
        assert "reference(resourceId('Microsoft.App/containerApps'" in outputs["appFqdn"]["value"]
        for name in (
            "containerAppsPublicNetworkAccess",
            "environmentDefaultDomain",
            "natGatewayId",
            "acaInfrastructureSubnetId",
            "managedIdentityPrincipalId",
            "managedIdentityResourceId",
            "acrLoginServer",
            "vnetName",
        ):
            assert outputs[name]["value"].startswith("[if(parameters('deployInfra'),"), name
        assert outputs["frontDoorPrivateLinkRequestMessage"]["value"].endswith(", '')]")

    def test_main_forwards_phase_parameters_and_secure_values(self) -> None:
        template = _compile_bicep(MAIN_BICEP, self.output_directory / "forwarding.json")

        for phase in _resources(template, "Microsoft.Resources/deployments"):
            parameters = phase["properties"]["parameters"]
            nested = phase["properties"]["template"]
            assert "subscriptionId" not in phase
            assert "resourceGroup" not in phase
            assert set(parameters) == set(nested["parameters"])
            assert {"deployInfra", "deployApp"}.isdisjoint(parameters)
            assert phase["properties"]["expressionEvaluationOptions"]["scope"] == "inner"
            for name, parameter in parameters.items():
                if "-application" not in phase["name"] or name not in {
                    "acrName",
                    "existingManagedIdentityResourceId",
                }:
                    assert parameter["value"] == f"[parameters('{name}')]", name
                assert nested["parameters"][name]["type"] == template["parameters"][name]["type"]
                if "-application" not in phase["name"] or name not in {
                    "containerImage",
                    "existingManagedIdentityResourceId",
                }:
                    assert nested["parameters"][name].get("defaultValue") == template["parameters"][name].get(
                        "defaultValue"
                    ), name
            for secret in {"envFileContents", "pyritConfigFileUri", "logAnalyticsSharedKey"} & set(parameters):
                assert nested["parameters"][secret]["type"] == "securestring"
                assert secret not in json.dumps(nested["outputs"])
        for secret in ("envFileContents", "pyritConfigFileUri", "logAnalyticsSharedKey"):
            assert template["parameters"][secret]["type"] == "securestring"
            assert secret not in json.dumps(template["outputs"])

    def test_standalone_phases_have_disjoint_write_sets(self) -> None:
        infrastructure = _compile_bicep(INFRASTRUCTURE_BICEP, self.output_directory / "infrastructure.json")
        application = _compile_bicep(APPLICATION_BICEP, self.output_directory / "application.json")

        assert {
            "containerImage",
            "entraTenantId",
            "entraClientId",
            "sqlServerFqdn",
            "sqlDatabaseName",
            "keyVaultResourceId",
            "pyritConfigFileUri",
            "envFileContents",
        }.isdisjoint(infrastructure["parameters"])
        assert {
            "vnetAddressPrefix",
            "infrastructureSubnetAddressPrefix",
            "enableFrontDoorPrivateLink",
            "disableContainerAppsPublicAccess",
            "protectEgressPublicIp",
            "logAnalyticsSharedKey",
        }.isdisjoint(application["parameters"])
        infrastructure_resources = infrastructure["resources"] + [
            resource
            for module in _resources(infrastructure, "Microsoft.Resources/deployments")
            for resource in module["properties"]["template"]["resources"]
        ]
        infrastructure_types = {resource["type"] for resource in infrastructure_resources}
        assert infrastructure_types == {
            "Microsoft.Resources/deployments",
            "Microsoft.ContainerRegistry/registries",
            "Microsoft.OperationalInsights/workspaces",
            "Microsoft.Insights/components",
            "Microsoft.ManagedIdentity/userAssignedIdentities",
            "Microsoft.App/managedEnvironments",
            "Microsoft.Network/publicIPAddresses",
            "Microsoft.Authorization/locks",
            "Microsoft.Network/natGateways",
            "Microsoft.Network/virtualNetworks",
            "Microsoft.Cdn/profiles",
            "Microsoft.Cdn/profiles/afdEndpoints",
            "Microsoft.Cdn/profiles/originGroups",
            "Microsoft.Cdn/profiles/originGroups/origins",
            "Microsoft.Cdn/profiles/afdEndpoints/routes",
        }
        assert len(application["resources"]) == 1
        assert application["resources"][0]["type"] == "Microsoft.App/containerApps"
        assert application["resources"][0]["type"] not in infrastructure_types
        assert "condition" not in application["resources"][0]
        assert {
            "frontDoorPrivateLinkRequestMessage",
            "frontDoorFqdn",
            "environmentDefaultDomain",
            "containerAppsPublicNetworkAccess",
            "egressPublicIpAddress",
            "natGatewayId",
            "acaInfrastructureSubnetId",
            "vnetName",
            "managedIdentityResourceId",
            "managedIdentityPrincipalId",
            "acrLoginServer",
            "appInsightsConnectionString",
        } <= set(infrastructure["outputs"])
        assert set(application["outputs"]) == {
            "appFqdn",
            "frontDoorFqdn",
            "containerAppsPublicNetworkAccess",
        }

    def test_application_reads_existing_state_and_preserves_authentication(self) -> None:
        template = _compile_bicep(APPLICATION_BICEP, self.output_directory / "application-config.json")

        for name in ("containerImage", "existingManagedIdentityResourceId"):
            assert template["parameters"][name]["minLength"] == 1
            assert "defaultValue" not in template["parameters"][name]
        assert "fail(" in template["variables"]["validatedAllowedGroupObjectIds"]
        assert "fail(" in template["variables"]["validatedAdminGroupObjectId"]
        assert "trim(" in template["variables"]["normalizedAllowedGroupObjectIds"]
        assert "trim(" in template["variables"]["normalizedAdminGroupObjectId"]
        assert "fail('App-only deployment requires an existing registry')" in template["variables"]["effectiveAcrName"]
        effective_allowed_cidr = template["variables"]["effectiveAllowedCidr"]
        assert "parameters('allowedCidr')" in effective_allowed_cidr
        assert "parameters('enableFrontDoor')" in effective_allowed_cidr
        assert "fail(" in effective_allowed_cidr
        container_app = _resources(template, "Microsoft.App/containerApps")[0]
        assert container_app["identity"]["type"] == "UserAssigned"
        assert container_app["properties"]["template"]["containers"][0]["image"] == "[parameters('containerImage')]"
        identity = template["variables"]["effectiveManagedIdentityId"]
        assert "resourceId(" in identity or "extensionResourceId(" in identity
        assert "variables('existingManagedIdentitySegments')[2]" in identity
        assert "variables('existingManagedIdentitySegments')[4]" in identity
        assert "Microsoft.ManagedIdentity/userAssignedIdentities" in identity
        container_env = container_app["properties"]["template"]["containers"][0]["env"]
        environment = {value["name"]: value["value"] for value in container_env if isinstance(value, dict)}
        assert "validatedAllowedGroupObjectIds" in environment["ENTRA_ALLOWED_GROUP_IDS"]
        assert "validatedAdminGroupObjectId" in environment["ENTRA_ADMIN_GROUP_ID"]
        assert "Microsoft.ManagedIdentity/userAssignedIdentities" in environment["AZURE_CLIENT_ID"]
        assert ".clientId" in environment["AZURE_CLIENT_ID"]
        assert environment["ENTRA_CLIENT_ID"] == "[parameters('entraClientId')]"
        assert environment["ENTRA_TENANT_ID"] == "[parameters('entraTenantId')]"
        assert "parameters('enableOtel')" in environment["OTEL_EXPORTER_OTLP_ENDPOINT"]
        assert "http://localhost:4318" in environment["OTEL_EXPORTER_OTLP_ENDPOINT"]
        secrets = container_app["properties"]["configuration"]["secrets"]
        assert "parameters('envFileContents')" in secrets
        assert "parameters('pyritConfigFileUri')" in secrets
        serialized_container_env = json.dumps(container_env)
        assert "'secretRef', 'env-file'" in serialized_container_env
        assert "'secretRef', 'config-file-uri'" in serialized_container_env
        assert "PYRIT_CONFIG_FILE" in serialized_container_env
        assert "PYRIT_ENV_AKV_REF" in serialized_container_env
        assert "keyvaultDns" in serialized_container_env
        cors_value = environment["PYRIT_CORS_ORIGINS"]
        assert "parameters('enableFrontDoor')" in cors_value
        assert "Microsoft.Cdn/profiles/afdEndpoints" in cors_value
        assert ".hostName" in cors_value
        assert "uniqueString(subscription().id, resourceGroup().id, parameters('appName'))" in cors_value
        assert "Microsoft.App/managedEnvironments" in cors_value
        assert ".publicNetworkAccess" in cors_value
        assert "'Disabled'" in cors_value
        assert ".defaultDomain" in cors_value
        assert "Microsoft.Resources/deployments" not in json.dumps(template)
        assert "deployInfra" not in json.dumps(template)
        assert "deployApp" not in json.dumps(template)

    def test_aca_nat_network_is_static_and_delegated(self):
        template = _compile_bicep(NETWORK_BICEP, self.output_directory / "network.json")

        public_ips = _resources(template, "Microsoft.Network/publicIPAddresses")
        assert len(public_ips) == 1
        public_ip = public_ips[0]
        assert public_ip["sku"]["name"] == "Standard"
        assert public_ip["sku"]["tier"] == "Regional"
        assert public_ip["properties"]["publicIPAllocationMethod"] == "Static"
        assert public_ip["properties"]["publicIPAddressVersion"] == "IPv4"
        assert public_ip["properties"]["ddosSettings"]["protectionMode"] == "VirtualNetworkInherited"
        assert public_ip["properties"]["ipTags"] == "[parameters('egressPublicIpTags')]"

        locks = _resources(template, "Microsoft.Authorization/locks")
        assert len(locks) == 1
        assert "parameters('protectEgressPublicIp')" in locks[0]["condition"]
        assert locks[0]["properties"]["level"] == "CanNotDelete"
        assert "publicIPAddresses" in locks[0]["scope"]

        nat_gateway = _resources(template, "Microsoft.Network/natGateways")[0]
        assert nat_gateway["sku"]["name"] == "Standard"
        assert len(nat_gateway["properties"]["publicIpAddresses"]) == 1
        assert not _resources(template, "Microsoft.Network/routeTables")

        assert not _resources(template, "Microsoft.Network/virtualNetworks/subnets")
        vnet = _resources(template, "Microsoft.Network/virtualNetworks")[0]
        assert vnet["properties"]["privateEndpointVNetPolicies"] == "Disabled"
        assert len(vnet["properties"]["subnets"]) == 1
        subnet = vnet["properties"]["subnets"][0]
        assert subnet["properties"]["addressPrefix"] == "[parameters('infrastructureSubnetAddressPrefix')]"
        assert subnet["properties"]["defaultOutboundAccess"] is False
        assert subnet["properties"]["delegations"][0]["properties"]["serviceName"] == "Microsoft.App/environments"
        assert "natGateway" in subnet["properties"]
        assert "networkSecurityGroup" not in subnet["properties"]

        assert not _resources(template, "Microsoft.Network/networkSecurityGroups")
        assert not _resources(template, "Microsoft.Network/networkSecurityGroups/securityRules")

    def test_front_door_uses_https_health_probe_without_caching(self):
        template = _compile_bicep(FRONT_DOOR_BICEP, self.output_directory / "front-door.json")

        profile = _resources(template, "Microsoft.Cdn/profiles")[0]
        assert profile["sku"]["name"] == "Premium_AzureFrontDoor"
        assert profile["properties"]["originResponseTimeoutSeconds"] == 240
        assert "namePrefix" in template["parameters"]["privateLinkRequestMessage"]["defaultValue"]

        origin_group = _resources(template, "Microsoft.Cdn/profiles/originGroups")[0]
        probe = origin_group["properties"]["healthProbeSettings"]
        assert probe["probePath"] == "/api/health"
        assert probe["probeProtocol"] == "Https"
        assert probe["probeRequestType"] == "GET"

        origin = _resources(template, "Microsoft.Cdn/profiles/originGroups/origins")[0]
        origin_properties = origin["properties"]
        assert "originHostHeader" in origin_properties
        assert "enforceCertificateNameCheck" in origin_properties
        assert "parameters('enablePrivateLink')" in origin_properties
        assert "sharedPrivateLinkResource" in origin_properties
        assert "managedEnvironments" in origin_properties
        assert "effectiveOriginResourceId" in origin_properties
        assert "effectiveOriginLocation" in origin_properties
        assert "Pending" in origin_properties

        route = _resources(template, "Microsoft.Cdn/profiles/afdEndpoints/routes")[0]
        assert route["properties"]["forwardingProtocol"] == "HttpsOnly"
        assert route["properties"]["httpsRedirect"] == "Enabled"
        assert "cacheConfiguration" not in route["properties"]

    def test_private_endpoint_approval_preserves_discovery_description(self):
        template = _compile_bicep(PRIVATE_ENDPOINT_APPROVAL_BICEP, self.output_directory / "approval.json")

        connections = _resources(template, "Microsoft.App/managedEnvironments/privateEndpointConnections")
        assert len(connections) == 1
        state = connections[0]["properties"]["privateLinkServiceConnectionState"]
        assert state["status"] == "Approved"
        assert state["description"] == "[parameters('approvalDescription')]"


if __name__ == "__main__":
    unittest.main()
