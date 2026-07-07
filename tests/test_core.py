"""Tests for the core sync module."""

import unittest
from typing import ClassVar
from unittest.mock import MagicMock, patch

from requests.exceptions import ConnectionError as RequestsConnectionError
from zabbix_utils import APIRequestError

from netbox_zabbix_sync.modules.core import Sync


class MockListObject:
    def __repr__(self):
        return str(self.object)

    def serialize(self):
        ret = {k: getattr(self, k) for k in ["object_id", "object_type"]}
        return ret

    def __getattr__(self, k):
        return getattr(self.object, k)

    def __iter__(self):
        for i in ["object_id", "object_type", "object"]:
            cur_attr = getattr(self, i)
            if isinstance(cur_attr, MockRecord):
                cur_attr = dict(cur_attr)
            yield i, cur_attr


class MockRecord:
    def __init__(self, values, api, endpoint):
        self.has_details = False
        self._full_cache = []
        self._init_cache = []
        self.api = api
        self.default_ret = MockRecord

    def __iter__(self):
        for i in dict(self._init_cache):
            cur_attr = getattr(self, i)
            if isinstance(cur_attr, MockRecord):
                yield i, dict(cur_attr)
            elif isinstance(cur_attr, list) and all(
                isinstance(i, (MockRecord, MockListObject)) for i in cur_attr
            ):
                yield i, [dict(x) for x in cur_attr]
            else:
                yield i, cur_attr

    def __getitem__(self, k):
        return dict(self)[k]

    def __str__(self):
        return (
            getattr(self, "name", None)
            or getattr(self, "label", None)
            or getattr(self, "display", None)
            or ""
        )

    def __repr__(self):
        return str(self)

    def __getstate__(self):
        return self.__dict__

    def __setstate__(self, d):
        self.__dict__.update(d)

    def __key__(self):
        if hasattr(self, "id"):
            return ("mock", self.id)
        else:
            return "mock"

    def __hash__(self):
        return hash(self.__key__())

    def __eq__(self, other):
        if isinstance(other, MockRecord):
            return self.__key__() == other.__key__()
        return NotImplemented


class MockNetboxDevice(MockRecord):
    """Mock NetBox device object."""

    def __init__(
        self,
        device_id=1,
        name="test-device",
        status_label="Active",
        zabbix_hostid=None,
        config_context=None,
        site=None,
        primary_ip=None,
        oob_ip=None,
        virtual_chassis=None,
        device_type=None,
        tenant=None,
        device_role=None,
        role=None,
        platform=None,
        serial="",
        tags=None,
    ):
        super().__init__(values={}, api=None, endpoint=None)
        self.id = device_id
        self.name = name
        self.status = MagicMock()
        self.status.label = status_label
        self.status.value = status_label.lower()
        self.custom_fields = {
            "zabbix_hostid": zabbix_hostid,
            "zabbix_oob_hostid": None,
            "zabbix_template": "TestTemplate",
        }
        self.config_context = config_context or {}
        self.tenant = tenant
        self.platform = platform
        self.serial = serial
        self.asset_tag = None
        self.location = None
        self.rack = None
        self.position = None
        self.face = None
        self.latitude = None
        self.longitude = None
        self.parent_device = None
        self.airflow = None
        self.cluster = None
        self.vc_position = None
        self.vc_priority = None
        self.description = ""
        self.comments = ""
        self.tags = tags or []
        self.oob_ip = None

        # Setup site with proper structure
        if site is None:
            self.site = MagicMock()
            self.site.name = "TestSite"
            self.site.slug = "testsite"
        else:
            self.site = site

        # Setup primary IP with proper structure
        if primary_ip is None:
            self.primary_ip = MagicMock()
            self.primary_ip.address = "192.168.1.1/24"
        else:
            self.primary_ip = primary_ip

        if oob_ip is not None:
            self.oob_ip = oob_ip

        self.primary_ip4 = self.primary_ip
        self.primary_ip6 = None

        # Setup device type with proper structure
        if device_type is None:
            self.device_type = MagicMock()
            self.device_type.custom_fields = {"zabbix_template": "TestTemplate"}
            self.device_type.manufacturer = MagicMock()
            self.device_type.manufacturer.name = "TestManufacturer"
            self.device_type.display = "Test Device Type"
            self.device_type.model = "Test Model"
            self.device_type.slug = "test-model"
        else:
            self.device_type = device_type

        if device_role is None and role is None:
            # Create default role
            mock_role = MagicMock()
            mock_role.name = "Switch"
            mock_role.slug = "switch"
            self.device_role = mock_role  # NetBox 2/3
            self.role = mock_role  # NetBox 4+
        else:
            self.device_role = device_role or role
            self.role = role or device_role

        self.virtual_chassis = virtual_chassis

    def save(self):
        """Mock save method for NetBox device."""


class MockNetboxVM(MockRecord):
    """Mock NetBox virtual machine object.

    Mirrors the real NetBox API response structure so the full VirtualMachine
    pipeline runs without mocking the class itself.
    """

    def __init__(
        self,
        vm_id=1,
        name="test-vm",
        status_label="Active",
        zabbix_hostid=None,
        config_context=None,
        site=None,
        primary_ip=None,
        role=None,
        cluster=None,
        tenant=None,
        platform=None,
        tags=None,
    ):
        super().__init__(values={}, api=None, endpoint=None)
        self.id = vm_id
        self.name = name
        self.status = MagicMock()
        self.status.label = status_label
        self.status.value = status_label.lower()
        self.custom_fields = {"zabbix_hostid": zabbix_hostid}
        # Default config_context includes a template so the VM is not skipped
        self.config_context = (
            config_context
            if config_context is not None
            else {"zabbix": {"templates": ["TestTemplate"]}}
        )
        self.tenant = tenant
        self.platform = platform
        self.serial = ""
        self.description = ""
        self.comments = ""
        self.vcpus = None
        self.memory = None
        self.disk = None
        self.virtual_chassis = None
        self.tags = tags or []
        self.oob_ip = None

        # Setup site
        if site is None:
            self.site = MagicMock()
            self.site.name = "TestSite"
            self.site.slug = "testsite"
            self.site.region = None
            self.site.group = None
        else:
            self.site = site

        # Setup primary IP
        if primary_ip is None:
            self.primary_ip = MagicMock()
            self.primary_ip.address = "192.168.1.1/24"
        else:
            self.primary_ip = primary_ip
        self.primary_ip4 = self.primary_ip
        self.primary_ip6 = None

        # Setup role
        if role is None:
            mock_role = MagicMock()
            mock_role.name = "Switch"
            mock_role.slug = "switch"
            self.role = mock_role
        else:
            self.role = role

        # Setup cluster
        if cluster is None:
            mock_cluster = MagicMock()
            mock_cluster.name = "TestCluster"
            mock_cluster_type = MagicMock()
            mock_cluster_type.name = "TestClusterType"
            mock_cluster.type = mock_cluster_type
            self.cluster = mock_cluster
        else:
            self.cluster = cluster

    def save(self):
        """Mock save method."""


class MockNetboxTenant(MockRecord):
    """Mock NetBox Tenant object."""

    def __init__(
        self,
        tenant_id=1,
        name="test-subscription",
        zabbix_hostid=None,
        subscription_id="sub-123",
        azure_tenant_id="tenant-123",
        group=None,
        custom_fields=None,
        tags=None,
    ):
        super().__init__(values={}, api=None, endpoint=None)
        self.id = tenant_id
        self.name = name
        self.tags = tags or []
        self.group = group
        if self.group is None and azure_tenant_id is not None:
            self.group = MagicMock()
            self.group.name = "tenant-management-group"
            self.group.custom_fields = {"azure_tenant_id": azure_tenant_id}
        self.custom_fields = (
            custom_fields
            if custom_fields is not None
            else {
                "zabbix_hostid": zabbix_hostid,
                "azure_subscription_id": subscription_id,
            }
        )

    def save(self):
        """Mock save method."""


class TestNetboxTokenHandling(unittest.TestCase):
    """Test that sync properly handles NetBox token authentication."""

    def test_v1_token_with_netbox_45(self):
        """Test that v1 token with NetBox 4.5+ logs warning but returns True."""
        syncer = Sync()

        with self.assertLogs("NetBox-Zabbix-sync", level="WARNING") as log_context:
            result = syncer._validate_netbox_token("token123", "4.5")

        self.assertTrue(result)
        self.assertTrue(
            any("v1 token format" in record.message for record in log_context.records)
        )

    def test_v2_token_with_netbox_35(self):
        """Test that v2 token with NetBox < 4.5 logs error and returns False."""
        syncer = Sync()

        with self.assertLogs("NetBox-Zabbix-sync", level="ERROR") as log_context:
            result = syncer._validate_netbox_token("nbt_key123.token123", "3.5")

        self.assertFalse(result)
        self.assertTrue(
            any(
                "v2 token format with Netbox version lower than 4.5" in record.message
                for record in log_context.records
            )
        )

    def test_v2_token_with_netbox_45(self):
        """Test that v2 token with NetBox 4.5+ logs debug and returns True."""
        syncer = Sync()

        with self.assertLogs("NetBox-Zabbix-sync", level="DEBUG") as log_context:
            result = syncer._validate_netbox_token("nbt_key123.token123", "4.5")

        self.assertTrue(result)
        self.assertTrue(
            any("v2 token format" in record.message for record in log_context.records)
        )

    def test_v1_token_with_netbox_35(self):
        """Test that v1 token with NetBox < 4.5 logs debug and returns True."""
        syncer = Sync()

        with self.assertLogs("NetBox-Zabbix-sync", level="DEBUG") as log_context:
            result = syncer._validate_netbox_token("token123", "3.5")

        self.assertTrue(result)
        self.assertTrue(
            any("v1 token format" in record.message for record in log_context.records)
        )


