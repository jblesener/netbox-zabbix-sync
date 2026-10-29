"""Core component of the sync process"""

import re
import ssl
from collections import defaultdict
from collections.abc import Mapping
from os import environ
from pathlib import Path
from pprint import pformat
from re import fullmatch
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

from pynetbox import api as nbapi
from pynetbox.core.query import RequestError as NetBoxRequestError
from requests.exceptions import ConnectionError as RequestsConnectionError
from zabbix_utils import APIRequestError, ProcessingError, ZabbixAPI

from netbox_zabbix_sync.modules.azure_subscription import AzureSubscription
from netbox_zabbix_sync.modules.device import (
    CLEANUP_INSTANCE_TAG,
    CLEANUP_SOURCE_TAG,
    NetboxClusterImport,
    NetboxDeviceImport,
    PhysicalDevice,
)
from netbox_zabbix_sync.modules.exceptions import SyncError, SyncInventoryError
from netbox_zabbix_sync.modules.logging import get_logger
from netbox_zabbix_sync.modules.settings import DEFAULT_CONFIG
from netbox_zabbix_sync.modules.status_journal import SyncStatusJournal
from netbox_zabbix_sync.modules.status_report import SyncStatusReport
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

    def outcome_for(self, label: str):
        """Return the recorded result and reason for one target label."""
        for reason, labels in self.failures.items():
            if label in labels:
                return "failed", reason
        for reason, labels in self.intentional.items():
            if label in labels:
                result = "removed" if reason.startswith("removed due to") else "skipped"
                return result, reason
        return None

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


