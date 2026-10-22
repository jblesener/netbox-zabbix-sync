"""Machine-readable status report for one synchronization run."""

import json
from datetime import UTC, datetime
from os import replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


class SyncStatusReport:
    """Collect target outcomes and write a JSON report."""

    def __init__(self):
        self._targets: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._source_records: dict[tuple[str, str, str], tuple[Any, str | None]] = {}
        self._models: dict[tuple[str, str, str], Any] = {}

    @staticmethod
    def _hostid(value: Any) -> str | None:
        if isinstance(value, (str, int)) and not isinstance(value, bool) and value:
            return str(value)
        return None

    @staticmethod
    def _json_value(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    @classmethod
    def _interface_list(cls, interfaces: Any) -> list[dict[str, Any]] | None:
        if interfaces is None:
            return None
        if not isinstance(interfaces, list):
            return None
        return [
            {
                "interfaceid": cls._json_value(interface.get("interfaceid")),
                "type": cls._json_value(interface.get("type")),
                "availability": cls._json_value(interface.get("available")),
            }
            for interface in interfaces
            if isinstance(interface, dict)
        ]

    @classmethod
    def _enabled_state(cls, status: Any) -> str | None:
        if status in (0, "0"):
            return "enabled"
        if status in (1, "1"):
            return "disabled"
        return None

    @staticmethod
    def netbox_status(record: Any) -> str | None:
        status = getattr(record, "status", None)
        if status is None:
            return None
        for attribute in ("label", "name", "value"):
            value = getattr(status, attribute, None)
            if isinstance(value, (str, int, float)):
                return str(value)
        if isinstance(status, (str, int, float)):
            return str(status)
        return None

    @staticmethod
    def _key(
        target_type: str,
        netbox_id: int | str | None,
        name: str,
        zabbix_hostid: Any = None,
    ) -> tuple[str, str, str]:
        source_id = (
            str(netbox_id)
            if netbox_id is not None
            else f"zabbix:{SyncStatusReport._hostid(zabbix_hostid) or ''}"
        )
        return target_type, source_id, name

    def add_target(
        self,
        *,
        target_type: str,
        name: str,
        netbox_id: int | str | None = None,
        netbox_status: str | None = None,
        zabbix_hostid: Any = None,
        sync_result: str = "pending",
        reason: str | None = None,
        source_record: Any = None,
        hostid_custom_field: str | None = None,
    ) -> tuple[str, str, str]:
        """Add or update one target record and return its internal key."""
        key = self._key(target_type, netbox_id, name, zabbix_hostid)
        self._targets[key] = {
            "type": target_type,
            "netbox_id": self._json_value(netbox_id),
            "name": name,
            "netbox_status": netbox_status,
            "zabbix_hostid": self._hostid(zabbix_hostid),
            "zabbix_enabled": None,
            "zabbix_interfaces": None,
            "sync_result": sync_result,
            "reason": reason,
            "status_error": None,
        }
        if source_record is not None:
            self._source_records[key] = (source_record, hostid_custom_field)
        return key

    def set_result(
        self,
        key: tuple[str, str, str],
        sync_result: str,
        reason: str | None = None,
    ):
        target = self._targets.get(key)
        if target is not None:
            target["sync_result"] = sync_result
            target["reason"] = reason

    def attach_model(self, key: tuple[str, str, str], model: Any):
        self._models[key] = model

    def set_zabbix_status(
        self,
        key: tuple[str, str, str],
        status: Any,
        interfaces: Any = None,
    ):
        target = self._targets.get(key)
        if target is not None:
            target["zabbix_enabled"] = self._enabled_state(status)
            target["zabbix_interfaces"] = self._interface_list(interfaces)

    def set_status_error(self, key: tuple[str, str, str], message: str):
        target = self._targets.get(key)
        if target is not None:
            target["status_error"] = message

    def refresh_from_models(self):
        """Refresh linked host IDs and status fields after all sync operations."""
        for key, target in self._targets.items():
            model = self._models.get(key)
            if model is not None:
                hostid = self._hostid(getattr(model, "zabbix_id", None))
                if hostid is not None or getattr(model, "zabbix_id", None) is None:
                    target["zabbix_hostid"] = hostid

                status = getattr(model, "zabbix_status", None)
                if status is not None:
                    target["zabbix_enabled"] = self._enabled_state(status)
                interfaces = getattr(model, "zabbix_interfaces", None)
                if interfaces is not None:
                    target["zabbix_interfaces"] = self._interface_list(interfaces)

            source = self._source_records.get(key)
            if source is not None and model is None:
                record, custom_field = source
                custom_fields = getattr(record, "custom_fields", None)
                if custom_field and hasattr(custom_fields, "get"):
                    target["zabbix_hostid"] = self._hostid(
                        custom_fields.get(custom_field)
                    )

    def targets_with_result(
        self, result: str
    ) -> list[tuple[tuple[str, str, str], dict]]:
        return [
            (key, target)
            for key, target in self._targets.items()
            if target["sync_result"] == result
        ]

    def targets_with_source_records(self) -> list[tuple[dict[str, Any], Any]]:
        """Return targets that are linked to their original NetBox records."""
        return [
            (target, source[0])
            for key, target in self._targets.items()
            if (source := self._source_records.get(key)) is not None
        ]

    def targets_missing_status(self) -> list[tuple[tuple[str, str, str], dict]]:
        return [
            (key, target)
            for key, target in self._targets.items()
            if target["zabbix_hostid"] is not None
            and target["zabbix_enabled"] is None
            and target["sync_result"] != "removed"
        ]

    def targets_with_hostid(self) -> list[tuple[tuple[str, str, str], dict]]:
        return [
            (key, target)
            for key, target in self._targets.items()
            if target["zabbix_hostid"] is not None
            and target["sync_result"] != "removed"
        ]

    def hostids(self) -> list[str]:
        return sorted(
            {
                target["zabbix_hostid"]
                for target in self._targets.values()
                if target["zabbix_hostid"] is not None
                and target["sync_result"] != "removed"
            }
        )

    def set_interface_error_for_all_hosts(self, message: str):
        for key, target in self.targets_with_hostid():
            target["zabbix_interfaces"] = None
            self.set_status_error(key, message)

    def set_interfaces_for_host(self, hostid: Any, interfaces: Any):
        normalized_hostid = self._hostid(hostid)
        if normalized_hostid is None:
            return
        normalized_interfaces = self._interface_list(interfaces)
        for target in self._targets.values():
            if target["zabbix_hostid"] == normalized_hostid:
                target["zabbix_interfaces"] = normalized_interfaces

    def document(self) -> dict[str, Any]:
        targets = sorted(
            self._targets.values(),
            key=lambda target: (
                target["type"].casefold(),
                target["name"].casefold(),
                str(target["netbox_id"] or ""),
            ),
        )
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "targets": targets,
        }

    def write(self, file_path: str | Path):
        """Atomically write the report so readers never see partial JSON."""
        path = Path(file_path)
        temporary_path = None
        try:
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(self.document(), temporary_file, indent=2)
                temporary_file.write("\n")
            replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