class TestSyncNetboxConnection(unittest.TestCase):
    """Test NetBox connection handling in sync function."""

    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_sync_error_on_netbox_connection_error(self, mock_api):
        """Test that sync returns False when NetBox connection fails."""
        mock_netbox = MagicMock()
        mock_api.return_value = mock_netbox
        # Simulate connection error when accessing version
        type(mock_netbox).version = property(
            lambda self: (_ for _ in ()).throw(RequestsConnectionError())
        )

        syncer = Sync()
        result = syncer.connect(
            nb_host="http://netbox.local",
            nb_token="token",
            zbx_host="http://zabbix.local",
            zbx_user="user",
            zbx_pass="pass",
            zbx_token=None,
        )

        self.assertFalse(result)


class TestZabbixUserTokenConflict(unittest.TestCase):
    """Test that sync returns False when both ZABBIX_USER/PASS and ZABBIX_TOKEN are set."""

    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_sync_error_on_user_token_conflict(self, mock_api):
        """Test that sync returns False when both user/pass and token are provided."""
        mock_netbox = MagicMock()
        mock_api.return_value = mock_netbox
        mock_netbox.version = "3.5"

        syncer = Sync()
        result = syncer.connect(
            nb_host="http://netbox.local",
            nb_token="token",
            zbx_host="http://zabbix.local",
            zbx_user="user",
            zbx_pass="pass",
            zbx_token="token",  # Both token and user/pass provided
        )

        self.assertFalse(result)


class TestSyncZabbixConnection(unittest.TestCase):
    """Test Zabbix connection handling in sync function."""

    def _setup_netbox_mock(self, mock_api):
        """Helper to setup a working NetBox mock."""
        mock_netbox = MagicMock()
        mock_api.return_value = mock_netbox
        mock_netbox.version = "3.5"
        return mock_netbox

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_sync_exits_on_zabbix_api_error(self, mock_api, mock_zabbix_api):
        """Test that sync exits when Zabbix API authentication fails."""
        # Simulate Netbox API
        self._setup_netbox_mock(mock_api)
        # Simulate Zabbix API error
        mock_zabbix_api.return_value.check_auth.side_effect = APIRequestError(
            "Invalid credentials"
        )
        # Start syncer and set connection details
        syncer = Sync()
        result = syncer.connect(
            nb_host="http://netbox.local",
            nb_token="token",
            zbx_host="http://zabbix.local",
            zbx_user="user",
            zbx_pass="pass",
            zbx_token=None,
        )
        # Should return False due to Zabbix API error
        self.assertFalse(result)
        result = syncer.connect(
            "http://netbox.local",
            "token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        # Validate that result is False due to Zabbix API error
        self.assertFalse(result)


class TestSyncZabbixAuthentication(unittest.TestCase):
    """Test Zabbix authentication methods."""

    def _setup_netbox_mock(self, mock_api):
        """Helper to setup a working NetBox mock."""
        mock_netbox = MagicMock()
        mock_api.return_value = mock_netbox
        mock_netbox.version = "3.5"
        mock_netbox.extras.custom_fields.filter.return_value = []
        mock_netbox.dcim.devices.filter.return_value = []
        mock_netbox.virtualization.virtual_machines.filter.return_value = []
        mock_netbox.dcim.site_groups.all.return_value = []
        mock_netbox.dcim.regions.all.return_value = []
        return mock_netbox

    def _setup_zabbix_mock(self, mock_zabbix_api, version="7.0"):
        """Helper to setup a working Zabbix mock."""
        mock_zabbix = MagicMock()
        mock_zabbix_api.return_value = mock_zabbix
        mock_zabbix.version = version
        mock_zabbix.hostgroup.get.return_value = []
        mock_zabbix.template.get.return_value = []
        mock_zabbix.proxy.get.return_value = []
        mock_zabbix.proxygroup.get.return_value = []
        return mock_zabbix

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_sync_uses_user_password_when_no_token(self, mock_api, mock_zabbix_api):
        """Test that sync uses user/password auth when no token is provided."""
        self._setup_netbox_mock(mock_api)

        syncer = Sync()
        syncer.connect(
            nb_host="http://netbox.local",
            nb_token="nb_token",
            zbx_host="http://zabbix.local",
            zbx_user="zbx_user",
            zbx_pass="zbx_pass",
        )

        # Verify ZabbixAPI was called with user/password and without token
        mock_zabbix_api.assert_called_once()
        call_kwargs = mock_zabbix_api.call_args.kwargs
        self.assertEqual(call_kwargs["user"], "zbx_user")
        self.assertEqual(call_kwargs["password"], "zbx_pass")
        self.assertNotIn("token", call_kwargs)

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_sync_uses_token_when_provided(self, mock_api, mock_zabbix_api):
        """Test that sync uses token auth when token is provided."""
        self._setup_netbox_mock(mock_api)
        self._setup_zabbix_mock(mock_zabbix_api)

        syncer = Sync()
        syncer.connect(
            nb_host="http://netbox.local",
            nb_token="nb_token",
            zbx_host="http://zabbix.local",
            zbx_token="zbx_token",
        )

        # Verify ZabbixAPI was called with token and without user/password
        mock_zabbix_api.assert_called_once()
        call_kwargs = mock_zabbix_api.call_args.kwargs
        self.assertEqual(call_kwargs["token"], "zbx_token")
        self.assertNotIn("user", call_kwargs)
        self.assertNotIn("password", call_kwargs)


class TestSyncDeviceProcessing(unittest.TestCase):
    """Test device processing in sync function."""

    def _setup_netbox_mock(self, mock_api, devices=None, vms=None):
        """Helper to setup a working NetBox mock."""
        mock_netbox = MagicMock()
        mock_api.return_value = mock_netbox
        mock_netbox.version = "3.5"
        mock_netbox.extras.custom_fields.filter.return_value = []
        mock_netbox.dcim.devices.filter.return_value = devices or []
        mock_netbox.virtualization.virtual_machines.filter.return_value = vms or []
        mock_netbox.dcim.site_groups.all.return_value = []
        mock_netbox.dcim.regions.all.return_value = []
        mock_netbox.extras.journal_entries = MagicMock()
        return mock_netbox

    def _setup_zabbix_mock(self, mock_zabbix_api, version="6.0"):
        """Helper to setup a working Zabbix mock."""
        mock_zabbix = MagicMock()
        mock_zabbix_api.return_value = mock_zabbix
        mock_zabbix.version = version
        mock_zabbix.hostgroup.get.return_value = [{"groupid": "1", "name": "TestGroup"}]
        mock_zabbix.template.get.return_value = [
            {"templateid": "1", "name": "TestTemplate"}
        ]
        mock_zabbix.proxy.get.return_value = []
        mock_zabbix.proxygroup.get.return_value = []
        return mock_zabbix

    @patch("netbox_zabbix_sync.modules.core.PhysicalDevice")
    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_sync_processes_devices_from_netbox(
        self, mock_api, mock_zabbix_api, mock_physical_device
    ):
        """Test that sync creates PhysicalDevice instances for NetBox devices."""
        device1 = MockNetboxDevice(device_id=1, name="device1")
        device2 = MockNetboxDevice(device_id=2, name="device2")

        self._setup_netbox_mock(mock_api, devices=[device1, device2])
        self._setup_zabbix_mock(mock_zabbix_api)

        # Mock PhysicalDevice to have no template (skip further processing)
        mock_device_instance = MagicMock()
        mock_device_instance.zbx_template_names = []
        mock_physical_device.return_value = mock_device_instance

        syncer = Sync()
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        # Verify PhysicalDevice was instantiated for each device
        self.assertEqual(mock_physical_device.call_count, 2)

    @patch("netbox_zabbix_sync.modules.core.VirtualMachine")
    @patch("netbox_zabbix_sync.modules.core.PhysicalDevice")
    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_sync_processes_vms_when_enabled(
        self, mock_api, mock_zabbix_api, mock_physical_device, mock_virtual_machine
    ):
        """Test that sync processes VMs when sync_vms is enabled."""
        vm1 = MockNetboxVM(vm_id=1, name="vm1")
        vm2 = MockNetboxVM(vm_id=2, name="vm2")

        self._setup_netbox_mock(mock_api, vms=[vm1, vm2])
        self._setup_zabbix_mock(mock_zabbix_api)

        # Mock VM to have no template (skip further processing)
        mock_vm_instance = MagicMock()
        mock_vm_instance.zbx_template_names = []
        mock_virtual_machine.return_value = mock_vm_instance

        syncer = Sync({"sync_vms": True})
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        # Verify VirtualMachine was instantiated for each VM
        self.assertEqual(mock_virtual_machine.call_count, 2)

    @patch("netbox_zabbix_sync.modules.core.VirtualMachine")
    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_sync_skips_vms_when_disabled(
        self, mock_api, mock_zabbix_api, mock_virtual_machine
    ):
        """Test that sync does NOT process VMs when sync_vms is disabled."""
        vm1 = MockNetboxVM(vm_id=1, name="vm1")

        self._setup_netbox_mock(mock_api, vms=[vm1])
        self._setup_zabbix_mock(mock_zabbix_api)

        syncer = Sync()
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        # Verify VirtualMachine was never called
        mock_virtual_machine.assert_not_called()


class TestSyncAzureSubscriptions(unittest.TestCase):
    """Test Azure subscription processing from NetBox Tenants."""

    def _setup_netbox_mock(self, mock_api, tenants=None):
        """Helper to setup a working NetBox mock."""
        mock_netbox = MagicMock()
        mock_api.return_value = mock_netbox
        mock_netbox.version = "3.5"
        mock_netbox.extras.custom_fields.filter.return_value = []
        mock_netbox.dcim.devices.filter.return_value = []
        mock_netbox.virtualization.virtual_machines.filter.return_value = []
        mock_netbox.dcim.site_groups.all.return_value = []
        mock_netbox.dcim.regions.all.return_value = []
        mock_netbox.extras.journal_entries = MagicMock()
        mock_netbox.tenancy.tenants.filter.return_value = tenants or []
        return mock_netbox

    def _setup_zabbix_mock(self, mock_zabbix_api, version="7.0"):
        """Helper to setup a working Zabbix mock."""
        mock_zabbix = MagicMock()
        mock_zabbix_api.return_value = mock_zabbix
        mock_zabbix.version = version
        mock_zabbix.hostgroup.get.return_value = []
        mock_zabbix.hostgroup.create.side_effect = [
            {"groupids": ["10"]},
            {"groupids": ["11"]},
        ]
        mock_zabbix.template.get.return_value = [
            {"templateid": "20", "name": "Azure by HTTP"}
        ]
        mock_zabbix.proxy.get.return_value = []
        mock_zabbix.proxygroup.get.return_value = []
        mock_zabbix.host.get.return_value = []
        mock_zabbix.host.create.return_value = {"hostids": ["30"]}
        return mock_zabbix

    def _sync_config(self, **overrides):
        config = {
            "sync_azure_subscriptions": True,
            "azure_app_id_vault": "secret/azure:app_id",
            "azure_password_vault": "secret/azure:password",
        }
        config.update(overrides)
        return config

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_sync_azure_subscriptions_disabled_does_not_query_tenants(
        self, mock_api, mock_zabbix_api
    ):
        """Tenants are not queried unless Azure subscription sync is enabled."""
        mock_netbox = self._setup_netbox_mock(mock_api)
        self._setup_zabbix_mock(mock_zabbix_api)

        syncer = Sync()
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_netbox.tenancy.tenants.filter.assert_not_called()

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_sync_azure_subscription_creates_zabbix_host(
        self, mock_api, mock_zabbix_api
    ):
        """A tagged Tenant creates an Azure by HTTP Zabbix host."""
        tenant = MockNetboxTenant(name="prod-subscription")
        self._setup_netbox_mock(mock_api, tenants=[tenant])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)

        syncer = Sync(self._sync_config())
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_zabbix.host.create.assert_called_once()
        create_kwargs = mock_zabbix.host.create.call_args.kwargs
        self.assertEqual(create_kwargs["host"], "prod-subscription")
        self.assertEqual(create_kwargs["interfaces"], [])
        self.assertEqual(create_kwargs["groups"], [{"groupid": "10"}])
        self.assertEqual(create_kwargs["templates"], [{"templateid": "20"}])
        self.assertEqual(
            create_kwargs["macros"],
            [
                {
                    "macro": "{$AZURE.APP.ID}",
                    "value": "secret/azure:app_id",
                    "type": "2",
                    "description": "",
                },
                {
                    "macro": "{$AZURE.PASSWORD}",
                    "value": "secret/azure:password",
                    "type": "2",
                    "description": "",
                },
                {
                    "macro": "{$AZURE.SUBSCRIPTION.ID}",
                    "value": "sub-123",
                    "type": "0",
                    "description": "",
                },
                {
                    "macro": "{$AZURE.TENANT.ID}",
                    "value": "tenant-123",
                    "type": "0",
                    "description": "",
                },
            ],
        )
        self.assertEqual(tenant.custom_fields["zabbix_hostid"], 30)
        mock_zabbix.hostgroup.create.assert_any_call(name="Azure/Subscriptions")
        mock_zabbix.hostgroup.create.assert_any_call(name="Azure")

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_sync_azure_subscription_updates_existing_macros(
        self, mock_api, mock_zabbix_api
    ):
        """A Tenant with a stored host ID reconciles the existing host."""
        tenant = MockNetboxTenant(zabbix_hostid=42)
        self._setup_netbox_mock(mock_api, tenants=[tenant])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)
        mock_zabbix.hostgroup.get.return_value = [
            {"groupid": "10", "name": "Azure/Subscriptions"}
        ]
        mock_zabbix.host.get.return_value = [
            {
                "hostid": "42",
                "host": "test-subscription",
                "name": "test-subscription",
                "parentTemplates": [{"templateid": "20"}],
                "hostgroups": [{"groupid": "10"}],
                "groups": [{"groupid": "10"}],
                "interfaces": [],
                "macros": [],
            }
        ]

        syncer = Sync(self._sync_config())
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_zabbix.host.create.assert_not_called()
        mock_zabbix.host.update.assert_called_once()
        update_kwargs = mock_zabbix.host.update.call_args.kwargs
        self.assertEqual(update_kwargs["hostid"], 42)
        self.assertEqual(
            [macro["macro"] for macro in update_kwargs["macros"]],
            [
                "{$AZURE.APP.ID}",
                "{$AZURE.PASSWORD}",
                "{$AZURE.SUBSCRIPTION.ID}",
                "{$AZURE.TENANT.ID}",
            ],
        )

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_sync_azure_subscription_skips_missing_required_data(
        self, mock_api, mock_zabbix_api
    ):
        """A Tenant missing required Azure data is skipped."""
        tenant = MockNetboxTenant(subscription_id=None, azure_tenant_id=None)
        self._setup_netbox_mock(mock_api, tenants=[tenant])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)

        syncer = Sync(self._sync_config(azure_password_vault=""))
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_zabbix.host.create.assert_not_called()
        self.assertIsNone(tenant.custom_fields["zabbix_hostid"])


