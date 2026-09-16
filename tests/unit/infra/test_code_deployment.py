# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
"""Exercise the shared Bash deployment flow without Azure or network calls."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOY_SCRIPT = REPO_ROOT / "infra" / "pipelines" / "deploy_gui.sh"
SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
RESOURCE_GROUP = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/copyrit-test"
APP = f"{RESOURCE_GROUP}/providers/Microsoft.App/containerApps/copyrit-test"
ENVIRONMENT = f"{RESOURCE_GROUP}/providers/Microsoft.App/managedEnvironments/copyrit-test-env"
VNET = f"{RESOURCE_GROUP}/providers/Microsoft.Network/virtualNetworks/copyrit-test-vnet"
SUBNET = f"{VNET}/subnets/copyrit-test-aca-subnet"
NAT = f"{RESOURCE_GROUP}/providers/Microsoft.Network/natGateways/copyrit-test-nat"
PIP = f"{RESOURCE_GROUP}/providers/Microsoft.Network/publicIPAddresses/copyrit-test-egress-pip"
IMAGE = f"copyritacr.azurecr.io/pyrit@sha256:{'a' * 64}"
PREVIOUS_IMAGE = f"copyritacr.azurecr.io/pyrit@sha256:{'b' * 64}"
ACA_HOST = "copyrit-test.example.westus2.azurecontainerapps.io"
AFD_HOST = "copyrit-test.example.azurefd.net"
REVISION = "copyrit-test--0000002"
PREVIOUS_REVISION = "copyrit-test--0000001"


def _find_bash() -> str | None:
    if os.name != "nt":
        return shutil.which("bash")
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Git" / "bin" / "bash.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "bin" / "bash.exe",
    ]
    return next((str(candidate) for candidate in candidates if candidate.is_file()), None)


BASH = _find_bash()
JQ = shutil.which("jq")

# Only the external services and elapsed time are replaced; source, jq, and Python run normally.
HARNESS = r"""
set -euo pipefail
az() {
  printf '%s\t' "$@" >> "$MOCK_DIRECTORY/az.log"; printf '\n' >> "$MOCK_DIRECTORY/az.log"
  if [[ -n "$MOCK_FAIL" && "$*" == "$MOCK_FAIL"* ]]; then return 23; fi
  local query='' previous='' argument deploy_app=false deploy_infra=false
  for argument in "$@"; do
    [[ "$previous" != --query ]] || query=$argument
    previous=$argument
    case "$argument" in
      deployApp=true) deploy_app=true ;;
      deployInfra=true) deploy_infra=true ;;
    esac
  done
  case "$1 $2 ${3:-}" in
    'account show --query') printf '%s\n' "$MOCK_SUBSCRIPTION" ;;
    'resource show --ids') : ;;
    'resource list --resource-group') printf '%s\n' "$MOCK_FRONT_DOOR_COUNT" ;;
    'group show --name') printf '%s\n' "$MOCK_RESOURCE_GROUP" ;;
    'containerapp show --resource-group')
      case "$query" in
        '{id:'*) printf '%s\n' "$MOCK_APP" ;;
        properties.latestRevisionName) printf '%s\n' "${MOCK_REVISION:-$(cat "$MOCK_DIRECTORY/revision")}" ;;
        properties.configuration.ingress.fqdn) printf '%s\n' "$MOCK_ACA_HOST" ;;
        '{latest:'*)
          touch "$MOCK_DIRECTORY/final-read"
          jq -cn --arg latest "${MOCK_FINAL_LATEST:-$(cat "$MOCK_DIRECTORY/revision")}" \
            --arg ready "${MOCK_FINAL_READY:-$(cat "$MOCK_DIRECTORY/revision")}" \
            --arg image "${MOCK_FINAL_IMAGE:-$(cat "$MOCK_DIRECTORY/image")}" \
            '{latest:$latest,ready:$ready,image:$image}' ;;
        *) echo "Unexpected app query: $query" >&2; return 97 ;;
      esac ;;
    'containerapp env show')
      if [[ "$query" == '{id:'* ]]; then printf '%s\n' "$MOCK_ENVIRONMENT"
      elif [[ "$query" == properties.publicNetworkAccess ]]; then
        if [[ -e "$MOCK_DIRECTORY/final-read" && -n "$MOCK_FINAL_ACCESS" ]]; then
          printf '%s\n' "$MOCK_FINAL_ACCESS"
        else cat "$MOCK_DIRECTORY/access"; fi
      else echo "Unexpected environment query: $query" >&2; return 97; fi ;;
    'network vnet show') printf '%s\n' "$MOCK_VNET" ;;
    'network vnet subnet') printf '%s\n' "$MOCK_SUBNET" ;;
    'network nat gateway') printf '%s\n' "$MOCK_NAT" ;;
    'network public-ip show')
      case "$query" in
        '{id:'*) printf '%s\n' "$MOCK_PIP" ;;
        'ipTags || `[]`') printf '[]\n' ;;
        id) printf '%s\n' "$MOCK_PIP_ID" ;;
        *) echo "Unexpected PIP query: $query" >&2; return 97 ;;
      esac ;;
    'deployment group what-if') printf '%s\n' "$MOCK_WHAT_IF" ;;
    'deployment group create')
      [[ "$*" != *-rollback-origin* ]] || touch "$MOCK_DIRECTORY/rollback"
      for argument in "$@"; do
        case "$argument" in
          containerImage=*)
            if [[ "$deploy_app" == true ]]; then
              printf '%s\n' "${argument#*=}" > "$MOCK_DIRECTORY/image"
              printf '%s\n' "$MOCK_DEPLOYED_REVISION" > "$MOCK_DIRECTORY/revision"
            fi ;;
          disableContainerAppsPublicAccess=true)
            if [[ "$deploy_infra" == true ]]; then printf 'Disabled\n' > "$MOCK_DIRECTORY/access"; fi ;;
          disableContainerAppsPublicAccess=false)
            if [[ "$deploy_infra" == true ]]; then printf 'Enabled\n' > "$MOCK_DIRECTORY/access"; fi ;;
        esac
      done ;;
    'deployment group show')
      case "$query" in
        properties.outputs.frontDoorPrivateLinkRequestMessage.value) printf '%s\n' "$MOCK_PL_MESSAGE" ;;
        properties.outputs.appFqdn.value) printf '%s\n' "$MOCK_ACA_HOST" ;;
        properties.outputs.frontDoorFqdn.value) printf '%s\n' "$MOCK_AFD_HOST" ;;
        properties.outputs.egressPublicIpAddress.value) printf '203.0.113.10\n' ;;
        *) echo "Unexpected deployment output: $query" >&2; return 97 ;;
      esac ;;
    'containerapp revision show')
      jq -cn --arg image "${MOCK_REVISION_IMAGE:-$(cat "$MOCK_DIRECTORY/image")}" \
        --arg health "$MOCK_HEALTH" '{image:$image,health:$health}' ;;
    'rest --method get') printf '%s\n' "$MOCK_ORIGIN" ;;
    'rest --method delete') : ;;
    'network private-endpoint-connection list')
      if [[ -e "$MOCK_DIRECTORY/rollback" ]]; then printf '[]\n'
      else printf '%s\n' "$MOCK_CONNECTIONS"; fi ;;
    *) echo "Unexpected Azure call: $*" >&2; return 97 ;;
  esac
}
curl() {
  printf '%s\t' "$@" >> "$MOCK_DIRECTORY/curl.log"; printf '\n' >> "$MOCK_DIRECTORY/curl.log"
  if [[ "${!#}" == "https://$MOCK_ACA_HOST/api/health" &&
    "$(cat "$MOCK_DIRECTORY/access")" == Disabled ]]; then
    printf '403'; return 0
  fi
  printf '%s' "$MOCK_HTTP_STATUS"
  return "$MOCK_HTTP_EXIT"
}
sleep() { SECONDS=$((SECONDS + $1)); }
"""


@unittest.skipIf(BASH is None or JQ is None, "Native Bash and jq are required")
class TestCodeDeployment(unittest.TestCase):
    def setUp(self) -> None:
        tags = {"owner": "copyrit"}
        self.environment = {
            "PYRIT_SLOT": "test",
            "PYRIT_DEPLOY_INFRA": "false",
            "PYRIT_BUILD_ID": "42",
            "PYRIT_SOURCE_DIRECTORY": REPO_ROOT.as_posix(),
            "PYRIT_DEPLOYMENT_RESOURCE_GROUP": "copyrit-test",
            "PYRIT_APP_NAME": "copyrit-test",
            "PYRIT_CONTAINER_IMAGE": IMAGE,
            "PYRIT_VNET_ADDRESS_PREFIX": "10.20.0.0/16",
            "PYRIT_INFRASTRUCTURE_SUBNET_ADDRESS_PREFIX": "10.20.0.0/23",
            "PYRIT_ALLOWED_CLIENT_CIDR": "",
            "PYRIT_MANAGED_IDENTITY_RESOURCE_ID": (
                f"{RESOURCE_GROUP}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/copyrit-id"
            ),
            "PYRIT_ENTRA_TENANT_ID": SUBSCRIPTION,
            "PYRIT_ENTRA_CLIENT_ID": SUBSCRIPTION,
            "PYRIT_ALLOWED_GROUP_OBJECT_IDS": SUBSCRIPTION,
            "PYRIT_ADMIN_GROUP_OBJECT_ID": SUBSCRIPTION,
            "PYRIT_CONFIG_FILE_URI": "",
            "PYRIT_SQL_SERVER_FQDN": "copyrit.database.windows.net",
            "PYRIT_SQL_DATABASE_NAME": "copyrit",
            "PYRIT_KEY_VAULT_RESOURCE_ID": f"{RESOURCE_GROUP}/providers/Microsoft.KeyVault/vaults/copyrit-kv",
            "PYRIT_ACR_RESOURCE_ID": f"{RESOURCE_GROUP}/providers/Microsoft.ContainerRegistry/registries/copyritacr",
            "PYRIT_ENABLE_OTEL": "false",
            "PYRIT_ENV_SECRET_NAME": "pyrit-env",
            "MOCK_SUBSCRIPTION": SUBSCRIPTION,
            "MOCK_RESOURCE_GROUP": RESOURCE_GROUP,
            "MOCK_FRONT_DOOR_COUNT": "0",
            "MOCK_PIP_ID": PIP,
            "MOCK_REVISION": "",
            "MOCK_DEPLOYED_REVISION": REVISION,
            "MOCK_REVISION_IMAGE": "",
            "MOCK_FINAL_LATEST": "",
            "MOCK_FINAL_READY": "",
            "MOCK_FINAL_IMAGE": "",
            "MOCK_FINAL_ACCESS": "",
            "MOCK_HEALTH": "Healthy",
            "MOCK_ACA_HOST": ACA_HOST,
            "MOCK_AFD_HOST": AFD_HOST,
            "MOCK_PL_MESSAGE": "Azure Front Door private access to copyrit-test",
            "MOCK_HTTP_STATUS": "200",
            "MOCK_HTTP_EXIT": "0",
            "MOCK_FAIL": "",
        }
        self.fixtures = {
            "MOCK_APP": {
                "id": APP,
                "environmentId": ENVIRONMENT,
                "tags": tags,
                "mode": "Single",
                "revision": PREVIOUS_REVISION,
                "containers": [{"name": "pyrit-gui", "image": PREVIOUS_IMAGE}],
            },
            "MOCK_ENVIRONMENT": {"id": ENVIRONMENT, "publicNetworkAccess": "Enabled"},
            "MOCK_VNET": {"id": VNET, "prefix": "10.20.0.0/16", "tags": tags},
            "MOCK_SUBNET": {"id": SUBNET, "prefix": "10.20.0.0/23", "natId": NAT},
            "MOCK_NAT": {"id": NAT, "pipId": PIP, "tags": tags},
            "MOCK_PIP": {"id": PIP, "ip": "203.0.113.10", "allocation": "Static", "sku": "Standard", "tags": tags},
            "MOCK_WHAT_IF": {
                "changes": [
                    {"changeType": "Modify", "resourceId": APP, "delta": [{"path": "properties.configuration"}]},
                    {"changeType": "NoChange", "resourceId": ENVIRONMENT},
                ]
            },
            "MOCK_ORIGIN": {"status": "Approved", "resourceId": ENVIRONMENT},
            "MOCK_CONNECTIONS": [
                {
                    "id": f"{ENVIRONMENT}/privateEndpointConnections/connection-1",
                    "properties": {
                        "privateLinkServiceConnectionState": {
                            "status": "Approved",
                            "description": self.environment["MOCK_PL_MESSAGE"],
                        }
                    },
                }
            ],
        }

    def _run(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        assert BASH is not None and JQ is not None
        environment = (
            os.environ
            | self.environment
            | {name: json.dumps(value) for name, value in self.fixtures.items()}
            | overrides
        )
        environment["MSYS2_ARG_CONV_EXCL"] = "*"
        environment.pop("BASH_ENV", None)
        with tempfile.TemporaryDirectory(prefix=".deployment-test-", dir=REPO_ROOT) as directory:
            fixture_dir = Path(directory)
            environment["MOCK_DIRECTORY"] = fixture_dir.as_posix()
            environment["PYRIT_AGENT_TEMP_DIRECTORY"] = fixture_dir.as_posix()
            (fixture_dir / "image").write_text(PREVIOUS_IMAGE, encoding="utf-8")
            (fixture_dir / "revision").write_text(PREVIOUS_REVISION, encoding="utf-8")
            access = json.loads(environment["MOCK_ENVIRONMENT"])["publicNetworkAccess"]
            (fixture_dir / "access").write_text(access, encoding="utf-8")
            for log in ("az", "curl"):
                (fixture_dir / f"{log}.log").touch()
            jq_flags = "-b" if os.name == "nt" else ""
            wrappers = (
                f'python3() {{ {shlex.quote(Path(sys.executable).as_posix())} "$@"; }}\n'
                f'jq() {{ {shlex.quote(Path(JQ).as_posix())} {jq_flags} "$@"; }}\n'
            )
            result = subprocess.run(
                [BASH, "--noprofile", "--norc", "-s"],
                input=wrappers + HARNESS + f"\nsource {shlex.quote(DEPLOY_SCRIPT.as_posix())}\n",
                capture_output=True,
                text=True,
                check=False,
                env=environment,
                timeout=60,
            )
            self.az_calls, self.curl_calls = [
                [line.rstrip("\t").split("\t") for line in (fixture_dir / f"{log}.log").read_text().splitlines()]
                for log in ("az", "curl")
            ]
        assert "Unexpected " not in result.stderr, result.stderr
        return result

    def _assert_app_only_writes(self) -> None:
        deployments = [call for call in self.az_calls if call[:3] == ["deployment", "group", "create"]]
        assert len(deployments) == 1, self.az_calls
        assert "deployInfra=false" in deployments[0]
        assert "deployApp=true" in deployments[0]
        assert deployments[0][deployments[0].index("--mode") + 1] == "Incremental"
        assert "containerImage=" + IMAGE in deployments[0]
        assert not any("rollback" in argument for call in self.az_calls for argument in call)
        assert not any(call[:2] == ["containerapp", "update"] for call in self.az_calls)
        assert not any(call[:2] == ["network", "private-endpoint-connection"] for call in self.az_calls)
        assert not any(call[0] == "rest" for call in self.az_calls)

    def test_app_only_reconciles_config_and_preserves_access_mode(self) -> None:
        for access, front_door in (("Enabled", "0"), ("Enabled", "1"), ("Disabled", "1")):
            with self.subTest(access=access, front_door=front_door):
                self.fixtures["MOCK_ENVIRONMENT"]["publicNetworkAccess"] = access
                result = self._run(MOCK_FRONT_DOOR_COUNT=front_door)
                assert result.returncode == 0, result.stdout + result.stderr
                self._assert_app_only_writes()
                deployment = next(call for call in self.az_calls if call[:3] == ["deployment", "group", "create"])
                preview = next(call for call in self.az_calls if call[:3] == ["deployment", "group", "what-if"])
                for call in (preview, deployment):
                    assert call[call.index("--template-file") + 1].endswith("/infra/main.bicep")
                    assert f"enableFrontDoor={'true' if front_door == '1' else 'false'}" in call
                    assert f"disableContainerAppsPublicAccess={'true' if access == 'Disabled' else 'false'}" in call
                    assert "sqlDatabaseName=copyrit" in call
                assert self.az_calls.index(preview) < self.az_calls.index(deployment)
                expected_host = AFD_HOST if access == "Disabled" else ACA_HOST
                assert self.curl_calls[0][-1] == f"https://{expected_host}/api/health"
                assert [call[-1] for call in self.curl_calls] == (
                    [f"https://{AFD_HOST}/api/health", f"https://{ACA_HOST}/api/health"]
                    if access == "Disabled"
                    else [f"https://{ACA_HOST}/api/health"]
                )
                assert all("--location" not in call and "--insecure" not in call for call in self.curl_calls)
                assert "Deployment healthy:" in result.stdout

    def test_infrastructure_then_app_deploys_the_app_once(self) -> None:
        infrastructure_preview = json.dumps(
            {
                "changes": [
                    {"changeType": "Ignore", "resourceId": APP},
                    {"changeType": "NoChange", "resourceId": APP + "/authConfigs/current"},
                    {
                        "changeType": "Modify",
                        "resourceId": ENVIRONMENT,
                        "delta": [{"path": "properties.publicNetworkAccess"}],
                    },
                ]
            }
        )
        result = self._run(
            PYRIT_DEPLOY_INFRA="true", PYRIT_CONTAINER_IMAGE="ignored:mutable", MOCK_WHAT_IF=infrastructure_preview
        )
        assert result.returncode == 0, result.stdout + result.stderr
        deployments = [call for call in self.az_calls if call[:3] == ["deployment", "group", "create"]]
        assert len(deployments) == 1
        for call in [deployments[0], next(c for c in self.az_calls if c[:3] == ["deployment", "group", "what-if"])]:
            assert call[call.index("--template-file") + 1].endswith("/infra/main.bicep")
            assert "deployInfra=true" in call
            assert "deployApp=false" in call
            assert not any(argument.startswith("containerImage=") for argument in call)
            assert call[call.index("--mode") + 1] == "Incremental"
            assert "enableFrontDoorPrivateLink=true" in call
            assert "disableContainerAppsPublicAccess=true" in call
        assert any(call[:2] == ["network", "private-endpoint-connection"] for call in self.az_calls)
        assert any(call[:3] == ["rest", "--method", "get"] for call in self.az_calls)
        assert self.curl_calls[0][-1] == f"https://{AFD_HOST}/api/health"
        assert f"app revision unchanged: {PREVIOUS_REVISION}" in result.stdout

        self.fixtures["MOCK_ENVIRONMENT"]["publicNetworkAccess"] = "Disabled"
        result = self._run(MOCK_FRONT_DOOR_COUNT="1")
        assert result.returncode == 0, result.stdout + result.stderr
        self._assert_app_only_writes()
        deployments += [call for call in self.az_calls if call[:3] == ["deployment", "group", "create"]]
        assert [call for call in deployments if "deployApp=true" in call] == [deployments[1]]
        assert f"Deployment healthy: {REVISION}" in result.stdout

    def test_infrastructure_preview_rejects_app_and_child_writes(self) -> None:
        for resource_id in (APP, APP.upper() + "/", APP + "/authConfigs/current"):
            with self.subTest(resource_id=resource_id):
                preview = {"changes": [{"changeType": "Modify", "resourceId": resource_id}]}
                result = self._run(PYRIT_DEPLOY_INFRA="true", MOCK_WHAT_IF=json.dumps(preview))
                assert result.returncode != 0
                assert "Infrastructure-only preview must not write the Container App" in result.stdout
                assert not any(call[:3] == ["deployment", "group", "create"] for call in self.az_calls)

    def test_infrastructure_failures_roll_back_without_deploying_app(self) -> None:
        for overrides, message in (
            ({"MOCK_REVISION": REVISION}, "changed the running app revision"),
            ({"MOCK_HTTP_STATUS": "504"}, "Application endpoint did not return a healthy response"),
        ):
            with self.subTest(overrides=overrides):
                result = self._run(
                    PYRIT_DEPLOY_INFRA="true",
                    MOCK_WHAT_IF=json.dumps({"changes": [{"changeType": "NoChange", "resourceId": APP}]}),
                    **overrides,
                )
                assert result.returncode != 0
                assert message in result.stdout, result.stdout + result.stderr
                assert "Public ACA origin rollback completed" in result.stdout
                deployments = [call for call in self.az_calls if call[:3] == ["deployment", "group", "create"]]
                assert len(deployments) == 3
                for call in deployments:
                    assert not any(argument.startswith("containerImage=") for argument in call)
                    if call[call.index("--template-file") + 1].endswith("/infra/main.bicep"):
                        assert "deployApp=false" in call
                        assert "deployInfra=true" in call
                        assert call[call.index("--mode") + 1] == "Incremental"

    def test_invalid_inputs_or_topology_fail_before_deployment(self) -> None:
        cases = [
            ({"PYRIT_SLOT": "$(slot)"}, "Required deployment value"),
            ({"PYRIT_CONTAINER_IMAGE": "copyritacr.azurecr.io/pyrit:latest"}, "immutable registry digest"),
            ({"PYRIT_CONTAINER_IMAGE": IMAGE.replace("copyritacr", "otheracr")}, "registry does not match"),
            ({"PYRIT_INFRASTRUCTURE_SUBNET_ADDRESS_PREFIX": "10.30.0.0/23"}, "Invalid network prefix"),
            ({"MOCK_SUBSCRIPTION": "22222222-2222-2222-2222-222222222222"}, "subscription does not match"),
            ({"MOCK_SUBNET": json.dumps(self.fixtures["MOCK_SUBNET"] | {"natId": NAT + "-other"})}, "topology"),
            ({"MOCK_APP": json.dumps(self.fixtures["MOCK_APP"] | {"mode": "Multiple"})}, "single-revision"),
            ({"MOCK_APP": json.dumps(self.fixtures["MOCK_APP"] | {"containers": []})}, "single-revision"),
        ]
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                result = self._run(**overrides)
                assert result.returncode != 0
                assert message in result.stdout, result.stdout + result.stderr
                assert not any(call[:2] == ["deployment", "group"] for call in self.az_calls)
                assert not self.curl_calls

    def test_app_only_what_if_rejects_infrastructure_and_protected_changes(self) -> None:
        cases = [
            (ENVIRONMENT, "Modify", "properties.publicNetworkAccess", "App-only preview"),
            (
                f"{RESOURCE_GROUP}/providers/Microsoft.Cdn/profiles/copyrit-test-afd",
                "Create",
                "sku",
                "App-only preview",
            ),
            (SUBNET, "Modify", "properties.addressPrefix", "protected-network change"),
        ]
        for resource_id, change_type, path, message in cases:
            with self.subTest(resource_id=resource_id):
                payload = {
                    "changes": [{"resourceId": resource_id, "changeType": change_type, "delta": [{"path": path}]}]
                }
                result = self._run(MOCK_WHAT_IF=json.dumps(payload))
                assert result.returncode != 0
                assert message in result.stdout, result.stdout + result.stderr
                assert not any(call[:3] == ["deployment", "group", "create"] for call in self.az_calls)
                assert not self.curl_calls

    def test_revision_and_current_ready_image_must_match(self) -> None:
        cases = [
            ({"MOCK_REVISION_IMAGE": PREVIOUS_IMAGE}, "requested image", False),
            ({"MOCK_HEALTH": "Unhealthy"}, "did not become healthy", False),
            ({"MOCK_FINAL_READY": "copyrit-test--old"}, "current ready revision", True),
            ({"MOCK_FINAL_LATEST": "copyrit-test--other"}, "current ready revision", True),
            ({"MOCK_FINAL_IMAGE": PREVIOUS_IMAGE}, "current ready revision", True),
            ({"MOCK_FINAL_ACCESS": "Disabled"}, "access mode changed", True),
        ]
        for overrides, message, probes_expected in cases:
            with self.subTest(overrides=overrides):
                result = self._run(**overrides)
                assert result.returncode != 0
                assert message in result.stdout, result.stdout + result.stderr
                assert bool(self.curl_calls) is probes_expected
                self._assert_app_only_writes()

    def test_private_http_failure_never_falls_back_or_rolls_back(self) -> None:
        self.fixtures["MOCK_ENVIRONMENT"]["publicNetworkAccess"] = "Disabled"
        for status, exit_code in (("302", "0"), ("504", "0"), ("200", "28")):
            with self.subTest(status=status, exit_code=exit_code):
                result = self._run(MOCK_FRONT_DOOR_COUNT="1", MOCK_HTTP_STATUS=status, MOCK_HTTP_EXIT=exit_code)
                assert result.returncode != 0
                assert "Application endpoint did not return a healthy response" in result.stdout
                assert "Deployment healthy:" not in result.stdout
                assert 1 <= len(self.curl_calls) <= 10
                assert all(call[-1] == f"https://{AFD_HOST}/api/health" for call in self.curl_calls)
                self._assert_app_only_writes()

    def test_cli_failures_do_not_report_success_or_roll_back(self) -> None:
        for command in (
            "resource show",
            "deployment group what-if",
            "deployment group create",
            "containerapp revision show",
        ):
            with self.subTest(command=command):
                result = self._run(MOCK_FAIL=command)
                assert result.returncode != 0
                assert "Deployment healthy:" not in result.stdout
                assert not self.curl_calls
                assert not any("rollback" in argument for call in self.az_calls for argument in call)
                assert not any(call[:2] == ["network", "private-endpoint-connection"] for call in self.az_calls)


if __name__ == "__main__":
    unittest.main()
