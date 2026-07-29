"""Tests for configuration parsing in the modules.config module."""

import os
from argparse import Namespace
from unittest.mock import MagicMock, patch

from netbox_zabbix_sync.modules.cli import _apply_cli_overrides
from netbox_zabbix_sync.modules.settings import (
    DEFAULT_CONFIG,
    load_config,
    load_config_file,
    load_env_variable,
)


def test_load_config_defaults():
    """Test that load_config returns default values when no config file or env vars are present"""
    with (
        patch(
            "netbox_zabbix_sync.modules.settings.load_config_file",
            return_value=DEFAULT_CONFIG.copy(),
        ),
        patch(
            "netbox_zabbix_sync.modules.settings.load_env_variable", return_value=None
        ),
    ):
        config = load_config()
        assert config == DEFAULT_CONFIG
        assert config["templates_config_context"] is False
        assert config["create_hostgroups"] is True
        # TLS settings default to disabled / no encryption
        assert config["tls_sync"] is False
        assert config["tls_connect"] == "none"
        assert config["tls_accept"] == ["none"]
        assert config["tls_issuer"] == ""
        assert config["tls_subject"] == ""
        assert config["tls_psk_identity"] == ""
        assert config["tls_psk"] == ""
        assert config["sync_azure_subscriptions"] is False
        assert config["azure_tag"] == "azure"
        assert config["azure_subscription_id_cf"] == "azure_subscription_id"
        assert config["azure_tenant_id_cf"] == "azure_tenant_id"
        assert config["azure_zabbix_hostid_cf"] == "zabbix_hostid"
        assert config["azure_template"] == "Azure by HTTP"
        assert config["azure_hostgroup"] == "Azure/Subscriptions"
        assert config["azure_app_id_vault"] == ""
        assert config["azure_password_vault"] == ""
        assert config["azure_vm_platform_keywords"] == ["azure"]
        assert config["azure_vm_discovered_templates"] == [
            "Azure Virtual Machine by HTTP"
        ]
        assert config["azure_vm_resource_id_cf"] == ""
        assert config["cleanup_deleted_hosts"] is False
        assert config["cleanup_instance_id"] == "default"


def test_load_config_file():
    """Test that load_config properly loads values from config file"""
    mock_config = DEFAULT_CONFIG.copy()
    mock_config["templates_config_context"] = True
    mock_config["sync_vms"] = True

    with (
        patch(
            "netbox_zabbix_sync.modules.settings.load_config_file",
            return_value=mock_config,
        ),
        patch(
            "netbox_zabbix_sync.modules.settings.load_env_variable", return_value=None
        ),
    ):
        config = load_config()
        assert config["templates_config_context"] is True
        assert config["sync_vms"] is True
        # Unchanged values should remain as defaults
        assert config["create_journal"] is False


def test_load_env_variables():
    """Test that load_config properly loads values from environment variables"""

    # Mock env variable loading to return values for specific keys
    def mock_load_env(key):
        if key == "sync_vms":
            return True
        if key == "create_journal":
            return True
        if key == "cleanup_deleted_hosts":
            return "false"
        if key == "cleanup_instance_id":
            return "deployment-a"
        return None

    with (
        patch(
            "netbox_zabbix_sync.modules.settings.load_config_file",
            return_value=DEFAULT_CONFIG.copy(),
        ),
        patch(
            "netbox_zabbix_sync.modules.settings.load_env_variable",
            side_effect=mock_load_env,
        ),
    ):
        config = load_config()
        assert config["sync_vms"] is True
        assert config["create_journal"] is True
        assert config["cleanup_deleted_hosts"] is False
        assert config["cleanup_instance_id"] == "deployment-a"
        # Unchanged values should remain as defaults
        assert config["templates_config_context"] is False


def test_env_vars_override_config_file():
    """Test that environment variables override values from config file"""
    mock_config = DEFAULT_CONFIG.copy()
    mock_config["templates_config_context"] = True
    mock_config["sync_vms"] = False

    # Mock env variable that will override the config file value
    def mock_load_env(key):
        if key == "sync_vms":
            return True
        return None

    with (
        patch(
            "netbox_zabbix_sync.modules.settings.load_config_file",
            return_value=mock_config,
        ),
        patch(
            "netbox_zabbix_sync.modules.settings.load_env_variable",
            side_effect=mock_load_env,
        ),
    ):
        config = load_config()
        # This should be overridden by the env var
        assert config["sync_vms"] is True
        # This should remain from the config file
        assert config["templates_config_context"] is True


def test_load_config_file_function():
    """Test the load_config_file function directly"""
    # Test when the file exists
    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("importlib.util.spec_from_file_location") as mock_spec,
    ):
        # Setup the mock module with attributes
        mock_module = MagicMock()
        mock_module.templates_config_context = True
        mock_module.sync_vms = True

        # Setup the mock spec
        mock_spec_instance = MagicMock()
        mock_spec.return_value = mock_spec_instance
        mock_spec_instance.loader.exec_module = lambda x: None

        # Patch module_from_spec to return our mock module
        with patch("importlib.util.module_from_spec", return_value=mock_module):
            config = load_config_file(DEFAULT_CONFIG.copy())
            assert config["templates_config_context"] is True
            assert config["sync_vms"] is True


def test_load_config_file_not_found():
    """Test load_config_file when the config file doesn't exist"""
    with patch("pathlib.Path.exists", return_value=False):
        result = load_config_file(DEFAULT_CONFIG.copy())
        # Should return a dict equal to DEFAULT_CONFIG, not a new object
        assert result == DEFAULT_CONFIG


def test_load_env_variable_function():
    """Test the load_env_variable function directly"""
    # Create a real environment variable for testing with correct prefix and uppercase
    test_var = "NBZX_TEMPLATES_CONFIG_CONTEXT"
    original_env = os.environ.get(test_var, None)
    try:
        # Set the environment variable with the proper prefix and case
        os.environ[test_var] = "True"

        # Test that it's properly read (using lowercase in the function call)
        value = load_env_variable("templates_config_context")
        assert value == "True"

        # Test when the environment variable doesn't exist
        value = load_env_variable("nonexistent_variable")
        assert value is None
    finally:
        # Clean up - restore original environment
        if original_env is not None:
            os.environ[test_var] = original_env
        else:
            os.environ.pop(test_var, None)


def test_cleanup_options_can_be_overridden_on_the_cli():
    config = DEFAULT_CONFIG.copy()

    result = _apply_cli_overrides(
        config,
        Namespace(cleanup_deleted_hosts=True, cleanup_instance_id="deployment-a"),
    )

    assert result["cleanup_deleted_hosts"] is True
    assert result["cleanup_instance_id"] == "deployment-a"