class TestSyncZabbixVersionHandling(unittest.TestCase):
    """Test Zabbix version-specific handling."""

    def _setup_netbox_mock(self, mock_api):
        """Helper to setup a working NetBox mock."""
        mock_netbox = MagicMock()
        mock_api.return_value = mock_netbox
        mock_netbox.version = "3.5"
        mock_netbox.extras.custom_fields.filter.return_value = []
        mock_netbox.dcim.devices.filter.return_value = []
        mock_netbox.virtualization.virtual_machines.filter.return_value = []
        mock_netbox.dcim.site_groups.all.return_value = []
        mock_netbox.dcim.regions.all.return_value = []
        return mock_netbox

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_sync_uses_host_proxy_name_for_zabbix_6(self, mock_api, mock_zabbix_api):
        """Test that sync uses 'host' as proxy name field for Zabbix 6."""
        self._setup_netbox_mock(mock_api)

        mock_zabbix = MagicMock()
        mock_zabbix_api.return_value = mock_zabbix
        mock_zabbix.version = "6.0"
        mock_zabbix.hostgroup.get.return_value = []
        mock_zabbix.template.get.return_value = []
        mock_zabbix.proxy.get.return_value = [{"proxyid": "1", "host": "proxy1"}]

        syncer = Sync()
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        # Verify proxy.get was called with 'host' field
        mock_zabbix.proxy.get.assert_called_with(output=["proxyid", "host"])

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_sync_uses_name_proxy_field_for_zabbix_7(self, mock_api, mock_zabbix_api):
        """Test that sync uses 'name' as proxy name field for Zabbix 7."""
        self._setup_netbox_mock(mock_api)

        mock_zabbix = MagicMock()
        mock_zabbix_api.return_value = mock_zabbix
        mock_zabbix.version = "7.0"
        mock_zabbix.hostgroup.get.return_value = []
        mock_zabbix.template.get.return_value = []
        mock_zabbix.proxy.get.return_value = [{"proxyid": "1", "name": "proxy1"}]
        mock_zabbix.proxygroup.get.return_value = []

        syncer = Sync()
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        # Verify proxy.get was called with 'name' field
        mock_zabbix.proxy.get.assert_called_with(output=["proxyid", "name"])

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_sync_fetches_proxygroups_for_zabbix_7(self, mock_api, mock_zabbix_api):
        """Test that sync fetches proxy groups for Zabbix 7."""
        self._setup_netbox_mock(mock_api)

        mock_zabbix = MagicMock()
        mock_zabbix_api.return_value = mock_zabbix
        mock_zabbix.version = "7.0"
        mock_zabbix.hostgroup.get.return_value = []
        mock_zabbix.template.get.return_value = []
        mock_zabbix.proxy.get.return_value = []
        mock_zabbix.proxygroup.get.return_value = []

        syncer = Sync()
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        # Verify proxygroup.get was called for Zabbix 7
        mock_zabbix.proxygroup.get.assert_called_once()

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_sync_skips_proxygroups_for_zabbix_6(self, mock_api, mock_zabbix_api):
        """Test that sync does NOT fetch proxy groups for Zabbix 6."""
        self._setup_netbox_mock(mock_api)

        mock_zabbix = MagicMock()
        mock_zabbix_api.return_value = mock_zabbix
        mock_zabbix.version = "6.0"
        mock_zabbix.hostgroup.get.return_value = []
        mock_zabbix.template.get.return_value = []
        mock_zabbix.proxy.get.return_value = []

        syncer = Sync()
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        # Verify proxygroup.get was NOT called for Zabbix 6
        mock_zabbix.proxygroup.get.assert_not_called()


