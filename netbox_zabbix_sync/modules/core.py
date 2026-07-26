"""Core component of the sync process"""

import ssl
from collections import defaultdict
from contextlib import suppress
from os import environ
from pprint import pformat
from typing import Any

from pynetbox import api as nbapi
from pynetbox.core.query import RequestError as NetBoxRequestError
from requests.exceptions import ConnectionError as RequestsConnectionError
from zabbix_utils import APIRequestError, ProcessingError, ZabbixAPI

from netbox_zabbix_sync.modules.azure_subscription import AzureSubscription
from netbox_zabbix_sync.modules.device import NetboxDeviceImport, PhysicalDevice
from netbox_zabbix_sync.modules.exceptions import SyncError
from netbox_zabbix_sync.modules.logging import get_logger
from netbox_zabbix_sync.modules.settings import DEFAULT_CONFIG
from netbox_zabbix_sync.modules.tools import (
    convert_recordset,
    proxy_prepper,
    verify_hg_format,
)
from netbox_zabbix_sync.modules.virtual_machine import VirtualMachine

logger = get_logger()


class UnsyncedSummary:
    """Collect NetBox objects that did not end the run as synced Zabbix hosts."""

    def __init__(self):
        self.failures: dict[str, list[str]] = defaultdict(list)
        self.intentional: dict[str, list[str]] = defaultdict(list)

    def record(self, label: str, reason: str, *, intentional: bool = False):
        """Record one object once for a stable, operator-facing reason."""
        records = self.intentional if intentional else self.failures
        if label not in records[reason]:
            records[reason].append(label)

    def log(self, log):
        """Write the end-of-run summary at warning level for normal job logs."""
        failed = sum(len(labels) for labels in self.failures.values())
        intentional = sum(len(labels) for labels in self.intentional.values())
        total = failed + intentional
        if not total:
            log.warning(
                "NetBox-to-Zabbix sync summary: no unsynced devices, VMs, or OOB imports."
            )
            return

        log.warning(
            "NetBox-to-Zabbix sync summary: %s unsynced target(s) "
            "(%s failed, %s intentionally excluded or removed).",
            total,
            failed,
            intentional,
        )
        for category, records in (
            ("Failed", self.failures),
            ("Intentional", self.intentional),
        ):
            for reason in sorted(records):
                log.warning(
                    "  %s - %s (%s): %s",
                    category,
                    reason,
                    len(records[reason]),
                    ", ".join(sorted(records[reason])),
                )


