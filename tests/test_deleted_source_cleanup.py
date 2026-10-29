"""Tests for opt-in cleanup of missing or filter-excluded owned hosts."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from zabbix_utils import APIRequestError

from netbox_zabbix_sync.modules.core import Sync
from netbox_zabbix_sync.modules.device import CLEANUP_INSTANCE_TAG, CLEANUP_SOURCE_TAG


def _owned_host(hostid, marker, *, flags="0", instance="deployment-a"):
    return {
        "hostid": str(hostid),
        "host": f"host-{hostid}",
        "flags": flags,
        "tags": [
            {"tag": CLEANUP_INSTANCE_TAG, "value": instance},
            {"tag": CLEANUP_SOURCE_TAG, "value": marker},
        ],
    }


def _syncer(devices=(), vms=(), hosts=()):
    syncer = Sync(
        {"cleanup_deleted_hosts": True, "cleanup_instance_id": "deployment-a"}
    )
    syncer.netbox = MagicMock()
    syncer.zabbix = MagicMock()
    syncer.netbox.dcim.devices.filter.return_value = list(devices)
    syncer.netbox.virtualization.virtual_machines.filter.return_value = list(vms)
    syncer.zabbix.host.get.return_value = list(hosts)
    return syncer


def test_cleanup_deletes_only_sources_missing_from_complete_inventory():
    """A source excluded from the normal sync filter remains protected by cleanup."""
    syncer = _syncer(
        devices=[{"id": 7}],
        vms=[{"id": 9}],
        hosts=[
            _owned_host(1, "device:7"),
            _owned_host(2, "device-oob:8"),
            _owned_host(3, "vm:9"),
            _owned_host(4, "vm:10"),
        ],
    )

    summary = syncer._cleanup_deleted_hosts()

    assert summary.removed == ["host-2", "host-4"]
    assert summary.failures == []
    assert syncer.zabbix.host.delete.call_args_list[0].args == ("2",)
    assert syncer.zabbix.host.delete.call_args_list[1].args == ("4",)
    syncer.netbox.dcim.devices.filter.assert_called_once_with()
    syncer.netbox.virtualization.virtual_machines.filter.assert_called_once_with()
    assert syncer.zabbix.host.get.call_args.kwargs["tags"] == [
        {"tag": CLEANUP_INSTANCE_TAG, "value": "deployment-a"}
    ]


def test_cleanup_deletes_custom_field_excluded_host_and_clears_link():
    record = SimpleNamespace(
        id=7,
        custom_fields={"zabbix_hostid": "1"},
        save=MagicMock(),
    )
    report_key = ("device", "7", "filtered-device")
    syncer = _syncer(devices=[{"id": 7}], hosts=[_owned_host(1, "device:7")])

    summary = syncer._cleanup_deleted_hosts(
        {
            "device:7": {
                "record": record,
                "hostid_custom_field": "zabbix_hostid",
                "report_key": report_key,
            }
        }
    )

    assert summary.removed == ["host-1"]
    assert summary.failures == []
    assert record.custom_fields["zabbix_hostid"] is None
    record.save.assert_called_once_with()
    assert summary.removed_targets[0]["report_key"] == report_key


def test_cleanup_does_not_clear_link_when_zabbix_delete_fails():
    record = SimpleNamespace(
        id=7,
        custom_fields={"zabbix_hostid": "1"},
        save=MagicMock(),
    )
    report_key = ("device", "7", "filtered-device")
    syncer = _syncer(devices=[{"id": 7}], hosts=[_owned_host(1, "device:7")])
    syncer.zabbix.host.delete.side_effect = APIRequestError("no permission")

    summary = syncer._cleanup_deleted_hosts(
        {
            "device:7": {
                "record": record,
                "hostid_custom_field": "zabbix_hostid",
                "report_key": report_key,
            }
        }
    )

    assert summary.removed == []
    assert summary.failures == ["host-1"]
    assert record.custom_fields["zabbix_hostid"] == "1"
    record.save.assert_not_called()
    assert summary.failure_targets[0]["report_key"] == report_key


def test_cleanup_reports_netbox_link_save_failure_and_restores_link():
    record = SimpleNamespace(
        id=7,
        custom_fields={"zabbix_hostid": "1"},
        save=MagicMock(side_effect=RuntimeError("NetBox unavailable")),
    )
    syncer = _syncer(devices=[{"id": 7}], hosts=[_owned_host(1, "device:7")])

    summary = syncer._cleanup_deleted_hosts(
        {
            "device:7": {
                "record": record,
                "hostid_custom_field": "zabbix_hostid",
                "report_key": ("device", "7", "filtered-device"),
            }
        }
    )

    assert summary.removed == ["host-1"]
    assert summary.failures == ["host-1"]
    assert record.custom_fields["zabbix_hostid"] == "1"
    assert "could not be cleared" in summary.failure_targets[0]["reason"]


def test_cleanup_can_remove_filter_excluded_azure_subscription_hosts():
    tenant = SimpleNamespace(
        id=9,
        custom_fields={"zabbix_hostid": "1"},
        save=MagicMock(),
    )
    syncer = _syncer(hosts=[_owned_host(1, "azure-subscription:9")])

    summary = syncer._cleanup_deleted_hosts(
        {
            "azure-subscription:9": {
                "record": tenant,
                "hostid_custom_field": "zabbix_hostid",
                "report_key": ("azure_subscription", "9", "filtered-tenant"),
            }
        }
    )

    assert summary.removed == ["host-1"]
    assert tenant.custom_fields["zabbix_hostid"] is None
    tenant.save.assert_called_once_with()


def test_filter_exclusion_cleanup_remains_opt_in():
    syncer = _syncer(hosts=[_owned_host(1, "device:7")])
    syncer.config["cleanup_deleted_hosts"] = False

    summary = syncer._cleanup_deleted_hosts({"device:7": {}})

    assert summary is None
    syncer.zabbix.host.delete.assert_not_called()


def test_cleanup_skips_ambiguous_malformed_discovery_and_foreign_hosts():
    syncer = _syncer(
        hosts=[
            _owned_host(1, "device:1", flags="4"),
            {
                **_owned_host(2, "device:2"),
                "tags": [
                    {"tag": CLEANUP_INSTANCE_TAG, "value": "deployment-a"},
                    {"tag": CLEANUP_SOURCE_TAG, "value": "device:2"},
                    {"tag": CLEANUP_SOURCE_TAG, "value": "vm:2"},
                ],
            },
            _owned_host(3, "tenant:3"),
            {
                **_owned_host(4, "device:4"),
                "tags": [
                    {"tag": CLEANUP_INSTANCE_TAG, "value": "deployment-a"},
                    {"tag": CLEANUP_INSTANCE_TAG, "value": "other-deployment"},
                    {"tag": CLEANUP_SOURCE_TAG, "value": "device:4"},
                ],
            },
        ]
    )

    summary = syncer._cleanup_deleted_hosts()

    assert summary.removed == []
    syncer.zabbix.host.delete.assert_not_called()


def test_cleanup_fails_closed_when_a_netbox_existence_query_fails():
    syncer = _syncer(hosts=[_owned_host(1, "device:1")])
    syncer.netbox.virtualization.virtual_machines.filter.side_effect = RuntimeError(
        "NetBox unavailable"
    )

    summary = syncer._cleanup_deleted_hosts()

    assert summary.removed == []
    syncer.zabbix.host.get.assert_not_called()
    syncer.zabbix.host.delete.assert_not_called()


def test_cleanup_continues_after_individual_zabbix_delete_failures():
    syncer = _syncer(hosts=[_owned_host(1, "device:1"), _owned_host(2, "vm:2")])
    syncer.zabbix.host.delete.side_effect = [APIRequestError("no permission"), None]

    summary = syncer._cleanup_deleted_hosts()

    assert summary.failures == ["host-1"]
    assert summary.removed == ["host-2"]
    assert syncer.zabbix.host.delete.call_count == len(summary.failures) + len(
        summary.removed
    )