class TestSyncProxyNameSanitization(unittest.TestCase):
    """Test proxy name field sanitization for Zabbix 6."""

    def _setup_netbox_mock(self, mock_api):
        """Helper to setup a working NetBox mock."""
        mock_netbox = MagicMock()
        mock_api.return_value = mock_netbox
        mock_netbox.version = "3.5"
        mock_netbox.extras.custom_fields.filter.return_value = []
        mock_netbox.dcim.devices.filter.return_value = []
        mock_netbox.virtualization.virtual_machines.filter.return_value = []
        mock_netbox.dcim.site_groups.all.return_value = []
        mock_netbox.dcim.regions.all.return_value = []
        return mock_netbox

    @patch("netbox_zabbix_sync.modules.core.proxy_prepper")
    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_sync_renames_host_to_name_for_zabbix_6_proxies(
        self, mock_api, mock_zabbix_api, mock_proxy_prepper
    ):
        """Test that for Zabbix 6, proxy 'host' field is renamed to 'name'."""
        self._setup_netbox_mock(mock_api)

        mock_zabbix = MagicMock()
        mock_zabbix_api.return_value = mock_zabbix
        mock_zabbix.version = "6.0"
        mock_zabbix.hostgroup.get.return_value = []
        mock_zabbix.template.get.return_value = []
        # Zabbix 6 returns 'host' field
        mock_zabbix.proxy.get.return_value = [
            {"proxyid": "1", "host": "proxy1"},
            {"proxyid": "2", "host": "proxy2"},
        ]
        mock_proxy_prepper.return_value = []

        syncer = Sync()
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        # Verify proxy_prepper was called with sanitized proxy list
        call_args = mock_proxy_prepper.call_args[0]
        proxies = call_args[0]
        # Check that 'host' was renamed to 'name'
        for proxy in proxies:
            self.assertIn("name", proxy)
            self.assertNotIn("host", proxy)


class TestDeviceHandeling(unittest.TestCase):
    """
    Tests several devices which can be synced to Zabbix.
    This class contains a lot of data in order to validate proper handling of different device types and configurations.
    """

    def _setup_netbox_mock(self, mock_api):
        """Helper to setup a working NetBox mock."""
        mock_netbox = MagicMock()
        mock_api.return_value = mock_netbox
        mock_netbox.version = "3.5"
        mock_netbox.extras.custom_fields.filter.return_value = []
        mock_netbox.dcim.devices.filter.return_value = []
        mock_netbox.virtualization.virtual_machines.filter.return_value = []
        mock_netbox.dcim.site_groups.all.return_value = []
        mock_netbox.dcim.regions.all.return_value = []
        return mock_netbox

    def _setup_zabbix_mock(self, mock_zabbix_api, version=7.0):
        """Helper to setup a working Zabbix mock."""
        mock_zabbix = MagicMock()
        mock_zabbix_api.return_value = mock_zabbix
        mock_zabbix.version = version
        mock_zabbix.hostgroup.get.return_value = [{"groupid": "1", "name": "TestGroup"}]
        mock_zabbix.template.get.return_value = [
            {"templateid": "1", "name": "TestTemplate"}
        ]
        mock_zabbix.proxy.get.return_value = []
        mock_zabbix.proxygroup.get.return_value = []
        # Mock host.get to return empty (host doesn't exist yet)
        mock_zabbix.host.get.return_value = []
        # Mock host.create to return success
        mock_zabbix.host.create.return_value = {"hostids": ["1"]}
        return mock_zabbix

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_sync_cluster_where_device_is_primary(self, mock_api, mock_zabbix_api):
        """Test that sync properly handles a device that is the primary in a virtual chassis."""
        # Create a device that is part of a virtual chassis and is the primary
        # Setup virtual chassis mock
        vc_master = MagicMock()
        vc_master.id = 1  # Same as device ID - device is primary

        virtual_chassis = MagicMock()
        virtual_chassis.master = vc_master
        virtual_chassis.name = "SW01"

        device = MockNetboxDevice(
            device_id=1,
            name="SW01N0",
            virtual_chassis=virtual_chassis,
        )

        # Setup NetBox mock with a site for hostgroup
        mock_netbox = self._setup_netbox_mock(mock_api)
        mock_netbox.dcim.devices.filter.return_value = [device]

        # Create a mock site for hostgroup generation
        mock_site = MagicMock()
        mock_site.name = "TestSite"
        device.site = mock_site

        # Setup Zabbix mock
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)

        # Run the sync with clustering enabled
        syncer = Sync({"clustering": True})
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        # Verify that host.create was called with the cluster name "SW01", not "SW01N0"
        mock_zabbix.host.create.assert_called_once()
        create_call_kwargs = mock_zabbix.host.create.call_args.kwargs

        # The host should be created with the virtual chassis name, not the device name
        self.assertEqual(create_call_kwargs["host"], "SW01")

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_sync_cluster_where_device_is_not_primary(self, mock_api, mock_zabbix_api):
        """Test that a non-primary cluster member is skipped and not created in Zabbix."""
        # vc_master.id (2) differs from device.id (1) → device is secondary
        vc_master = MagicMock()
        vc_master.id = 2  # Different from device ID → device is NOT primary

        virtual_chassis = MagicMock()
        virtual_chassis.master = vc_master
        virtual_chassis.name = "SW01"

        device = MockNetboxDevice(
            device_id=1,
            name="SW01N1",
            virtual_chassis=virtual_chassis,
        )

        mock_netbox = self._setup_netbox_mock(mock_api)
        mock_netbox.dcim.devices.filter.return_value = [device]

        mock_site = MagicMock()
        mock_site.name = "TestSite"
        device.site = mock_site

        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)

        syncer = Sync({"clustering": True})
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        # Secondary cluster member must be skipped — no host should be created
        mock_zabbix.host.create.assert_not_called()

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_templates_from_config_context(self, mock_api, mock_zabbix_api):
        """Test that templates_config_context=True uses the config context template."""
        device = MockNetboxDevice(
            device_id=1,
            name="Router01",
            config_context={
                "zabbix": {
                    "templates": ["ContextTemplate"],
                }
            },
        )

        mock_netbox = self._setup_netbox_mock(mock_api)
        mock_netbox.dcim.devices.filter.return_value = [device]

        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)
        # Both templates exist in Zabbix
        mock_zabbix.template.get.return_value = [
            {"templateid": "1", "name": "TestTemplate"},
            {"templateid": "2", "name": "ContextTemplate"},
        ]

        syncer = Sync({"templates_config_context": True})
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        # Verify host was created with the config context template, not the custom field one
        mock_zabbix.host.create.assert_called_once()
        create_call_kwargs = mock_zabbix.host.create.call_args.kwargs
        self.assertEqual(create_call_kwargs["templates"], [{"templateid": "2"}])

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_templates_config_context_overrule(self, mock_api, mock_zabbix_api):
        """Test that templates_config_context_overrule=True prefers config context over custom field.

        The device has:
          - Custom field template (device type): "TestTemplate"
          - Config context template (device):    "ContextTemplate"

        With overrule enabled the config context should win and the host should
        be created with "ContextTemplate" only.
        """
        device = MockNetboxDevice(
            device_id=1,
            name="Router01",
            config_context={
                "zabbix": {
                    "templates": ["ContextTemplate"],
                }
            },
        )

        mock_netbox = self._setup_netbox_mock(mock_api)
        mock_netbox.dcim.devices.filter.return_value = [device]

        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)
        # Both templates exist in Zabbix
        mock_zabbix.template.get.return_value = [
            {"templateid": "1", "name": "TestTemplate"},
            {"templateid": "2", "name": "ContextTemplate"},
        ]

        syncer = Sync({"templates_config_context_overrule": True})
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        # Config context overrides the custom field - only "ContextTemplate" should be used
        mock_zabbix.host.create.assert_called_once()
        create_call_kwargs = mock_zabbix.host.create.call_args.kwargs
        self.assertEqual(create_call_kwargs["templates"], [{"templateid": "2"}])
        # Verify the custom field template was NOT used
        self.assertNotIn({"templateid": "1"}, create_call_kwargs["templates"])

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_config_context_oob_creates_primary_and_oob(
        self, mock_api, mock_zabbix_api
    ):
        """A zabbix.oob config creates a separate host for oob_ip."""
        oob_ip = MagicMock()
        oob_ip.address = "10.0.0.10/24"
        device = MockNetboxDevice(
            device_id=1,
            name="vmhost1",
            oob_ip=oob_ip,
            config_context={
                "zabbix": {
                    "templates": ["PrimaryTemplate"],
                    "interface_type": 1,
                    "oob": {
                        "name_prefix": "drac-",
                        "templates": ["OobTemplate"],
                        "interface_type": 2,
                        "snmp": {"version": 2, "community": "public"},
                    },
                }
            },
        )

        mock_netbox = self._setup_netbox_mock(mock_api)
        mock_netbox.dcim.devices.filter.return_value = [device]

        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)
        mock_zabbix.template.get.return_value = [
            {"templateid": "1", "name": "PrimaryTemplate"},
            {"templateid": "2", "name": "OobTemplate"},
        ]

        syncer = Sync()
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        self.assertEqual(mock_zabbix.host.create.call_count, 2)
        create_calls = mock_zabbix.host.create.call_args_list
        self.assertEqual(create_calls[0].kwargs["host"], "vmhost1")
        self.assertEqual(create_calls[0].kwargs["interfaces"][0]["ip"], "192.168.1.1")
        self.assertEqual(create_calls[0].kwargs["templates"], [{"templateid": "1"}])
        self.assertEqual(create_calls[0].kwargs["interfaces"][0]["type"], 1)
        self.assertEqual(create_calls[1].kwargs["host"], "drac-vmhost1")
        self.assertEqual(create_calls[1].kwargs["interfaces"][0]["ip"], "10.0.0.10")
        self.assertEqual(create_calls[1].kwargs["templates"], [{"templateid": "2"}])
        self.assertEqual(create_calls[1].kwargs["interfaces"][0]["type"], 2)
        self.assertEqual(
            create_calls[1].kwargs["interfaces"][0]["details"]["community"], "public"
        )
        self.assertEqual(device.custom_fields["zabbix_hostid"], 1)
        self.assertEqual(device.custom_fields["zabbix_oob_hostid"], 1)

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_config_context_oob_skip_missing_oob_ip(self, mock_api, mock_zabbix_api):
        """A missing oob_ip skips only the OOB import."""
        device = MockNetboxDevice(
            device_id=1,
            name="Router01",
            config_context={
                "zabbix": {
                    "templates": ["PrimaryTemplate"],
                    "oob": {"templates": ["OobTemplate"]},
                }
            },
        )

        mock_netbox = self._setup_netbox_mock(mock_api)
        mock_netbox.dcim.devices.filter.return_value = [device]

        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)
        mock_zabbix.template.get.return_value = [
            {"templateid": "1", "name": "PrimaryTemplate"},
            {"templateid": "2", "name": "OobTemplate"},
        ]

        syncer = Sync()
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_zabbix.host.create.assert_called_once()
        self.assertEqual(mock_zabbix.host.create.call_args.kwargs["host"], "Router01")

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_config_context_oob_default_suffix(self, mock_api, mock_zabbix_api):
        """An OOB import defaults to a -oob hostname suffix."""
        oob_ip = MagicMock()
        oob_ip.address = "10.0.0.10/24"
        device = MockNetboxDevice(
            device_id=1,
            name="Router01",
            oob_ip=oob_ip,
            config_context={
                "zabbix": {
                    "templates": ["PrimaryTemplate"],
                    "oob": {"templates": ["OobTemplate"]},
                }
            },
        )

        mock_netbox = self._setup_netbox_mock(mock_api)
        mock_netbox.dcim.devices.filter.return_value = [device]

        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)
        mock_zabbix.template.get.return_value = [
            {"templateid": "1", "name": "PrimaryTemplate"},
            {"templateid": "2", "name": "OobTemplate"},
        ]

        syncer = Sync()
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        self.assertEqual(mock_zabbix.host.create.call_count, 2)
        self.assertEqual(
            mock_zabbix.host.create.call_args_list[1].kwargs["host"], "Router01-oob"
        )


