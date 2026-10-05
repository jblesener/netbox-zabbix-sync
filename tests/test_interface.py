"""Tests for the ZabbixInterface class in the interface module."""

import unittest
from typing import cast

from netbox_zabbix_sync.modules.exceptions import InterfaceConfigError
from netbox_zabbix_sync.modules.interface import ZabbixInterface


class TestZabbixInterface(unittest.TestCase):
    """Test class for ZabbixInterface functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.test_ip = "192.168.1.1"
        self.empty_context = {}
        self.default_interface = ZabbixInterface(self.empty_context, self.test_ip)

        # Create some test contexts for different scenarios
        self.snmpv2_context = {
            "zabbix": {
                "interface_type": 2,
                "interface_port": "161",
                "snmp": {"version": 2, "community": "public", "bulk": 1},
            }
        }

        self.snmpv3_context = {
            "zabbix": {
                "interface_type": 2,
                "snmp": {
                    "version": 3,
                    "securityname": "snmpuser",
                    "securitylevel": "authPriv",
                    "authprotocol": "SHA",
                    "authpassphrase": "authpass123",
                    "privprotocol": "AES",
                    "privpassphrase": "privpass123",
                    "contextname": "context1",
                },
            }
        }

        self.agent_context = {
            "zabbix": {"interface_type": 1, "interface_port": "10050"}
        }

    def test_init(self):
        """Test initialization of ZabbixInterface."""
        interface = ZabbixInterface(self.empty_context, self.test_ip)

        # Check basic properties
        self.assertEqual(interface.ip, self.test_ip)
        self.assertEqual(interface.context, self.empty_context)
        self.assertEqual(interface.interface["ip"], self.test_ip)
        self.assertEqual(interface.interface["main"], "1")
        self.assertEqual(interface.interface["useip"], "1")
        self.assertEqual(interface.interface["dns"], "")

    def test_get_context_empty(self):
        """Test get_context with empty context."""
        interface = ZabbixInterface(self.empty_context, self.test_ip)
        result = interface.get_context()
        self.assertFalse(result)

    def test_get_context_with_interface_type(self):
        """Test get_context with interface_type but no port."""
        context = {"zabbix": {"interface_type": 2}}
        interface = ZabbixInterface(context, self.test_ip)

        # Should set type and default port
        result = interface.get_context()
        self.assertTrue(result)
        self.assertEqual(interface.interface["type"], 2)
        self.assertEqual(interface.interface["port"], "161")  # Default port for SNMP

    def test_get_context_with_interface_type_and_port(self):
        """Test get_context with both interface_type and port."""
        context = {"zabbix": {"interface_type": 1, "interface_port": "12345"}}
        interface = ZabbixInterface(context, self.test_ip)

        # Should set type and specified port
        result = interface.get_context()
        self.assertTrue(result)
        self.assertEqual(interface.interface["type"], 1)
        self.assertEqual(interface.interface["port"], "12345")

    def test_set_default_port(self):
        """Test _set_default_port for different interface types."""
        interface = ZabbixInterface(self.empty_context, self.test_ip)

        # Test for agent type (1)
        interface.interface["type"] = 1
        interface._set_default_port()
        self.assertEqual(interface.interface["port"], "10050")

        # Test for SNMP type (2)
        interface.interface["type"] = 2
        interface._set_default_port()
        self.assertEqual(interface.interface["port"], "161")

        # Test for IPMI type (3)
        interface.interface["type"] = 3
        interface._set_default_port()
        self.assertEqual(interface.interface["port"], "623")

        # Test for JMX type (4)
        interface.interface["type"] = 4
        interface._set_default_port()
        self.assertEqual(interface.interface["port"], "12345")

        # Test for unsupported type
        interface.interface["type"] = 99
        result = interface._set_default_port()
        self.assertFalse(result)

    def test_set_snmp_v2(self):
        """Test set_snmp with SNMPv2 configuration."""
        interface = ZabbixInterface(self.snmpv2_context, self.test_ip)
        interface.get_context()  # Set the interface type

        # Call set_snmp
        interface.set_snmp()

        # Check SNMP details
        details = cast(dict[str, str], interface.interface["details"])
        self.assertEqual(details["version"], "2")
        self.assertEqual(details["community"], "public")
        self.assertEqual(details["bulk"], "1")

    def test_set_snmp_v3(self):
        """Test set_snmp with SNMPv3 configuration."""
        interface = ZabbixInterface(self.snmpv3_context, self.test_ip)
        interface.get_context()  # Set the interface type

        # Call set_snmp
        interface.set_snmp()

        # Check SNMP details
        details = cast(dict[str, str], interface.interface["details"])
        self.assertEqual(details["version"], "3")
        self.assertEqual(details["securityname"], "snmpuser")
        self.assertEqual(details["securitylevel"], "authPriv")
        self.assertEqual(details["authprotocol"], "SHA")
        self.assertEqual(details["authpassphrase"], "authpass123")
        self.assertEqual(details["privprotocol"], "AES")
        self.assertEqual(details["privpassphrase"], "privpass123")
        self.assertEqual(details["contextname"], "context1")

    def test_set_snmp_no_snmp_config(self):
        """Test set_snmp with missing SNMP configuration."""
        # Create context with interface type but no SNMP config
        context = {"zabbix": {"interface_type": 2}}
        interface = ZabbixInterface(context, self.test_ip)
        interface.get_context()  # Set the interface type

        # Call set_snmp - should raise exception
        with self.assertRaises(InterfaceConfigError):
            interface.set_snmp()

    def test_set_snmp_unsupported_version(self):
        """Test set_snmp with unsupported SNMP version."""
        # Create context with invalid SNMP version
        context = {
            "zabbix": {
                "interface_type": 2,
                "snmp": {
                    "version": 4  # Invalid version
                },
            }
        }
        interface = ZabbixInterface(context, self.test_ip)
        interface.get_context()  # Set the interface type

        # Call set_snmp - should raise exception
        with self.assertRaises(InterfaceConfigError):
            interface.set_snmp()

    def test_set_snmp_no_version(self):
        """Test set_snmp with missing SNMP version."""
        # Create context without SNMP version
        context = {
            "zabbix": {
                "interface_type": 2,
                "snmp": {
                    "community": "public"  # No version specified
                },
            }
        }
        interface = ZabbixInterface(context, self.test_ip)
        interface.get_context()  # Set the interface type

        # Call set_snmp - should raise exception
        with self.assertRaises(InterfaceConfigError):
            interface.set_snmp()

    def test_set_snmp_non_snmp_interface(self):
        """Test set_snmp with non-SNMP interface type."""
        interface = ZabbixInterface(self.agent_context, self.test_ip)
        interface.get_context()  # Set the interface type

        # Call set_snmp - should raise exception
        with self.assertRaises(InterfaceConfigError):
            interface.set_snmp()

    def test_set_default_snmp(self):
        """Test set_default_snmp method."""
        interface = ZabbixInterface(self.empty_context, self.test_ip)
        interface.set_default_snmp()

        # Check interface properties
        self.assertEqual(interface.interface["type"], "2")
        self.assertEqual(interface.interface["port"], "161")
        details = cast(dict[str, str], interface.interface["details"])
        self.assertEqual(details["version"], "2")
        self.assertEqual(details["community"], "{$SNMP_COMMUNITY}")
        self.assertEqual(details["bulk"], "1")

    def test_set_default_agent(self):
        """Test set_default_agent method."""
        interface = ZabbixInterface(self.empty_context, self.test_ip)
        interface.set_default_agent()

        # Check interface properties
        self.assertEqual(interface.interface["type"], "1")
        self.assertEqual(interface.interface["port"], "10050")

    def test_snmpv2_no_community(self):
        """Test SNMPv2 with no community string specified."""
        # Create context with SNMPv2 but no community
        context = {"zabbix": {"interface_type": 2, "snmp": {"version": 2}}}
        interface = ZabbixInterface(context, self.test_ip)
        interface.get_context()  # Set the interface type

        # Call set_snmp
        interface.set_snmp()

        # Should use default community string
        details = cast(dict[str, str], interface.interface["details"])
        self.assertEqual(details["community"], "{$SNMP_COMMUNITY}")

    def test_builds_additional_agent_interface_for_snmp_host(self):
        """The primary SNMP interface and an additional agent interface are built."""
        context = {
            "zabbix": {
                "interface_type": 2,
                "snmp": {"version": 2, "community": "public"},
                "additional_interfaces": [{"interface_type": 1}],
            }
        }

        interfaces = ZabbixInterface.build_interfaces(
            context, self.test_ip, default_type=2
        )

        self.assertEqual(
            interfaces,
            [
                {
                    "main": "1",
                    "useip": "1",
                    "dns": "",
                    "ip": self.test_ip,
                    "type": 2,
                    "port": "161",
                    "details": {
                        "bulk": "1",
                        "version": "2",
                        "community": "public",
                    },
                },
                {
                    "main": "1",
                    "useip": "1",
                    "dns": "",
                    "ip": self.test_ip,
                    "type": 1,
                    "port": "10050",
                },
            ],
        )

    def test_additional_interfaces_default_main_once_per_type(self):
        """Only the first interface of each type defaults to main."""
        context = {
            "zabbix": {
                "interface_type": 1,
                "additional_interfaces": [
                    {"interface_type": 1, "interface_port": 10051},
                    {"interface_type": 2, "snmp": {"version": 2}},
                ],
            }
        }

        interfaces = ZabbixInterface.build_interfaces(
            context, self.test_ip, default_type=2
        )

        self.assertEqual(
            [interface["main"] for interface in interfaces], ["1", "0", "1"]
        )
        self.assertEqual([interface["type"] for interface in interfaces], [1, 1, 2])

    def test_additional_interface_can_use_dns_without_primary_ip(self):
        """Explicitly addressed additions are available to IP-less VMs."""
        context = {
            "zabbix": {
                "additional_interfaces": [
                    {"interface_type": 1, "ip": "192.0.2.8"},
                    {
                        "interface_type": 1,
                        "dns": "agent.example.net",
                        "useip": 0,
                        "interface_port": 10051,
                    },
                ]
            }
        }

        interfaces = ZabbixInterface.build_interfaces(
            context, None, default_type=1, include_primary=False
        )

        self.assertEqual([interface["main"] for interface in interfaces], ["1", "0"])
        self.assertEqual(interfaces[0]["ip"], "192.0.2.8")
        self.assertEqual(interfaces[1]["ip"], "")
        self.assertEqual(interfaces[1]["dns"], "agent.example.net")
        self.assertEqual(interfaces[1]["useip"], "0")
        self.assertEqual(interfaces[1]["port"], "10051")

    def test_additional_interface_does_not_mutate_snmp_context(self):
        """SNMP details are read without consuming keys from config context."""
        context = {
            "zabbix": {
                "interface_type": 2,
                "snmp": {"version": 2, "community": "public", "bulk": 0},
                "additional_interfaces": [
                    {"interface_type": 2, "snmp": {"version": 2}}
                ],
            }
        }
        original = {
            "zabbix": {
                "interface_type": 2,
                "snmp": {"version": 2, "community": "public", "bulk": 0},
                "additional_interfaces": [
                    {"interface_type": 2, "snmp": {"version": 2}}
                ],
            }
        }

        ZabbixInterface.build_interfaces(context, self.test_ip, default_type=2)

        self.assertEqual(context, original)

    def test_rejects_malformed_additional_interfaces(self):
        """Invalid additional interface definitions fail with a config error."""
        invalid_contexts = [
            {"zabbix": {"additional_interfaces": {}}},
            {"zabbix": {"additional_interfaces": [{}]}},
            {"zabbix": {"additional_interfaces": [{"interface_type": 99}]}},
            {
                "zabbix": {
                    "additional_interfaces": [
                        {"interface_type": 1, "interface_port": None}
                    ]
                }
            },
            {"zabbix": {"additional_interfaces": [{"interface_type": 1, "useip": 0}]}},
            {
                "zabbix": {
                    "interface_type": 1,
                    "additional_interfaces": [{"interface_type": 1, "main": 1}],
                }
            },
        ]

        for context in invalid_contexts:
            with self.subTest(context=context), self.assertRaises(InterfaceConfigError):
                ZabbixInterface.build_interfaces(context, self.test_ip, default_type=2)
