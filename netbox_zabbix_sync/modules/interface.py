"""
All of the Zabbix interface related configuration
"""

from collections.abc import Mapping
from typing import Any

from netbox_zabbix_sync.modules.exceptions import InterfaceConfigError

DEFAULT_PORTS = {1: 10050, 2: 161, 3: 623, 4: 12345}
SNMP_INTERFACE_TYPE = 2


class ZabbixInterface:
    """Class that represents a Zabbix interface."""

    def __init__(self, context, ip):
        self.context = context
        self.ip = ip
        self.skelet: dict[str, Any] = {
            "main": "1",
            "useip": "1",
            "dns": "",
            "ip": self.ip,
        }
        self.interface = self.skelet.copy()

    @staticmethod
    def _interface_type(value):
        """Normalize and validate a Zabbix interface type."""
        if isinstance(value, bool):
            raise InterfaceConfigError("Interface type must be an integer from 1 to 4.")
        try:
            interface_type = int(value)
        except (OverflowError, TypeError, ValueError) as e:
            raise InterfaceConfigError(
                "Interface type must be an integer from 1 to 4."
            ) from e
        if (
            str(value).strip() != str(interface_type)
            or interface_type not in DEFAULT_PORTS
        ):
            raise InterfaceConfigError(
                f"Unsupported interface type '{value}'. Supported types are 1 to 4."
            )
        return interface_type

    @staticmethod
    def _binary_value(value, field):
        """Normalize a Zabbix binary setting such as main or useip."""
        if isinstance(value, bool):
            return "1" if value else "0"
        if str(value) not in {"0", "1"}:
            raise InterfaceConfigError(f"Interface {field} must be 0 or 1.")
        return str(value)

    def _set_default_port(self):
        """Sets default TCP / UDP port for different interface types."""
        try:
            interface_type = int(self.interface["type"])
        except (TypeError, ValueError):
            return False
        if interface_type not in DEFAULT_PORTS:
            return False
        self.interface["port"] = str(DEFAULT_PORTS[interface_type])
        return True

    def get_context(self):
        """check if NetBox custom context has been defined."""
        if "zabbix" in self.context:
            zabbix = self.context["zabbix"]
            if "interface_type" in zabbix:
                self.interface["type"] = zabbix["interface_type"]
                if "interface_port" not in zabbix:
                    self._set_default_port()
                    return True
                self.interface["port"] = zabbix["interface_port"]
                return True
            return False
        return False

    def set_snmp(self):
        """Check if interface is type SNMP."""
        if self._interface_type(self.interface["type"]) == SNMP_INTERFACE_TYPE:
            snmp = self.context["zabbix"].get("snmp")
            if isinstance(snmp, Mapping):
                details: dict[str, str] = {}
                self.interface["details"] = details
                details["bulk"] = str(snmp.get("bulk", 1))
                version_value = snmp.get("version")
                if version_value:
                    details["version"] = str(version_value)
                else:
                    raise InterfaceConfigError("SNMP version option is not defined.")
                if details["version"] in ["1", "2"]:
                    details["community"] = str(
                        snmp.get("community", "{$SNMP_COMMUNITY}")
                    )
                elif details["version"] == "3":
                    items = [
                        "securityname",
                        "securitylevel",
                        "authpassphrase",
                        "privpassphrase",
                        "authprotocol",
                        "privprotocol",
                        "contextname",
                    ]
                    details.update(
                        {key: str(value) for key, value in snmp.items() if key in items}
                    )
                else:
                    raise InterfaceConfigError("Unsupported SNMP version.")
            else:
                raise InterfaceConfigError(
                    "Interface type SNMP but no parameters provided."
                )
        else:
            raise InterfaceConfigError(
                "Interface type is not SNMP, unable to set SNMP details"
            )

    def set_default_snmp(self):
        """Set default config to SNMPv2, port 161 and community macro."""
        self.interface = self.skelet.copy()
        self.interface["type"] = "2"
        self.interface["port"] = "161"
        self.interface["details"] = {
            "version": "2",
            "community": "{$SNMP_COMMUNITY}",
            "bulk": "1",
        }

    def set_default_agent(self):
        """Sets interface to Zabbix agent defaults"""
        self.interface["type"] = "1"
        self.interface["port"] = "10050"

    def _configure(self, options, default_type, *, additional):
        """Configure this interface from one config-context interface definition."""
        if not isinstance(options, Mapping):
            raise InterfaceConfigError("Interface configuration must be an object.")
        if "interface_type" not in options and additional:
            raise InterfaceConfigError(
                "Each additional interface must define interface_type."
            )

        configured_type = options.get("interface_type", str(default_type))
        interface_type = self._interface_type(configured_type)
        self.interface["type"] = configured_type
        port = options.get("interface_port", DEFAULT_PORTS[interface_type])
        if (
            isinstance(port, bool)
            or not isinstance(port, (int, str))
            or not str(port).strip()
        ):
            raise InterfaceConfigError(
                "Interface port must be a non-empty string or integer."
            )
        self.interface["port"] = str(port)

        if "ip" in options and not isinstance(options["ip"], str):
            raise InterfaceConfigError("Interface ip must be a string.")

        if additional:
            if "dns" in options and not isinstance(options["dns"], str):
                raise InterfaceConfigError("Interface dns must be a string.")
            self.interface["ip"] = options.get("ip", self.ip) or ""
            self.interface["dns"] = options.get("dns", "")
            self.interface["useip"] = self._binary_value(
                options.get("useip", 1), "useip"
            )
            self.interface["main"] = (
                self._binary_value(options["main"], "main")
                if "main" in options
                else None
            )

        if interface_type == SNMP_INTERFACE_TYPE and not (
            not additional and "interface_type" not in options
        ):
            self.context = {"zabbix": options}
            self.set_snmp()
        elif interface_type == SNMP_INTERFACE_TYPE:
            self.set_default_snmp()

        if not additional and "ip" in options:
            self.interface["ip"] = options["ip"]

        if self.interface["useip"] == "1" and (
            not isinstance(self.interface["ip"], str)
            or not self.interface["ip"].strip()
        ):
            raise InterfaceConfigError(
                "An IP-addressed interface must define a non-empty ip."
            )
        if self.interface["useip"] == "0" and not self.interface["dns"].strip():
            raise InterfaceConfigError(
                "A DNS-addressed interface must define dns when useip is 0."
            )
        return self.interface

    @classmethod
    def build_interfaces(cls, context, ip, default_type, *, include_primary=True):
        """Build the primary and any additional interfaces from NetBox context."""
        if context is None:
            context = {}
        if not isinstance(context, Mapping):
            raise InterfaceConfigError("Zabbix config context must be an object.")
        zabbix = context.get("zabbix", {})
        if zabbix is None:
            zabbix = {}
        if not isinstance(zabbix, Mapping):
            raise InterfaceConfigError(
                "The zabbix config-context value must be an object."
            )
        additional = zabbix.get("additional_interfaces", [])
        if not isinstance(additional, list):
            raise InterfaceConfigError("additional_interfaces must be a list.")

        interfaces = []
        if include_primary:
            primary_options = (
                zabbix if "interface_type" in zabbix or "ip" in zabbix else {}
            )
            primary = cls(context, ip)
            primary._configure(primary_options, default_type, additional=False)
            interfaces.append(primary.interface)

        for options in additional:
            interface = cls(context, ip)
            interface._configure(options, default_type, additional=True)
            interfaces.append(interface.interface)

        seen_types = set()
        main_types = set()
        for interface in interfaces:
            interface_type = cls._interface_type(interface["type"])
            type_key = str(interface_type)
            if interface["main"] is None:
                interface["main"] = "0" if type_key in seen_types else "1"
            seen_types.add(type_key)
            if interface["main"] == "1":
                if type_key in main_types:
                    raise InterfaceConfigError(
                        f"More than one main interface is configured for type {type_key}."
                    )
                main_types.add(type_key)

        missing_main = seen_types - main_types
        if missing_main:
            types = ", ".join(sorted(missing_main))
            raise InterfaceConfigError(
                f"Each interface type must have exactly one main interface; "
                f"missing main for type(s) {types}."
            )
        return interfaces