class TestDeviceStatusHandling(unittest.TestCase):
    """
    Tests device status handling during NetBox to Zabbix synchronization.

    Validates the correct sync behavior for various combinations of NetBox device
    status, Zabbix host state, and the 'zabbix_device_removal' / 'zabbix_device_disable'
    configuration settings.

    Scenarios:
      1. Active, not in Zabbix          → created enabled
      2. Active, already in Zabbix      → consistency check passes, no update
      3. Staged, not in Zabbix          → created disabled
      4. Staged, already in Zabbix      → consistency check passes, no update
      5. Decommissioning, not in Zabbix → skipped entirely
      6. Decommissioning, in Zabbix     → host deleted from Zabbix (cleanup)
      7. Active, in Zabbix but disabled → host re-enabled via consistency check
      8. Failed, in Zabbix but enabled  → host disabled via consistency check
    """

    # Hostgroup produced by the default "site/manufacturer/role" format
    # for the default MockNetboxDevice attributes.
    EXPECTED_HOSTGROUP = "TestSite/TestManufacturer/Switch"

    def _setup_netbox_mock(self, mock_api, devices=None):
        """Helper to setup a working NetBox mock."""
        mock_netbox = MagicMock()
        mock_api.return_value = mock_netbox
        mock_netbox.version = "3.5"
        mock_netbox.extras.custom_fields.filter.return_value = []
        mock_netbox.dcim.devices.filter.return_value = devices or []
        mock_netbox.virtualization.virtual_machines.filter.return_value = []
        mock_netbox.dcim.site_groups.all.return_value = []
        mock_netbox.dcim.regions.all.return_value = []
        mock_netbox.extras.journal_entries = MagicMock()
        return mock_netbox

    def _setup_zabbix_mock(self, mock_zabbix_api, version=7.0):
        """Helper to setup a working Zabbix mock."""
        mock_zabbix = MagicMock()
        mock_zabbix_api.return_value = mock_zabbix
        mock_zabbix.version = version
        mock_zabbix.hostgroup.get.return_value = [
            {"groupid": "1", "name": self.EXPECTED_HOSTGROUP}
        ]
        mock_zabbix.hostgroup.create.return_value = {"groupids": ["2"]}
        mock_zabbix.template.get.return_value = [
            {"templateid": "1", "name": "TestTemplate"}
        ]
        mock_zabbix.proxy.get.return_value = []
        mock_zabbix.proxygroup.get.return_value = []
        mock_zabbix.host.get.return_value = []
        mock_zabbix.host.create.return_value = {"hostids": ["1"]}
        mock_zabbix.host.update.return_value = {"hostids": ["42"]}
        mock_zabbix.host.delete.return_value = [42]
        return mock_zabbix

    def _make_zabbix_host(self, hostname="test-device", status="0"):
        """Build a minimal but complete Zabbix host response for consistency_check."""
        return [
            {
                "hostid": "42",
                "host": hostname,
                "name": hostname,
                "parentTemplates": [{"templateid": "1"}],
                "hostgroups": [{"groupid": "1"}],
                "groups": [{"groupid": "1"}],
                "status": status,
                # Single empty-dict interface: len==1 avoids SyncInventoryError,
                # empty keys prevent any spurious interface-update calls.
                "interfaces": [{}],
                "inventory_mode": "-1",
                "inventory": {},
                "macros": [],
                "tags": [],
                "proxy_hostid": "0",
                "proxyid": "0",
                "proxy_groupid": "0",
            }
        ]

    # ------------------------------------------------------------------
    # Scenario 1: Active device, not yet in Zabbix → created enabled (status=0)
    # ------------------------------------------------------------------
    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_active_device_not_in_zabbix_is_created(self, mock_api, mock_zabbix_api):
        """Active device not yet synced to Zabbix should be created with status enabled (0)."""
        device = MockNetboxDevice(
            name="test-device", status_label="Active", zabbix_hostid=None
        )
        self._setup_netbox_mock(mock_api, devices=[device])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)

        syncer = Sync()
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_zabbix.host.create.assert_called_once()
        create_kwargs = mock_zabbix.host.create.call_args.kwargs
        self.assertEqual(create_kwargs["host"], "test-device")
        self.assertEqual(create_kwargs["status"], 0)

    # ------------------------------------------------------------------
    # Scenario 2: Active device, already in Zabbix → consistency check,
    #             Zabbix status matches → no updates
    # ------------------------------------------------------------------
    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_active_device_in_zabbix_is_consistent(self, mock_api, mock_zabbix_api):
        """Active device already in Zabbix with matching status should require no updates."""
        device = MockNetboxDevice(
            name="test-device", status_label="Active", zabbix_hostid=42
        )
        self._setup_netbox_mock(mock_api, devices=[device])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)
        mock_zabbix.host.get.return_value = self._make_zabbix_host(status="0")

        syncer = Sync()
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_zabbix.host.create.assert_not_called()
        mock_zabbix.host.update.assert_not_called()

    # ------------------------------------------------------------------
    # Scenario 3: Staged device, not yet in Zabbix → created disabled (status=1)
    # ------------------------------------------------------------------
    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_staged_device_not_in_zabbix_is_created_disabled(
        self, mock_api, mock_zabbix_api
    ):
        """Staged device not yet in Zabbix should be created with status disabled (1)."""
        device = MockNetboxDevice(
            name="test-device", status_label="Staged", zabbix_hostid=None
        )
        self._setup_netbox_mock(mock_api, devices=[device])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)

        syncer = Sync()
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_zabbix.host.create.assert_called_once()
        create_kwargs = mock_zabbix.host.create.call_args.kwargs
        self.assertEqual(create_kwargs["status"], 1)

    # ------------------------------------------------------------------
    # Scenario 4: Staged device, already in Zabbix as disabled → no update needed
    # ------------------------------------------------------------------
    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_staged_device_in_zabbix_is_consistent(self, mock_api, mock_zabbix_api):
        """Staged device already in Zabbix as disabled should pass consistency check with no updates."""
        device = MockNetboxDevice(
            name="test-device", status_label="Staged", zabbix_hostid=42
        )
        self._setup_netbox_mock(mock_api, devices=[device])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)
        mock_zabbix.host.get.return_value = self._make_zabbix_host(status="1")

        syncer = Sync()
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_zabbix.host.create.assert_not_called()
        mock_zabbix.host.update.assert_not_called()

    # ------------------------------------------------------------------
    # Scenario 5: Decommissioning device, not in Zabbix → skipped (no create, no delete)
    # ------------------------------------------------------------------
    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_decommissioning_device_not_in_zabbix_is_skipped(
        self, mock_api, mock_zabbix_api
    ):
        """Decommissioning device with no Zabbix ID should be skipped entirely."""
        device = MockNetboxDevice(
            name="test-device", status_label="Decommissioning", zabbix_hostid=None
        )
        self._setup_netbox_mock(mock_api, devices=[device])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)

        syncer = Sync()
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_zabbix.host.create.assert_not_called()
        mock_zabbix.host.delete.assert_not_called()

    # ------------------------------------------------------------------
    # Scenario 6: Decommissioning device, already in Zabbix → cleanup (host deleted)
    # ------------------------------------------------------------------
    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_decommissioning_device_in_zabbix_is_deleted(
        self, mock_api, mock_zabbix_api
    ):
        """Decommissioning device with a Zabbix ID should be deleted from Zabbix."""
        device = MockNetboxDevice(
            name="test-device", status_label="Decommissioning", zabbix_hostid=42
        )
        self._setup_netbox_mock(mock_api, devices=[device])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)
        # Zabbix still has the host → it should be deleted
        mock_zabbix.host.get.return_value = [{"hostid": "42"}]

        syncer = Sync()
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_zabbix.host.delete.assert_called_once_with(42)

    # ------------------------------------------------------------------
    # Scenario 7: Active device, Zabbix host is disabled → re-enable via consistency check
    # ------------------------------------------------------------------
    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_active_device_disabled_in_zabbix_is_enabled(
        self, mock_api, mock_zabbix_api
    ):
        """Active device whose Zabbix host is disabled should be re-enabled by consistency check."""
        device = MockNetboxDevice(
            name="test-device", status_label="Active", zabbix_hostid=42
        )
        self._setup_netbox_mock(mock_api, devices=[device])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)
        # Zabbix host currently disabled; device is Active → status out-of-sync
        mock_zabbix.host.get.return_value = self._make_zabbix_host(status="1")

        syncer = Sync()
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_zabbix.host.update.assert_called_once_with(hostid=42, status="0")

    # ------------------------------------------------------------------
    # Scenario 8: Failed device, Zabbix host is enabled → disable via consistency check
    # ------------------------------------------------------------------
    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_failed_device_enabled_in_zabbix_is_disabled(
        self, mock_api, mock_zabbix_api
    ):
        """Failed device whose Zabbix host is enabled should be disabled by consistency check."""
        device = MockNetboxDevice(
            name="test-device", status_label="Failed", zabbix_hostid=42
        )
        self._setup_netbox_mock(mock_api, devices=[device])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)
        # Zabbix host currently enabled; device is Failed → status out-of-sync
        mock_zabbix.host.get.return_value = self._make_zabbix_host(status="0")

        syncer = Sync()
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_zabbix.host.update.assert_called_once_with(hostid=42, status="1")


