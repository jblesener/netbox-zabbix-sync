"""
Device specific handeling for NetBox to Zabbix
"""

from collections.abc import Mapping
from copy import deepcopy
from logging import getLogger
from operator import itemgetter
from re import fullmatch
from types import SimpleNamespace
from typing import Any

from pynetbox import RequestError as NetboxRequestError
from zabbix_utils import APIRequestError

from netbox_zabbix_sync.modules.exceptions import (
    InterfaceConfigError,
    SyncExternalError,
    SyncInventoryError,
    TemplateError,
)
from netbox_zabbix_sync.modules.host_description import Description
from netbox_zabbix_sync.modules.hostgroups import Hostgroup
from netbox_zabbix_sync.modules.interface import ZabbixInterface
from netbox_zabbix_sync.modules.settings import load_config
from netbox_zabbix_sync.modules.tags import ZabbixTags
from netbox_zabbix_sync.modules.tools import (
    cf_to_string,
    field_mapper,
    sanatize_log_output,
)
from netbox_zabbix_sync.modules.usermacros import ZabbixUsermacros

# Zabbix encryption modes mapped to their API integer values.
# tls_connect uses a single value; tls_accept is an OR-summed bitmask.
TLS_MODES = {"none": 1, "psk": 2, "cert": 4}
ZABBIX_INVENTORY_TYPE_MAX_LENGTH = 64
CLEANUP_INSTANCE_TAG = "netbox-zabbix-sync-instance"
CLEANUP_SOURCE_TAG = "netbox-zabbix-sync-source"


class NetboxDeviceImport:
    """
    Adapter for importing one NetBox device as a distinct Zabbix host.

    It delegates all normal NetBox fields to the original device while overriding
    the effective host name, primary IP, and config context for one import entry.
    """

    def __init__(self, nb, name, ip_object, zabbix_context):
        self.nb = nb
        self.name = name
        self.primary_ip = ip_object
        self.config_context = {"zabbix": zabbix_context}

    def __getattr__(self, name):
        return getattr(self.nb, name)

    def __getitem__(self, key):
        return getattr(self, key)

    def __iter__(self):
        data = dict(self.nb)
        data["name"] = self.name
        data["primary_ip"] = self.primary_ip
        data["config_context"] = self.config_context
        yield from data.items()


class NetboxClusterImport:
    """Adapt a NetBox virtualization cluster for the VM sync workflow.

    NetBox clusters do not have a native primary IP or site.  ONTAP clusters
    store their management address in a custom field and inherit their site
    from their member devices, so expose those values in the shape expected by
    :class:`PhysicalDevice` and :class:`VirtualMachine`.
    """

    def __init__(self, nb, site, primary_ip_cf):
        self.nb = nb
        self.site = site
        self.primary_ip = self._primary_ip(
            getattr(nb, "custom_fields", {}).get(primary_ip_cf)
        )
        self.config_context = getattr(nb, "config_context", {}) or {}
        # Clusters have no role.  Supplying None keeps VM hostgroup formats
        # containing role usable while omitting that empty segment.
        self.role = getattr(nb, "role", None)
        self.platform = getattr(nb, "platform", None)
        self.cluster = None
        self.virtual_chassis = None
        self.owner = getattr(nb, "owner", None)

    @staticmethod
    def _primary_ip(value):
        """Normalize an IP-address custom-field value to a NetBox IP object."""
        if not value:
            return None
        if hasattr(value, "address"):
            return value
        if isinstance(value, Mapping):
            value = value.get("address") or value.get("display") or value.get("value")
        if not value:
            return None
        return SimpleNamespace(address=str(value))

    def __getattr__(self, name):
        return getattr(self.nb, name)

    def __getitem__(self, key):
        return getattr(self, key)

    def __iter__(self):
        data = dict(self.nb)
        data["site"] = self.site
        data["primary_ip"] = self.primary_ip
        data["config_context"] = self.config_context
        yield from data.items()


