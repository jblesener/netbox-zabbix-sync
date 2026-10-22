"""Transition-based feedback of Zabbix sync outcomes in NetBox journals."""

from hashlib import sha256
from typing import Any

from pynetbox.core.query import RequestError as NetBoxRequestError
from requests.exceptions import RequestException

_MARKER_PREFIX = "[NetBox-Zabbix-Sync status v1:"
_SOURCE_CONTENT_TYPES = {
    "device": "dcim.device",
    "device_oob": "dcim.device",
    "vm": "virtualization.virtualmachine",
    "ontap_cluster": "virtualization.cluster",
    "azure_subscription": "tenancy.tenant",
}


class SyncStatusJournal:
    """Append a journal entry only when a target's outcome changes."""

    def __init__(self, journal_endpoint: Any, instance_id: str, logger: Any):
        self.journal_endpoint = journal_endpoint
        self.instance_id = instance_id
        self.logger = logger

    def _marker(self, target_type: str) -> str:
        identity = f"{self.instance_id}\0{target_type}".encode()
        return f"{_MARKER_PREFIX}{sha256(identity).hexdigest()}]"

    @staticmethod
    def _comments(entry: Any) -> str | None:
        comments = (
            entry.get("comments")
            if isinstance(entry, dict)
            else getattr(entry, "comments", None)
        )
        return comments if isinstance(comments, str) else None

    @staticmethod
    def _state(comments: str) -> tuple[str, str | None] | None:
        outcome = None
        reason = None
        for line in comments.splitlines()[1:]:
            if line.startswith("Outcome: "):
                outcome = line.removeprefix("Outcome: ").strip()
            elif line.startswith("Reason: "):
                reason = line.removeprefix("Reason: ").strip() or None
        if outcome not in {"synced", "failed"}:
            return None
        return outcome, reason

    def _previous_state(
        self,
        content_type: str,
        record_id: int,
        marker: str,
    ) -> tuple[str, str | None] | None:
        entries = self.journal_endpoint.filter(
            assigned_object_type=content_type,
            assigned_object_id=record_id,
            q=marker,
            ordering="-created",
        )
        for entry in entries:
            comments = self._comments(entry)
            if comments is None or not comments.splitlines():
                continue
            if comments.splitlines()[0] == marker:
                return self._state(comments)
        return None

    @staticmethod
    def _entry_state(target: dict[str, Any]) -> tuple[str, str | None]:
        result = target["sync_result"]
        reason = target.get("reason") if result == "failed" else None
        return result, reason

    def _render_comments(self, marker: str, target: dict[str, Any]) -> str:
        result, reason = self._entry_state(target)
        lines = [
            marker,
            f"Outcome: {result}",
            f"Zabbix instance: {self.instance_id}",
            f"Target: {target['type']} {target['name']}",
        ]
        if reason:
            lines.append(f"Reason: {reason}")
        return "\n".join(lines)

    def write(self, report: Any):
        """Write changed sync outcomes for targets with a source NetBox record."""
        for target, _source_record in report.targets_with_source_records():
            if target["sync_result"] not in {"synced", "failed"}:
                continue
            content_type = _SOURCE_CONTENT_TYPES.get(target["type"])
            if content_type is None or target["netbox_id"] is None:
                continue
            marker = self._marker(target["type"])
            record_id = int(target["netbox_id"])
            try:
                previous = self._previous_state(content_type, record_id, marker)
            except (NetBoxRequestError, RequestException) as error:
                self.logger.error(
                    "Unable to read previous sync-status journal for %s %s: %s",
                    content_type,
                    record_id,
                    error,
                )
                continue

            desired_state = self._entry_state(target)
            if previous == desired_state:
                continue
            kind = "success" if desired_state[0] == "synced" else "danger"
            payload = {
                "assigned_object_type": content_type,
                "assigned_object_id": record_id,
                "kind": kind,
                "comments": self._render_comments(marker, target),
            }
            try:
                self.journal_endpoint.create(payload)
            except (NetBoxRequestError, RequestException) as error:
                self.logger.error(
                    "Unable to write sync-status journal for %s %s (%s, %s): %s",
                    content_type,
                    record_id,
                    target["type"],
                    self.instance_id,
                    error,
                )
                continue
            self.logger.debug(
                "Recorded %s sync status for %s %s (%s).",
                desired_state[0],
                content_type,
                record_id,
                self.instance_id,
            )