class TestVMStatusHandling(unittest.TestCase):
    """
    Mirrors TestDeviceStatusHandling for VirtualMachine objects.

    Validates the VM sync loop in core.py using real VirtualMachine instances
    (not mocked) for the same 8 status scenarios.
    """

    # Hostgroup produced by vm_hostgroup_format "site/role" with default MockNetboxVM values.
    EXPECTED_HOSTGROUP = "TestSite/Switch"

    def _setup_netbox_mock(self, mock_api, vms=None):
        """Helper to setup a working NetBox mock."""
        mock_netbox = MagicMock()
        mock_api.return_value = mock_netbox
        mock_netbox.version = "3.5"
        mock_netbox.extras.custom_fields.filter.return_value = []
        mock_netbox.dcim.devices.filter.return_value = []
        mock_netbox.virtualization.virtual_machines.filter.return_value = vms or []
        mock_netbox.dcim.site_groups.all.return_value = []
        mock_netbox.dcim.regions.all.return_value = []
        mock_netbox.extras.journal_entries = MagicMock()
        return mock_netbox

    def _setup_zabbix_mock(self, mock_zabbix_api, version=7.0):
        """Helper to setup a working Zabbix mock."""
        mock_zabbix = MagicMock()
        mock_zabbix_api.return_value = mock_zabbix
        mock_zabbix.version = version
        mock_zabbix.hostgroup.get.return_value = [
            {"groupid": "1", "name": self.EXPECTED_HOSTGROUP}
        ]
        mock_zabbix.hostgroup.create.return_value = {"groupids": ["2"]}
        mock_zabbix.template.get.return_value = [
            {"templateid": "1", "name": "TestTemplate"}
        ]
        mock_zabbix.proxy.get.return_value = []
        mock_zabbix.proxygroup.get.return_value = []
        mock_zabbix.host.get.return_value = []
        mock_zabbix.host.create.return_value = {"hostids": ["1"]}
        mock_zabbix.host.update.return_value = {"hostids": ["42"]}
        mock_zabbix.host.delete.return_value = [42]
        return mock_zabbix

    def _make_zabbix_host(self, hostname="test-vm", status="0"):
        """Build a minimal Zabbix host response for consistency_check."""
        return [
            {
                "hostid": "42",
                "host": hostname,
                "name": hostname,
                "parentTemplates": [{"templateid": "1"}],
                "hostgroups": [{"groupid": "1"}],
                "groups": [{"groupid": "1"}],
                "status": status,
                # Single empty-dict interface: len==1 avoids SyncInventoryError,
                # empty keys mean no spurious interface-update calls.
                "interfaces": [{}],
                "inventory_mode": "-1",
                "inventory": {},
                "macros": [],
                "tags": [],
                "proxy_hostid": "0",
                "proxyid": "0",
                "proxy_groupid": "0",
            }
        ]

    # Simple Sync config that enables VM sync with a flat hostgroup format
    _SYNC_CFG: ClassVar[dict] = {"sync_vms": True, "vm_hostgroup_format": "site/role"}

    # ------------------------------------------------------------------
    # Scenario 1: Active VM, not yet in Zabbix → created enabled (status=0)
    # ------------------------------------------------------------------
    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_active_vm_not_in_zabbix_is_created(self, mock_api, mock_zabbix_api):
        """Active VM not yet synced to Zabbix should be created with status enabled (0)."""
        vm = MockNetboxVM(name="test-vm", status_label="Active", zabbix_hostid=None)
        self._setup_netbox_mock(mock_api, vms=[vm])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)

        syncer = Sync(self._SYNC_CFG)
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_zabbix.host.create.assert_called_once()
        create_kwargs = mock_zabbix.host.create.call_args.kwargs
        self.assertEqual(create_kwargs["host"], "test-vm")
        self.assertEqual(create_kwargs["status"], 0)

    # ------------------------------------------------------------------
    # Scenario 2: Active VM, already in Zabbix → consistency check, no update
    # ------------------------------------------------------------------
    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_active_vm_in_zabbix_is_consistent(self, mock_api, mock_zabbix_api):
        """Active VM already in Zabbix with matching status should require no updates."""
        vm = MockNetboxVM(name="test-vm", status_label="Active", zabbix_hostid=42)
        self._setup_netbox_mock(mock_api, vms=[vm])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)
        mock_zabbix.host.get.return_value = self._make_zabbix_host(status="0")

        syncer = Sync(self._SYNC_CFG)
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_zabbix.host.create.assert_not_called()
        mock_zabbix.host.update.assert_not_called()

    # ------------------------------------------------------------------
    # Scenario 3: Staged VM, not yet in Zabbix → created disabled (status=1)
    # ------------------------------------------------------------------
    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_staged_vm_not_in_zabbix_is_created_disabled(
        self, mock_api, mock_zabbix_api
    ):
        """Staged VM not yet in Zabbix should be created with status disabled (1)."""
        vm = MockNetboxVM(name="test-vm", status_label="Staged", zabbix_hostid=None)
        self._setup_netbox_mock(mock_api, vms=[vm])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)

        syncer = Sync(self._SYNC_CFG)
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_zabbix.host.create.assert_called_once()
        create_kwargs = mock_zabbix.host.create.call_args.kwargs
        self.assertEqual(create_kwargs["status"], 1)

    # ------------------------------------------------------------------
    # Scenario 4: Staged VM, already in Zabbix as disabled → no update needed
    # ------------------------------------------------------------------
    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_staged_vm_in_zabbix_is_consistent(self, mock_api, mock_zabbix_api):
        """Staged VM already in Zabbix as disabled should pass consistency check with no updates."""
        vm = MockNetboxVM(name="test-vm", status_label="Staged", zabbix_hostid=42)
        self._setup_netbox_mock(mock_api, vms=[vm])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)
        mock_zabbix.host.get.return_value = self._make_zabbix_host(status="1")

        syncer = Sync(self._SYNC_CFG)
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_zabbix.host.create.assert_not_called()
        mock_zabbix.host.update.assert_not_called()

    # ------------------------------------------------------------------
    # Scenario 5: Decommissioning VM, not in Zabbix → skipped (no create, no delete)
    # ------------------------------------------------------------------
    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_decommissioning_vm_not_in_zabbix_is_skipped(
        self, mock_api, mock_zabbix_api
    ):
        """Decommissioning VM with no Zabbix ID should be skipped entirely."""
        vm = MockNetboxVM(
            name="test-vm", status_label="Decommissioning", zabbix_hostid=None
        )
        self._setup_netbox_mock(mock_api, vms=[vm])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)

        syncer = Sync(self._SYNC_CFG)
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_zabbix.host.create.assert_not_called()
        mock_zabbix.host.delete.assert_not_called()

    # ------------------------------------------------------------------
    # Scenario 6: Decommissioning VM, already in Zabbix → cleanup (host deleted)
    # ------------------------------------------------------------------
    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_decommissioning_vm_in_zabbix_is_deleted(self, mock_api, mock_zabbix_api):
        """Decommissioning VM with a Zabbix ID should be deleted from Zabbix."""
        vm = MockNetboxVM(
            name="test-vm", status_label="Decommissioning", zabbix_hostid=42
        )
        self._setup_netbox_mock(mock_api, vms=[vm])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)
        mock_zabbix.host.get.return_value = [{"hostid": "42"}]

        syncer = Sync(self._SYNC_CFG)
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_zabbix.host.delete.assert_called_once_with(42)

    # ------------------------------------------------------------------
    # Scenario 7: Active VM, Zabbix host is disabled → re-enable via consistency check
    # ------------------------------------------------------------------
    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_active_vm_disabled_in_zabbix_is_enabled(self, mock_api, mock_zabbix_api):
        """Active VM whose Zabbix host is disabled should be re-enabled by consistency check."""
        vm = MockNetboxVM(name="test-vm", status_label="Active", zabbix_hostid=42)
        self._setup_netbox_mock(mock_api, vms=[vm])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)
        mock_zabbix.host.get.return_value = self._make_zabbix_host(status="1")

        syncer = Sync(self._SYNC_CFG)
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_zabbix.host.update.assert_called_once_with(hostid=42, status="0")

    # ------------------------------------------------------------------
    # Scenario 8: Failed VM, Zabbix host is enabled → disable via consistency check
    # ------------------------------------------------------------------
    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_failed_vm_enabled_in_zabbix_is_disabled(self, mock_api, mock_zabbix_api):
        """Failed VM whose Zabbix host is enabled should be disabled by consistency check."""
        vm = MockNetboxVM(name="test-vm", status_label="Failed", zabbix_hostid=42)
        self._setup_netbox_mock(mock_api, vms=[vm])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)
        mock_zabbix.host.get.return_value = self._make_zabbix_host(status="0")

        syncer = Sync(self._SYNC_CFG)
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_zabbix.host.update.assert_called_once_with(hostid=42, status="1")