class PhysicalDevice:
    """
    Represents Network device.
    INPUT: (NetBox device class, ZabbixAPI class, journal flag, NB journal class)
    """

    def __init__(
        self,
        nb,
        zabbix,
        nb_journal_class,
        nb_version,
        journal=None,
        logger=None,
        config=None,
    ):
        self.config = config if config is not None else load_config()
        self.nb = nb
        self.device_cf = self.config["device_cf"]
        self.id = nb.id
        self.name = nb.name
        self.visible_name = None
        self.status = nb.status.label
        self.zabbix = zabbix
        self.zabbix_id = None
        self.group_ids = []
        self.nb_api_version = nb_version
        self.zbx_template_names = []
        self.zbx_templates = []
        self.hostgroups = []
        self.hostgroup_type = "dev"
        self.tenant = nb.tenant
        self.config_context = nb.config_context
        self.zbxproxy = None
        self.zabbix_state = 0
        self.zabbix_status = None
        self.zabbix_interfaces = None
        self.journal = journal
        self.nb_journals = nb_journal_class
        self.inventory_mode = -1
        self.inventory = {}
        self.usermacros = []
        self.tags: list[dict[str, Any]] = []
        self.cleanup_source_marker = None
        self.tls = {}
        self.adopted_azure_discovered_host = False
        self.logger = logger if logger else getLogger(__name__)
        self._set_basics()

    def __repr__(self):
        return self.name

    def __str__(self):
        return self.__repr__()

    def _inventory_map(self):
        """Use device inventory maps"""
        return self.config["device_inventory_map"]

    def _usermacro_map(self):
        """Use device inventory maps"""
        return self.config["device_usermacro_map"]

    def _tag_map(self):
        """Use device host tag maps"""
        return self.config["device_tag_map"]

    def set_cleanup_source(self, source_type):
        """Associate this import with the source marker used by opt-in cleanup."""
        if source_type not in {"device", "device-oob", "vm", "ontap-cluster"}:
            raise ValueError(f"Unsupported cleanup source type: {source_type}")
        self.cleanup_source_marker = f"{source_type}:{self.id}"

    @staticmethod
    def _is_cleanup_tag(tag):
        """Return whether a tag is reserved for owned-host cleanup."""
        return tag.get("tag") in {CLEANUP_INSTANCE_TAG, CLEANUP_SOURCE_TAG}

    @staticmethod
    def _tag_values(tags):
        """Normalize tags to the fields accepted by host.create/host.update."""
        return [
            {"tag": tag["tag"], "value": tag["value"]}
            for tag in tags
            if "tag" in tag and "value" in tag
        ]

    def _cleanup_ownership_tags(self):
        """Return this deployment's ownership tags when cleanup is enabled."""
        if (
            not self.config.get("cleanup_deleted_hosts")
            or not self.cleanup_source_marker
        ):
            return []
        return [
            {
                "tag": CLEANUP_INSTANCE_TAG,
                "value": str(self.config.get("cleanup_instance_id", "default")),
            },
            {"tag": CLEANUP_SOURCE_TAG, "value": self.cleanup_source_marker},
        ]

    def _tags_preserving_cleanup_ownership(self, existing_tags, desired_tags):
        """Keep reserved tags even when normal NetBox tag sync is disabled later."""
        protected = [tag for tag in existing_tags if self._is_cleanup_tag(tag)]
        return self._unique_by_key(
            self._tag_values(desired_tags) + self._tag_values(protected),
            lambda tag: (tag["tag"], tag["value"]),
        )

    def _sync_cleanup_ownership(self, host):
        """Backfill ownership tags without taking ownership of ordinary tags."""
        desired = self._unique_by_key(
            self._tag_values(host.get("tags", [])) + self._cleanup_ownership_tags(),
            lambda tag: (tag["tag"], tag["value"]),
        )
        current = self._tag_values(host.get("tags", []))
        if sorted(current, key=lambda tag: (tag["tag"], tag["value"])) == sorted(
            desired, key=lambda tag: (tag["tag"], tag["value"])
        ):
            return
        self.logger.info(
            "Host %s: Adding owned-host cleanup ownership tags.", self.name
        )
        self.update_zabbix_host(tags=desired)

    def _set_basics(self):
        """
        Sets basic information like IP address.
        """
        # A primary IP is required for physical devices. VM subclasses may
        # explicitly opt out when their monitoring does not use an interface.
        if self.nb.primary_ip:
            self.cidr = self.nb.primary_ip.address
            self.ip = self.cidr.split("/")[0]
        elif self._requires_primary_ip():
            e = f"Host {self.name}: no primary IP."
            self.logger.warning(e)
            raise SyncInventoryError(e)
        else:
            self.cidr = None
            self.ip = None

        # Check if device has custom field for ZBX ID
        if self.device_cf in self.nb.custom_fields:
            self.zabbix_id = self.nb.custom_fields[self.device_cf]
        else:
            e = f"Host {self.name}: Custom field {self.device_cf} not present"
            self.logger.error(e)
            raise SyncInventoryError(e)

        self._validate_hostname()

    def _validate_hostname(self):
        """Use a safe technical name when the current host name is unsupported."""
        self.use_visible_name = False
        if not self._hostname_supported_by_zabbix(self.name):
            self.name = f"NETBOX_ID{self.id}"
            self.visible_name = self.nb.name
            self.use_visible_name = True
            self.logger.info(
                "Host %s contains characters unsupported by Zabbix technical host names. "
                "Using %s as name for the NetBox object and %s as visible name in Zabbix.",
                self.visible_name,
                self.name,
                self.visible_name,
            )

    def normalize_hostname(self):
        """Append the configured NetBox domain to an unqualified Zabbix host name."""
        if self.use_visible_name:
            return
        if "." in self.name:
            self.logger.warning(
                "Host %s is already fully qualified; keeping its name unchanged.",
                self.name,
            )
            return

        domain_cf = self.config.get("fqdn_domain_cf", "management_domain")
        custom_fields = getattr(self.nb, "custom_fields", None)
        domain = (
            custom_fields.get(domain_cf) if isinstance(custom_fields, Mapping) else None
        )
        domain = str(domain).strip() if domain is not None else ""
        if not domain:
            self.logger.warning(
                "Host %s: NetBox custom field %s is missing or empty; "
                "syncing without FQDN normalization.",
                self.name,
                domain_cf,
            )
            return

        self.name = f"{self.name}.{domain}"
        self._validate_hostname()

    def _requires_primary_ip(self):
        """Return whether this import type needs an interface address."""
        return True

    @staticmethod
    def _hostname_supported_by_zabbix(hostname: str) -> bool:
        """
        Check whether `host` is valid for Zabbix technical host name.

        Allowed characters: alphanumerics, spaces, dots, dashes, underscores.
        Leading and trailing spaces are not allowed.
        """
        if hostname != hostname.strip():
            return False
        return bool(fullmatch(r"[A-Za-z0-9._ -]+", hostname))

    def set_hostgroup(self, hg_format, nb_site_groups, nb_regions):
        """Set the hostgroup for this device"""
        # Create new Hostgroup instance
        hg = Hostgroup(
            self.hostgroup_type,
            self.nb,
            self.nb_api_version,
            logger=self.logger,
            nested_sitegroup_flag=self.config["traverse_site_groups"],
            nested_region_flag=self.config["traverse_regions"],
            nb_groups=nb_site_groups,
            nb_regions=nb_regions,
        )
        # Generate hostgroup based on hostgroup format
        if isinstance(hg_format, list):
            self.hostgroups = [hg.generate(f) for f in hg_format]
        else:
            self.hostgroups.append(hg.generate(hg_format))
        # Remove duplicates and None values
        self.hostgroups = list(filter(None, list(set(self.hostgroups))))
        if self.hostgroups:
            self.logger.debug(
                "Host %s: Should be member of groups: %s", self.name, self.hostgroups
            )
            return True
        return False

    def set_template(self, prefer_config_context, overrule_custom):
        """Set Template"""
        self.zbx_template_names = None
        # Gather templates ONLY from the device specific context
        if prefer_config_context:
            try:
                self.zbx_template_names = self.get_templates_context()
            except TemplateError as e:
                self.logger.warning(e)
            return True
        # Gather templates from the custom field but overrule
        # them should there be any device specific templates
        if overrule_custom:
            try:  # noqa: SIM105
                self.zbx_template_names = self.get_templates_context()
            except TemplateError:
                pass
            if not self.zbx_template_names:
                self.zbx_template_names = self.get_templates_cf()
            return True
        # Gather templates ONLY from the custom field
        self.zbx_template_names = self.get_templates_cf()
        return True

    def get_templates_cf(self):
        """Get template from custom field"""
        # Get Zabbix templates from the device type
        device_type_cfs = self.nb.device_type.custom_fields
        # Check if the ZBX Template CF is present
        if self.config["template_cf"] in device_type_cfs:
            # Set value to template
            return [device_type_cfs[self.config["template_cf"]]]
        # Custom field not found, return error
        e = (
            f"Custom field {self.config['template_cf']} not "
            f"found for {self.nb.device_type.manufacturer.name}"
            f" - {self.nb.device_type.display}."
        )
        self.logger.warning(e)
        raise TemplateError(e)

    def get_templates_context(self):
        """Get Zabbix templates from the device context"""
        if "zabbix" not in self.config_context:
            e = (
                f"Host {self.name}: Key 'zabbix' not found in config "
                "context for template lookup"
            )
            raise TemplateError(e)
        if "templates" not in self.config_context["zabbix"]:
            e = (
                f"Host {self.name}: Key 'templates' not found in config "
                "context 'zabbix' for template lookup"
            )
            raise TemplateError(e)
        # Check if format is list or string.
        if isinstance(self.config_context["zabbix"]["templates"], str):
            return [self.config_context["zabbix"]["templates"]]
        return self.config_context["zabbix"]["templates"]

    def set_inventory(self, nbdevice):
        """Set host inventory"""
        # Set inventory mode. Default is disabled (see class init function).
        if self.config["inventory_mode"] == "disabled":
            if self.config["inventory_sync"]:
                self.logger.error(
                    "Host %s: Unable to map NetBox inventory to Zabbix."
                    "Inventory sync is enabled in  config but inventory mode is disabled",
                    self.name,
                )
            return True
        if self.config["inventory_mode"] == "manual":
            self.inventory_mode = 0
        elif self.config["inventory_mode"] == "automatic":
            self.inventory_mode = 1
        else:
            self.logger.error(
                "Host %s: Specified value for inventory mode in config is not valid. Got value %s",
                self.name,
                self.config["inventory_mode"],
            )
            return False
        self.inventory = {}
        if self.config["inventory_sync"] and self.inventory_mode in [0, 1]:
            self.logger.debug("Host %s: Starting inventory mapper.", self.name)
            self.inventory = field_mapper(
                self.name, self._inventory_map(), nbdevice, self.logger
            )
            inventory_type = self.inventory.get("type")
            if (
                isinstance(inventory_type, str)
                and len(inventory_type) > ZABBIX_INVENTORY_TYPE_MAX_LENGTH
            ):
                self.logger.warning(
                    "Host %s: Truncating Zabbix inventory type from %s to %s characters.",
                    self.name,
                    len(inventory_type),
                    ZABBIX_INVENTORY_TYPE_MAX_LENGTH,
                )
                self.inventory["type"] = inventory_type[
                    :ZABBIX_INVENTORY_TYPE_MAX_LENGTH
                ]
            self.logger.debug(
                "Host %s: Resolved inventory: %s", self.name, self.inventory
            )
        return True

    def is_cluster(self):
        """
        Checks if device is part of cluster.
        """
        return bool(self.nb.virtual_chassis)

    def get_cluster_master(self):
        """
        Returns chassis master ID.
        """
        if not self.is_cluster():
            e = (
                f"Unable to proces {self.name} for cluster calculation: "
                f"not part of a cluster."
            )
            self.logger.info(e)
            raise SyncInventoryError(e)
        if not self.nb.virtual_chassis.master:
            e = (
                f"{self.name} is part of a NetBox virtual chassis which does "
                "not have a master configured. Skipping for this reason."
            )
            self.logger.warning(e)
            raise SyncInventoryError(e)
        return self.nb.virtual_chassis.master.id

    def promote_primary_device(self):
        """
        If device is Primary in cluster,
        promote device name to the cluster name.
        Returns True if succesfull, returns False if device is secondary.
        """
        masterid = self.get_cluster_master()
        if masterid == self.id:
            self.logger.info(
                "Host %s is primary cluster member. Modifying hostname from %s to %s.",
                self.name,
                self.name,
                self.nb.virtual_chassis.name,
            )
            self.name = self.nb.virtual_chassis.name
            return True
        self.logger.info("Host %s is non-primary cluster member.", self.name)
        return False

    def zbx_template_prepper(self, templates):
        """
        Returns Zabbix template IDs
        INPUT: list of templates from Zabbix
        OUTPUT: True
        """
        # Check if there are templates defined
        if not self.zbx_template_names:
            e = f"Host {self.name}: No templates found"
            self.logger.warning(e)
            raise SyncInventoryError()
        # Set variable to empty list
        self.zbx_templates = []
        # Go through all templates definded in NetBox
        for nb_template in self.zbx_template_names:
            template_match = False
            # Go through all templates found in Zabbix
            for zbx_template in templates:
                # If the template names match
                if zbx_template["name"] == nb_template:
                    # Set match variable to true, add template details
                    # to class variable and return debug log
                    template_match = True
                    self.zbx_templates.append(
                        {
                            "templateid": zbx_template["templateid"],
                            "name": zbx_template["name"],
                        }
                    )
                    e = (
                        f"Host {self.name}: Found template '{zbx_template['name']}' "
                        f"(ID:{zbx_template['templateid']})"
                    )
                    self.logger.debug(e)
            # Return error should the template not be found in Zabbix
            if not template_match:
                e = (
                    f"Unable to find template {nb_template} "
                    f"for host {self.name} in Zabbix. Skipping host..."
                )
                self.logger.warning(e)
                raise SyncInventoryError(e)

    def set_zbx_groupid(self, groups):
        """
        Sets Zabbix group ID as instance variable
        INPUT: list of hostgroups
        OUTPUT: True / False
        """
        # Go through all groups
        for hg in self.hostgroups:
            for group in groups:
                if group["name"] == hg:
                    self.group_ids.append({"groupid": group["groupid"]})
                    e = (
                        f"Host {self.name}: Matched group "
                        f'"{group["name"]}" (ID:{group["groupid"]})'
                    )
                    self.logger.debug(e)
        return len(self.group_ids) == len(self.hostgroups)

    def cleanup(self):
        """
        Removes device from external resources.
        Resets custom fields in NetBox.
        """
        if self.zabbix_id:
            try:
                # Check if the Zabbix host exists in Zabbix
                zbx_host = bool(
                    self.zabbix.host.get(filter={"hostid": self.zabbix_id}, output=[])
                )
                e = (
                    f"Host {self.name}: was already deleted from Zabbix."
                    " Removed link in NetBox."
                )
                if zbx_host:
                    # Delete host should it exists
                    self.zabbix.host.delete(self.zabbix_id)
                    e = f"Host {self.name}: Deleted host from Zabbix."
                self._zeroize_cf()
                self.logger.info(e)
                self.create_journal_entry("warning", "Deleted host from Zabbix")
            except APIRequestError as e:
                message = f"Zabbix returned the following error: {e}."
                self.logger.error(message)
                raise SyncExternalError(message) from e

    def _zeroize_cf(self):
        """Sets the hostID custom field in NetBox to zero,
        effectively destroying the link"""
        self.nb.custom_fields[self.device_cf] = None
        self.nb.save()

    def _zabbix_hostname_exists(self):
        """
        Checks if hostname exists in Zabbix.
        """
        # Validate the hostname or visible name field
        if not self.use_visible_name:
            zbx_filter = {"host": self.name}
        else:
            zbx_filter = {"name": self.visible_name}
        host = self.zabbix.host.get(filter=zbx_filter, output=[])
        return bool(host)

    def _platform_name(self):
        """Return normalized platform name for scope matching."""
        platform = getattr(self.nb, "platform", None)
        if not platform:
            return ""
        if hasattr(platform, "name") and platform.name:
            return str(platform.name)
        return str(platform)

    def _custom_field_value(self, field_name):
        """Return a custom field value from the NetBox object."""
        if not field_name:
            return None
        custom_fields = getattr(self.nb, "custom_fields", None)
        if not custom_fields:
            return None
        value = custom_fields.get(field_name)
        if isinstance(value, dict):
            return value.get("value") or value.get("name") or value.get("display")
        return value

    def _azure_resource_id(self):
        """Return the configured NetBox Azure resource ID value, if present."""
        value = self._custom_field_value(self.config.get("azure_vm_resource_id_cf"))
        return str(value).lower() if value else ""

    def _object_name(self, obj):
        """Return a lowercase displayable name for a NetBox related object."""
        if not obj:
            return ""
        if hasattr(obj, "name") and obj.name:
            return str(obj.name).lower()
        if hasattr(obj, "slug") and obj.slug:
            return str(obj.slug).lower()
        return str(obj).lower()

    def _config_list(self, key):
        """Return a config value as a list."""
        value = self.config.get(key, [])
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value or []

    def _is_azure_vm_candidate(self):
        """Return whether this NetBox VM appears to represent an Azure VM."""
        if self.hostgroup_type != "vm":
            return False
        if self._azure_resource_id():
            return True
        keywords = [
            str(keyword).lower()
            for keyword in self._config_list("azure_vm_platform_keywords")
            if str(keyword)
        ]
        if not keywords:
            return False
        candidates = [self._platform_name().lower()]
        cluster = getattr(self.nb, "cluster", None)
        candidates.append(self._object_name(cluster))
        candidates.append(self._object_name(getattr(cluster, "type", None)))
        candidates.append(self._object_name(getattr(self.nb, "tenant", None)))
        for tag in getattr(self.nb, "tags", []) or []:
            candidates.append(self._object_name(tag))
        return any(
            keyword in candidate
            for keyword in keywords
            for candidate in candidates
            if candidate
        )

    def _is_adoption_candidate(self):
        """Check whether this object should be considered for host adoption."""
        if not self.config.get("adopt_existing_hosts"):
            return False
        if self.hostgroup_type == "vm" and not self.config.get("adopt_for_vms", True):
            return False
        scope = str(self.config.get("adopt_scope", "esxi")).lower()
        if scope in ("", "all"):
            return True
        if scope == "esxi":
            return "esxi" in self._platform_name().lower()
        if scope in ("azure", "cloud"):
            return self._is_azure_vm_candidate()
        self.logger.warning(
            "Host %s: Unsupported adopt_scope '%s'. Skipping adoption.",
            self.name,
            scope,
        )
        return False

    def _uses_esxi_dual_host_mode(self):
        """Return whether this device keeps dedicated and LLD Zabbix host links."""
        return bool(
            self.hostgroup_type == "dev"
            and self.config.get("esxi_adopted_hostid_cf")
            and self._is_adoption_candidate()
            and "esxi" in self._platform_name().lower()
        )

    def _uses_vmware_vm_dual_host_mode(self):
        """Return whether this VM keeps dedicated and VMware LLD host links."""
        return bool(
            self.hostgroup_type == "vm"
            and self.config.get("vmware_vm_adopted_hostid_cf")
            and self.config.get("adopt_existing_hosts")
            and self.config.get("adopt_for_vms", True)
            and "vmware" in self._platform_name().lower()
        )

    def _is_vmware_vm(self):
        """Return whether this import represents a VM on a VMware platform."""
        return self.hostgroup_type == "vm" and "vmware" in self._platform_name().lower()

    def _uses_lld_dual_host_mode(self):
        """Return whether this object manages a dedicated host and linked LLD host."""
        return self._uses_esxi_dual_host_mode() or self._uses_vmware_vm_dual_host_mode()

    def _zabbix_name_lookup_candidates(self):
        """Build host.get filters for name-based lookup."""
        lookups = [{"host": self.name}]
        if self.use_visible_name:
            lookups.append({"name": self.visible_name})
        else:
            lookups.append({"name": self.name})
        return list(dict.fromkeys(tuple(sorted(i.items())) for i in lookups))

    @staticmethod
    def _short_hostname(value):
        """Return the case-insensitive short name for a hostname value."""
        if value is None:
            return ""
        return str(value).split(".", 1)[0].lower()

    def _zabbix_normalized_name_lookup_candidates(self):
        """Build field, original-name, and short-name tuples for adoption searches."""
        visible_name = self.visible_name if self.use_visible_name else self.name
        return list(
            dict.fromkeys(
                (field, str(value), self._short_hostname(value))
                for field, value in (("host", self.name), ("name", visible_name))
                if value
            )
        )

    def _host_azure_resource_id_matches(self, host):
        """Return whether the Zabbix host metadata contains the configured Azure ID."""
        resource_id = self._azure_resource_id()
        if not resource_id:
            return False
        for value in (host.get("host"), host.get("name")):
            if value and str(value).lower() == resource_id:
                return True
        inventory = host.get("inventory") or {}
        if isinstance(inventory, dict):
            for value in inventory.values():
                if value and str(value).lower() == resource_id:
                    return True
        for macro in host.get("macros", []) or []:
            if str(macro.get("value", "")).lower() == resource_id:
                return True
        for tag in host.get("tags", []) or []:
            if str(tag.get("value", "")).lower() == resource_id:
                return True
        return False

    def _host_has_azure_discovered_template(self, host):
        """Return whether a matched Zabbix host has an Azure VM template link."""
        template_names = {
            str(name).lower()
            for name in self._config_list("azure_vm_discovered_templates")
            if str(name)
        }
        if not template_names:
            return False
        for template in host.get("parentTemplates", []) or []:
            name = str(template.get("name", "")).lower()
            if any(template_name in name for template_name in template_names):
                return True
        return False

    def _find_unique_existing_zabbix_host(
        self, exclude_hostid=None, *, exclude_discovered=False
    ):
        """Return one existing host matched by name, or ``None`` when unsafe."""
        matches = {}
        try:
            for lookup_tuple in self._zabbix_name_lookup_candidates():
                lookup = dict(lookup_tuple)
                for host in self.zabbix.host.get(
                    filter=lookup,
                    output=["hostid", "host", "name", "flags"],
                    selectParentTemplates=["templateid", "name"],
                    selectInventory="extend",
                    selectMacros=["macro", "value"],
                    selectTags=["tag", "value"],
                ):
                    if "hostid" in host:
                        matches[host["hostid"]] = host
            if self.config.get("adopt_fqdn_normalization"):
                for (
                    field,
                    source_name,
                    short_name,
                ) in self._zabbix_normalized_name_lookup_candidates():
                    for host in self.zabbix.host.get(
                        search={field: short_name},
                        output=["hostid", "host", "name", "flags"],
                        selectParentTemplates=["templateid", "name"],
                        selectInventory="extend",
                        selectMacros=["macro", "value"],
                        selectTags=["tag", "value"],
                    ):
                        if (
                            "hostid" in host
                            and self._short_hostname(host.get(field)) == short_name
                            and ("." in source_name or "." in str(host.get(field, "")))
                        ):
                            matches[host["hostid"]] = host
        except APIRequestError as e:
            message = f"Host {self.name}: Adoption lookup failed. Zabbix returned {e}."
            self.logger.error(message)
            raise SyncExternalError(message) from e

        if exclude_hostid is not None:
            matches.pop(str(exclude_hostid), None)
        if exclude_discovered:
            matches = {
                hostid: host
                for hostid, host in matches.items()
                if not self._is_discovered_host(host)
            }
        if not matches:
            self.logger.debug(
                "Host %s: No existing Zabbix host matched for adoption.", self.name
            )
            return None
        resource_matches = {
            hostid: host
            for hostid, host in matches.items()
            if self._host_azure_resource_id_matches(host)
        }
        if resource_matches:
            matches = resource_matches
        if len(matches) > 1:
            self.logger.warning(
                "Host %s: Multiple Zabbix hosts matched by name. Skipping adoption.",
                self.name,
            )
            return None
        return next(iter(matches.values()))

    def _save_zabbix_hostid_custom_field(self, field_name, hostid, link_name):
        """Persist a Zabbix host ID in NetBox, restoring local state on failure."""
        previous_custom_field = self.nb.custom_fields.get(field_name)
        self.nb.custom_fields[field_name] = hostid
        try:
            self.nb.save()
        except NetboxRequestError as e:
            self.nb.custom_fields[field_name] = previous_custom_field
            message = (
                f"Host {self.name}: Unable to save the {link_name} Zabbix host ID in "
                f"NetBox. NetBox returned {e}. Check the NetBox object data for "
                "validation conflicts, such as platform and device type "
                "compatibility."
            )
            self.logger.error(message)
            raise SyncExternalError(message) from e

    def repair_vmware_vm_lld_host_link(self):
        """Move a linked VMware LLD host ID out of the primary host field."""
        if not self.zabbix_id or not self._is_vmware_vm():
            return None

        try:
            hosts = self.zabbix.host.get(
                filter={"hostid": self.zabbix_id},
                output=["hostid", "flags"],
            )
        except APIRequestError as e:
            message = (
                f"Host {self.name}: Unable to verify primary Zabbix host "
                f"{self.zabbix_id} before VMware LLD link repair. "
                f"Zabbix returned {e}."
            )
            self.logger.error(message)
            raise SyncExternalError(message) from e

        if len(hosts) != 1 or not self._is_discovered_host(hosts[0]):
            return None

        hostid = int(self.zabbix_id)
        field_name = self.config.get("vmware_vm_adopted_hostid_cf")
        custom_fields = self.nb.custom_fields
        if (
            not self._uses_vmware_vm_dual_host_mode()
            or not field_name
            or field_name == self.device_cf
            or field_name not in custom_fields
        ):
            self.logger.warning(
                "Host %s: Primary Zabbix host %s is VMware LLD-created; "
                "dual-host adoption is not configured correctly. Leaving the "
                "NetBox links unchanged and skipping reconciliation.",
                self.name,
                hostid,
            )
            return False

        existing_lld_hostid = custom_fields[field_name]
        if existing_lld_hostid not in (None, "") and str(existing_lld_hostid) != str(
            hostid
        ):
            self.logger.warning(
                "Host %s: VMware LLD host %s cannot be moved to custom field %s "
                "because it already contains host ID %s. Leaving the NetBox "
                "links unchanged and skipping reconciliation.",
                self.name,
                hostid,
                field_name,
                existing_lld_hostid,
            )
            return False

        previous_primary_hostid = custom_fields[self.device_cf]
        previous_lld_hostid = existing_lld_hostid
        custom_fields[field_name] = hostid
        custom_fields[self.device_cf] = None
        try:
            self.nb.save()
        except NetboxRequestError as e:
            custom_fields[self.device_cf] = previous_primary_hostid
            custom_fields[field_name] = previous_lld_hostid
            message = (
                f"Host {self.name}: Unable to move VMware LLD host ID {hostid} "
                f"to NetBox custom field {field_name}. NetBox returned {e}."
            )
            self.logger.error(message)
            raise SyncExternalError(message) from e

        self.zabbix_id = None
        self.logger.info(
            "Host %s: Moved VMware LLD host ID %s to custom field %s; "
            "a dedicated primary host will be synchronized.",
            self.name,
            hostid,
            field_name,
        )
        return True

    def adopt_existing_zabbix_host(self):
        """
        Link to an existing Zabbix host by name if this object is in adoption scope.

        Returns True when adoption succeeded, otherwise False.
        """
        if self.zabbix_id or not self._is_adoption_candidate():
            return False
        dual_host_mode = self._uses_lld_dual_host_mode()
        host = self._find_unique_existing_zabbix_host(exclude_discovered=dual_host_mode)
        if not host:
            return False
        if self._is_discovered_host(host) and (dual_host_mode or self._is_vmware_vm()):
            self.logger.warning(
                "Host %s: Matched Zabbix host %s is LLD-created; "
                "it will not be adopted as the primary managed host.",
                self.name,
                host["hostid"],
            )
            return False

        previous_zabbix_id = self.zabbix_id
        self.zabbix_id = int(host["hostid"])
        try:
            self._save_zabbix_hostid_custom_field(
                self.device_cf, self.zabbix_id, "adopted"
            )
        except SyncExternalError:
            self.zabbix_id = previous_zabbix_id
            raise
        if self._host_has_azure_discovered_template(host):
            self.adopted_azure_discovered_host = True
        self.logger.info(
            "Host %s: Adopted existing Zabbix host by name. (ID:%s)",
            self.name,
            self.zabbix_id,
        )
        return True

    def _get_adopted_lld_host_field(self):
        """Return the configured LLD host-ID field or raise for a bad NetBox object."""
        if self._uses_esxi_dual_host_mode():
            field_name = self.config.get("esxi_adopted_hostid_cf")
            host_type = "vSphere"
        else:
            field_name = self.config.get("vmware_vm_adopted_hostid_cf")
            host_type = "VMware VM Discovery"
        if field_name not in self.nb.custom_fields:
            message = (
                f"Host {self.name}: Custom field {field_name} not present for "
                f"adopted {host_type} LLD host ID."
            )
            self.logger.error(message)
            raise SyncInventoryError(message)
        return field_name

    def _get_adopted_lld_host(self, hostid):
        """Retrieve the discovery-owned host used for metadata synchronization."""
        try:
            hosts = self.zabbix.host.get(
                filter={"hostid": hostid},
                output=["hostid", "flags"],
                selectMacros=[
                    "hostmacroid",
                    "macro",
                    "value",
                    "type",
                    "description",
                    "automatic",
                ],
                selectTags=["tag", "value", "automatic"],
            )
        except APIRequestError as e:
            message = (
                f"Host {self.name}: Unable to retrieve adopted LLD host "
                f"{hostid}. Zabbix returned {e}."
            )
            self.logger.error(message)
            raise SyncExternalError(message) from e
        if len(hosts) != 1:
            self.logger.warning(
                "Host %s: Adopted LLD host ID %s no longer resolves uniquely.",
                self.name,
                hostid,
            )
            return None
        host = hosts[0]
        if not self._is_discovered_host(host):
            self.logger.warning(
                "Host %s: Zabbix host ID %s is not LLD-created; skipping adopted "
                "LLD metadata sync.",
                self.name,
                hostid,
            )
            return None
        return host

    def _update_zabbix_host_by_id(self, hostid, **kwargs):
        """Update an explicit Zabbix host without changing this object's main link."""
        try:
            self.zabbix.host.update(hostid=hostid, **kwargs)
        except APIRequestError as e:
            message = (
                f"Host {self.name}: Unable to update Zabbix host {hostid}. "
                f"Zabbix returned the following error: {e}."
            )
            self.logger.error(message)
            raise SyncExternalError(message) from e
        self.logger.info(
            "Host %s: updated Zabbix host %s with data %s.",
            self.name,
            hostid,
            sanatize_log_output(kwargs),
        )
        self.create_journal_entry("info", "Updated host in Zabbix with latest NB data.")

    def sync_adopted_lld_host(self):
        """Adopt and safely enrich this object's separate LLD host."""
        if not self._uses_lld_dual_host_mode():
            return False
        field_name = self._get_adopted_lld_host_field()
        adopted_hostid = self.nb.custom_fields[field_name]
        if adopted_hostid:
            host = self._get_adopted_lld_host(adopted_hostid)
        else:
            match = self._find_unique_existing_zabbix_host(
                exclude_hostid=self.zabbix_id
            )
            if not match:
                return False
            if not self._is_discovered_host(match):
                self.logger.warning(
                    "Host %s: Matched Zabbix host %s is not LLD-created; skipping "
                    "LLD adoption.",
                    self.name,
                    match["hostid"],
                )
                return False
            adopted_hostid = int(match["hostid"])
            self._save_zabbix_hostid_custom_field(
                field_name, adopted_hostid, "adopted LLD"
            )
            host = self._get_adopted_lld_host(adopted_hostid)
        if not host:
            return False

        self._sync_discovered_macros(host, hostid=adopted_hostid)
        self._sync_discovered_tags(host, hostid=adopted_hostid)
        return True

    def sync_adopted_esxi_lld_host(self):
        """Adopt and safely enrich the separate vSphere LLD host for an ESXi device."""
        return self.sync_adopted_lld_host()

    @staticmethod
    def _is_discovered_host(host):
        """Return whether Zabbix created this host from an LLD host prototype."""
        return str(host.get("flags", "0")) == "4"

    def _discovered_host_group_ids(self, host):
        """Return host groups owned by LLD, or ``None`` when they cannot be read."""
        group_dictname = "hostgroups"
        if str(self.zabbix.version).startswith(("6", "5")):
            group_dictname = "groups"

        protected = {
            str(group["groupid"])
            for group in host.get(group_dictname, [])
            if str(group.get("flags", "0")) == "4"
        }
        discovery = host.get("hostDiscovery") or {}
        prototype_id = discovery.get("parent_hostid")
        if not prototype_id:
            return protected

        try:
            prototypes = self.zabbix.hostprototype.get(
                hostids=prototype_id,
                output=["hostid"],
                selectGroupLinks=["groupid"],
            )
        except APIRequestError as e:
            self.logger.warning(
                "Host %s: Unable to read LLD host prototype groups; preserving all "
                "existing host groups. Zabbix returned %s.",
                self.name,
                e,
            )
            return None

        for prototype in prototypes:
            protected.update(
                str(group["groupid"]) for group in prototype.get("groupLinks", [])
            )
        return protected

    @staticmethod
    def _unique_by_key(items, key):
        """Return entries in order, keeping the first entry for each key."""
        unique_items = []
        seen = set()
        for item in items:
            item_key = key(item)
            if item_key not in seen:
                unique_items.append(item)
                seen.add(item_key)
        return unique_items

    def _sync_discovered_templates(self, host):
        """Sync manual templates without unlinking templates assigned by LLD."""
        current = host["parentTemplates"]
        lld_templates = [
            {"templateid": template["templateid"]}
            for template in current
            if str(template.get("link_type", "0")) == "1"
        ]
        desired = self._unique_by_key(
            lld_templates
            + [
                {"templateid": template["templateid"]}
                for template in self.zbx_templates
            ],
            lambda template: str(template["templateid"]),
        )
        current_ids = {str(template["templateid"]) for template in current}
        desired_ids = {str(template["templateid"]) for template in desired}
        if current_ids == desired_ids:
            self.logger.debug("Host %s: Template(s) in-sync.", self.name)
            return

        templates_clear = [
            {"templateid": template["templateid"]}
            for template in current
            if str(template.get("link_type", "0")) != "1"
            and str(template["templateid"]) not in desired_ids
        ]
        self.logger.info("Host %s: Template(s) OUT of sync.", self.name)
        self.update_zabbix_host(templates_clear=templates_clear, templates=desired)

    def _sync_discovered_groups(self, host):
        """Sync manual groups without unlinking groups assigned by LLD."""
        protected_ids = self._discovered_host_group_ids(host)
        if protected_ids is None:
            return

        group_dictname = "hostgroups"
        if str(self.zabbix.version).startswith(("6", "5")):
            group_dictname = "groups"
        current = host[group_dictname]
        desired = self._unique_by_key(
            [
                {"groupid": group["groupid"]}
                for group in current
                if str(group["groupid"]) in protected_ids
            ]
            + self.group_ids,
            lambda group: str(group["groupid"]),
        )
        current_ids = {str(group["groupid"]) for group in current}
        desired_ids = {str(group["groupid"]) for group in desired}
        if current_ids == desired_ids:
            self.logger.debug("Host %s: Hostgroups in-sync.", self.name)
            return

        self.logger.info("Host %s: Hostgroups OUT of sync.", self.name)
        self.update_zabbix_host(groups=desired)

    def _sync_discovered_macros(self, host, hostid=None):
        """Sync manual macros without deleting protected LLD-created macros."""
        if not self.config["usermacro_sync"]:
            return

        automatic = [
            macro for macro in host["macros"] if str(macro.get("automatic", "0")) == "1"
        ]
        automatic_names = {macro["macro"] for macro in automatic}

        netbox_macros = []
        for macro in self.usermacros:
            if macro["macro"] in automatic_names:
                self.logger.warning(
                    "Host %s: Preserving LLD macro %s instead of replacing it from NetBox.",
                    self.name,
                    macro["macro"],
                )
                continue
            netbox_macros.append(macro)

        sync_mode = str(self.config["usermacro_sync"]).lower()
        if sync_mode == "partial":
            manual = [
                macro
                for macro in host["macros"]
                if str(macro.get("automatic", "0")) != "1"
            ]
            netbox_macros = ZabbixUsermacros.merge_partial(manual, netbox_macros)

        desired_macros = automatic + netbox_macros
        compare_macros = deepcopy(desired_macros)
        if sync_mode != "full":
            for macro in compare_macros:
                if macro.get("type") == str(1):
                    macro.pop("value", None)

        def comparable(macro):
            return {
                key: str(macro.get(key, "0" if key == "automatic" else ""))
                for key in ("macro", "value", "type", "description", "automatic")
            }

        current = sorted(
            (comparable(macro) for macro in host["macros"]), key=itemgetter("macro")
        )
        expected = sorted(
            (comparable(macro) for macro in compare_macros), key=itemgetter("macro")
        )
        if current == expected:
            self.logger.debug("Host %s: Usermacros in-sync.", self.name)
            return

        self.logger.info("Host %s: Usermacros OUT of sync.", self.name)
        update_macros = [
            {key: value for key, value in macro.items() if key != "automatic"}
            for macro in desired_macros
        ]
        self._update_zabbix_host_by_id(hostid or self.zabbix_id, macros=update_macros)

    def _sync_discovered_tags(self, host, hostid=None):
        """Sync manual tags without deleting tags created by LLD."""
        if not self.config["tag_sync"]:
            return

        automatic = [
            {"tag": tag["tag"], "value": tag["value"]}
            for tag in host["tags"]
            if str(tag.get("automatic", "0")) == "1"
        ]
        desired = self._unique_by_key(
            automatic
            + self._tags_preserving_cleanup_ownership(host["tags"], self.tags),
            lambda tag: (tag["tag"], tag["value"]),
        )
        current = sorted(
            ({"tag": tag["tag"], "value": tag["value"]} for tag in host["tags"]),
            key=lambda tag: (tag["tag"], tag["value"]),
        )
        expected = sorted(desired, key=lambda tag: (tag["tag"], tag["value"]))
        if current == expected:
            self.logger.debug("Host %s: Tags in-sync.", self.name)
            return

        self.logger.info("Host %s: Tags OUT of sync.", self.name)
        self._update_zabbix_host_by_id(hostid or self.zabbix_id, tags=desired)

    def _sync_discovered_host(self, host, sync_hostgroups=False):
        """Reconcile the NetBox-managed parts of an LLD-created host."""
        self.logger.info(
            "Host %s: Preserving fields managed by Zabbix low-level discovery.",
            self.name,
        )
        self._sync_discovered_templates(host)
        if sync_hostgroups:
            self._sync_discovered_groups(host)
        self._sync_discovered_macros(host)
        self._sync_discovered_tags(host)

    @staticmethod
    def _hostgroups_in_sync(current_groups, desired_groups):
        """Compare host groups by their managed Zabbix IDs."""
        return sorted(str(group["groupid"]) for group in current_groups) == sorted(
            str(group["groupid"]) for group in desired_groups
        )

    @staticmethod
    def _macros_in_sync(current_macros, desired_macros):
        """Compare managed macro fields while ignoring Zabbix API metadata."""

        def comparable(macro):
            result = {
                key: str(macro.get(key, "")) for key in ("macro", "type", "description")
            }
            if "value" in macro:
                result["value"] = str(macro["value"])
            return result

        return sorted(
            (comparable(macro) for macro in current_macros),
            key=itemgetter("macro"),
        ) == sorted(
            (comparable(macro) for macro in desired_macros),
            key=itemgetter("macro"),
        )

    @staticmethod
    def _interface_value(interface, key):
        """Return a normalized scalar for an interface comparison."""
        value = interface.get(key)
        return "" if value is None else str(value)

    @classmethod
    def _interfaces_have_same_endpoint(cls, current, desired):
        """Compare stable endpoint fields when matching an existing interface."""
        return all(
            cls._interface_value(current, key) == cls._interface_value(desired, key)
            for key in ("type", "ip", "dns", "useip", "port")
        )

    @classmethod
    def _interfaces_have_same_address(cls, current, desired):
        """Compare address fields when identifying an interrupted type transition."""
        return all(
            cls._interface_value(current, key) == cls._interface_value(desired, key)
            for key in ("ip", "dns", "useip")
        )

    @classmethod
    def _interface_matches_desired(cls, current, desired):
        """Compare the NetBox-managed settings for one interface."""
        for key, value in desired.items():
            if key == "details":
                current_details = current.get("details") or {}
                if not isinstance(current_details, Mapping) or any(
                    cls._interface_value(current_details, detail_key)
                    != cls._interface_value(value, detail_key)
                    for detail_key in value
                ):
                    return False
            elif cls._interface_value(current, key) != cls._interface_value(
                desired, key
            ):
                return False
        return True

    @staticmethod
    def _interface_update_payload(interface):
        """Return only writable interface fields for a retained interface."""
        fields = (
            "interfaceid",
            "type",
            "main",
            "useip",
            "ip",
            "dns",
            "port",
            "details",
        )
        return {key: interface[key] for key in fields if key in interface}

    def _refresh_zabbix_interfaces(self):
        """Fetch the current interface IDs after adding transition interfaces."""
        try:
            hosts = self.zabbix.host.get(
                filter={"hostid": self.zabbix_id},
                selectInterfaces=[
                    "type",
                    "ip",
                    "dns",
                    "port",
                    "useip",
                    "main",
                    "details",
                    "interfaceid",
                    "available",
                ],
            )
        except APIRequestError as error:
            message = (
                f"Host {self.name}: Unable to refresh Zabbix interfaces. "
                f"Zabbix returned the following error: {error}."
            )
            self.logger.error(message)
            raise SyncExternalError(message) from error
        if (
            not isinstance(hosts, list)
            or len(hosts) != 1
            or not isinstance(hosts[0], Mapping)
            or not isinstance(hosts[0].get("interfaces"), list)
        ):
            message = (
                f"Host {self.name}: Zabbix returned an invalid interface response "
                "after adding transition interfaces."
            )
            self.logger.error(message)
            raise SyncExternalError(message)
        return hosts[0]["interfaces"]

    def _add_missing_interface_types(self, host, desired_interfaces):
        """Add new interface types without removing interfaces used by old templates."""
        current_interfaces = host.get("interfaces") or []
        current_types = {
            self._interface_value(interface, "type") for interface in current_interfaces
        }
        missing_types = {
            self._interface_value(interface, "type")
            for interface in desired_interfaces
            if self._interface_value(interface, "type") not in current_types
        }
        if not missing_types:
            return False

        current_payload = []
        for interface in current_interfaces:
            if interface.get("interfaceid") is None:
                message = (
                    f"Host {self.name}: Cannot stage an interface type change because "
                    "Zabbix did not return an ID for an existing interface."
                )
                self.logger.error(message)
                raise SyncInventoryError(message)
            current_payload.append(self._interface_update_payload(interface))

        desired_payload = [
            {**interface, "hostid": self.zabbix_id}
            for interface in desired_interfaces
            if self._interface_value(interface, "type") in missing_types
        ]
        self.update_zabbix_host(interfaces=current_payload + desired_payload)
        host["interfaces"] = self._refresh_zabbix_interfaces()
        self.zabbix_interfaces = host["interfaces"]
        return True

    def _move_items_to_interface(self, old_interface_id, new_interface_id):
        """Move linked items before removing the interface they reference."""
        try:
            items = self.zabbix.item.get(
                hostids=[self.zabbix_id],
                interfaceids=[old_interface_id],
                output=["itemid"],
            )
        except APIRequestError as error:
            message = (
                f"Host {self.name}: Unable to retrieve items linked to interface "
                f"{old_interface_id} before changing interface type. "
                f"Zabbix returned the following error: {error}."
            )
            self.logger.error(message)
            raise SyncExternalError(message) from error

        if not isinstance(items, list) or any(
            not isinstance(item, Mapping) or item.get("itemid") is None
            for item in items
        ):
            message = (
                f"Host {self.name}: Zabbix returned an invalid item response while "
                f"preparing to remove interface {old_interface_id}."
            )
            self.logger.error(message)
            raise SyncExternalError(message)

        if not items:
            return

        updates = [
            {"itemid": item["itemid"], "interfaceid": new_interface_id}
            for item in items
        ]
        try:
            result = self.zabbix.item.update(updates)
        except APIRequestError as error:
            message = (
                f"Host {self.name}: Unable to move items from interface "
                f"{old_interface_id} to {new_interface_id} before removing the old "
                f"interface. Zabbix returned the following error: {error}."
            )
            self.logger.error(message)
            raise SyncExternalError(message) from error

        expected_ids = {str(item["itemid"]) for item in items}
        updated_ids = (
            {str(itemid) for itemid in result["itemids"]}
            if isinstance(result, Mapping) and isinstance(result.get("itemids"), list)
            else set()
        )
        if updated_ids != expected_ids:
            message = (
                f"Host {self.name}: Zabbix did not confirm moving all items from "
                f"interface {old_interface_id}; the old interface was retained."
            )
            self.logger.error(message)
            raise SyncExternalError(message)

    def _replace_interface_type(
        self, host, old_interface, desired_interface, target_interface_index=None
    ):
        """Stage a replacement and move item references before removing the old type."""
        old_interface_id = old_interface.get("interfaceid")
        if old_interface_id is None:
            message = (
                f"Host {self.name}: Cannot replace an interface type because "
                "Zabbix did not return an ID for the existing interface."
            )
            self.logger.error(message)
            raise SyncInventoryError(message)

        current_interfaces = host.get("interfaces") or []
        current_ids = {
            str(interface["interfaceid"])
            for interface in current_interfaces
            if interface.get("interfaceid") is not None
        }

        if target_interface_index is not None:
            target_interface = current_interfaces[target_interface_index]
            new_interface_id = target_interface.get("interfaceid")
        else:
            self._add_missing_interface_types(host, [desired_interface])
            added_interfaces = [
                interface
                for interface in host.get("interfaces") or []
                if self._interface_value(interface, "type")
                == self._interface_value(desired_interface, "type")
                and interface.get("interfaceid") is not None
                and str(interface["interfaceid"]) not in current_ids
            ]
            if len(added_interfaces) != 1:
                message = (
                    f"Host {self.name}: Zabbix did not return exactly one new "
                    "interface for the requested type change."
                )
                self.logger.error(message)
                raise SyncExternalError(message)
            new_interface_id = added_interfaces[0]["interfaceid"]

        if new_interface_id is None:
            message = (
                f"Host {self.name}: Zabbix did not return an ID for the replacement "
                "interface."
            )
            self.logger.error(message)
            raise SyncExternalError(message)

        self._move_items_to_interface(old_interface_id, new_interface_id)

    def _reconcile_interfaces(
        self, host, desired_interfaces, *, transfer_linked_items=True
    ):
        """Replace the host interface list, migrating linked items before type changes."""
        current_interfaces = host.get("interfaces") or []
        unmatched = set(range(len(current_interfaces)))
        matches = {}

        for desired_index, desired in enumerate(desired_interfaces):
            same_type = [
                current_index
                for current_index in sorted(unmatched)
                if self._interface_value(current_interfaces[current_index], "type")
                == self._interface_value(desired, "type")
            ]
            exact = [
                current_index
                for current_index in same_type
                if self._interfaces_have_same_endpoint(
                    current_interfaces[current_index], desired
                )
            ]
            if exact:
                match_index = exact[0]
            elif same_type:
                match_index = same_type[0]
            else:
                continue
            matches[desired_index] = match_index
            unmatched.remove(match_index)

        unmatched_desired = [
            index for index in range(len(desired_interfaces)) if index not in matches
        ]
        if transfer_linked_items and len(unmatched) == 1:
            current_index = next(iter(unmatched))
            current = current_interfaces[current_index]
            current_type = self._interface_value(current, "type")
            current_types = {
                self._interface_value(interface, "type")
                for interface in current_interfaces
            }
            replacement_candidates = []
            if len(unmatched_desired) == 1:
                desired_index = unmatched_desired[0]
                if (
                    self._interface_value(desired_interfaces[desired_index], "type")
                    not in current_types
                ):
                    replacement_candidates.append(desired_index)
            elif not unmatched_desired:
                replacement_candidates = [
                    index
                    for index, desired in enumerate(desired_interfaces)
                    if index in matches
                    and self._interface_value(desired, "type") != current_type
                    and self._interfaces_have_same_address(current, desired)
                ]

            if len(replacement_candidates) == 1:
                desired_index = replacement_candidates[0]
                self._replace_interface_type(
                    host,
                    current,
                    desired_interfaces[desired_index],
                    target_interface_index=matches.get(desired_index),
                )
                return self._reconcile_interfaces(
                    host, desired_interfaces, transfer_linked_items=False
                )

        desired_payload = []
        interfaces_changed = len(current_interfaces) != len(desired_interfaces)
        for desired_index, desired in enumerate(desired_interfaces):
            payload_interface = desired.copy()
            current_index = matches.get(desired_index)
            if current_index is None:
                payload_interface["hostid"] = self.zabbix_id
                interfaces_changed = True
            else:
                current = current_interfaces[current_index]
                interface_id = current.get("interfaceid")
                if interface_id is not None:
                    payload_interface["interfaceid"] = interface_id
                else:
                    payload_interface["hostid"] = self.zabbix_id
                    interfaces_changed = True
                if not self._interface_matches_desired(current, desired):
                    interfaces_changed = True
            desired_payload.append(payload_interface)

        if unmatched:
            interfaces_changed = True
        if not interfaces_changed:
            self.logger.debug("Host %s: Interfaces in-sync.", self.name)
            return True

        self.logger.info("Host %s: Interfaces OUT of sync.", self.name)
        self.update_zabbix_host(interfaces=desired_payload)
        return True

    def set_interface_details(self):
        """
        Checks interface parameters from NetBox and
        creates a model for the interface to be used in Zabbix.
        """
        return self._build_interface_details(default_type=2)

    def _build_interface_details(self, default_type, *, include_primary=True):
        """Build the interface list while reporting configuration errors per host."""
        try:
            return ZabbixInterface.build_interfaces(
                self.nb.config_context,
                self.ip,
                default_type,
                include_primary=include_primary,
            )
        except InterfaceConfigError as e:
            message = f"{self.name}: {e}"
            self.logger.warning(message)
            raise SyncInventoryError(message) from e

    def set_usermacros(self):
        """
        Generates Usermacros
        """
        macros = ZabbixUsermacros(
            self.nb,
            self._usermacro_map(),
            self.config["usermacro_sync"],
            logger=self.logger,
            host=self.name,
        )
        if macros.sync is False:
            self.usermacros = []
            return True

        self.usermacros = macros.generate()
        return True

    def set_tags(self):
        """
        Generates Host Tags
        """
        tags = ZabbixTags(
            self.nb,
            self._tag_map(),
            tag_sync=self.config["tag_sync"],
            tag_lower=self.config["tag_lower"],
            tag_name=self.config["tag_name"],
            tag_value=self.config["tag_value"],
            logger=self.logger,
            host=self.name,
        )
        if self.config["tag_sync"] is False:
            self.tags = []
            return False
        self.tags = tags.generate()
        return True

    def _tls_config_value(self, key):
        """
        Resolves a single TLS setting: the global config.py default is used
        unless the NetBox config context overrides it under the 'zabbix' key.
        """
        value = self.config[key]
        if "zabbix" in self.config_context and key in self.config_context["zabbix"]:
            value = self.config_context["zabbix"][key]
        return value

    def _tls_modes_to_bitmask(self, modes):
        """
        Translates one or more encryption mode names into a Zabbix bitmask.
        Accepts a single string or a list of strings. Returns None on an
        unknown mode name.
        """
        if isinstance(modes, str):
            modes = [modes]
        bitmask = 0
        for mode in modes:
            if mode not in TLS_MODES:
                self.logger.warning(
                    "Host %s: invalid TLS mode '%s'. Valid modes are: %s.",
                    self.name,
                    mode,
                    ", ".join(TLS_MODES),
                )
                return None
            bitmask |= TLS_MODES[mode]
        return bitmask

    def set_tls(self):
        """
        Sets the Zabbix encryption (TLS) configuration for this host.

        Values come from the global config defaults and may be overruled per
        host through the NetBox config context. Only the keys relevant to the
        configured connect/accept modes are sent to Zabbix.
        """
        self.tls = {}
        if not self.config["tls_sync"]:
            return False

        # Resolve and translate the connect (single) and accept (bitmask) modes.
        connect = self._tls_modes_to_bitmask(self._tls_config_value("tls_connect"))
        accept = self._tls_modes_to_bitmask(self._tls_config_value("tls_accept"))
        if connect is None or accept is None:
            return False

        tls = {"tls_connect": connect, "tls_accept": accept}
        uses_cert = bool((connect | accept) & TLS_MODES["cert"])
        uses_psk = bool((connect | accept) & TLS_MODES["psk"])

        # Certificate issuer/subject are optional even when cert is used.
        if uses_cert:
            issuer = self._tls_config_value("tls_issuer")
            subject = self._tls_config_value("tls_subject")
            if issuer:
                tls["tls_issuer"] = issuer
            if subject:
                tls["tls_subject"] = subject

        # PSK identity and key are mandatory whenever PSK is used.
        if uses_psk:
            identity = self._tls_config_value("tls_psk_identity")
            psk = self._tls_config_value("tls_psk")
            if not identity or not psk:
                self.logger.error(
                    "Host %s: PSK encryption requires both tls_psk_identity "
                    "and tls_psk to be set. Skipping TLS configuration.",
                    self.name,
                )
                self.tls = {}
                return False
            tls["tls_psk_identity"] = identity
            tls["tls_psk"] = psk

        self.tls = tls
        return True

    def _set_proxy(self, proxy_list: list[dict[str, Any]]) -> bool:
        """
        Sets proxy or proxy group if this
        value has been defined in config context
        or custom fields.

        input: List of all proxies and proxy groups in standardized format
        """
        # Proxy group takes priority over a proxy due
        # to it being HA and therefore being more reliable
        # Includes proxy group fix since Zabbix <= 6 should ignore this
        proxy_types = ["proxy"]
        proxy_name = None

        zabbix_7_version = 7.0

        if self.zabbix.version >= zabbix_7_version:
            # Only insert groups in front of list for Zabbix7
            proxy_types.insert(0, "proxy_group")

        # loop through supported proxy-types
        for proxy_type in proxy_types:
            # Check if we should use custom fields for proxy config
            field_config = "proxy_cf" if proxy_type == "proxy" else "proxy_group_cf"
            if self.config[field_config]:
                if (
                    self.config[field_config] in self.nb.custom_fields
                    and self.nb.custom_fields[self.config[field_config]]
                ):
                    proxy_name = cf_to_string(
                        self.nb.custom_fields[self.config[field_config]]
                    )
                elif (
                    self.config[field_config] in self.nb.site.custom_fields
                    and self.nb.site.custom_fields[self.config[field_config]]
                ):
                    proxy_name = cf_to_string(
                        self.nb.site.custom_fields[self.config[field_config]]
                    )

            # Otherwise check if the proxy is configured in NetBox CC
            if (
                not proxy_name
                and "zabbix" in self.nb.config_context
                and proxy_type in self.nb.config_context["zabbix"]
            ):
                proxy_name = self.nb.config_context["zabbix"][proxy_type]

            # If a proxy name was found, loop through all proxies to find a match
            if proxy_name:
                for proxy in proxy_list:
                    # If the proxy does not match the type, ignore and continue
                    if proxy["type"] != proxy_type:
                        continue
                    # If the proxy name matches
                    if proxy["name"] == proxy_name:
                        self.logger.debug(
                            "Host %s: using %s '%s'",
                            self.name,
                            proxy["type"],
                            proxy_name,
                        )
                        self.zbxproxy = proxy
                        return True

                self.logger.warning(
                    "Host %s: unable to find proxy %s", self.name, proxy_name
                )
        return False

    def create_in_zabbix(self, groups, templates, proxies):
        """
        Creates Zabbix host object with parameters from NetBox object.
        """
        # Check if hostname is already present in Zabbix
        if not self._zabbix_hostname_exists():
            # Set group and template ID's for host
            if not self.set_zbx_groupid(groups):
                e = (
                    f"Unable to find group '{self.hostgroups}' "
                    f"for host {self.name} in Zabbix."
                )
                self.logger.warning(e)
                raise SyncInventoryError(e)
            self.zbx_template_prepper(templates)
            templateids = []
            for template in self.zbx_templates:
                templateids.append({"templateid": template["templateid"]})
            # Set interface, group and template configuration
            interfaces = self.set_interface_details()
            # Set Zabbix proxy if defined
            self._set_proxy(proxies)
            # Set description
            description_handler = Description(
                self.nb,
                self.config,
                logger=self.logger,
                nb_version=self.nb_api_version,
            )
            description = description_handler.generate()
            # Set basic data for host creation
            create_data = {
                "host": self.name,
                "name": self.visible_name,
                "status": self.zabbix_state,
                "interfaces": interfaces,
                "groups": self.group_ids,
                "templates": templateids,
                "description": description,
                "inventory_mode": self.inventory_mode,
                "inventory": self.inventory,
                "macros": self.usermacros,
                "tags": self._unique_by_key(
                    self._tag_values(self.tags) + self._cleanup_ownership_tags(),
                    lambda tag: (tag["tag"], tag["value"]),
                ),
            }
            # Add TLS / encryption settings when tls_sync is enabled.
            # Empty when disabled, leaving Zabbix defaults untouched.
            create_data.update(self.tls)
            # If a Zabbix proxy or Zabbix Proxy group has been defined
            if self.zbxproxy:
                # If a lower version than 7 is used, we can assume that
                # the proxy is a normal proxy and not a proxy group
                if not str(self.zabbix.version).startswith("7"):
                    create_data["proxy_hostid"] = self.zbxproxy["id"]
                else:
                    # Configure either a proxy or proxy group
                    create_data[self.zbxproxy["idtype"]] = self.zbxproxy["id"]
                    create_data["monitored_by"] = self.zbxproxy["monitored_by"]
            # Add host to Zabbix
            try:
                host = self.zabbix.host.create(**create_data)
                self.zabbix_id = host["hostids"][0]
                self.zabbix_status = str(self.zabbix_state)
            except APIRequestError as e:
                msg = f"Host {self.name}: Couldn't create. Zabbix returned {e}."
                self.logger.error(msg)
                raise SyncExternalError(msg) from e
            # Set NetBox custom field to hostID value.
            self.nb.custom_fields[self.device_cf] = int(self.zabbix_id)
            self.nb.save()
            msg = f"Host {self.name}: Created host in Zabbix. (ID:{self.zabbix_id})"
            self.logger.info(msg)
            self.create_journal_entry("success", msg)
        else:
            self.logger.error(
                "Host %s: Unable to add to Zabbix. Host already present.", self.name
            )

    def create_zbx_hostgroup(self, hostgroups):
        """
        Creates Zabbix host group based on hostgroup format.
        Creates multiple when using a nested format.
        """
        final_data = []
        # Check if the hostgroup is in a nested format and check each parent
        for hostgroup in self.hostgroups:
            for pos in range(len(hostgroup.split("/"))):
                zabbix_hg = hostgroup.rsplit("/", pos)[0]
                if self.zbx_hostgroup_lookup(hostgroups, zabbix_hg):
                    # Hostgroup already exists
                    continue
                # Create new group
                try:
                    # API call to Zabbix
                    groupid = self.zabbix.hostgroup.create(name=zabbix_hg)
                    e = f"Hostgroup '{zabbix_hg}': created in Zabbix."
                    self.logger.info(e)
                    # Add group to final data
                    final_data.append(
                        {"groupid": groupid["groupids"][0], "name": zabbix_hg}
                    )
                except APIRequestError as e:
                    msg = f"Hostgroup '{zabbix_hg}': unable to create. Zabbix returned {e}."
                    self.logger.error(msg)
                    raise SyncExternalError(msg) from e
        return final_data

    def zbx_hostgroup_lookup(self, group_list, lookup_group):
        """
        Function to check if a hostgroup
        exists in a list of Zabbix hostgroups
        INPUT: Group list and group lookup
        OUTPUT: Boolean
        """
        return any(group["name"] == lookup_group for group in group_list)

    def update_zabbix_host(self, **kwargs):
        """
        Updates Zabbix host with given parameters.
        INPUT: Key word arguments for Zabbix host object.
        """
        try:
            self.zabbix.host.update(hostid=self.zabbix_id, **kwargs)
        except APIRequestError as e:
            e = (
                f"Host {self.name}: Unable to update. "
                f"Zabbix returned the following error: {e}."
            )
            self.logger.error(e)
            raise SyncExternalError(e) from None
        if "status" in kwargs:
            self.zabbix_status = kwargs["status"]
        self.logger.info(
            "Host %s: updated with data %s.", self.name, sanatize_log_output(kwargs)
        )
        self.create_journal_entry("info", "Updated host in Zabbix with latest NB data.")

    def consistency_check(
        self,
        groups,
        templates,
        proxies,
        proxy_power,
        create_hostgroups,
        full_sync=True,
        cleanup_ownership=False,
        hostgroup_resolver=None,
    ) -> bool:
        """
        Checks if Zabbix object is still valid with NetBox parameters.
        """
        clear_stale_hostids = bool(self.config.get("clear_stale_zabbix_hostids", False))
        if full_sync and not clear_stale_hostids:
            # Preserve the existing default ordering; when stale-link recovery
            # is enabled, verify the linked host before resolving dependencies.
            self.zbx_template_prepper(templates)
        # Get host object from Zabbix
        try:
            host = self.zabbix.host.get(
                filter={"hostid": self.zabbix_id},
                selectInterfaces=[
                    "type",
                    "ip",
                    "dns",
                    "port",
                    "useip",
                    "main",
                    "details",
                    "interfaceid",
                    "available",
                ],
                selectGroups=["groupid", "flags"],
                selectHostGroups=["groupid", "flags"],
                selectHostDiscovery=["parent_hostid"],
                selectParentTemplates=["templateid", "link_type"],
                selectInventory=list(self._inventory_map().values()),
                selectMacros=[
                    "hostmacroid",
                    "macro",
                    "value",
                    "type",
                    "description",
                    "automatic",
                ],
                selectTags=["tag", "value", "automatic"],
            )
        except APIRequestError as error:
            if not clear_stale_hostids:
                raise
            message = (
                f"Host {self.name}: Unable to verify linked Zabbix host ID "
                f"{self.zabbix_id}. Zabbix returned {error}."
            )
            self.logger.error(message)
            raise SyncExternalError(message) from error
        if clear_stale_hostids and (
            not isinstance(host, list)
            or any(not isinstance(host_record, Mapping) for host_record in host)
        ):
            message = (
                f"Host {self.name}: Zabbix returned an invalid response while "
                f"verifying host ID {self.zabbix_id}; retaining the NetBox link."
            )
            self.logger.error(message)
            raise SyncExternalError(message)
        if len(host) > 1:
            e = (
                f"Got {len(host)} results for Zabbix hosts "
                f"with ID {self.zabbix_id} - hostname {self.name}."
            )
            self.logger.error(e)
            raise SyncInventoryError(e)
        if len(host) == 0:
            if clear_stale_hostids:
                stale_hostid = self.zabbix_id
                self._zeroize_cf()
                self.zabbix_id = None
                self.logger.info(
                    "Host %s: Zabbix host ID %s no longer exists. "
                    "Cleared NetBox custom field '%s'.",
                    self.name,
                    stale_hostid,
                    self.device_cf,
                )
                return False
            e = (
                f"Host {self.name}: No Zabbix host found. "
                f"This is likely the result of a deleted Zabbix host "
                f"without zeroing the ID field in NetBox."
            )
            self.logger.error(e)
            raise SyncInventoryError(e)
        if full_sync and clear_stale_hostids:
            # LLD hosts still need their desired manual templates prepared, but
            # their hostgroup work must wait until discovery status is known.
            self.zbx_template_prepper(templates)
        host = host[0]
        self.zabbix_status = host.get("status")
        self.zabbix_interfaces = host.get("interfaces")
        discovered_host = self._is_discovered_host(host)
        sync_lld_hostgroups = bool(self.config.get("sync_lld_hostgroups", False))

        # Resolve, validate, and create NetBox-derived hostgroups only after
        # checking whether Zabbix created this host through LLD.  This lets
        # LLD hosts retain all existing groups by default, even when their
        # configured NetBox group is absent or cannot be generated.
        if full_sync and (not discovered_host or sync_lld_hostgroups):
            if hostgroup_resolver and not hostgroup_resolver():
                e = f"Host {self.name}: has no valid hostgroups."
                self.logger.warning(e)
                raise SyncInventoryError(e)
            if not self.set_zbx_groupid(groups):
                if create_hostgroups:
                    new_groups = self.create_zbx_hostgroup(groups)
                    groups.extend(new_groups)
                if not self.group_ids:
                    zbx_groupid_confirmation = self.set_zbx_groupid(groups)
                    if not zbx_groupid_confirmation and not create_hostgroups:
                        e = (
                            f"Host {self.name}: different hostgroup is required but "
                            "unable to create hostgroup without generation permission."
                        )
                        self.logger.warning(e)
                        raise SyncInventoryError(e)

        if discovered_host:
            if full_sync:
                self._sync_discovered_host(host, sync_hostgroups=sync_lld_hostgroups)
            else:
                # metadata_only adoption still permits the safe metadata
                # reconciliation paths, without falling back to destructive
                # full replacements of automatic LLD entries.
                self._sync_discovered_macros(host)
                self._sync_discovered_tags(host)
            full_sync = False

        if full_sync:
            self._set_proxy(proxies)

        desired_interfaces = None
        templates_changed = False
        if full_sync:
            if host["host"] == self.name:
                self.logger.debug("Host %s: Hostname in-sync.", self.name)
            else:
                self.logger.info(
                    "Host %s: Hostname OUT of sync. Received value: %s",
                    self.name,
                    host["host"],
                )
                self.update_zabbix_host(host=self.name)

            # Execute check depending on wether the name is special or not
            if self.use_visible_name:
                if host["name"] == self.visible_name:
                    self.logger.debug("Host %s: Visible name in-sync.", self.name)
                else:
                    self.logger.info(
                        "Host %s: Visible name OUT of sync. Received value: %s",
                        self.name,
                        host["name"],
                    )
                    self.update_zabbix_host(name=self.visible_name)

            # Check if the templates are in-sync
            templates_in_sync = self.zbx_template_comparer(host["parentTemplates"])
            if not templates_in_sync:
                templates_changed = True
                desired_interfaces = self.set_interface_details()
                self._add_missing_interface_types(host, desired_interfaces)
                self.logger.info("Host %s: Template(s) OUT of sync.", self.name)
                # Prepare Templates for API parsing
                templateids = []
                for template in self.zbx_templates:
                    templateids.append({"templateid": template["templateid"]})
                # Update Zabbix with NB templates and clear any old / lost templates
                self.update_zabbix_host(
                    templates_clear=[
                        {"templateid": template["templateid"]}
                        for template in host["parentTemplates"]
                    ],
                    templates=templateids,
                )
            else:
                self.logger.debug("Host %s: Template(s) in-sync.", self.name)

            # Check if Zabbix version is 6 or higher. Issue #93
            group_dictname = "hostgroups"
            if str(self.zabbix.version).startswith(("6", "5")):
                group_dictname = "groups"
            # Check if hostgroups match
            if self._hostgroups_in_sync(host[group_dictname], self.group_ids):
                self.logger.debug("Host %s: Hostgroups in-sync.", self.name)
            else:
                self.logger.info("Host %s: Hostgroups OUT of sync.", self.name)
                self.update_zabbix_host(groups=self.group_ids)

            if int(host["status"]) == self.zabbix_state:
                self.logger.debug("Host %s: Status in-sync.", self.name)
            else:
                self.logger.info("Host %s: Status OUT of sync.", self.name)
                self.update_zabbix_host(status=str(self.zabbix_state))

            # Check if a proxy has been defined
            if self.zbxproxy:
                # Check if proxy or proxy group is defined.
                # Check for proxy_hostid for backwards compatibility with Zabbix <= 6
                if (
                    self.zbxproxy["idtype"] in host
                    and host[self.zbxproxy["idtype"]] == self.zbxproxy["id"]
                ) or (
                    "proxy_hostid" in host
                    and host["proxy_hostid"] == self.zbxproxy["id"]
                ):
                    self.logger.debug("Host %s: Proxy in-sync.", self.name)
                # Proxy does not match, update Zabbix
                else:
                    self.logger.info("Host %s: Proxy OUT of sync.", self.name)
                    # Zabbix <= 6 patch
                    if not str(self.zabbix.version).startswith("7"):
                        self.update_zabbix_host(proxy_hostid=self.zbxproxy["id"])
                    # Zabbix 7+
                    else:
                        # Prepare data structure for updating either proxy or group
                        update_data = {
                            self.zbxproxy["idtype"]: self.zbxproxy["id"],
                            "monitored_by": self.zbxproxy["monitored_by"],
                        }
                        self.update_zabbix_host(**update_data)
            else:
                # No proxy is defined in NetBox
                proxy_set = False
                # Check if a proxy is defined. Uses the proxy_hostid key for backwards compatibility
                for key in ("proxy_hostid", "proxyid", "proxy_groupid"):
                    if key in host and bool(int(host[key])):
                        proxy_set = True
                if proxy_power and proxy_set:
                    # Zabbix <= 6 fix
                    self.logger.warning(
                        "Host %s: No proxy is configured in NetBox but is configured in Zabbix."
                        "Removing proxy config in Zabbix",
                        self.name,
                    )
                    if "proxy_hostid" in host and bool(host["proxy_hostid"]):
                        self.update_zabbix_host(proxy_hostid=0)
                    # Zabbix 7 proxy
                    elif "proxyid" in host and bool(host["proxyid"]):
                        self.update_zabbix_host(proxyid=0, monitored_by=0)
                    # Zabbix 7 proxy group
                    elif "proxy_groupid" in host and bool(host["proxy_groupid"]):
                        self.update_zabbix_host(proxy_groupid=0, monitored_by=0)
                # Checks if a proxy has been defined in Zabbix and if proxy_power config has been set
                if proxy_set and not proxy_power:
                    # Display error message
                    self.logger.warning(
                        "Host %s: Is configured with proxy in Zabbix but not in NetBox."
                        "full_proxy_sync is not set: no changes have been made.",
                        self.name,
                    )
                if not proxy_set:
                    self.logger.debug("Host %s: Proxy in-sync.", self.name)
        # Inventory and TLS can be set by an LLD host prototype, so leave them
        # untouched for discovered hosts along with other scalar host settings.
        if not discovered_host:
            if str(host["inventory_mode"]) == str(self.inventory_mode):
                self.logger.debug("Host %s: inventory_mode in-sync.", self.name)
            else:
                self.logger.info("Host %s: inventory_mode OUT of sync.", self.name)
                self.update_zabbix_host(inventory_mode=str(self.inventory_mode))
            if self.config["inventory_sync"] and self.inventory_mode in [0, 1]:
                # Check host inventory mapping
                if host["inventory"] == self.inventory:
                    self.logger.debug("Host %s: Inventory in-sync.", self.name)
                else:
                    self.logger.info("Host %s: Inventory OUT of sync.", self.name)
                    self.update_zabbix_host(inventory=self.inventory)

        # Check host TLS / encryption settings
        if not discovered_host and self.config["tls_sync"] and self.tls:
            # tls_psk is write-only and never returned by host.get, so it can't
            # be compared. All other resolved keys are checked as strings.
            tls_in_sync = all(
                key == "tls_psk" or str(host.get(key, "")) == str(value)
                for key, value in self.tls.items()
            )
            if tls_in_sync:
                self.logger.debug("Host %s: TLS settings in-sync.", self.name)
            else:
                self.logger.info("Host %s: TLS settings OUT of sync.", self.name)
                # self.tls carries tls_psk/tls_psk_identity when PSK is used,
                # which Zabbix requires to be present on the update.
                self.update_zabbix_host(**self.tls)

        # Check host usermacros
        if not discovered_host and self.config["usermacro_sync"]:
            # Make a full copy since we dont want to lose the original value
            # of secret type macros from Netbox
            netbox_macros = deepcopy(self.usermacros)
            # Set the sync bit
            full_sync_bit = bool(str(self.config["usermacro_sync"]).lower() == "full")
            partial_sync_bit = bool(
                str(self.config["usermacro_sync"]).lower() == "partial"
            )
            for macro in netbox_macros:
                # If the Macro is a secret and full sync is NOT activated
                if macro["type"] == str(1) and not full_sync_bit:
                    # Remove the value as the Zabbix api does not return the value key
                    # This is required when you want to do a diff between both lists
                    macro.pop("value")

            if partial_sync_bit:
                compare_macros = ZabbixUsermacros.merge_partial(
                    host["macros"], netbox_macros
                )
                update_macros = ZabbixUsermacros.merge_partial(
                    host["macros"], self.usermacros
                )
            else:
                compare_macros = netbox_macros
                update_macros = self.usermacros

            # Check if both lists are the same
            if self._macros_in_sync(host["macros"], compare_macros):
                self.logger.debug("Host %s: Usermacros in-sync.", self.name)
            else:
                self.logger.info("Host %s: Usermacros OUT of sync.", self.name)
                # Update Zabbix with NetBox usermacros
                self.update_zabbix_host(macros=update_macros)

        # Check host tags
        if not discovered_host and self.config["tag_sync"]:
            desired_tags = self._tags_preserving_cleanup_ownership(
                host["tags"],
                self._tag_values(self.tags)
                + (self._cleanup_ownership_tags() if cleanup_ownership else []),
            )
            if sorted(
                self._tag_values(host["tags"]),
                key=lambda tag: (tag["tag"], tag["value"]),
            ) == sorted(desired_tags, key=lambda tag: (tag["tag"], tag["value"])):
                self.logger.debug("Host %s: Tags in-sync.", self.name)
            else:
                self.logger.info("Host %s: Tags OUT of sync.", self.name)
                self.update_zabbix_host(tags=desired_tags)

        if cleanup_ownership and not discovered_host and not self.config["tag_sync"]:
            self._sync_cleanup_ownership(host)

        if full_sync:
            if desired_interfaces is None:
                desired_interfaces = self.set_interface_details()
            self._reconcile_interfaces(
                host,
                desired_interfaces,
                transfer_linked_items=not templates_changed,
            )
        return True

    def create_journal_entry(self, severity, message):
        """
        Send a new Journal entry to NetBox. Usefull for viewing actions
        in NetBox without having to look in Zabbix or the script log output
        """
        if self.journal:
            # Check if the severity is valid
            if severity not in ["info", "success", "warning", "danger"]:
                self.logger.warning(
                    "Value %s not valid for NB journal entries.", severity
                )
                return False
            journal = {
                "assigned_object_type": "dcim.device",
                "assigned_object_id": self.id,
                "kind": severity,
                "comments": message,
            }
            try:
                self.nb_journals.create(journal)
                self.logger.debug("Host %s: Created journal entry in NetBox", self.name)
                return True
            except NetboxRequestError as e:
                self.logger.warning(
                    "Unable to create journal entry for %s: NB returned %s",
                    self.name,
                    e,
                )
            return False
        return False

    def zbx_template_comparer(self, tmpls_from_zabbix):
        """
        Compares the NetBox and Zabbix templates with each other.
        Should there be a mismatch then the function will return false

        INPUT: list of NB and ZBX templates
        OUTPUT: Boolean True/False
        """
        succesfull_templates = []
        # Go through each NetBox template
        for nb_tmpl in self.zbx_templates:
            # Go through each Zabbix template
            for pos, zbx_tmpl in enumerate(tmpls_from_zabbix):
                # Check if template IDs match
                if nb_tmpl["templateid"] == zbx_tmpl["templateid"]:
                    # Templates match. Remove this template from the Zabbix templates
                    # and add this NB template to the list of successfull templates
                    tmpls_from_zabbix.pop(pos)
                    succesfull_templates.append(nb_tmpl)
                    self.logger.debug(
                        "Host %s: Template '%s' is present in Zabbix.",
                        self.name,
                        nb_tmpl["name"],
                    )
                    break
        # The following condition is only true if:
        # all of the NetBox templates have been confirmed as successful
        # and the ZBX template list is empty. This means that
        # all of the templates match.
        return (
            len(succesfull_templates) == len(self.zbx_templates)
            and len(tmpls_from_zabbix) == 0
        )