class Sync:
    """
    Class that hosts the main sync process.
    This class is used to connect to NetBox and Zabbix and run the sync process.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        """
        Docstring for __init__

        :param self: Description
        :param config: Description
        """
        self.netbox = None
        self.zabbix = None
        self.nb_version = None

        default_config = DEFAULT_CONFIG.copy()

        combined_config = {
            **default_config,
            **(config if config else {}),
        }

        self.config: dict[str, Any] = combined_config
        self.last_unsynced_summary: UnsyncedSummary | None = None

    def _combine_filters(self, config_filter, method_filter):
        """
        Combine filters from config and method parameters.
        Method parameters will overwrite config filters if there are overlaps.
        """
        # Check if method filter is provided,
        # if not return config filter directly
        combined_filter = config_filter.copy()
        if method_filter:
            combined_filter.update(method_filter)
        return combined_filter

    def _build_oob_hostname(self, primary_name: str, oob_context: dict[str, Any]):
        """Build the OOB host name from the primary host name."""
        prefix = oob_context.get("name_prefix", "")
        suffix = oob_context.get("name_suffix", "")
        if not prefix and not suffix:
            suffix = "-oob"
        return f"{prefix}{primary_name}{suffix}"

    @staticmethod
    def _summary_label(object_type: str, name: str):
        """Build a concise, type-qualified name for the operator summary."""
        return f"{object_type} {name}"

    def _record_sync_error(self, summary, label, error):
        """Translate expected sync exceptions into stable report categories."""
        message = str(error).lower()
        if "no primary ip" in message:
            reason = "no primary IP"
        elif "custom field" in message and "not present" in message:
            reason = "missing Zabbix host ID custom field"
        elif "key 'zabbix' not found" in message:
            reason = "missing Zabbix config context"
        elif "key 'templates' not found" in message:
            reason = "missing Zabbix templates in config context"
        elif "template" in message and "unable to find" in message:
            reason = "configured Zabbix template is unavailable"
        elif "hostgroup" in message:
            reason = "no usable Zabbix hostgroup"
        elif "interface" in message:
            reason = "invalid Zabbix interface configuration"
        elif "virtual chassis" in message and "master" in message:
            reason = "virtual chassis has no primary member"
        elif type(error).__name__ == "SyncExternalError":
            reason = "external NetBox or Zabbix sync error"
        else:
            reason = "unclassified sync error"
        summary.record(label, reason)

    @staticmethod
    def _template_skip_reason(nb_object, config, *, vm=False):
        """Return a stable reason when template selection leaves no templates."""
        context = getattr(nb_object, "config_context", {})
        context_required = vm or config["templates_config_context"]
        if context_required:
            if not isinstance(context, dict) or "zabbix" not in context:
                return "missing Zabbix config context"
            zabbix_context = context["zabbix"]
            if not isinstance(zabbix_context, dict):
                return "invalid Zabbix config context"
            if "templates" not in zabbix_context:
                return "missing Zabbix templates in config context"
            if not zabbix_context["templates"]:
                return "no Zabbix templates configured"
        if not vm and not config["templates_config_context_overrule"]:
            device_type_cfs = getattr(nb_object.device_type, "custom_fields", {})
            if config["template_cf"] not in device_type_cfs:
                return "missing device-type Zabbix template custom field"
        return "no Zabbix templates configured"

    def _get_device_imports(self, nb_device, summary):
        """Return effective NetBox objects/configs for primary and optional OOB imports."""
        zabbix_context = nb_device.config_context.get("zabbix", {})
        primary_config = self.config
        if "templates" in zabbix_context:
            primary_config = self.config.copy()
            primary_config["templates_config_context_overrule"] = True
        imports = [
            (
                nb_device,
                primary_config,
                False,
                self._summary_label("Device", nb_device.name),
            )
        ]
        oob_context = zabbix_context.get("oob")
        if oob_context is None:
            return imports
        if not isinstance(oob_context, dict):
            logger.warning(
                "Host %s: zabbix.oob is not a dictionary, skipping OOB import.",
                nb_device.name,
            )
            summary.record(
                self._summary_label("Device OOB", nb_device.name),
                "invalid OOB Zabbix config context",
            )
            return imports

        if not nb_device.oob_ip:
            logger.warning(
                "Host %s: zabbix.oob is configured but oob_ip is missing, skipping OOB import.",
                nb_device.name,
            )
            summary.record(
                self._summary_label(
                    "Device OOB", self._build_oob_hostname(nb_device.name, oob_context)
                ),
                "missing OOB IP",
            )
            return imports

        metadata_keys = {"name_prefix", "name_suffix"}
        effective_context = {
            key: value for key, value in oob_context.items() if key not in metadata_keys
        }
        effective_config = self.config.copy()
        effective_config["device_cf"] = self.config["oob_device_cf"]
        if "templates" in effective_context:
            effective_config["templates_config_context_overrule"] = True
        imports.append(
            (
                NetboxDeviceImport(
                    nb_device,
                    self._build_oob_hostname(nb_device.name, oob_context),
                    nb_device.oob_ip,
                    effective_context,
                ),
                effective_config,
                True,
                self._summary_label(
                    "Device OOB", self._build_oob_hostname(nb_device.name, oob_context)
                ),
            )
        )
        return imports

    def _process_device(
        self,
        nb_device,
        device_config,
        netbox_journals,
        netbox_site_groups,
        netbox_regions,
        zabbix_groups,
        zabbix_templates,
        zabbix_proxy_list,
        summary,
        summary_label,
        split_import=False,
    ):
        """Run the physical device sync pipeline for one effective device import."""
        device = PhysicalDevice(
            nb_device,
            self.zabbix,
            netbox_journals,
            self.nb_version,
            device_config["create_journal"],
            logger,
            config=device_config,
        )
        logger.debug("Host %s: Started operations on device.", device.name)
        device.set_template(
            device_config["templates_config_context"],
            device_config["templates_config_context_overrule"],
        )
        # Check if a valid template has been found for this device.
        if not device.zbx_template_names:
            summary.record(
                summary_label,
                self._template_skip_reason(nb_device, device_config),
            )
            return True
        device.set_hostgroup(
            device_config["hostgroup_format"], netbox_site_groups, netbox_regions
        )
        # Check if a valid hostgroup has been found for this device.
        if not device.hostgroups:
            logger.warning(
                "Host %s: has no valid hostgroups, Skipping this host...",
                device.name,
            )
            summary.record(summary_label, "no usable Zabbix hostgroup")
            return True
        if device_config["extended_site_properties"] and nb_device.site:
            logger.debug("Host %s: extending site information.", device.name)
            nb_device.site.full_details()
        if device_config["extended_virtual_chassis"] and nb_device.virtual_chassis:
            logger.debug("Host %s: extending virtual chassis information.", device.name)
            nb_device.virtual_chassis.full_details()
            if "members" in dict(nb_device.virtual_chassis):
                for member in nb_device.virtual_chassis.members:
                    member.full_details()

        logger.debug("Host %s NetBox data: %s", device.name, pformat(dict(nb_device)))

        device.set_inventory(nb_device)
        device.set_usermacros()
        device.set_tags()
        device.set_tls()

        # Split imports require explicit unique hostnames and must not be collapsed
        # into a virtual chassis hostname.
        if not split_import and device.is_cluster() and device_config["clustering"]:
            # Check if device is primary or secondary
            if device.promote_primary_device():
                logger.info("Host %s: is part of cluster and primary.", device.name)
            else:
                # Device is secondary in cluster.
                # Don't continue with this device.
                logger.info(
                    "Host %s: Is part of cluster but not primary. Skipping this host...",
                    device.name,
                )
                summary.record(
                    summary_label,
                    "non-primary virtual chassis member",
                    intentional=True,
                )
                return True
        # Checks if device is in cleanup state
        if device.status in device_config["zabbix_device_removal"]:
            if device.zabbix_id:
                # Delete device from Zabbix and remove hostID from NetBox.
                device.cleanup()
                logger.info("Host %s: cleanup complete", device.name)
                summary.record(
                    summary_label,
                    f"removed due to NetBox status {device.status}",
                    intentional=True,
                )
                return True
            # Device has been added to NetBox but is not in Activate state
            logger.info(
                "Host %s: Skipping since this host is not in the active state.",
                device.name,
            )
            summary.record(
                summary_label,
                f"excluded due to NetBox status {device.status}",
                intentional=True,
            )
            return True
        # Check if the device is in the disabled state
        if device.status in device_config["zabbix_device_disable"]:
            device.zabbix_state = 1
        # Add hostgroup is config is set
        if device_config["create_hostgroups"]:
            # Create new hostgroup. Potentially multiple groups if nested
            hostgroups = device.create_zbx_hostgroup(zabbix_groups)
            # go through all newly created hostgroups
            for group in hostgroups:
                # Add new hostgroups to zabbix group list
                zabbix_groups.append(group)
        adopted = device.adopt_existing_zabbix_host()
        full_sync = True
        if adopted and (
            device.adopted_azure_discovered_host
            or str(device_config.get("adopt_enrich_mode", "full")).lower()
            == "metadata_only"
        ):
            full_sync = False
        # Check if device is already in Zabbix
        if device.zabbix_id:
            device.consistency_check(
                zabbix_groups,
                zabbix_templates,
                zabbix_proxy_list,
                device_config["full_proxy_sync"],
                device_config["create_hostgroups"],
                full_sync=full_sync,
            )
            return True
        # Add device to Zabbix
        device.create_in_zabbix(zabbix_groups, zabbix_templates, zabbix_proxy_list)
        if not device.zabbix_id:
            summary.record(
                summary_label, "Zabbix host already exists without NetBox linkage"
            )

    def _process_azure_subscription(
        self,
        nb_tenant,
        zabbix_groups,
        zabbix_templates,
    ):
        """Run the Azure subscription sync pipeline for one NetBox Tenant."""
        azure_subscription = AzureSubscription(
            nb_tenant,
            self.zabbix,
            logger,
            config=self.config,
        )
        logger.debug(
            "Host %s: Started operations on Azure subscription.",
            azure_subscription.name,
        )
        if not azure_subscription.validate():
            return True
        azure_subscription.set_usermacros()
        adopted = azure_subscription.adopt_existing_zabbix_host()
        if azure_subscription.zabbix_id:
            azure_subscription.consistency_check(
                zabbix_groups,
                zabbix_templates,
                self.config["create_hostgroups"],
            )
            return True
        if not adopted:
            azure_subscription.create_in_zabbix(
                zabbix_groups,
                zabbix_templates,
                self.config["create_hostgroups"],
            )
        return True

    def _validate_netbox_token(self, token: str, nb_version: str) -> bool:
        """Validate the format of the NetBox token based on the NetBox version.
        :param token: The NetBox token to validate.
        :param nb_version: The version of NetBox being used.
        :return: True if the token format is valid for the given NetBox version, False otherwise.
        """
        support_token_url = (
            "https://netboxlabs.com/docs/netbox/integrations/rest-api/#v1-and-v2-tokens"  # noqa: S105
        )
        token_prefix = "nbt_"  # noqa: S105
        nb_v2_support_version = "4.5"
        v2_token = bool(token.startswith(token_prefix) and "." in token)
        v2_error_token = bool(token.startswith(token_prefix) and "." not in token)
        # Check if the token is passed without a proper key.token format
        if v2_error_token:
            logger.error(
                "It looks like an invalid v2 token was passed. For more info, see %s",
                support_token_url,
            )
            return False
        # Warning message for Netbox token v1 with Netbox v4.5 and higher
        if not v2_token and nb_version >= nb_v2_support_version:
            logger.warning(
                "Using Netbox v1 token format. "
                "Consider updating to a v2 token. For more info, see %s",
                support_token_url,
            )
        elif v2_token and nb_version < nb_v2_support_version:
            logger.error(
                "Using Netbox v2 token format with Netbox version lower than 4.5. "
                "Revert to v1 token or upgrade Netbox to 4.5 or higher. For more info, see %s",
                support_token_url,
            )
            return False
        elif v2_token and nb_version >= nb_v2_support_version:
            logger.debug("Using NetBox v2 token format.")
        else:
            logger.debug("Using NetBox v1 token format.")
        return True

    def connect(
        self, nb_host, nb_token, zbx_host, zbx_user=None, zbx_pass=None, zbx_token=None
    ):
        """
        Docstring for connect

        :param self: Description
        :param nb_host: Description
        :param nb_token: Description
        :param zbx_host: Description
        :param zbx_user: Description
        :param zbx_pass: Description
        :param zbx_token: Description
        """
        # Initialize Netbox API connection
        netbox = nbapi(nb_host, token=nb_token, threading=True)
        try:
            # Get NetBox version
            nb_version = netbox.version
            # Test API access by attempting to access a basic endpoint
            # This will catch authorization errors early
            netbox.dcim.devices.count()
            logger.debug("NetBox version is %s.", nb_version)
            self.netbox = netbox
            self.nb_version = nb_version
        except RequestsConnectionError:
            logger.error(
                "Unable to connect to NetBox with URL %s. Please check the URL and status of NetBox.",
                nb_host,
            )
            return False
        except NetBoxRequestError as nb_error:
            e = f"NetBox returned the following error: {nb_error}."
            logger.error(e)
            return False
        # Check Netbox API token format based on NetBox version
        if not self._validate_netbox_token(nb_token, self.nb_version):
            return False
        # Set Zabbix API
        if (zbx_pass or zbx_user) and zbx_token:
            e = (
                "Both ZABBIX_PASS, ZABBIX_USER and ZABBIX_TOKEN environment variables are set. "
                "Please choose between token or password based authentication."
            )
            logger.error(e)
            return False
        try:
            ssl_ctx = ssl.create_default_context()

            # If a custom CA bundle is set for pynetbox (requests), also use it for the Zabbix API
            if environ.get("REQUESTS_CA_BUNDLE", None):
                ssl_ctx.load_verify_locations(environ["REQUESTS_CA_BUNDLE"])
            if not zbx_token:
                logger.debug("Using user/password authentication for Zabbix API.")
                self.zabbix = ZabbixAPI(
                    zbx_host, user=zbx_user, password=zbx_pass, ssl_context=ssl_ctx
                )
            else:
                logger.debug("Using token authentication for Zabbix API.")
                self.zabbix = ZabbixAPI(zbx_host, token=zbx_token, ssl_context=ssl_ctx)
            self.zabbix.check_auth()
            logger.debug("Zabbix version is %s.", self.zabbix.version)
        except (APIRequestError, ProcessingError) as zbx_error:
            e = f"Zabbix returned the following error: {zbx_error}."
            logger.error(e)
            return False
        return True

    def logout(self):
        """
        Logout from Zabbix API
        """
        if self.zabbix:
            self.zabbix.logout()
            logger.debug("Logged out from Zabbix API.")
            return True
        return False

    def start(self, device_filter=None, vm_filter=None):
        """
        Run the NetBox to Zabbix sync process.
        """
        if not self.netbox or not self.zabbix:
            e = "Not able to start sync: No connection to NetBox or Zabbix API."
            logger.error(e)
            return False
        summary = UnsyncedSummary()
        self.last_unsynced_summary = summary
        device_cfs = []
        vm_cfs = []
        # Create API call to get all custom fields which are on the device objects
        device_cfs = list(
            self.netbox.extras.custom_fields.filter(
                type=["text", "object", "select"], content_types="dcim.device"
            )
        )
        # Check if the provided Hostgroup layout is valid
        verify_hg_format(
            self.config["hostgroup_format"],
            device_cfs=device_cfs,
            hg_type="dev",
            logger=logger,
        )
        if self.config["sync_vms"]:
            vm_cfs = list(
                self.netbox.extras.custom_fields.filter(
                    type=["text", "object", "select"],
                    content_types="virtualization.virtualmachine",
                )
            )
            verify_hg_format(
                self.config["vm_hostgroup_format"],
                vm_cfs=vm_cfs,
                hg_type="vm",
                logger=logger,
            )
        # Set API parameter mapping based on API version
        proxy_name = "host" if str(self.zabbix.version) < "7" else "name"
        # Get all Zabbix and NetBox data
        dev_filter_combined = self._combine_filters(
            self.config["nb_device_filter"], device_filter
        )
        netbox_devices = list(self.netbox.dcim.devices.filter(**dev_filter_combined))
        netbox_vms = []
        if self.config["sync_vms"]:
            vm_filter_combined = self._combine_filters(
                self.config["nb_vm_filter"], vm_filter
            )
            netbox_vms = list(
                self.netbox.virtualization.virtual_machines.filter(**vm_filter_combined)
            )
        netbox_azure_subscriptions = []
        if self.config["sync_azure_subscriptions"]:
            netbox_azure_subscriptions = list(
                self.netbox.tenancy.tenants.filter(tag=self.config["azure_tag"])
            )
        netbox_site_groups = convert_recordset(self.netbox.dcim.site_groups.all())
        netbox_regions = convert_recordset(self.netbox.dcim.regions.all())
        netbox_journals = self.netbox.extras.journal_entries
        zabbix_groups = self.zabbix.hostgroup.get(  # type: ignore
            output=["groupid", "name"]
        )
        zabbix_templates = self.zabbix.template.get(  # type: ignore
            output=["templateid", "name"]
        )
        zabbix_proxies = self.zabbix.proxy.get(  # type: ignore
            output=["proxyid", proxy_name]
        )
        # Set empty list for proxy processing Zabbix <= 6
        zabbix_proxygroups = []
        if str(self.zabbix.version) >= "7":
            zabbix_proxygroups = self.zabbix.proxygroup.get(  # type: ignore
                output=["proxy_groupid", "name"]
            )
        # Sanitize proxy data
        if proxy_name == "host":
            for proxy in zabbix_proxies:
                proxy["name"] = proxy.pop("host")
        # Prepare list of all proxy and proxy_groups
        zabbix_proxy_list = proxy_prepper(zabbix_proxies, zabbix_proxygroups)

        for nb_tenant in netbox_azure_subscriptions:
            with suppress(SyncError):
                self._process_azure_subscription(
                    nb_tenant,
                    zabbix_groups,
                    zabbix_templates,
                )

        # Go through all NetBox devices
        for nb_vm in netbox_vms:
            summary_label = self._summary_label("VM", nb_vm.name)
            try:
                vm = VirtualMachine(
                    nb_vm,
                    self.zabbix,
                    netbox_journals,
                    self.nb_version,
                    self.config["create_journal"],
                    logger,
                    config=self.config,
                )
                logger.debug("Host %s: Started operations on VM.", vm.name)
                vm.set_vm_template()
                # Check if a valid template has been found for this VM.
                if not vm.zbx_template_names:
                    summary.record(
                        summary_label,
                        self._template_skip_reason(nb_vm, self.config, vm=True),
                    )
                    continue
                vm.set_hostgroup(
                    self.config["vm_hostgroup_format"],
                    netbox_site_groups,
                    netbox_regions,
                )
                # Check if a valid hostgroup has been found for this VM.
                if not vm.hostgroups:
                    summary.record(summary_label, "no usable Zabbix hostgroup")
                    continue
                if self.config["extended_site_properties"] and nb_vm.site:
                    logger.debug("Host %s: extending site information.", vm.name)
                    nb_vm.site.full_details()
                vm.set_inventory(nb_vm)
                vm.set_usermacros()
                vm.set_tags()
                vm.set_tls()
                logger.debug(
                    "Host %s NetBox data: %s",
                    vm.name,
                    pformat(dict(nb_vm)),
                )
                # Checks if device is in cleanup state
                if vm.status in self.config["zabbix_device_removal"]:
                    if vm.zabbix_id:
                        # Delete device from Zabbix
                        # and remove hostID from self.netbox.
                        vm.cleanup()
                        logger.info("Host %s: cleanup complete", vm.name)
                        summary.record(
                            summary_label,
                            f"removed due to NetBox status {vm.status}",
                            intentional=True,
                        )
                        continue
                    # Device has been added to NetBox
                    # but is not in Activate state
                    logger.info(
                        "Host %s: Skipping since this host is not in the active state.",
                        vm.name,
                    )
                    summary.record(
                        summary_label,
                        f"excluded due to NetBox status {vm.status}",
                        intentional=True,
                    )
                    continue
                # Check if the VM is in the disabled state
                if vm.status in self.config["zabbix_device_disable"]:
                    vm.zabbix_state = 1
                # Add hostgroup if config is set
                if self.config["create_hostgroups"]:
                    # Create new hostgroup. Potentially multiple groups if nested
                    hostgroups = vm.create_zbx_hostgroup(zabbix_groups)
                    # go through all newly created hostgroups
                    for group in hostgroups:
                        # Add new hostgroups to zabbix group list
                        zabbix_groups.append(group)
                adopted = vm.adopt_existing_zabbix_host()
                full_sync = True
                if adopted and (
                    vm.adopted_azure_discovered_host
                    or str(self.config.get("adopt_enrich_mode", "full")).lower()
                    == "metadata_only"
                ):
                    full_sync = False
                # Check if VM is already in Zabbix
                if vm.zabbix_id:
                    vm.consistency_check(
                        zabbix_groups,
                        zabbix_templates,
                        zabbix_proxy_list,
                        self.config["full_proxy_sync"],
                        self.config["create_hostgroups"],
                        full_sync=full_sync,
                    )
                    continue
                # Add VM to Zabbix
                vm.create_in_zabbix(zabbix_groups, zabbix_templates, zabbix_proxy_list)
                if not vm.zabbix_id:
                    summary.record(
                        summary_label,
                        "Zabbix host already exists without NetBox linkage",
                    )
            except SyncError as error:
                self._record_sync_error(summary, summary_label, error)

        for nb_device in netbox_devices:
            for (
                device_import,
                device_config,
                split_import,
                summary_label,
            ) in self._get_device_imports(nb_device, summary):
                try:
                    self._process_device(
                        device_import,
                        device_config,
                        netbox_journals,
                        netbox_site_groups,
                        netbox_regions,
                        zabbix_groups,
                        zabbix_templates,
                        zabbix_proxy_list,
                        summary,
                        summary_label,
                        split_import=split_import,
                    )
                except SyncError as error:
                    self._record_sync_error(summary, summary_label, error)
        summary.log(logger)
        return True