class TestCombineFilters(unittest.TestCase):
    """Test the _combine_filters method and filter override behavior in start()."""

    def _setup_netbox_mock(self, mock_api, devices=None, vms=None):
        """Helper to setup a working NetBox mock."""
        mock_netbox = MagicMock()
        mock_api.return_value = mock_netbox
        mock_netbox.version = "3.5"
        mock_netbox.extras.custom_fields.filter.return_value = []
        mock_netbox.dcim.devices.filter.return_value = devices or []
        mock_netbox.virtualization.virtual_machines.filter.return_value = vms or []
        mock_netbox.dcim.site_groups.all.return_value = []
        mock_netbox.dcim.regions.all.return_value = []
        mock_netbox.extras.journal_entries = MagicMock()
        return mock_netbox

    def _setup_zabbix_mock(self, mock_zabbix_api, version="7.0"):
        """Helper to setup a working Zabbix mock."""
        mock_zabbix = MagicMock()
        mock_zabbix_api.return_value = mock_zabbix
        # Set version as float to match expected type in device.py comparisons
        mock_zabbix.version = float(version)
        mock_zabbix.hostgroup.get.return_value = [{"groupid": "1", "name": "TestGroup"}]
        mock_zabbix.template.get.return_value = [
            {"templateid": "1", "name": "TestTemplate"}
        ]
        mock_zabbix.proxy.get.return_value = []
        mock_zabbix.proxygroup.get.return_value = []
        mock_zabbix.host.get.return_value = []
        mock_zabbix.host.create.return_value = {"hostids": ["1"]}
        return mock_zabbix

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_filter_override_with_name_parameter(self, mock_api, mock_zabbix_api):
        """Test that method filter parameter overrides config filter for name.

        Scenario:
        - Config has nb_device_filter with name="SW01N0"
        - start() is called with device_filter {"name": "Testdev02"}
        - Only the device matching "Testdev02" should be processed
        """
        # Create two mock devices
        device_matching_method_filter = MockNetboxDevice(
            device_id=1, name="Testdev02", status_label="Active"
        )
        device_matching_config_filter = MockNetboxDevice(
            device_id=2, name="SW01N0", status_label="Active"
        )

        # Setup mocks - the filter should be called with the combined/overridden filter
        self._setup_netbox_mock(
            mock_api,
            devices=[
                device_matching_method_filter,
                device_matching_config_filter,
            ],
        )
        self._setup_zabbix_mock(mock_zabbix_api)

        # Create sync with config filter specifying one name
        syncer = Sync({"nb_device_filter": {"name": "SW01N0"}})
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )

        # Call start with method filter specifying a different name
        # The method filter should override the config filter
        syncer.start(device_filter={"name": "Testdev02"})

        # Verify that nbapi.dcim.devices.filter was called with the override filter
        mock_netbox = mock_api.return_value
        filter_call_kwargs = mock_netbox.dcim.devices.filter.call_args[1]
        self.assertEqual(filter_call_kwargs.get("name"), "Testdev02")

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_filter_override_site_parameter(self, mock_api, mock_zabbix_api):
        """Test that site filter override works correctly.

        Scenario:
        - Config has no device filter set
        - start() is called with device_filter {"site": "fra01"}
        - Only devices in fra01 site should be processed
        """
        # Create mock sites
        site_fra01 = MagicMock()
        site_fra01.name = "fra01"
        site_fra01.slug = "fra01"

        site_ams01 = MagicMock()
        site_ams01.name = "ams01"
        site_ams01.slug = "ams01"

        # Create devices in different sites
        device_fra01 = MockNetboxDevice(
            device_id=1, name="device-fra01", status_label="Active", site=site_fra01
        )
        device_ams01 = MockNetboxDevice(
            device_id=2, name="device-ams01", status_label="Active", site=site_ams01
        )

        self._setup_netbox_mock(mock_api, devices=[device_fra01, device_ams01])
        self._setup_zabbix_mock(mock_zabbix_api)

        syncer = Sync()
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )

        # Call start with site filter for fra01
        syncer.start(device_filter={"site": "fra01"})

        # Verify that nbapi.dcim.devices.filter was called with the site filter
        mock_netbox = mock_api.return_value
        filter_call_kwargs = mock_netbox.dcim.devices.filter.call_args[1]
        self.assertEqual(filter_call_kwargs.get("site"), "fra01")

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_config_filter_overridden_by_start_parameter(
        self, mock_api, mock_zabbix_api
    ):
        """Test that start() method filter overrides config filter.

        Scenario:
        - Config specifies nb_device_filter with {"name": "SW01N0", "site": "ams01"}
        - start() is called with {"name": "Testdev02"} (only overriding name)
        - The final filter should be {"name": "Testdev02", "site": "ams01"}
        - Both name and site filters should be applied with the override
        """
        device_matching_all = MockNetboxDevice(
            device_id=1, name="Testdev02", status_label="Active"
        )

        self._setup_netbox_mock(mock_api, devices=[device_matching_all])
        self._setup_zabbix_mock(mock_zabbix_api)

        # Create sync with config filter having multiple parameters
        syncer = Sync({"nb_device_filter": {"name": "SW01N0", "site": "ams01"}})
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )

        # Call start with method filter that overrides only the name
        syncer.start(device_filter={"name": "Testdev02"})

        # Verify that nbapi.dcim.devices.filter was called with combined filter
        # (site from config + name from method parameter)
        mock_netbox = mock_api.return_value
        filter_call_kwargs = mock_netbox.dcim.devices.filter.call_args[1]
        self.assertEqual(filter_call_kwargs.get("name"), "Testdev02")
        self.assertEqual(filter_call_kwargs.get("site"), "ams01")

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_vm_filter_override_with_method_parameter(self, mock_api, mock_zabbix_api):
        """Test that VM filter override works correctly.

        Scenario:
        - Config enables VM sync with nb_vm_filter {"name": "vm-prod"}
        - start() is called with vm_filter {"name": "vm-test"}
        - Only VMs matching "vm-test" should be processed
        """
        # Create two mock VMs
        vm_matching_method_filter = MockNetboxVM(
            vm_id=1, name="vm-test", status_label="Active"
        )
        vm_matching_config_filter = MockNetboxVM(
            vm_id=2, name="vm-prod", status_label="Active"
        )

        self._setup_netbox_mock(
            mock_api,
            vms=[vm_matching_method_filter, vm_matching_config_filter],
        )
        self._setup_zabbix_mock(mock_zabbix_api)

        # Create sync with config filter for VMs
        syncer = Sync(
            {
                "sync_vms": True,
                "nb_vm_filter": {"name": "vm-prod"},
            }
        )
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )

        # Call start with method filter that overrides the VM name filter
        syncer.start(vm_filter={"name": "vm-test"})

        # Verify that nbapi.virtualization.virtual_machines.filter was called with override
        mock_netbox = mock_api.return_value
        filter_call_kwargs = (
            mock_netbox.virtualization.virtual_machines.filter.call_args[1]
        )
        self.assertEqual(filter_call_kwargs.get("name"), "vm-test")

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_multiple_filter_parameters_combined(self, mock_api, mock_zabbix_api):
        """Test that multiple filter parameters are correctly combined.

        Scenario:
        - Config has nb_device_filter with {"site": "fra01", "status": "active"}
        - start() is called with {"name": "router*"}
        - The final filter should have all three parameters
        """
        device = MockNetboxDevice(device_id=1, name="router01", status_label="Active")

        self._setup_netbox_mock(mock_api, devices=[device])
        self._setup_zabbix_mock(mock_zabbix_api)

        syncer = Sync({"nb_device_filter": {"site": "fra01", "status": "active"}})
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )

        syncer.start(device_filter={"name": "router*"})

        mock_netbox = mock_api.return_value
        filter_call_kwargs = mock_netbox.dcim.devices.filter.call_args[1]

        # All three parameters should be present
        self.assertEqual(filter_call_kwargs.get("site"), "fra01")
        self.assertEqual(filter_call_kwargs.get("status"), "active")
        self.assertEqual(filter_call_kwargs.get("name"), "router*")


