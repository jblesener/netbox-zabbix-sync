"""Focused tests for creating and reconciling multiple Zabbix interfaces."""

from unittest.mock import MagicMock

from netbox_zabbix_sync.modules.device import PhysicalDevice
from netbox_zabbix_sync.modules.virtual_machine import VirtualMachine


def build_host_object(
    config_context=None, *, primary_ip="192.0.2.10/24", host_class=PhysicalDevice
):
    """Create a minimal NetBox-backed host for interface behavior tests."""
    nb = MagicMock()
    nb.id = 123
    nb.name = "test-host"
    nb.status.label = "Active"
    nb.custom_fields = {"zabbix_hostid": None}
    nb.config_context = config_context or {}
    nb.tenant = None
    if primary_ip is None:
        nb.primary_ip = None
    else:
        nb.primary_ip.address = primary_ip

    host = host_class(
        nb,
        MagicMock(),
        MagicMock(),
        "3.0",
        logger=MagicMock(),
        config={"device_cf": "zabbix_hostid"},
    )
    host.zabbix_id = 42
    return host


def agent_interface(**overrides):
    """Return one normal Zabbix agent-interface payload."""
    interface = {
        "main": "1",
        "useip": "1",
        "dns": "",
        "ip": "192.0.2.10",
        "type": 1,
        "port": "10050",
    }
    interface.update(overrides)
    return interface


def snmp_interface(**overrides):
    """Return one normal Zabbix SNMP-interface payload."""
    interface = {
        "main": "1",
        "useip": "1",
        "dns": "",
        "ip": "192.0.2.10",
        "type": 2,
        "port": "161",
        "details": {
            "bulk": "1",
            "version": "2",
            "community": "{$SNMP_COMMUNITY}",
        },
    }
    interface.update(overrides)
    return interface


def test_physical_device_builds_snmp_primary_and_agent_addition():
    context = {
        "zabbix": {
            "interface_type": 2,
            "snmp": {"version": 2},
            "additional_interfaces": [{"interface_type": 1}],
        }
    }
    host = build_host_object(context)

    interfaces = host.set_interface_details()

    assert [interface["type"] for interface in interfaces] == [2, 1]
    assert [interface["main"] for interface in interfaces] == ["1", "1"]
    assert [interface["ip"] for interface in interfaces] == [
        "192.0.2.10",
        "192.0.2.10",
    ]


def test_ipless_vm_builds_only_explicitly_addressed_additional_interfaces():
    context = {
        "zabbix": {"additional_interfaces": [{"interface_type": 1, "ip": "192.0.2.20"}]}
    }
    vm = build_host_object(context, primary_ip=None, host_class=VirtualMachine)

    interfaces = vm.set_interface_details()

    assert len(interfaces) == 1
    assert interfaces[0]["ip"] == "192.0.2.20"
    assert interfaces[0]["type"] == 1


def test_ipless_vm_builds_primary_interface_from_explicit_ip_override():
    context = {"zabbix": {"ip": "192.0.2.20"}}
    vm = build_host_object(context, primary_ip=None, host_class=VirtualMachine)

    interfaces = vm.set_interface_details()

    assert len(interfaces) == 1
    assert interfaces[0]["ip"] == "192.0.2.20"
    assert interfaces[0]["type"] == "1"


def test_vm_primary_agent_interface_retains_legacy_default():
    vm = build_host_object(host_class=VirtualMachine)

    interfaces = vm.set_interface_details()

    assert len(interfaces) == 1
    assert interfaces[0]["type"] == "1"
    assert interfaces[0]["port"] == "10050"


def test_reconciliation_adds_new_interface_and_preserves_matched_id():
    host = build_host_object()
    existing_snmp = snmp_interface(interfaceid="70")

    host._reconcile_interfaces(
        {"interfaces": [existing_snmp]},
        [snmp_interface(), agent_interface()],
    )

    host.zabbix.host.update.assert_called_once_with(
        hostid=42,
        interfaces=[
            {**snmp_interface(), "interfaceid": "70"},
            {**agent_interface(), "hostid": 42},
        ],
    )


def test_reconciliation_updates_same_type_interface_in_place():
    host = build_host_object()
    existing = agent_interface(interfaceid="71")
    desired = agent_interface(port="10051")

    host._reconcile_interfaces({"interfaces": [existing]}, [desired])

    host.zabbix.host.update.assert_called_once_with(
        hostid=42,
        interfaces=[{**desired, "interfaceid": "71"}],
    )


def test_reconciliation_removes_unconfigured_interfaces():
    host = build_host_object()

    host._reconcile_interfaces({"interfaces": [agent_interface(interfaceid="71")]}, [])

    host.zabbix.host.update.assert_called_once_with(hostid=42, interfaces=[])


def test_reconciliation_does_not_update_matching_interfaces_in_any_order():
    host = build_host_object()
    existing = [
        {**agent_interface(), "interfaceid": "71"},
        {**snmp_interface(), "interfaceid": "70"},
    ]

    host._reconcile_interfaces(
        {"interfaces": existing}, [snmp_interface(), agent_interface()]
    )

    host.zabbix.host.update.assert_not_called()


def test_reconciliation_replaces_interface_when_type_changes():
    host = build_host_object()

    host._reconcile_interfaces(
        {"interfaces": [snmp_interface(interfaceid="70")]},
        [agent_interface()],
    )

    host.zabbix.host.update.assert_called_once_with(
        hostid=42,
        interfaces=[{**agent_interface(), "hostid": 42}],
    )
