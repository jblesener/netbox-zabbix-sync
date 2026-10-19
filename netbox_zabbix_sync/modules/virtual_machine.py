"""Module that hosts all functions for virtual machine processing"""

from collections.abc import Mapping

from netbox_zabbix_sync.modules.device import PhysicalDevice
from netbox_zabbix_sync.modules.exceptions import TemplateError


class VirtualMachine(PhysicalDevice):
    """Model for virtual machines"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hostgroup = None
        self.zbx_template_names = None
        self.hostgroup_type = "vm"

    def _inventory_map(self):
        """use VM inventory maps"""
        return self.config["vm_inventory_map"]

    def _usermacro_map(self):
        """use VM usermacro maps"""
        return self.config["vm_usermacro_map"]

    def _tag_map(self):
        """use VM tag maps"""
        return self.config["vm_tag_map"]

    def set_vm_template(self):
        """Set Template for VMs. Overwrites default class
        to skip a lookup of custom fields."""
        # Gather templates ONLY from the device specific context
        try:
            self.zbx_template_names = self.get_templates_context()
        except TemplateError as e:
            self.logger.warning(e)
        return True

    def _requires_primary_ip(self):
        """VMs used for interface-free checks do not need a primary IP."""
        return False

    def set_interface_details(self):
        """
        Overwrites device function to select an agent interface type by default
        Agent type interfaces are more likely to be used with VMs then SNMP
        """
        context = getattr(self.nb, "config_context", None)
        zabbix_context = (
            context.get("zabbix", {}) if isinstance(context, Mapping) else {}
        )
        has_ip_override = isinstance(zabbix_context, Mapping) and "ip" in zabbix_context
        return self._build_interface_details(
            default_type=1, include_primary=bool(self.ip) or has_ip_override
        )
