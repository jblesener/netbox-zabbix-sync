"""Every place the sync reads a custom field, fed what each NetBox version sends.

NetBox 4.7 changed how choice custom fields are returned (see
netbox_payloads.py), and the sync reads custom fields in several unrelated
places. Each is tested here against the captured payload of every version in
the functional matrix, and each asserts the same result for all of them: a
NetBox upgrade must not change which hostgroup, proxy or template a host gets.

The consumers:

- a hostgroup format segment that is not a built-in option (hostgroups.py)
- `proxy_cf` / `proxy_group_cf`, on the device and then on its site (host.py)
- `template_cf` on the device type (host.py)
- `custom_fields/...` paths in the inventory, tag and usermacro maps, which all
  go through `field_mapper` and are tested with it in test_tools.py
- the `device_cf` write-back, which goes through pynetbox's `save()`

Only text, select and object are tested as names: those are the types
core.py fetches for `verify_hg_format`, so the only ones a hostgroup format can
name, and the ones documented for the proxy settings.
"""

from unittest.mock import MagicMock, patch

import pynetbox
import pytest
from pynetbox.core.response import Record

from netbox_zabbix_sync.modules.hostgroups import Hostgroup
from tests.fakes import FakeNetBox, physical_device
from tests.netbox_payloads import (
    EXPECTED,
    NETBOX_VERSIONS,
    cf_cases,
    device_payload,
)

NAMED_TYPES = ("text", "select", "object")

ZABBIX_HOSTID = 42


def nb_device(
    custom_fields=None,
    site_custom_fields=None,
    type_custom_fields=None,
    config_context=None,
):
    """A NetBox device carrying the given custom fields.

    Site and device type custom fields are not in the nested form NetBox
    returns, so the sync reads them through a lazy load of the full object.
    """
    netbox = FakeNetBox()
    return netbox.device(
        site=netbox.site("AMS-01", custom_fields=site_custom_fields or {}),
        role=netbox.role("Server"),
        device_type=netbox.device_type(
            "Test Model", custom_fields=type_custom_fields or {}
        ),
        custom_fields=custom_fields,
        config_context=config_context,
    )


def proxies(*names, proxy_type="proxy"):
    """Proxies in the shape proxy_prepper hands to `_set_proxy`."""
    return [
        {"name": name, "type": proxy_type, "id": str(i), "monitored_by": 1}
        for i, name in enumerate(names, start=1)
    ]


class TestHostgroupSegment:
    """A custom field named in the hostgroup format contributes its value."""

    @pytest.mark.parametrize(("value", "cf_type"), cf_cases(*NAMED_TYPES))
    def test_segment_resolves_to_the_same_name_on_every_version(self, value, cf_type):
        nb = nb_device(custom_fields={"zone": value})

        hostgroup = Hostgroup("dev", nb, "4.7", MagicMock()).generate("site/zone/role")

        assert hostgroup == f"AMS-01/{EXPECTED[cf_type]}/Server"


class TestProxyCustomField:
    """`proxy_cf` / `proxy_group_cf` name a custom field holding the proxy name."""

    @pytest.mark.parametrize(("value", "cf_type"), cf_cases(*NAMED_TYPES))
    def test_proxy_from_the_device(self, value, cf_type):
        device = physical_device(nb_device({"proxy": value}), proxy_cf="proxy")

        assert device._set_proxy(proxies("decoy", EXPECTED[cf_type]))
        assert device.zbxproxy is not None
        assert device.zbxproxy["name"] == EXPECTED[cf_type]

    def test_server_proxy_mode_overrides_custom_fields_and_context(self):
        nb = nb_device(
            custom_fields={"proxy": "device-proxy", "proxy_group": "device-group"},
            site_custom_fields={"proxy": "site-proxy", "proxy_group": "site-group"},
            config_context={
                "zabbix": {
                    "proxy_mode": "server",
                    "proxy": "context-proxy",
                    "proxy_group": "context-group",
                }
            },
        )
        device = physical_device(nb, proxy_cf="proxy", proxy_group_cf="proxy_group")
        proxy_list = proxies("device-proxy", "site-proxy", "context-proxy") + proxies(
            "device-group", "site-group", "context-group", proxy_type="proxy_group"
        )

        assert device.proxy_mode_server
        assert not device._set_proxy(proxy_list)
        assert device.zbxproxy is None

    def test_unsupported_proxy_mode_warns_and_keeps_proxy_resolution(self):
        logger = MagicMock()
        nb = nb_device(
            config_context={"zabbix": {"proxy_mode": "proxy", "proxy": "context-proxy"}}
        )
        device = physical_device(nb, logger=logger)

        assert not device.proxy_mode_server
        assert device._set_proxy(proxies("context-proxy"))
        logger.warning.assert_called_once()
        assert "unsupported zabbix.proxy_mode" in logger.warning.call_args.args[0]


