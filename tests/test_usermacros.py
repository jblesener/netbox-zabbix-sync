import unittest
from unittest.mock import MagicMock, patch

from netbox_zabbix_sync.modules.device import PhysicalDevice
from netbox_zabbix_sync.modules.usermacros import ZabbixUsermacros


class DummyNB:
    def __init__(self, name="dummy", config_context=None, **kwargs):
        self.name = name
        self.config_context = config_context or {}
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __getitem__(self, key):
        return getattr(self, key)


class TestUsermacroSync(unittest.TestCase):
    def setUp(self):
        self.nb = DummyNB(serial="1234")
        self.logger = MagicMock()
        self.usermacro_map = {"serial": "{$HW_SERIAL}"}

    def create_mock_device(self, config=None):
        """Helper method to create a properly mocked PhysicalDevice"""
        # Mock the NetBox device with all required attributes
        mock_nb = MagicMock()
        mock_nb.id = 1
        mock_nb.name = "dummy"
        mock_nb.status.label = "Active"
        mock_nb.tenant = None
        mock_nb.config_context = {}
        mock_nb.primary_ip.address = "192.168.1.1/24"
        mock_nb.custom_fields = {"zabbix_hostid": None}

        device_config = config if config is not None else {"device_cf": "zabbix_hostid"}

        # Create device with proper initialization
        device = PhysicalDevice(
            nb=mock_nb,
            zabbix=MagicMock(),
            nb_journal_class=MagicMock(),
            nb_version="3.0",
            logger=self.logger,
            config=device_config,
        )

        return device

    @patch.object(PhysicalDevice, "_usermacro_map")
    def test_usermacro_sync_false(self, mock_usermacro_map):
        mock_usermacro_map.return_value = self.usermacro_map
        device = self.create_mock_device(
            config={
                "usermacro_sync": False,
                "device_cf": "zabbix_hostid",
                "tag_sync": False,
            }
        )

        # Call set_usermacros
        result = device.set_usermacros()

        self.assertEqual(device.usermacros, [])
        self.assertTrue(result is True or result is None)

    @patch("netbox_zabbix_sync.modules.device.ZabbixUsermacros")
    @patch.object(PhysicalDevice, "_usermacro_map")
    def test_usermacro_sync_true(self, mock_usermacro_map, mock_usermacros_class):
        mock_usermacro_map.return_value = self.usermacro_map
        # Mock the ZabbixUsermacros class to return some test data
        mock_macros_instance = MagicMock()
        mock_macros_instance.sync = True  # This is important - sync must be True
        mock_macros_instance.generate.return_value = [
            {"macro": "{$HW_SERIAL}", "value": "1234"}
        ]
        mock_usermacros_class.return_value = mock_macros_instance

        device = self.create_mock_device(
            config={
                "usermacro_sync": True,
                "device_cf": "zabbix_hostid",
                "tag_sync": False,
            }
        )

        # Call set_usermacros
        device.set_usermacros()

        self.assertIsInstance(device.usermacros, list)
        self.assertGreater(len(device.usermacros), 0)

    @patch("netbox_zabbix_sync.modules.device.ZabbixUsermacros")
    @patch.object(PhysicalDevice, "_usermacro_map")
    def test_usermacro_sync_full(self, mock_usermacro_map, mock_usermacros_class):
        mock_usermacro_map.return_value = self.usermacro_map
        # Mock the ZabbixUsermacros class to return some test data
        mock_macros_instance = MagicMock()
        mock_macros_instance.sync = True  # This is important - sync must be True
        mock_macros_instance.generate.return_value = [
            {"macro": "{$HW_SERIAL}", "value": "1234"}
        ]
        mock_usermacros_class.return_value = mock_macros_instance

        device = self.create_mock_device(
            config={
                "usermacro_sync": "full",
                "device_cf": "zabbix_hostid",
                "tag_sync": False,
            }
        )

        # Call set_usermacros
        device.set_usermacros()

        self.assertIsInstance(device.usermacros, list)
        self.assertGreater(len(device.usermacros), 0)

    @patch("netbox_zabbix_sync.modules.device.ZabbixUsermacros")
    @patch.object(PhysicalDevice, "_usermacro_map")
    def test_usermacro_sync_partial(self, mock_usermacro_map, mock_usermacros_class):
        mock_usermacro_map.return_value = self.usermacro_map
        mock_macros_instance = MagicMock()
        mock_macros_instance.sync = True
        mock_macros_instance.generate.return_value = [
            {"macro": "{$HW_SERIAL}", "value": "1234"}
        ]
        mock_usermacros_class.return_value = mock_macros_instance

        device = self.create_mock_device(
            config={
                "usermacro_sync": "partial",
                "device_cf": "zabbix_hostid",
                "tag_sync": False,
            }
        )

        device.set_usermacros()

        self.assertIsInstance(device.usermacros, list)
        self.assertGreater(len(device.usermacros), 0)


