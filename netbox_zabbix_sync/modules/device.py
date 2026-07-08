"""
Device specific handeling for NetBox to Zabbix
"""

from copy import deepcopy
from logging import getLogger
from operator import itemgetter
from re import fullmatch
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
    remove_duplicates,
    sanatize_log_output,
)
from netbox_zabbix_sync.modules.usermacros import ZabbixUsermacros

# Zabbix encryption modes mapped to their API integer values.
# tls_connect uses a single value; tls_accept is an OR-summed bitmask.
TLS_MODES = {"none": 1, "psk": 2, "cert": 4}


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
        self.journal = journal
        self.nb_journals = nb_journal_class
        self.inventory_mode = -1
        self.inventory = {}
        self.usermacros = []
        self.tags = {}
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

    def _set_basics(self):
        """
        Sets basic information like IP address.
        """
        # Return error if device does not have primary IP.
        if self.nb.primary_ip:
            self.cidr = self.nb.primary_ip.address
            self.ip = self.cidr.split("/")[0]
        else:
            e = f"Host {self.name}: no primary IP."
            self.logger.warning(e)
            raise SyncInventoryError(e)

        # Check if device has custom field for ZBX ID
        if self.device_cf in self.nb.custom_fields:
            self.zabbix_id = self.nb.custom_fields[self.device_cf]
        else:
            e = f"Host {self.name}: Custom field {self.device_cf} not present"
            self.logger.error(e)
            raise SyncInventoryError(e)

        # Validate hostname format.
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

    def _zabbix_name_lookup_candidates(self):
        """Build host.get filters for name-based lookup."""
        lookups = [{"host": self.name}]
        if self.use_visible_name:
            lookups.append({"name": self.visible_name})
        else:
            lookups.append({"name": self.name})
        return list(dict.fromkeys(tuple(sorted(i.items())) for i in lookups))

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

    def adopt_existing_zabbix_host(self):
        """
        Link to an existing Zabbix host by name if this object is in adoption scope.

        Returns True when adoption succeeded, otherwise False.
        """
        if self.zabbix_id or not self._is_adoption_candidate():
            return False
        matches = {}
        try:
            for lookup_tuple in self._zabbix_name_lookup_candidates():
                lookup = dict(lookup_tuple)
                for host in self.zabbix.host.get(
                    filter=lookup,
                    output=["hostid", "host", "name"],
                    selectParentTemplates=["templateid", "name"],
                    selectInventory="extend",
                    selectMacros=["macro", "value"],
                    selectTags=["tag", "value"],
                ):
                    if "hostid" in host:
                        matches[host["hostid"]] = host
        except APIRequestError as e:
            message = f"Host {self.name}: Adoption lookup failed. Zabbix returned {e}."
            self.logger.error(message)
            raise SyncExternalError(message) from e

        if not matches:
            self.logger.debug(
                "Host %s: No existing Zabbix host matched for adoption.", self.name
            )
            return False
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
            return False

        host = next(iter(matches.values()))
        self.zabbix_id = int(host["hostid"])
        self.nb.custom_fields[self.device_cf] = self.zabbix_id
        self.nb.save()
        if self._host_has_azure_discovered_template(host):
            self.adopted_azure_discovered_host = True
        self.logger.info(
            "Host %s: Adopted existing Zabbix host by name. (ID:%s)",
            self.name,
            self.zabbix_id,
        )
        return True

    def set_interface_details(self):
        """
        Checks interface parameters from NetBox and
        creates a model for the interface to be used in Zabbix.
        """
        try:
            # Initiate interface class
            interface = ZabbixInterface(self.nb.config_context, self.ip)
            # Check if NetBox has device context.
            # If not fall back to old config.
            if interface.get_context():
                # If device is SNMP type, add aditional information.
                snmp_interface_type = 2
                if interface.interface["type"] == snmp_interface_type:
                    interface.set_snmp()
            else:
                interface.set_default_snmp()
            return [interface.interface]
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
                "tags": self.tags,
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
    ):
        """
        Checks if Zabbix object is still valid with NetBox parameters.
        """
        if full_sync:
            # If group is found or if the hostgroup is nested
            # or len(self.hostgroups.split("/")) > 1:
            if not self.set_zbx_groupid(groups):
                if create_hostgroups:
                    # Script is allowed to create a new hostgroup
                    new_groups = self.create_zbx_hostgroup(groups)
                    for group in new_groups:
                        # Add all new groups to the list of groups
                        groups.append(group)
                # check if the initial group was not already found (and this is a nested folder check)
                if not self.group_ids:
                    zbx_groupid_confirmation = self.set_zbx_groupid(groups)
                    if not zbx_groupid_confirmation and not create_hostgroups:
                        # Function returns true / false but also sets GroupID
                        e = (
                            f"Host {self.name}: different hostgroup is required but "
                            "unable to create hostgroup without generation permission."
                        )
                        self.logger.warning(e)
                        raise SyncInventoryError(e)

            # Prepare templates and proxy config
            self.zbx_template_prepper(templates)
            self._set_proxy(proxies)
        # Get host object from Zabbix
        host = self.zabbix.host.get(
            filter={"hostid": self.zabbix_id},
            selectInterfaces=["type", "ip", "port", "details", "interfaceid"],
            selectGroups=["groupid"],
            selectHostGroups=["groupid"],
            selectParentTemplates=["templateid"],
            selectInventory=list(self._inventory_map().values()),
            selectMacros=["macro", "value", "type", "description"],
            selectTags=["tag", "value"],
        )
        if len(host) > 1:
            e = (
                f"Got {len(host)} results for Zabbix hosts "
                f"with ID {self.zabbix_id} - hostname {self.name}."
            )
            self.logger.error(e)
            raise SyncInventoryError(e)
        if len(host) == 0:
            e = (
                f"Host {self.name}: No Zabbix host found. "
                f"This is likely the result of a deleted Zabbix host "
                f"without zeroing the ID field in NetBox."
            )
            self.logger.error(e)
            raise SyncInventoryError(e)
        host = host[0]
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
            if not self.zbx_template_comparer(host["parentTemplates"]):
                self.logger.info("Host %s: Template(s) OUT of sync.", self.name)
                # Prepare Templates for API parsing
                templateids = []
                for template in self.zbx_templates:
                    templateids.append({"templateid": template["templateid"]})
                # Update Zabbix with NB templates and clear any old / lost templates
                self.update_zabbix_host(
                    templates_clear=host["parentTemplates"], templates=templateids
                )
            else:
                self.logger.debug("Host %s: Template(s) in-sync.", self.name)

            # Check if Zabbix version is 6 or higher. Issue #93
            group_dictname = "hostgroups"
            if str(self.zabbix.version).startswith(("6", "5")):
                group_dictname = "groups"
            # Check if hostgroups match
            if sorted(host[group_dictname], key=itemgetter("groupid")) == sorted(
                self.group_ids, key=itemgetter("groupid")
            ):
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
        # Check host inventory mode
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
        if self.config["tls_sync"] and self.tls:
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
        if self.config["usermacro_sync"]:
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

            # Sort all lists
            def filter_with_macros(macro):
                return macro["macro"]

            host["macros"].sort(key=filter_with_macros)
            compare_macros.sort(key=filter_with_macros)
            # Check if both lists are the same
            if host["macros"] == compare_macros:
                self.logger.debug("Host %s: Usermacros in-sync.", self.name)
            else:
                self.logger.info("Host %s: Usermacros OUT of sync.", self.name)
                # Update Zabbix with NetBox usermacros
                self.update_zabbix_host(macros=update_macros)

        # Check host tags
        if self.config["tag_sync"]:
            if remove_duplicates(
                host["tags"], lambda tag: f"{tag['tag']}{tag['value']}"
            ) == remove_duplicates(
                self.tags, lambda tag: f"{tag['tag']}{tag['value']}"
            ):
                self.logger.debug("Host %s: Tags in-sync.", self.name)
            else:
                self.logger.info("Host %s: Tags OUT of sync.", self.name)
                self.update_zabbix_host(tags=self.tags)

        if full_sync:
            # If only 1 interface has been found
            if len(host["interfaces"]) == 1:
                updates = {}
                # Go through each key / item and check if it matches Zabbix
                for key, item in self.set_interface_details()[0].items():
                    # Check if NetBox value is found in Zabbix
                    if key in host["interfaces"][0]:
                        # If SNMP is used, go through nested dict
                        # to compare SNMP parameters
                        if isinstance(item, dict) and key == "details":
                            for k, i in item.items():
                                # Check if the key is found in Zabbix and if the value matches
                                if k in host["interfaces"][0][key] and host[
                                    "interfaces"
                                ][0][key][k] != str(i):
                                    # If dict has not been created, add it
                                    if key not in updates:
                                        updates[key] = {}
                                    updates[key][k] = str(i)
                                    # If SNMP version has been changed
                                    # break loop and force full SNMP update
                                    if k == "version":
                                        break
                            # Force full SNMP config update
                            # when version has changed.
                            if key in updates and "version" in updates[key]:
                                for k, i in item.items():
                                    updates[key][k] = str(i)
                            continue
                        # Set update if values don't match
                        if host["interfaces"][0][key] != str(item):
                            updates[key] = item
                if updates:
                    # If interface updates have been found: push to Zabbix
                    self.logger.info("Host %s: Interface OUT of sync.", self.name)
                    if "type" in updates:
                        # Changing interface type not supported. Raise exception.
                        e = (
                            f"Host {self.name}: Changing interface type to "
                            f"{updates['type']} is not supported."
                        )
                        self.logger.error(e)
                        raise InterfaceConfigError(e)
                    # Set interfaceID for Zabbix config
                    updates["interfaceid"] = host["interfaces"][0]["interfaceid"]
                    try:
                        # API call to Zabbix
                        self.zabbix.hostinterface.update(updates)
                        err_msg = (
                            f"Host {self.name}: Updated interface "
                            f"with data {sanatize_log_output(updates)}."
                        )
                        self.logger.info(err_msg)
                        self.create_journal_entry("info", err_msg)
                    except APIRequestError as e:
                        msg = f"Zabbix returned the following error: {e}."
                        self.logger.error(msg)
                        raise SyncExternalError(msg) from e
                else:
                    # If no updates are found, Zabbix interface is in-sync
                    self.logger.debug("Host %s: Interface in-sync.", self.name)
            else:
                err_msg = (
                    f"Host {self.name}: Has unsupported interface configuration."
                    f" Host has total of {len(host['interfaces'])} interfaces. "
                    "Manual intervention required."
                )
                self.logger.error(err_msg)
                raise SyncInventoryError(err_msg)

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