class TestServerProxyOverride:
    @pytest.mark.parametrize(
        ("version", "host", "expected"),
        [
            (6.0, {"proxy_hostid": "12"}, {"proxy_hostid": 0}),
            (
                7.0,
                {"proxyid": "12", "proxy_groupid": "0"},
                {"proxyid": 0, "monitored_by": 0},
            ),
            (
                7.0,
                {"proxyid": "0", "proxy_groupid": "12"},
                {"proxy_groupid": 0, "monitored_by": 0},
            ),
        ],
    )
    def test_clears_proxy_assignment_using_version_specific_fields(
        self, version, host, expected
    ):
        device = physical_device(nb_device())
        device.zabbix.version = version

        with patch.object(device, "update_zabbix_host") as update:
            assert device._clear_zabbix_proxy(host)

        update.assert_called_once_with(**expected)

    def test_does_not_update_a_host_already_using_the_server(self):
        device = physical_device(nb_device())

        with patch.object(device, "update_zabbix_host") as update:
            assert not device._clear_zabbix_proxy(
                {"proxy_hostid": "0", "proxyid": "0", "proxy_groupid": "0"}
            )

        update.assert_not_called()

    @pytest.mark.parametrize(("value", "cf_type"), cf_cases(*NAMED_TYPES))
    def test_proxy_falls_back_to_the_site(self, value, cf_type):
        nb = nb_device(site_custom_fields={"proxy": value})
        device = physical_device(nb, proxy_cf="proxy")

        assert device._set_proxy(proxies("decoy", EXPECTED[cf_type]))
        assert device.zbxproxy is not None
        assert device.zbxproxy["name"] == EXPECTED[cf_type]

    @pytest.mark.parametrize(("value", "cf_type"), cf_cases(*NAMED_TYPES))
    def test_proxy_group_from_the_device(self, value, cf_type):
        device = physical_device(nb_device({"pg": value}), proxy_group_cf="pg")
        groups = proxies("decoy", EXPECTED[cf_type], proxy_type="proxy_group")

        assert device._set_proxy(groups)
        assert device.zbxproxy is not None
        assert device.zbxproxy["name"] == EXPECTED[cf_type]


class TestTemplateCustomField:
    """`template_cf` names a device type custom field holding the template."""

    @pytest.mark.parametrize(("value", "cf_type"), cf_cases("text", "select"))
    def test_template_name_is_the_same_on_every_version(self, value, cf_type):
        nb = nb_device(type_custom_fields={"zabbix_template": value})
        device = physical_device(nb, template_cf="zabbix_template")

        assert device._get_templates_cf() == [EXPECTED[cf_type]]
        assert device.get_templates_cf() == [EXPECTED[cf_type]]


class TestWriteBack:
    """The host ID write-back must survive a device with choice fields set.

    `create_in_zabbix` writes the Zabbix host ID into `device_cf` and calls
    `save()`, NetBox 4.7 returns read-only dicts for choice fields, which need to be handled by pynetbox.
    """

    @pytest.mark.parametrize("version", NETBOX_VERSIONS)
    def test_save_payload_has_the_bare_values_netbox_accepts(self, version):
        # A real api object for URL parsing; nothing here touches the network.
        api = pynetbox.api("http://localhost:8000")
        record = Record(device_payload(version), api, api.dcim.devices)
        record.custom_fields["zabbix_hostid"] = ZABBIX_HOSTID

        sent = record.serialize()["custom_fields"]

        assert sent["zabbix_hostid"] == ZABBIX_HOSTID
        assert sent["cf_select"] == "staging"
        assert sent["cf_multiselect"] == ["iso27001", "soc2"]
        assert sent["cf_object"] == 1
