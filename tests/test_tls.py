"""Tests for the TLS / encryption handling in the PhysicalDevice class."""

import unittest
from unittest.mock import MagicMock

from netbox_zabbix_sync.modules.device import TLS_MODES, PhysicalDevice
from netbox_zabbix_sync.modules.tools import sanatize_log_output


def build_device(config, config_context=None):
    """Helper that builds a PhysicalDevice with the given TLS config/context."""
    nb_device = MagicMock()
    nb_device.id = 123
    nb_device.name = "test-device"
    nb_device.status.label = "Active"
    nb_device.custom_fields = {"zabbix_hostid": None}
    nb_device.config_context = config_context or {}

    primary_ip = MagicMock()
    primary_ip.address = "192.168.1.1/24"
    nb_device.primary_ip = primary_ip

    base_config = {"device_cf": "zabbix_hostid"}
    base_config.update(config)

    return PhysicalDevice(
        nb_device,
        MagicMock(),
        MagicMock(),
        "3.0",
        logger=MagicMock(),
        config=base_config,
    )


# Defaults that mirror DEFAULT_CONFIG so individual tests only override what matters.
TLS_DEFAULTS = {
    "tls_sync": True,
    "tls_connect": "none",
    "tls_accept": ["none"],
    "tls_issuer": "",
    "tls_subject": "",
    "tls_psk_identity": "",
    "tls_psk": "",
}


class TestSetTLS(unittest.TestCase):
    """Tests for PhysicalDevice.set_tls()."""

    def test_disabled_returns_false_and_empty(self):
        """tls_sync=False should produce no TLS payload."""
        config = {**TLS_DEFAULTS, "tls_sync": False, "tls_connect": "cert"}
        device = build_device(config)
        self.assertFalse(device.set_tls())
        self.assertEqual(device.tls, {})

    def test_cert_from_config_context(self):
        """Certificate config from config context maps to the right bitmask."""
        config = {**TLS_DEFAULTS}
        context = {
            "zabbix": {
                "tls_connect": "cert",
                "tls_accept": ["cert"],
                "tls_issuer": "CN=My CA",
                "tls_subject": "CN=host1",
            }
        }
        device = build_device(config, context)
        self.assertTrue(device.set_tls())
        self.assertEqual(device.tls["tls_connect"], TLS_MODES["cert"])
        self.assertEqual(device.tls["tls_accept"], TLS_MODES["cert"])
        self.assertEqual(device.tls["tls_issuer"], "CN=My CA")
        self.assertEqual(device.tls["tls_subject"], "CN=host1")
        # No PSK keys should be present for a cert-only host.
        self.assertNotIn("tls_psk", device.tls)
        self.assertNotIn("tls_psk_identity", device.tls)

    def test_accept_bitmask_combination(self):
        """tls_accept with multiple modes ORs into a single bitmask."""
        config = {
            **TLS_DEFAULTS,
            "tls_accept": ["psk", "cert"],
            "tls_psk_identity": "id1",
            "tls_psk": "abcdef",
        }
        device = build_device(config)
        self.assertTrue(device.set_tls())
        self.assertEqual(
            device.tls["tls_accept"], TLS_MODES["psk"] | TLS_MODES["cert"]
        )

    def test_global_default_applied(self):
        """The global config default is used when config context lacks the keys."""
        config = {**TLS_DEFAULTS, "tls_connect": "cert", "tls_accept": ["cert"]}
        device = build_device(config, {"zabbix": {}})
        self.assertTrue(device.set_tls())
        self.assertEqual(device.tls["tls_connect"], TLS_MODES["cert"])

    def test_config_context_overrides_global_default(self):
        """Config context overrules the global default."""
        config = {**TLS_DEFAULTS, "tls_connect": "none", "tls_accept": ["none"]}
        context = {"zabbix": {"tls_connect": "cert", "tls_accept": ["cert"]}}
        device = build_device(config, context)
        self.assertTrue(device.set_tls())
        self.assertEqual(device.tls["tls_connect"], TLS_MODES["cert"])

    def test_psk_requires_identity_and_key(self):
        """PSK mode without identity/key should fail and clear the payload."""
        config = {
            **TLS_DEFAULTS,
            "tls_connect": "psk",
            "tls_accept": ["psk"],
            "tls_psk_identity": "id1",
            "tls_psk": "",
        }
        device = build_device(config)
        self.assertFalse(device.set_tls())
        self.assertEqual(device.tls, {})
        device.logger.error.assert_called()

    def test_psk_complete(self):
        """A complete PSK config produces the identity and key in the payload."""
        config = {
            **TLS_DEFAULTS,
            "tls_connect": "psk",
            "tls_accept": ["psk"],
            "tls_psk_identity": "id1",
            "tls_psk": "abcdef0123456789",
        }
        device = build_device(config)
        self.assertTrue(device.set_tls())
        self.assertEqual(device.tls["tls_psk_identity"], "id1")
        self.assertEqual(device.tls["tls_psk"], "abcdef0123456789")

    def test_invalid_mode_returns_false(self):
        """An unknown mode name should fail with a warning."""
        config = {**TLS_DEFAULTS, "tls_connect": "bogus"}
        device = build_device(config)
        self.assertFalse(device.set_tls())
        self.assertEqual(device.tls, {})
        device.logger.warning.assert_called()


class TestSanitizePSK(unittest.TestCase):
    """The log sanitizer should mask the PSK secret."""

    def test_masks_psk(self):
        data = {"tls_psk": "abcdef0123456789", "tls_psk_identity": "id1"}
        result = sanatize_log_output(data)
        self.assertEqual(result["tls_psk"], "********")
        self.assertEqual(result["tls_psk_identity"], "********")


if __name__ == "__main__":
    unittest.main()
