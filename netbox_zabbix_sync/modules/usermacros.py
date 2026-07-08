"""
All of the Zabbix Usermacro related configuration
"""

from logging import getLogger
from re import finditer, match

from netbox_zabbix_sync.modules.tools import field_mapper, sanatize_log_output

MAX_VALUE_SIZE = 2048
NETBOX_PLACEHOLDER_PATTERN = r"\{netbox:([^{}]+)\}"


class ZabbixUsermacros:
    """Class that represents Zabbix usermacros."""

    def __init__(self, nb, usermacro_map, usermacro_sync, logger=None, host=None):
        self.nb = nb
        self.name = host if host else nb.name
        self.usermacro_map = usermacro_map
        self.logger = logger if logger else getLogger(__name__)
        self.usermacros = {}
        self.usermacro_sync = usermacro_sync
        self.sync = False
        self.force_sync = False
        self.partial_sync = False
        self._set_config()

    def __repr__(self):
        return self.name

    def __str__(self):
        return self.__repr__()

    def _set_config(self):
        """
        Setup class
        """
        sync_mode = str(self.usermacro_sync).lower()
        if sync_mode == "full":
            self.sync = True
            self.force_sync = True
        elif sync_mode == "partial":
            self.sync = True
            self.partial_sync = True
        elif self.usermacro_sync:
            self.sync = True
        return True

    @staticmethod
    def merge_partial(zabbix_macros, netbox_macros):
        """
        Merge NetBox macros into the existing Zabbix macro list by macro name.
        """
        netbox_by_name = {macro["macro"]: macro for macro in netbox_macros}
        merged = []
        merged_names = set()

        for macro in zabbix_macros:
            name = macro["macro"]
            if name in netbox_by_name:
                if name not in merged_names:
                    merged.append(netbox_by_name[name])
                    merged_names.add(name)
                continue
            merged.append(macro)

        for name, macro in netbox_by_name.items():
            if name not in merged_names:
                merged.append(macro)

        return merged

    def validate_macro(self, macro_name):
        """
        Validates usermacro name
        """
        pattern = r"\{\$[A-Z0-9\._]*(\:.*)?\}"
        return match(pattern, macro_name)

    def _lookup_netbox_path(self, macro_name, path):
        """
        Resolve a slash-delimited NetBox field path for config context macros.
        """
        value = self.nb
        for item in path.split("/"):
            if not item:
                self.logger.warning(
                    "Host %s: Usermacro %s has invalid NetBox placeholder path '%s', skipping.",
                    self.name,
                    macro_name,
                    path,
                )
                return None, False
            try:
                if isinstance(value, dict):
                    value = value[item]
                elif hasattr(value, item):
                    value = getattr(value, item)
                else:
                    value = value[item]
            except (AttributeError, KeyError, IndexError, TypeError):
                self.logger.warning(
                    "Host %s: Usermacro %s references unknown NetBox field '%s', skipping.",
                    self.name,
                    macro_name,
                    path,
                )
                return None, False

        if (value and isinstance(value, int | float | str | list | dict)) or (
            isinstance(value, int | float) and int(value) == 0
        ):
            return str(value), True
        if not value:
            self.logger.info(
                "Host %s: NetBox lookup for '%s' returned an empty value.",
                self.name,
                path,
            )
            return "", True

        self.logger.warning(
            "Host %s: Usermacro %s NetBox lookup for '%s' returned an unexpected type, skipping.",
            self.name,
            macro_name,
            path,
        )
        return None, False

    def _expand_netbox_placeholders(self, macro_name, value):
        """
        Replace {netbox:path/to/field} placeholders in config context macro values.
        """
        if not isinstance(value, str):
            return value

        expanded = value
        for placeholder in finditer(NETBOX_PLACEHOLDER_PATTERN, value):
            path = placeholder.group(1)
            resolved, valid = self._lookup_netbox_path(macro_name, path)
            if not valid:
                return None
            expanded = expanded.replace(placeholder.group(0), resolved)
        return expanded

    def _expand_config_context_properties(self, macro_name, properties):
        """
        Expand NetBox placeholders for config context usermacro properties.
        """
        if isinstance(properties, dict):
            expanded = properties.copy()
            if "value" in expanded:
                expanded["value"] = self._expand_netbox_placeholders(
                    macro_name, expanded["value"]
                )
                if expanded["value"] is None:
                    return None
            return expanded

        return self._expand_netbox_placeholders(macro_name, properties)

    def render_macro(self, macro_name, macro_properties):
        """
        Renders a full usermacro from partial input
        """
        macro = {}
        macrotypes = {"text": 0, "secret": 1, "vault": 2}
        if self.validate_macro(macro_name):
            macro["macro"] = str(macro_name)
            if isinstance(macro_properties, dict):
                if "value" not in macro_properties:
                    self.logger.info(
                        "Host %s: Usermacro %s has no value in Netbox, skipping.",
                        self.name,
                        macro_name,
                    )
                    return False
                macro["value"] = macro_properties["value"]

                if (
                    "type" in macro_properties
                    and macro_properties["type"].lower() in macrotypes
                ):
                    macro["type"] = str(macrotypes[macro_properties["type"]])
                else:
                    macro["type"] = str(0)

                if "description" in macro_properties and isinstance(
                    macro_properties["description"], str
                ):
                    macro["description"] = macro_properties["description"]
                else:
                    macro["description"] = ""

            elif isinstance(macro_properties, str) and macro_properties:
                macro["value"] = macro_properties
                macro["type"] = str(0)
                macro["description"] = ""

            else:
                self.logger.info(
                    "Host %s: Usermacro %s has no value, skipping.",
                    self.name,
                    macro_name,
                )
                return False
        else:
            self.logger.warning(
                "Host %s: Usermacro %s is not a valid usermacro name, skipping.",
                self.name,
                macro_name,
            )
            return False
        if len(macro["value"]) > MAX_VALUE_SIZE:
            self.logger.warning(
                "Host %s: Usermacro %s has a value that is %s bytes which is too large, skipping.",
                self.name,
                macro_name,
                len(macro["value"]),
            )
            return False
        return macro

    def generate(self):
        """
        Generate full set of Usermacros
        """
        macros = []
        data = {}
        # Parse the field mapper for usermacros
        if self.usermacro_map:
            self.logger.debug("Host %s: Starting usermacro mapper.", self.nb.name)
            field_macros = field_mapper(
                self.nb.name, self.usermacro_map, self.nb, self.logger
            )
            for macro, value in field_macros.items():
                m = self.render_macro(macro, value)
                if m:
                    macros.append(m)
        # Parse NetBox config context for usermacros
        if (
            "zabbix" in self.nb.config_context
            and "usermacros" in self.nb.config_context["zabbix"]
        ):
            for macro, properties in self.nb.config_context["zabbix"][
                "usermacros"
            ].items():
                expanded_properties = self._expand_config_context_properties(
                    macro, properties
                )
                if expanded_properties is None:
                    continue
                m = self.render_macro(macro, expanded_properties)
                if m:
                    macros.append(m)
        data = {"macros": macros}
        self.logger.debug(
            "Host %s: Resolved macros: %s", self.name, sanatize_log_output(data)
        )
        return macros