class CleanupSummary:
    """Collect results from the opt-in deleted-source cleanup pass."""

    def __init__(self):
        self.removed: list[str] = []
        self.failures: list[str] = []
        self.removed_targets: list[dict[str, Any]] = []
        self.failure_targets: list[dict[str, Any]] = []

    def log(self, log):
        if self.removed:
            log.warning(
                "Deleted-source cleanup removed %s host(s): %s",
                len(self.removed),
                ", ".join(self.removed),
            )
        if self.failures:
            log.error(
                "Deleted-source cleanup failed for %s host(s): %s",
                len(self.failures),
                ", ".join(self.failures),
            )
        if not self.removed and not self.failures:
            log.info("Deleted-source cleanup completed without host removals.")


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
        self.zabbix_host = None
        self.nb_version = None

        default_config = DEFAULT_CONFIG.copy()

        combined_config = {
            **default_config,
            **(config if config else {}),
        }

        self.config: dict[str, Any] = combined_config
        self.last_unsynced_summary: UnsyncedSummary | None = None
        self.last_cleanup_summary: CleanupSummary | None = None
        self.last_status_report: SyncStatusReport | None = None

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

    @staticmethod
    def _sync_error_reason(error):
        """Translate expected sync exceptions into stable report categories."""
        message = str(error).lower()
        reason = "unclassified sync error"
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
        return reason

    def _record_sync_error(self, summary, label, error):
        """Record a stable summary category for an expected sync exception."""
        reason = self._sync_error_reason(error)
        summary.record(label, reason)

    @staticmethod
    def _record_id(record: Any) -> int:
        """Get an integer NetBox record ID from an API record or serialized dict."""
        record_id: Any = (
            record.get("id")
            if isinstance(record, dict)
            else getattr(record, "id", None)
        )
        if record_id is None:
            raise ValueError("NetBox record is missing an ID")
        return int(record_id)

    @staticmethod
    def _host_label(host):
        """Build a stable Zabbix host label for cleanup logs and summaries."""
        return str(host.get("host") or host.get("name") or host.get("hostid"))

    @staticmethod
    def _compile_custom_field_filters(custom_field_filters, object_type):
        """Compile configured custom-field regexes or raise a clear config error."""
        if isinstance(custom_field_filters, Mapping):
            filter_maps = (custom_field_filters,)
        elif isinstance(custom_field_filters, tuple):
            filter_maps = custom_field_filters
        else:
            raise TypeError(
                f"Custom-field filters for {object_type} must be a mapping or "
                "a tuple of mappings from field names to regex patterns."
            )

        compiled_filters = []
        for filter_map in filter_maps:
            if not isinstance(filter_map, Mapping):
                raise TypeError(
                    f"Custom-field filters for {object_type} must each be a mapping "
                    "from field names to regex patterns."
                )
            for field_name, pattern in filter_map.items():
                if not isinstance(field_name, str) or not field_name:
                    raise ValueError(
                        f"Custom-field filter names for {object_type} must be "
                        "non-empty strings."
                    )
                if not isinstance(pattern, str):
                    raise TypeError(
                        f"Regex for {object_type} custom field {field_name!r} "
                        "must be a string."
                    )
                try:
                    compiled_filters.append(
                        (field_name, re.compile(pattern, re.IGNORECASE))
                    )
                except re.error as error:
                    raise ValueError(
                        f"Invalid regex for {object_type} custom field "
                        f"{field_name!r}: {error}"
                    ) from error
        return compiled_filters

    @staticmethod
    def _custom_field_value_matches(value, pattern):
        """Match a regex against a custom-field value; empty values pass."""
        if value is None:
            return True
        if isinstance(value, str):
            return not value or pattern.search(value) is not None
        if isinstance(value, Mapping):
            return not value or pattern.search(str(value)) is not None
        if isinstance(value, (list, tuple, set)):
            values = [item for item in value if item is not None and item != ""]
            return not values or any(pattern.search(str(item)) for item in values)
        return pattern.search(str(value)) is not None

    @classmethod
    def _filter_custom_fields(cls, records, compiled_filters):
        """Apply every configured custom-field regex to a set of records."""
        if not compiled_filters:
            return records

        filtered_records = []
        for record in records:
            custom_fields = getattr(record, "custom_fields", None)
            if not isinstance(custom_fields, Mapping):
                custom_fields = {}
            if all(
                cls._custom_field_value_matches(custom_fields.get(field_name), pattern)
                for field_name, pattern in compiled_filters
            ):
                filtered_records.append(record)
        return filtered_records

    def _collect_filtered_objects(
        self, endpoint, filters, custom_field_filters=None, object_type="NetBox object"
    ):
        """Return filtered objects and the complete candidate set for the endpoint."""
        compiled_custom_field_filters = self._compile_custom_field_filters(
            custom_field_filters if custom_field_filters is not None else {},
            object_type,
        )
        filtered = list(endpoint.filter(**filters))
        candidates = list(endpoint.all()) if filters else list(filtered)
        records_by_id = {self._record_id(record): record for record in candidates}
        records_by_id.update({self._record_id(record): record for record in filtered})
        filtered = self._filter_custom_fields(filtered, compiled_custom_field_filters)
        return filtered, list(records_by_id.values())

    def _add_status_target(
        self,
        report,
        target_type,
        nb_object,
        name,
        hostid_custom_field,
        *,
        result="pending",
        reason=None,
    ):
        custom_fields = getattr(nb_object, "custom_fields", None)
        hostid = (
            custom_fields.get(hostid_custom_field)
            if hostid_custom_field and hasattr(custom_fields, "get")
            else None
        )
        return report.add_target(
            target_type=target_type,
            name=str(name),
            netbox_id=self._record_id(nb_object),
            netbox_status=report.netbox_status(nb_object),
            zabbix_hostid=hostid,
            sync_result=result,
            reason=reason,
            source_record=nb_object,
            hostid_custom_field=hostid_custom_field,
        )

    @staticmethod
    def _apply_summary_outcome(report, target_key, summary, label):
        outcome = summary.outcome_for(label)
        if outcome is None:
            report.set_result(target_key, "synced")
        else:
            result, reason = outcome
            report.set_result(target_key, result, reason)

    def _add_device_status_targets(self, report, nb_device, *, selected, keys_by_label):
        """Register primary and configured OOB imports for one device candidate."""
        filter_result = "pending" if selected else "excluded_by_filter"
        filter_reason = (
            None if selected else "Does not match configured device sync filters."
        )
        primary_label = self._summary_label("Device", nb_device.name)
        primary_key = self._add_status_target(
            report,
            "device",
            nb_device,
            nb_device.name,
            self.config["device_cf"],
            result=filter_result,
            reason=filter_reason,
        )
        keys_by_label[primary_label] = primary_key

        context = getattr(nb_device, "config_context", None)
        zabbix_context = context.get("zabbix", {}) if isinstance(context, dict) else {}
        if not isinstance(zabbix_context, dict) or "oob" not in zabbix_context:
            return
        oob_context = zabbix_context["oob"]
        if oob_context is None:
            return
        oob_name = (
            self._build_oob_hostname(nb_device.name, oob_context)
            if isinstance(oob_context, dict)
            else nb_device.name
        )
        oob_label = self._summary_label("Device OOB", oob_name)
        oob_key = self._add_status_target(
            report,
            "device_oob",
            nb_device,
            oob_name,
            self.config["oob_device_cf"],
            result=filter_result,
            reason=filter_reason,
        )
        keys_by_label[oob_label] = oob_key

    def _collect_report_zabbix_status(self, report):
        """Complete report status fields using final linked-host data."""
        report.refresh_from_models()
        zabbix = cast(Any, self.zabbix)

        targets_missing_status = report.targets_missing_status()
        status_hostids = sorted(
            {target["zabbix_hostid"] for _, target in targets_missing_status}
        )
        if status_hostids:
            try:
                hosts = zabbix.host.get(
                    output=["hostid", "status"],
                    hostids=status_hostids,
                    selectInterfaces=["interfaceid", "type", "available"],
                )
            except (APIRequestError, ProcessingError) as error:
                logger.error(
                    "Status report: unable to read final Zabbix status for linked "
                    "hosts: %s",
                    error,
                )
                for key, _ in targets_missing_status:
                    report.set_status_error(
                        key, "Unable to retrieve linked Zabbix host status."
                    )
            else:
                if not isinstance(hosts, list):
                    logger.error(
                        "Status report: Zabbix returned an invalid host-status response."
                    )
                    hosts = []
                    for key, _ in targets_missing_status:
                        report.set_status_error(
                            key, "Zabbix returned an invalid host-status response."
                        )
                found_hostids = set()
                for host in hosts:
                    if not isinstance(host, dict):
                        continue
                    hostid = str(host.get("hostid", ""))
                    found_hostids.add(hostid)
                    for key, target in targets_missing_status:
                        if target["zabbix_hostid"] == hostid:
                            status = host.get("status")
                            report.set_zabbix_status(
                                key, status, host.get("interfaces")
                            )
                            if status not in (0, 1, "0", "1"):
                                logger.warning(
                                    "Status report: Zabbix host ID %s returned no "
                                    "valid enabled state.",
                                    hostid,
                                )
                                report.set_status_error(
                                    key,
                                    "Zabbix host response omitted a valid enabled state.",
                                )
                for key, target in targets_missing_status:
                    hostid = target["zabbix_hostid"]
                    if hostid not in found_hostids:
                        logger.warning(
                            "Status report: linked Zabbix host ID %s was not found.",
                            hostid,
                        )
                        report.set_status_error(
                            key, "Linked Zabbix host was not found."
                        )

        hostids = report.hostids()
        if not hostids:
            return
        try:
            interfaces = zabbix.hostinterface.get(
                hostids=hostids,
                output=["interfaceid", "hostid", "type", "available"],
            )
        except (APIRequestError, ProcessingError) as error:
            logger.error(
                "Status report: unable to read Zabbix interface availability: %s",
                error,
            )
            report.set_interface_error_for_all_hosts(
                "Unable to retrieve Zabbix interface availability."
            )
            return
        if not isinstance(interfaces, list):
            logger.error(
                "Status report: Zabbix returned an invalid interface-status response."
            )
            report.set_interface_error_for_all_hosts(
                "Zabbix returned an invalid interface-status response."
            )
            return
        interfaces_by_hostid: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for interface in interfaces:
            if not isinstance(interface, dict) or interface.get("hostid") is None:
                continue
            interfaces_by_hostid[str(interface["hostid"])].append(interface)
        for hostid in hostids:
            report.set_interfaces_for_host(hostid, interfaces_by_hostid.get(hostid, []))

    def _write_status_report(self, report):
        self._collect_report_zabbix_status(report)
        instance_id = self._sync_status_instance_id()
        if instance_id is None:
            logger.error(
                "Unable to write sync-status journals: configure "
                "sync_status_instance_id or connect with a Zabbix endpoint."
            )
        else:
            SyncStatusJournal(
                cast(Any, self.netbox).extras.journal_entries,
                instance_id,
                logger,
            ).write(report)
        try:
            report.write(Path.cwd() / "sync-status.json")
        except (OSError, TypeError, ValueError) as error:
            logger.error("Unable to write sync-status.json: %s", error)

    def _sync_status_instance_id(self) -> str | None:
        """Return the configured status-journal label or sanitized Zabbix endpoint."""
        configured_id = self.config.get("sync_status_instance_id")
        if configured_id:
            normalized_id = " ".join(str(configured_id).split())
            return normalized_id or None

        if not self.zabbix_host:
            return None
        endpoint = str(self.zabbix_host).strip()
        if not endpoint:
            return None
        parsed = urlsplit(endpoint)
        if not parsed.hostname:
            parsed = urlsplit(f"//{endpoint.lstrip('/')}")
        hostname = parsed.hostname
        if not hostname:
            return None
        if ":" in hostname:
            hostname = f"[{hostname}]"
        netloc = hostname.casefold()
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit(
            (parsed.scheme.casefold(), netloc, parsed.path.rstrip("/"), "", "")
        )

    def _cleanup_deleted_hosts(self):
        """Remove only this deployment's marked hosts whose NetBox source vanished."""
        if not self.config.get("cleanup_deleted_hosts"):
            return None

        summary = CleanupSummary()
        self.last_cleanup_summary = summary
        if self.netbox is None or self.zabbix is None:
            logger.error(
                "Deleted-source cleanup skipped: API connections are unavailable."
            )
            return summary
        # pynetbox and zabbix-utils expose their endpoint attributes dynamically,
        # so their library stubs cannot express the chained API calls below.
        netbox = cast(Any, self.netbox)
        zabbix = cast(Any, self.zabbix)
        instance_id = str(self.config.get("cleanup_instance_id", "default"))
        try:
            # Deliberately do not use the configured sync filters here: an object
            # excluded from a normal sync is still a live cleanup source.
            device_ids = {
                self._record_id(record) for record in netbox.dcim.devices.filter()
            }
            vm_ids = {
                self._record_id(record)
                for record in netbox.virtualization.virtual_machines.filter()
            }
        except Exception as error:  # Fail closed for any NetBox existence lookup error.
            logger.error(
                "Deleted-source cleanup skipped: unable to fetch complete NetBox "
                "device and VM ID sets: %s",
                error,
            )
            return summary

        try:
            hosts = zabbix.host.get(
                output=["hostid", "host", "name", "flags", "status"],
                selectInterfaces=["interfaceid", "type", "available"],
                selectTags=["tag", "value"],
                tags=[{"tag": CLEANUP_INSTANCE_TAG, "value": instance_id}],
                evaltype=0,
            )
        except (APIRequestError, ProcessingError) as error:
            logger.error(
                "Deleted-source cleanup skipped: unable to query Zabbix ownership "
                "tags for instance '%s': %s",
                instance_id,
                error,
            )
            return summary

        try:
            ontap_cluster_ids = {
                self._record_id(record)
                for record in netbox.virtualization.clusters.filter()
            }
        except Exception as error:
            logger.error(
                "Deleted-source cleanup skipped: unable to fetch the complete NetBox "
                "virtualization cluster ID set: %s",
                error,
            )
            return summary

        source_sets = {
            "device": device_ids,
            "device-oob": device_ids,
            "vm": vm_ids,
            "ontap-cluster": ontap_cluster_ids,
        }
        for host in hosts:
            label = self._host_label(host)
            tags = host.get("tags", []) or []
            instance_values = [
                str(tag.get("value", ""))
                for tag in tags
                if tag.get("tag") == CLEANUP_INSTANCE_TAG
            ]
            source_values = [
                str(tag.get("value", ""))
                for tag in tags
                if tag.get("tag") == CLEANUP_SOURCE_TAG
            ]
            if len(instance_values) != 1 or instance_values[0] != instance_id:
                logger.warning(
                    "Deleted-source cleanup: skipping host %s with ambiguous or foreign "
                    "ownership namespace tags.",
                    label,
                )
                continue
            if str(host.get("flags", "0")) == "4":
                logger.warning(
                    "Deleted-source cleanup: skipping discovery-owned host %s.", label
                )
                continue
            if len(source_values) != 1:
                logger.warning(
                    "Deleted-source cleanup: skipping host %s with ambiguous source markers.",
                    label,
                )
                continue
            marker_match = fullmatch(
                r"(device|device-oob|vm|ontap-cluster):([1-9][0-9]*)",
                source_values[0],
            )
            if not marker_match:
                logger.warning(
                    "Deleted-source cleanup: skipping host %s with malformed or foreign "
                    "source marker '%s'.",
                    label,
                    source_values[0],
                )
                continue
            source_type, source_id = marker_match.groups()
            if int(source_id) in source_sets[source_type]:
                continue
            try:
                zabbix.host.delete(host["hostid"])
            except (APIRequestError, ProcessingError) as error:
                logger.error(
                    "Deleted-source cleanup: unable to delete host %s (ID:%s): %s",
                    label,
                    host.get("hostid"),
                    error,
                )
                summary.failures.append(label)
                summary.failure_targets.append(
                    {
                        "name": label,
                        "hostid": host.get("hostid"),
                        "source_type": source_type,
                        "source_id": int(source_id),
                        "status": host.get("status"),
                        "interfaces": host.get("interfaces"),
                    }
                )
                continue
            logger.info(
                "Deleted-source cleanup: deleted host %s (ID:%s) for missing %s %s.",
                label,
                host.get("hostid"),
                source_type,
                source_id,
            )
            summary.removed.append(label)
            summary.removed_targets.append(
                {
                    "name": label,
                    "hostid": host.get("hostid"),
                    "source_type": source_type,
                    "source_id": int(source_id),
                }
            )
        summary.log(logger)
        return summary

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

    @staticmethod
    def _is_ontap_cluster(nb_cluster):
        """Return whether a virtualization cluster is a NetApp ONTAP cluster."""
        cluster_type = getattr(nb_cluster, "type", None)
        return str(getattr(cluster_type, "name", "")).casefold() == "netapp ontap"

    def _infer_cluster_site(self, nb_cluster):
        """Infer a cluster site when all attached devices share one site."""
        if self.netbox is None:
            raise SyncError("NetBox connection is not initialized.")
        netbox = cast(Any, self.netbox)
        devices = list(
            netbox.dcim.devices.filter(cluster_id=self._record_id(nb_cluster))
        )
        sites = [getattr(device, "site", None) for device in devices]
        sites = [site for site in sites if site]
        if not sites:
            logger.warning(
                "ONTAP cluster %s has no attached devices with a site.", nb_cluster.name
            )
            return None

        def site_key(site):
            for attribute in ("id", "slug", "name"):
                value = getattr(site, attribute, None)
                if value:
                    return str(value)
            return str(site)

        unique_sites = {site_key(site): site for site in sites}
        if len(unique_sites) != 1:
            logger.warning(
                "ONTAP cluster %s has attached devices in multiple sites; "
                "cannot infer a single site.",
                nb_cluster.name,
            )
            return None
        return next(iter(unique_sites.values()))

    def _process_vm_like(
        self,
        nb_object,
        source_type,
        netbox_journals,
        netbox_site_groups,
        netbox_regions,
        zabbix_groups,
        zabbix_templates,
        zabbix_proxy_list,
        summary,
        summary_label,
        status_report,
        report_key,
    ):
        """Sync a VM-shaped NetBox object, including an adapted ONTAP cluster."""
        vm = VirtualMachine(
            nb_object,
            self.zabbix,
            netbox_journals,
            self.nb_version,
            self.config["create_journal"],
            logger,
            config=self.config,
        )
        vm.normalize_hostname()
        status_report.attach_model(report_key, vm)
        vm.set_cleanup_source(source_type)
        linked_before_run = bool(vm.zabbix_id)
        logger.debug("Host %s: Started operations on %s.", vm.name, source_type)
        vm.set_vm_template()
        if not vm.zbx_template_names:
            summary.record(
                summary_label,
                self._template_skip_reason(nb_object, self.config, vm=True),
            )
            return

        def resolve_hostgroups():
            if vm.set_hostgroup(
                self.config["vm_hostgroup_format"], netbox_site_groups, netbox_regions
            ):
                return True
            fallback_group = self.config["vm_hostgroup_fallback"]
            if not isinstance(fallback_group, str) or not fallback_group.strip():
                raise SyncInventoryError(
                    "vm_hostgroup_fallback must be a non-empty string."
                )
            vm.hostgroups = [fallback_group]
            logger.warning(
                "Host %s: VM hostgroup format produced no groups; using fallback "
                "hostgroup '%s'.",
                vm.name,
                fallback_group,
            )
            return True

        if self.config["extended_site_properties"] and nb_object.site:
            logger.debug("Host %s: extending site information.", vm.name)
            nb_object.site.full_details()
        vm.set_inventory(nb_object)
        vm.set_usermacros()
        vm.set_tags()
        vm.set_tls()
        logger.debug("Host %s NetBox data: %s", vm.name, pformat(dict(nb_object)))
        if source_type == "vm" and vm.repair_vmware_vm_lld_host_link() is False:
            summary.record(
                summary_label,
                "VMware LLD host link is not configured for safe dual-host sync",
                intentional=True,
            )
            return
        if vm.status in self.config["zabbix_device_removal"]:
            if vm.zabbix_id:
                vm.cleanup()
                logger.info("Host %s: cleanup complete", vm.name)
                summary.record(
                    summary_label,
                    f"removed due to NetBox status {vm.status}",
                    intentional=True,
                )
                return
            logger.info("Host %s: Skipping since this host is not active.", vm.name)
            summary.record(
                summary_label,
                f"excluded due to NetBox status {vm.status}",
                intentional=True,
            )
            return
        if vm.status in self.config["zabbix_device_disable"]:
            vm.zabbix_state = 1
        host_consistent = False
        if vm.zabbix_id:
            host_consistent = vm.consistency_check(
                zabbix_groups,
                zabbix_templates,
                zabbix_proxy_list,
                self.config["full_proxy_sync"],
                self.config["create_hostgroups"],
                full_sync=True,
                cleanup_ownership=bool(
                    self.config.get("cleanup_deleted_hosts") and linked_before_run
                ),
                hostgroup_resolver=resolve_hostgroups,
            )
        if not host_consistent:
            adopted = vm.adopt_existing_zabbix_host()
            cleanup_ownership = bool(
                self.config.get("cleanup_deleted_hosts")
                and (linked_before_run or not adopted)
            )
            full_sync = not (
                adopted
                and (
                    vm.adopted_azure_discovered_host
                    or str(self.config.get("adopt_enrich_mode", "full")).lower()
                    == "metadata_only"
                )
            )
            if vm.zabbix_id:
                host_consistent = vm.consistency_check(
                    zabbix_groups,
                    zabbix_templates,
                    zabbix_proxy_list,
                    self.config["full_proxy_sync"],
                    self.config["create_hostgroups"],
                    full_sync=full_sync,
                    cleanup_ownership=cleanup_ownership,
                    hostgroup_resolver=resolve_hostgroups,
                )
        if host_consistent:
            vm.sync_adopted_lld_host()
            return
        if not resolve_hostgroups():
            summary.record(summary_label, "no usable Zabbix hostgroup")
            return
        if self.config["create_hostgroups"]:
            hostgroups = vm.create_zbx_hostgroup(zabbix_groups)
            zabbix_groups.extend(hostgroups)
        vm.create_in_zabbix(zabbix_groups, zabbix_templates, zabbix_proxy_list)
        if not vm.zabbix_id:
            summary.record(
                summary_label, "Zabbix host already exists without NetBox linkage"
            )
        else:
            vm.sync_adopted_lld_host()

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
        status_report,
        report_key,
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
        status_report.attach_model(report_key, device)
        device.set_cleanup_source("device-oob" if split_import else "device")
        linked_before_run = bool(device.zabbix_id)
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

        def resolve_hostgroups():
            return device.set_hostgroup(
                device_config["hostgroup_format"], netbox_site_groups, netbox_regions
            )

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
        device.normalize_hostname()
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
            else:
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
        host_consistent = False
        if device.zabbix_id:
            host_consistent = device.consistency_check(
                zabbix_groups,
                zabbix_templates,
                zabbix_proxy_list,
                device_config["full_proxy_sync"],
                device_config["create_hostgroups"],
                full_sync=True,
                cleanup_ownership=bool(
                    device_config.get("cleanup_deleted_hosts") and linked_before_run
                ),
                hostgroup_resolver=resolve_hostgroups,
            )
        if not host_consistent:
            adopted = device.adopt_existing_zabbix_host()
            cleanup_ownership = bool(
                device_config.get("cleanup_deleted_hosts")
                and (linked_before_run or not adopted)
            )
            full_sync = not (
                adopted
                and (
                    device.adopted_azure_discovered_host
                    or str(device_config.get("adopt_enrich_mode", "full")).lower()
                    == "metadata_only"
                )
            )
            if device.zabbix_id:
                host_consistent = device.consistency_check(
                    zabbix_groups,
                    zabbix_templates,
                    zabbix_proxy_list,
                    device_config["full_proxy_sync"],
                    device_config["create_hostgroups"],
                    full_sync=full_sync,
                    cleanup_ownership=cleanup_ownership,
                    hostgroup_resolver=resolve_hostgroups,
                )
        if host_consistent:
            device.sync_adopted_esxi_lld_host()
            return True
        if not resolve_hostgroups():
            logger.warning(
                "Host %s: has no valid hostgroups, Skipping this host...",
                device.name,
            )
            summary.record(summary_label, "no usable Zabbix hostgroup")
            return True
        if device_config["create_hostgroups"]:
            hostgroups = device.create_zbx_hostgroup(zabbix_groups)
            zabbix_groups.extend(hostgroups)
        # Add device to Zabbix
        device.create_in_zabbix(zabbix_groups, zabbix_templates, zabbix_proxy_list)
        if not device.zabbix_id:
            summary.record(
                summary_label, "Zabbix host already exists without NetBox linkage"
            )
        else:
            device.sync_adopted_esxi_lld_host()
        return True

    def _process_azure_subscription(
        self,
        nb_tenant,
        zabbix_groups,
        zabbix_templates,
        status_report,
        report_key,
    ) -> tuple[str, str | None]:
        """Run the Azure subscription sync pipeline for one NetBox Tenant."""
        azure_subscription = AzureSubscription(
            nb_tenant,
            self.zabbix,
            logger,
            config=self.config,
        )
        status_report.attach_model(report_key, azure_subscription)
        logger.debug(
            "Host %s: Started operations on Azure subscription.",
            azure_subscription.name,
        )
        if not azure_subscription.validate():
            return "skipped", "Azure subscription validation failed"
        azure_subscription.set_usermacros()
        had_linked_hostid = bool(azure_subscription.zabbix_id)
        adopted = azure_subscription.adopt_existing_zabbix_host()
        host_consistent = False
        if azure_subscription.zabbix_id:
            host_consistent = azure_subscription.consistency_check(
                zabbix_groups,
                zabbix_templates,
                self.config["create_hostgroups"],
            )
        if not host_consistent and (had_linked_hostid or adopted):
            adopted = azure_subscription.adopt_existing_zabbix_host()
            if azure_subscription.zabbix_id:
                host_consistent = azure_subscription.consistency_check(
                    zabbix_groups,
                    zabbix_templates,
                    self.config["create_hostgroups"],
                )
        if host_consistent:
            return "synced", None
        if not azure_subscription.zabbix_id:
            adopted = False
        if not adopted:
            created = azure_subscription.create_in_zabbix(
                zabbix_groups,
                zabbix_templates,
                self.config["create_hostgroups"],
            )
            if not created:
                return "failed", "Zabbix host already exists without NetBox linkage"
        return "synced", None

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
        self.zabbix_host = zbx_host
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
        status_report = SyncStatusReport()
        self.last_status_report = status_report
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
        netbox_devices, all_netbox_devices = self._collect_filtered_objects(
            self.netbox.dcim.devices,
            dev_filter_combined,
            (
                self.config["nb_custom_field_filter"],
                self.config["nb_device_custom_field_filter"],
            ),
            "device",
        )
        netbox_vms = []
        all_netbox_vms = []
        if self.config["sync_vms"]:
            vm_filter_combined = self._combine_filters(
                self.config["nb_vm_filter"], vm_filter
            )
            netbox_vms, all_netbox_vms = self._collect_filtered_objects(
                self.netbox.virtualization.virtual_machines,
                vm_filter_combined,
                (
                    self.config["nb_custom_field_filter"],
                    self.config["nb_vm_custom_field_filter"],
                ),
                "VM",
            )
        netbox_ontap_clusters = []
        all_netbox_ontap_clusters = []
        if self.config["sync_ontap_clusters"]:
            (
                netbox_ontap_clusters,
                all_netbox_ontap_clusters,
            ) = self._collect_filtered_objects(
                self.netbox.virtualization.clusters,
                self.config["nb_ontap_cluster_filter"],
                (
                    self.config["nb_custom_field_filter"],
                    self.config["nb_ontap_cluster_custom_field_filter"],
                ),
                "ONTAP cluster",
            )
        netbox_azure_subscriptions = []
        all_netbox_azure_subscriptions = []
        if self.config["sync_azure_subscriptions"]:
            (
                netbox_azure_subscriptions,
                all_netbox_azure_subscriptions,
            ) = self._collect_filtered_objects(
                self.netbox.tenancy.tenants,
                {"tag": self.config["azure_tag"]},
                (
                    self.config["nb_custom_field_filter"],
                    self.config["nb_azure_subscription_custom_field_filter"],
                ),
                "Azure subscription Tenant",
            )

        report_keys_by_label = {}
        selected_device_ids = {self._record_id(record) for record in netbox_devices}
        for nb_device in all_netbox_devices:
            selected = self._record_id(nb_device) in selected_device_ids
            self._add_device_status_targets(
                status_report,
                nb_device,
                selected=selected,
                keys_by_label=report_keys_by_label,
            )

        selected_vm_ids = {self._record_id(record) for record in netbox_vms}
        for nb_vm in all_netbox_vms:
            selected = self._record_id(nb_vm) in selected_vm_ids
            label = self._summary_label("VM", nb_vm.name)
            key = self._add_status_target(
                status_report,
                "vm",
                nb_vm,
                nb_vm.name,
                self.config["device_cf"],
                result="pending" if selected else "excluded_by_filter",
                reason=None
                if selected
                else "Does not match configured VM sync filters.",
            )
            report_keys_by_label[label] = key

        selected_cluster_ids = {
            self._record_id(record) for record in netbox_ontap_clusters
        }
        for nb_cluster in all_netbox_ontap_clusters:
            if not self._is_ontap_cluster(nb_cluster):
                continue
            selected = self._record_id(nb_cluster) in selected_cluster_ids
            label = self._summary_label("ONTAP cluster", nb_cluster.name)
            key = self._add_status_target(
                status_report,
                "ontap_cluster",
                nb_cluster,
                nb_cluster.name,
                self.config["device_cf"],
                result="pending" if selected else "excluded_by_filter",
                reason=None
                if selected
                else "Does not match configured ONTAP cluster sync filters.",
            )
            report_keys_by_label[label] = key

        selected_tenant_ids = {
            self._record_id(record) for record in netbox_azure_subscriptions
        }
        for nb_tenant in all_netbox_azure_subscriptions:
            selected = self._record_id(nb_tenant) in selected_tenant_ids
            label = self._summary_label("Azure subscription", nb_tenant.name)
            key = self._add_status_target(
                status_report,
                "azure_subscription",
                nb_tenant,
                nb_tenant.name,
                self.config["azure_zabbix_hostid_cf"],
                result="pending" if selected else "excluded_by_filter",
                reason=None
                if selected
                else "Does not match configured Azure subscription sync filters.",
            )
            report_keys_by_label[label] = key

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
            label = self._summary_label("Azure subscription", nb_tenant.name)
            key = report_keys_by_label[label]
            try:
                outcome, reason = self._process_azure_subscription(
                    nb_tenant,
                    zabbix_groups,
                    zabbix_templates,
                    status_report,
                    key,
                )
            except SyncError as error:
                status_report.set_result(key, "failed", self._sync_error_reason(error))
            else:
                status_report.set_result(key, outcome, reason)

        # Go through all NetBox VMs.
        for nb_vm in netbox_vms:
            summary_label = self._summary_label("VM", nb_vm.name)
            report_key = report_keys_by_label[summary_label]
            try:
                self._process_vm_like(
                    nb_vm,
                    "vm",
                    netbox_journals,
                    netbox_site_groups,
                    netbox_regions,
                    zabbix_groups,
                    zabbix_templates,
                    zabbix_proxy_list,
                    summary,
                    summary_label,
                    status_report,
                    report_key,
                )
            except SyncError as error:
                self._record_sync_error(summary, summary_label, error)
            self._apply_summary_outcome(
                status_report, report_key, summary, summary_label
            )

        for nb_cluster in netbox_ontap_clusters:
            if not self._is_ontap_cluster(nb_cluster):
                continue
            summary_label = self._summary_label("ONTAP cluster", nb_cluster.name)
            report_key = report_keys_by_label[summary_label]
            try:
                cluster_import = NetboxClusterImport(
                    nb_cluster,
                    self._infer_cluster_site(nb_cluster),
                    self.config["ontap_cluster_primary_ip_cf"],
                )
                self._process_vm_like(
                    cluster_import,
                    "ontap-cluster",
                    netbox_journals,
                    netbox_site_groups,
                    netbox_regions,
                    zabbix_groups,
                    zabbix_templates,
                    zabbix_proxy_list,
                    summary,
                    summary_label,
                    status_report,
                    report_key,
                )
            except SyncError as error:
                self._record_sync_error(summary, summary_label, error)
            self._apply_summary_outcome(
                status_report, report_key, summary, summary_label
            )

        for nb_device in netbox_devices:
            for (
                device_import,
                device_config,
                split_import,
                summary_label,
            ) in self._get_device_imports(nb_device, summary):
                report_key = report_keys_by_label[summary_label]
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
                        status_report,
                        report_key,
                        split_import=split_import,
                    )
                except SyncError as error:
                    self._record_sync_error(summary, summary_label, error)
                self._apply_summary_outcome(
                    status_report, report_key, summary, summary_label
                )
            config_context = getattr(nb_device, "config_context", None)
            zabbix_context = (
                config_context.get("zabbix", {})
                if isinstance(config_context, dict)
                else {}
            )
            if isinstance(zabbix_context, dict):
                oob_context = zabbix_context.get("oob")
                if oob_context is not None:
                    oob_name = (
                        self._build_oob_hostname(nb_device.name, oob_context)
                        if isinstance(oob_context, dict)
                        else nb_device.name
                    )
                    oob_label = self._summary_label("Device OOB", oob_name)
                    oob_key = report_keys_by_label.get(oob_label)
                    if oob_key is not None:
                        self._apply_summary_outcome(
                            status_report, oob_key, summary, oob_label
                        )

        cleanup_summary = self._cleanup_deleted_hosts()
        if cleanup_summary is not None:
            for target in cleanup_summary.removed_targets:
                key = status_report.add_target(
                    target_type=target["source_type"].replace("-", "_"),
                    name=target["name"],
                    netbox_id=target["source_id"],
                    zabbix_hostid=target["hostid"],
                    sync_result="removed",
                    reason="Removed by deleted-source cleanup.",
                )
                status_report.set_zabbix_status(key, None)
            for target in cleanup_summary.failure_targets:
                key = status_report.add_target(
                    target_type=target["source_type"].replace("-", "_"),
                    name=target["name"],
                    netbox_id=target["source_id"],
                    zabbix_hostid=target["hostid"],
                    sync_result="failed",
                    reason="Deleted-source cleanup failed.",
                )
                status_report.set_zabbix_status(
                    key, target["status"], target["interfaces"]
                )
        self._write_status_report(status_report)
        summary.log(logger)
        return True