class TestZabbixUsermacros(unittest.TestCase):
    def setUp(self):
        self.nb = DummyNB()
        self.logger = MagicMock()

    def test_validate_macro_valid(self):
        macros = ZabbixUsermacros(self.nb, {}, False, logger=self.logger)
        self.assertTrue(macros.validate_macro("{$TEST_MACRO}"))
        self.assertTrue(macros.validate_macro("{$A1_2.3}"))
        self.assertTrue(macros.validate_macro("{$FOO:bar}"))

    def test_validate_macro_invalid(self):
        macros = ZabbixUsermacros(self.nb, {}, False, logger=self.logger)
        self.assertFalse(macros.validate_macro("$TEST_MACRO"))
        self.assertFalse(macros.validate_macro("{TEST_MACRO}"))
        self.assertFalse(macros.validate_macro("{$test}"))  # lower-case not allowed
        self.assertFalse(macros.validate_macro(""))

    def test_partial_mode_sets_partial_sync_without_force_sync(self):
        macros = ZabbixUsermacros(self.nb, {}, "partial", logger=self.logger)

        self.assertTrue(macros.sync)
        self.assertTrue(macros.partial_sync)
        self.assertFalse(macros.force_sync)

    def test_render_macro_dict(self):
        macros = ZabbixUsermacros(self.nb, {}, False, logger=self.logger)
        macro = macros.render_macro(
            "{$FOO}", {"value": "bar", "type": "secret", "description": "desc"}
        )
        self.assertEqual(macro["macro"], "{$FOO}")
        self.assertEqual(macro["value"], "bar")
        self.assertEqual(macro["type"], "1")
        self.assertEqual(macro["description"], "desc")

    def test_render_macro_dict_missing_value(self):
        macros = ZabbixUsermacros(self.nb, {}, False, logger=self.logger)
        result = macros.render_macro("{$FOO}", {"type": "text"})
        self.assertFalse(result)
        self.logger.info.assert_called()

    def test_render_macro_str(self):
        macros = ZabbixUsermacros(self.nb, {}, False, logger=self.logger)
        macro = macros.render_macro("{$FOO}", "bar")
        self.assertEqual(macro["macro"], "{$FOO}")
        self.assertEqual(macro["value"], "bar")
        self.assertEqual(macro["type"], "0")
        self.assertEqual(macro["description"], "")

    def test_render_macro_invalid_name(self):
        macros = ZabbixUsermacros(self.nb, {}, False, logger=self.logger)
        result = macros.render_macro("FOO", "bar")
        self.assertFalse(result)
        self.logger.warning.assert_called()

    def test_generate_from_map(self):
        nb = DummyNB(memory="bar", role="baz")
        usermacro_map = {"memory": "{$FOO}", "role": "{$BAR}"}
        macros = ZabbixUsermacros(nb, usermacro_map, True, logger=self.logger)
        result = macros.generate()
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["macro"], "{$FOO}")
        self.assertEqual(result[1]["macro"], "{$BAR}")

    def test_generate_config_context_overrides_field_macro_in_partial_merge(self):
        config_context = {"zabbix": {"usermacros": {"{$FOO}": "from-context"}}}
        nb = DummyNB(config_context=config_context, serial="from-map")
        macros = ZabbixUsermacros(
            nb, {"serial": "{$FOO}"}, "partial", logger=self.logger
        )

        result = macros.generate()
        merged = ZabbixUsermacros.merge_partial([], result)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["macro"], "{$FOO}")
        self.assertEqual(merged[0]["value"], "from-context")

    def test_generate_from_config_context(self):
        config_context = {"zabbix": {"usermacros": {"{$TEST_MACRO}": "test_value"}}}
        nb = DummyNB(config_context=config_context)
        macros = ZabbixUsermacros(nb, {}, True, logger=self.logger)
        result = macros.generate()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["macro"], "{$TEST_MACRO}")
        self.assertEqual(result[0]["value"], "test_value")

    def test_generate_expands_config_context_string_value(self):
        config_context = {
            "zabbix": {"usermacros": {"{$VMWARE.URL}": "https://{netbox:name}/sdk"}}
        }
        nb = DummyNB(name="server.company.com", config_context=config_context)
        macros = ZabbixUsermacros(nb, {}, True, logger=self.logger)

        result = macros.generate()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["macro"], "{$VMWARE.URL}")
        self.assertEqual(result[0]["value"], "https://server.company.com/sdk")

    def test_generate_expands_config_context_dict_value(self):
        config_context = {
            "zabbix": {
                "usermacros": {
                    "{$VMWARE.URL}": {
                        "type": "text",
                        "value": "https://{netbox:name}/sdk",
                        "description": "VMware SDK endpoint",
                    }
                }
            }
        }
        nb = DummyNB(name="server.company.com", config_context=config_context)
        macros = ZabbixUsermacros(nb, {}, True, logger=self.logger)

        result = macros.generate()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["value"], "https://server.company.com/sdk")
        self.assertEqual(result[0]["type"], "0")
        self.assertEqual(result[0]["description"], "VMware SDK endpoint")

    def test_generate_expands_nested_netbox_path(self):
        config_context = {
            "zabbix": {"usermacros": {"{$PRIMARY_IP}": "{netbox:primary_ip/address}"}}
        }
        nb = DummyNB(
            config_context=config_context,
            primary_ip=DummyNB(address="192.0.2.1/24"),
        )
        macros = ZabbixUsermacros(nb, {}, True, logger=self.logger)

        result = macros.generate()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["value"], "192.0.2.1/24")

    def test_generate_expands_custom_field_path(self):
        config_context = {
            "zabbix": {
                "usermacros": {
                    "{$VMWARE.URL}": "https://{netbox:custom_fields/vmware_fqdn}/sdk"
                }
            }
        }
        nb = DummyNB(
            config_context=config_context,
            custom_fields={"vmware_fqdn": "server.company.com"},
        )
        macros = ZabbixUsermacros(nb, {}, True, logger=self.logger)

        result = macros.generate()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["value"], "https://server.company.com/sdk")

    def test_generate_expands_multiple_placeholders(self):
        config_context = {
            "zabbix": {
                "usermacros": {
                    "{$SUMMARY}": "{netbox:name} {netbox:primary_ip/address}"
                }
            }
        }
        nb = DummyNB(
            name="server.company.com",
            config_context=config_context,
            primary_ip=DummyNB(address="192.0.2.1/24"),
        )
        macros = ZabbixUsermacros(nb, {}, True, logger=self.logger)

        result = macros.generate()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["value"], "server.company.com 192.0.2.1/24")

    def test_generate_empty_placeholder_value_expands_to_empty_string(self):
        config_context = {
            "zabbix": {"usermacros": {"{$OPTIONAL}": "value-{netbox:serial}"}}
        }
        nb = DummyNB(config_context=config_context, serial="")
        macros = ZabbixUsermacros(nb, {}, True, logger=self.logger)

        result = macros.generate()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["value"], "value-")
        self.logger.info.assert_called()

    def test_generate_invalid_placeholder_path_skips_macro(self):
        config_context = {
            "zabbix": {"usermacros": {"{$BAD}": "value-{netbox:not_a_field}"}}
        }
        nb = DummyNB(config_context=config_context)
        macros = ZabbixUsermacros(nb, {}, True, logger=self.logger)

        result = macros.generate()

        self.assertEqual(result, [])
        self.logger.warning.assert_called()

    def test_generate_expands_secret_macro_value(self):
        config_context = {
            "zabbix": {
                "usermacros": {
                    "{$SECRET}": {
                        "type": "secret",
                        "value": "secret-{netbox:name}",
                    }
                }
            }
        }
        nb = DummyNB(name="server.company.com", config_context=config_context)
        macros = ZabbixUsermacros(nb, {}, True, logger=self.logger)

        result = macros.generate()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["value"], "secret-server.company.com")
        self.assertEqual(result[0]["type"], "1")

    def test_partial_merge_preserves_overwrites_and_adds_macros(self):
        zabbix_macros = [
            {
                "macro": "{$ZABBIX_ONLY}",
                "value": "keep",
                "type": "0",
                "description": "",
            },
            {
                "macro": "{$OVERWRITE}",
                "value": "old",
                "type": "0",
                "description": "",
            },
        ]
        netbox_macros = [
            {
                "macro": "{$OVERWRITE}",
                "value": "new",
                "type": "0",
                "description": "from-netbox",
            },
            {
                "macro": "{$NETBOX_ONLY}",
                "value": "add",
                "type": "0",
                "description": "",
            },
        ]

        result = ZabbixUsermacros.merge_partial(zabbix_macros, netbox_macros)

        self.assertEqual(
            result,
            [
                {
                    "macro": "{$ZABBIX_ONLY}",
                    "value": "keep",
                    "type": "0",
                    "description": "",
                },
                {
                    "macro": "{$OVERWRITE}",
                    "value": "new",
                    "type": "0",
                    "description": "from-netbox",
                },
                {
                    "macro": "{$NETBOX_ONLY}",
                    "value": "add",
                    "type": "0",
                    "description": "",
                },
            ],
        )

    def test_partial_merge_keeps_existing_secret_macro_in_sync_shape(self):
        zabbix_macros = [{"macro": "{$SECRET}", "type": "1", "description": "secret"}]
        netbox_macros = [{"macro": "{$SECRET}", "type": "1", "description": "secret"}]

        result = ZabbixUsermacros.merge_partial(zabbix_macros, netbox_macros)

        self.assertEqual(result, zabbix_macros)


if __name__ == "__main__":
    unittest.main()
