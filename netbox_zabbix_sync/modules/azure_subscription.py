"""Azure subscription sync support."""

from copy import deepcopy
from logging import getLogger
from operator import itemgetter
from re import fullmatch
from typing import Any

from zabbix_utils import APIRequestError

from netbox_zabbix_sync.modules.exceptions import SyncExternalError, SyncInventoryError
from netbox_zabbix_sync.modules.tools import remove_duplicates, sanatize_log_output


class AzureSubscription:
    """Represents an Azure subscription modeled as a NetBox Tenant."""

    def __init__(
        self,
        nb_tenant,
        zabbix,
        logger=None,
        config=None,
    ):
        self.nb = nb_tenant
        self.zabbix = zabbix
        self.logger = logger if logger else getLogger(__name__)
        self.config = config or {}
        self.id = nb_tenant.id
        self.name = nb_tenant.name
        self.visible_name = None
        self.use_visible_name = False
        self.zabbix_id = None
        self.group_ids = []
        self.zbx_templates = []
        self.macros = []
        self.hostgroups = [self.config["azure_hostgroup"]]
        self.zbx_template_names = [self.config["azure_template"]]
        self._set_basics()

    def __repr__(self):
        return self.name

    def __str__(self):
        return self.__repr__()

    def _set_basics(self):
        """Set host ID and ensure the Zabbix technical hostname is valid."""
        hostid_cf = self.config["azure_zabbix_hostid_cf"]
        if hostid_cf not in self.nb.custom_fields:
            message = f"Host {self.name}: Custom field {hostid_cf} not present"
            self.logger.error(message)
            raise SyncInventoryError(message)
        self.zabbix_id = self.nb.custom_fields[hostid_cf]

        if not self._hostname_supported_by_zabbix(self.name):
            self.name = f"NETBOX_TENANT_ID{self.id}"
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
        """Return whether `hostname` is valid for Zabbix technical host names."""
        if hostname != hostname.strip():
            return False
        return bool(fullmatch(r"[A-Za-z0-9._ -]+", hostname))

    def _custom_field_value(self, obj, field_name):
        """Return a custom field value from a pynetbox object."""
        if not obj:
            return None
        custom_fields = getattr(obj, "custom_fields", None)
        if custom_fields is None and hasattr(obj, "full_details"):
            obj.full_details()
            custom_fields = getattr(obj, "custom_fields", None)
        if not custom_fields:
            return None
        return custom_fields.get(field_name)

    def _subscription_id(self):
        return self._custom_field_value(
            self.nb, self.config["azure_subscription_id_cf"]
        )

    def _tenant_id(self):
        tenant_group = getattr(self.nb, "group", None)
        return self._custom_field_value(tenant_group, self.config["azure_tenant_id_cf"])

    def validate(self):
        """Validate required NetBox and config data for Azure monitoring."""
        missing = []
        if not self._subscription_id():
            missing.append(self.config["azure_subscription_id_cf"])
        if not getattr(self.nb, "group", None):
            missing.append("group")
        elif not self._tenant_id():
            missing.append(f"group.{self.config['azure_tenant_id_cf']}")
        if not self.config["azure_app_id_vault"]:
            missing.append("azure_app_id_vault")
        if not self.config["azure_password_vault"]:
            missing.append("azure_password_vault")
        if missing:
            self.logger.warning(
                "Host %s: Missing Azure subscription data: %s. Skipping this host.",
                self.name,
                ", ".join(missing),
            )
            return False
        return True

    def set_usermacros(self):
        """Set the Azure by HTTP macros required by the Zabbix template."""
        self.macros = [
            {
                "macro": "{$AZURE.APP.ID}",
                "value": self.config["azure_app_id_vault"],
                "type": "2",
                "description": "",
            },
            {
                "macro": "{$AZURE.PASSWORD}",
                "value": self.config["azure_password_vault"],
                "type": "2",
                "description": "",
            },
            {
                "macro": "{$AZURE.SUBSCRIPTION.ID}",
                "value": str(self._subscription_id()),
                "type": "0",
                "description": "",
            },
            {
                "macro": "{$AZURE.TENANT.ID}",
                "value": str(self._tenant_id()),
                "type": "0",
                "description": "",
            },
        ]
        self.logger.debug(
            "Host %s: Resolved macros: %s",
            self.name,
            sanatize_log_output({"macros": self.macros}),
        )
        return True

    def zbx_template_prepper(self, templates):
        """Resolve configured Azure template names to Zabbix template IDs."""
        self.zbx_templates = []
        for template_name in self.zbx_template_names:
            template_match = False
            for zbx_template in templates:
                if zbx_template["name"] == template_name:
                    template_match = True
                    self.zbx_templates.append(
                        {
                            "templateid": zbx_template["templateid"],
                            "name": zbx_template["name"],
                        }
                    )
                    self.logger.debug(
                        "Host %s: Found template '%s' (ID:%s)",
                        self.name,
                        zbx_template["name"],
                        zbx_template["templateid"],
                    )
            if not template_match:
                message = (
                    f"Unable to find template {template_name} "
                    f"for host {self.name} in Zabbix. Skipping host..."
                )
                self.logger.warning(message)
                raise SyncInventoryError(message)

    def set_zbx_groupid(self, groups):
        """Resolve configured Azure host groups to Zabbix host group IDs."""
        self.group_ids = []
        for hostgroup in self.hostgroups:
            for group in groups:
                if group["name"] == hostgroup:
                    self.group_ids.append({"groupid": group["groupid"]})
                    self.logger.debug(
                        'Host %s: Matched group "%s" (ID:%s)',
                        self.name,
                        group["name"],
                        group["groupid"],
                    )
        return len(self.group_ids) == len(self.hostgroups)

    def create_zbx_hostgroup(self, hostgroups):
        """Create the configured Azure host group and missing parents."""
        final_data = []
        for hostgroup in self.hostgroups:
            for pos in range(len(hostgroup.split("/"))):
                zabbix_hg = hostgroup.rsplit("/", pos)[0]
                if any(group["name"] == zabbix_hg for group in hostgroups):
                    continue
                try:
                    groupid = self.zabbix.hostgroup.create(name=zabbix_hg)
                    self.logger.info("Hostgroup '%s': created in Zabbix.", zabbix_hg)
                    final_data.append(
                        {"groupid": groupid["groupids"][0], "name": zabbix_hg}
                    )
                except APIRequestError as e:
                    message = (
                        f"Hostgroup '{zabbix_hg}': unable to create. "
                        f"Zabbix returned {e}."
                    )
                    self.logger.error(message)
                    raise SyncExternalError(message) from e
        return final_data

    def _zabbix_hostname_exists(self):
        """Return whether a Zabbix host already exists for this subscription."""
        zbx_filter = (
            {"name": self.visible_name} if self.use_visible_name else {"host": self.name}
        )
        return bool(self.zabbix.host.get(filter=zbx_filter, output=[]))

    def adopt_existing_zabbix_host(self):
        """Link a unique same-name Zabbix host when no NetBox host ID is stored."""
        if self.zabbix_id:
            return False
        lookups = [{"host": self.name}]
        if self.use_visible_name:
            lookups.append({"name": self.visible_name})
        else:
            lookups.append({"name": self.name})
        matches = {}
        try:
            for lookup in lookups:
                for host in self.zabbix.host.get(
                    filter=lookup, output=["hostid", "host", "name"]
                ):
                    if "hostid" in host:
                        matches[host["hostid"]] = host
        except APIRequestError as e:
            message = f"Host {self.name}: Adoption lookup failed. Zabbix returned {e}."
            self.logger.error(message)
            raise SyncExternalError(message) from e
        if not matches:
            return False
        if len(matches) > 1:
            self.logger.warning(
                "Host %s: Multiple Zabbix hosts matched by name. Skipping adoption.",
                self.name,
            )
            return False
        host = next(iter(matches.values()))
        self.zabbix_id = int(host["hostid"])
        self.nb.custom_fields[self.config["azure_zabbix_hostid_cf"]] = self.zabbix_id
        self.nb.save()
        self.logger.info(
            "Host %s: Adopted existing Zabbix host by name. (ID:%s)",
            self.name,
            self.zabbix_id,
        )
        return True

    def _prepare_zabbix_references(self, groups, templates, create_hostgroups):
        """Resolve host group and template IDs, creating groups when allowed."""
        if not self.set_zbx_groupid(groups):
            if create_hostgroups:
                new_groups = self.create_zbx_hostgroup(groups)
                groups.extend(new_groups)
                self.set_zbx_groupid(groups)
            if not self.group_ids:
                message = (
                    f"Host {self.name}: Azure hostgroup is required but unable to "
                    "create hostgroup without generation permission."
                )
                self.logger.warning(message)
                raise SyncInventoryError(message)
        self.zbx_template_prepper(templates)

    def create_in_zabbix(self, groups, templates, create_hostgroups):
        """Create the Azure subscription host in Zabbix."""
        if self._zabbix_hostname_exists():
            self.logger.error(
                "Host %s: Unable to add to Zabbix. Host already present.", self.name
            )
            return False
        self._prepare_zabbix_references(groups, templates, create_hostgroups)
        create_data = {
            "host": self.name,
            "name": self.visible_name,
            "status": 0,
            "interfaces": [],
            "groups": self.group_ids,
            "templates": [
                {"templateid": template["templateid"]} for template in self.zbx_templates
            ],
            "macros": self.macros,
        }
        try:
            host = self.zabbix.host.create(**create_data)
            self.zabbix_id = host["hostids"][0]
        except APIRequestError as e:
            message = f"Host {self.name}: Couldn't create. Zabbix returned {e}."
            self.logger.error(message)
            raise SyncExternalError(message) from e
        self.nb.custom_fields[self.config["azure_zabbix_hostid_cf"]] = int(
            self.zabbix_id
        )
        self.nb.save()
        self.logger.info(
            "Host %s: Created Azure subscription host in Zabbix. (ID:%s)",
            self.name,
            self.zabbix_id,
        )
        return True

    def update_zabbix_host(self, **kwargs):
        """Update Zabbix host with given parameters."""
        try:
            self.zabbix.host.update(hostid=self.zabbix_id, **kwargs)
        except APIRequestError as e:
            message = (
                f"Host {self.name}: Unable to update. "
                f"Zabbix returned the following error: {e}."
            )
            self.logger.error(message)
            raise SyncExternalError(message) from None
        self.logger.info(
            "Host %s: updated with data %s.", self.name, sanatize_log_output(kwargs)
        )

    def consistency_check(self, groups, templates, create_hostgroups):
        """Reconcile an existing Zabbix host with NetBox Tenant data."""
        self._prepare_zabbix_references(groups, templates, create_hostgroups)
        host = self.zabbix.host.get(
            filter={"hostid": self.zabbix_id},
            selectInterfaces=["interfaceid"],
            selectGroups=["groupid"],
            selectHostGroups=["groupid"],
            selectParentTemplates=["templateid"],
            selectMacros=["macro", "value", "type", "description"],
        )
        if len(host) > 1:
            message = (
                f"Got {len(host)} results for Zabbix hosts "
                f"with ID {self.zabbix_id} - hostname {self.name}."
            )
            self.logger.error(message)
            raise SyncInventoryError(message)
        if len(host) == 0:
            message = (
                f"Host {self.name}: No Zabbix host found. "
                "This is likely the result of a deleted Zabbix host "
                "without zeroing the ID field in NetBox."
            )
            self.logger.error(message)
            raise SyncInventoryError(message)
        host = host[0]
        if host["host"] != self.name:
            self.logger.info("Host %s: Hostname OUT of sync.", self.name)
            self.update_zabbix_host(host=self.name)
        if self.use_visible_name and host["name"] != self.visible_name:
            self.logger.info("Host %s: Visible name OUT of sync.", self.name)
            self.update_zabbix_host(name=self.visible_name)
        if not self.zbx_template_comparer(host["parentTemplates"]):
            self.logger.info("Host %s: Template(s) OUT of sync.", self.name)
            self.update_zabbix_host(
                templates_clear=host["parentTemplates"],
                templates=[
                    {"templateid": template["templateid"]}
                    for template in self.zbx_templates
                ],
            )
        group_dictname = "hostgroups"
        if str(self.zabbix.version).startswith(("6", "5")):
            group_dictname = "groups"
        if sorted(host[group_dictname], key=itemgetter("groupid")) != sorted(
            self.group_ids, key=itemgetter("groupid")
        ):
            self.logger.info("Host %s: Hostgroups OUT of sync.", self.name)
            self.update_zabbix_host(groups=self.group_ids)
        if host["interfaces"]:
            self.logger.info("Host %s: Interfaces OUT of sync.", self.name)
            self.update_zabbix_host(interfaces=[])
        if not self._macros_in_sync(host["macros"]):
            self.logger.info("Host %s: Usermacros OUT of sync.", self.name)
            self.update_zabbix_host(macros=self.macros)

    def _macros_in_sync(self, zabbix_macros):
        """Compare desired macros with Zabbix macros."""
        zbx_macros = deepcopy(zabbix_macros)
        desired_macros = deepcopy(self.macros)

        def sortkey(macro: dict[str, Any]):
            return macro["macro"]

        return remove_duplicates(zbx_macros, sortkey) == remove_duplicates(
            desired_macros, sortkey
        )

    def zbx_template_comparer(self, templates_from_zabbix):
        """Compare desired and actual Zabbix templates."""
        successful_templates = []
        for nb_template in self.zbx_templates:
            for pos, zbx_template in enumerate(templates_from_zabbix):
                if nb_template["templateid"] == zbx_template["templateid"]:
                    templates_from_zabbix.pop(pos)
                    successful_templates.append(nb_template)
                    break
        return (
            len(successful_templates) == len(self.zbx_templates)
            and len(templates_from_zabbix) == 0
        )