class TestAdoptExistingHosts(unittest.TestCase):
    """Test adoption of existing Zabbix hosts by name."""

    EXPECTED_DEVICE_HOSTGROUP = "TestSite/TestManufacturer/Switch"
    EXPECTED_VM_HOSTGROUP = "TestSite/Switch"

    def _setup_netbox_mock(self, mock_api, devices=None, vms=None):
        """Helper to setup a working NetBox mock."""
        mock_netbox = MagicMock()
        mock_api.return_value = mock_netbox
        mock_netbox.version = "3.5"
        mock_netbox.extras.custom_fields.filter.return_value = []
        mock_netbox.dcim.devices.filter.return_value = devices or []
        mock_netbox.virtualization.virtual_machines.filter.return_value = vms or []
        mock_netbox.dcim.site_groups.all.return_value = []
        mock_netbox.dcim.regions.all.return_value = []
        mock_netbox.extras.journal_entries = MagicMock()
        return mock_netbox

    def _setup_zabbix_mock(self, mock_zabbix_api, version=7.0, vm_mode=False):
        """Helper to setup a working Zabbix mock."""
        mock_zabbix = MagicMock()
        mock_zabbix_api.return_value = mock_zabbix
        mock_zabbix.version = version
        expected_group = (
            self.EXPECTED_VM_HOSTGROUP if vm_mode else self.EXPECTED_DEVICE_HOSTGROUP
        )
        mock_zabbix.hostgroup.get.return_value = [
            {"groupid": "1", "name": expected_group}
        ]
        mock_zabbix.hostgroup.create.return_value = {"groupids": ["2"]}
        mock_zabbix.template.get.return_value = [
            {"templateid": "1", "name": "TestTemplate"}
        ]
        mock_zabbix.proxy.get.return_value = []
        mock_zabbix.proxygroup.get.return_value = []
        mock_zabbix.host.get.return_value = []
        mock_zabbix.host.create.return_value = {"hostids": ["1"]}
        mock_zabbix.host.update.return_value = {"hostids": ["42"]}
        mock_zabbix.host.delete.return_value = [42]
        return mock_zabbix

    def _make_zabbix_host(self, hostname="test-host", status="0"):
        """Build a minimal host response for consistency_check."""
        return [
            {
                "hostid": "77",
                "host": hostname,
                "name": hostname,
                "parentTemplates": [{"templateid": "1"}],
                "hostgroups": [{"groupid": "1"}],
                "groups": [{"groupid": "1"}],
                "status": status,
                "interfaces": [{}],
                "inventory_mode": "-1",
                "inventory": {},
                "macros": [],
                "tags": [],
                "proxy_hostid": "0",
                "proxyid": "0",
                "proxy_groupid": "0",
            }
        ]

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_adopt_existing_esxi_device_by_name(self, mock_api, mock_zabbix_api):
        """ESXi device with empty hostid should be adopted and synced as linked."""
        platform = MagicMock()
        platform.name = "VMware ESXi"
        device = MockNetboxDevice(
            name="esxi-host01",
            status_label="Active",
            zabbix_hostid=None,
            platform=platform,
        )
        self._setup_netbox_mock(mock_api, devices=[device])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)
        mock_zabbix.host.get.side_effect = [
            [{"hostid": "77", "host": "esxi-host01", "name": "esxi-host01"}],
            [],
            self._make_zabbix_host(hostname="esxi-host01", status="0"),
        ]

        syncer = Sync({"adopt_existing_hosts": True})
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        self.assertEqual(device.custom_fields["zabbix_hostid"], 77)
        mock_zabbix.host.create.assert_not_called()
        mock_zabbix.host.update.assert_not_called()

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_adopt_existing_esxi_vm_by_name(self, mock_api, mock_zabbix_api):
        """ESXi VM with empty hostid should be adopted and synced as linked."""
        platform = MagicMock()
        platform.name = "VMware ESXi"
        vm = MockNetboxVM(
            name="esxi-vm01",
            status_label="Active",
            zabbix_hostid=None,
            platform=platform,
        )
        self._setup_netbox_mock(mock_api, vms=[vm])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api, vm_mode=True)
        mock_zabbix.host.get.side_effect = [
            [{"hostid": "77", "host": "esxi-vm01", "name": "esxi-vm01"}],
            [],
            self._make_zabbix_host(hostname="esxi-vm01", status="0"),
        ]

        syncer = Sync(
            {
                "sync_vms": True,
                "vm_hostgroup_format": "site/role",
                "adopt_existing_hosts": True,
            }
        )
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        self.assertEqual(vm.custom_fields["zabbix_hostid"], 77)
        mock_zabbix.host.create.assert_not_called()
        mock_zabbix.host.update.assert_not_called()

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_non_esxi_device_not_adopted(self, mock_api, mock_zabbix_api):
        """Non-ESXi device should continue create path when hostid is empty."""
        platform = MagicMock()
        platform.name = "Ubuntu Linux"
        device = MockNetboxDevice(
            name="linux-host01",
            status_label="Active",
            zabbix_hostid=None,
            platform=platform,
        )
        self._setup_netbox_mock(mock_api, devices=[device])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)

        syncer = Sync({"adopt_existing_hosts": True})
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_zabbix.host.create.assert_called_once()
        self.assertEqual(device.custom_fields["zabbix_hostid"], 1)

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_multiple_name_matches_are_not_adopted(self, mock_api, mock_zabbix_api):
        """Multiple name matches should skip adoption to avoid wrong links."""
        platform = MagicMock()
        platform.name = "VMware ESXi"
        device = MockNetboxDevice(
            name="esxi-host02",
            status_label="Active",
            zabbix_hostid=None,
            platform=platform,
        )
        self._setup_netbox_mock(mock_api, devices=[device])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api)
        mock_zabbix.host.get.side_effect = [
            [
                {"hostid": "77", "host": "esxi-host02", "name": "esxi-host02"},
                {"hostid": "78", "host": "esxi-host02", "name": "esxi-host02"},
            ],
            [],
            [{"hostid": "77"}],
        ]

        syncer = Sync({"adopt_existing_hosts": True})
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        self.assertIsNone(device.custom_fields["zabbix_hostid"])
        mock_zabbix.host.create.assert_not_called()

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_metadata_only_mode_skips_status_update_after_adoption(
        self, mock_api, mock_zabbix_api
    ):
        """metadata_only should avoid full consistency updates after adoption."""
        platform = MagicMock()
        platform.name = "VMware ESXi"
        vm = MockNetboxVM(
            name="esxi-vm02",
            status_label="Active",
            zabbix_hostid=None,
            platform=platform,
        )
        self._setup_netbox_mock(mock_api, vms=[vm])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api, vm_mode=True)
        mock_zabbix.host.get.side_effect = [
            [{"hostid": "77", "host": "esxi-vm02", "name": "esxi-vm02"}],
            [],
            self._make_zabbix_host(hostname="esxi-vm02", status="1"),
        ]

        syncer = Sync(
            {
                "sync_vms": True,
                "vm_hostgroup_format": "site/role",
                "adopt_existing_hosts": True,
                "adopt_enrich_mode": "metadata_only",
            }
        )
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        self.assertEqual(vm.custom_fields["zabbix_hostid"], 77)
        mock_zabbix.host.update.assert_not_called()

    @patch("netbox_zabbix_sync.modules.core.ZabbixAPI")
    @patch("netbox_zabbix_sync.modules.core.nbapi")
    def test_full_mode_keeps_status_sync_after_adoption(
        self, mock_api, mock_zabbix_api
    ):
        """full mode should retain existing consistency behavior after adoption."""
        platform = MagicMock()
        platform.name = "VMware ESXi"
        vm = MockNetboxVM(
            name="esxi-vm03",
            status_label="Active",
            zabbix_hostid=None,
            platform=platform,
        )
        self._setup_netbox_mock(mock_api, vms=[vm])
        mock_zabbix = self._setup_zabbix_mock(mock_zabbix_api, vm_mode=True)
        mock_zabbix.host.get.side_effect = [
            [{"hostid": "77", "host": "esxi-vm03", "name": "esxi-vm03"}],
            [],
            self._make_zabbix_host(hostname="esxi-vm03", status="1"),
        ]

        syncer = Sync(
            {
                "sync_vms": True,
                "vm_hostgroup_format": "site/role",
                "adopt_existing_hosts": True,
            }
        )
        syncer.connect(
            "http://netbox.local",
            "nb_token",
            "http://zabbix.local",
            "user",
            "pass",
            None,
        )
        syncer.start()

        mock_zabbix.host.update.assert_called_once_with(hostid=77, status="0")
