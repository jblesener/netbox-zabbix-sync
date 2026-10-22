"""Tests for transition-based NetBox sync-status journals."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from requests.exceptions import ConnectionError

from netbox_zabbix_sync.modules.core import Sync
from netbox_zabbix_sync.modules.status_journal import SyncStatusJournal
from netbox_zabbix_sync.modules.status_report import SyncStatusReport

RECORD_ID = 123
EXPECTED_TARGET_COUNT = 2


def _report(target_type="device", result="synced", reason=None, record_id=123):
    report = SyncStatusReport()
    record = SimpleNamespace(id=record_id)
    key = report.add_target(
        target_type=target_type,
        name="router-01",
        netbox_id=record_id,
        sync_result=result,
        reason=reason,
        source_record=record,
    )
    return report, key


def _writer(endpoint, instance_id="https://zabbix.example"):
    return SyncStatusJournal(endpoint, instance_id, MagicMock())


def test_writes_successful_outcome_to_source_record():
    report, _ = _report()
    endpoint = MagicMock()
    endpoint.filter.return_value = []
    writer = _writer(endpoint)

    writer.write(report)

    endpoint.filter.assert_called_once_with(
        assigned_object_type="dcim.device",
        assigned_object_id=123,
        q=writer._marker("device"),
        ordering="-created",
    )
    payload = endpoint.create.call_args.args[0]
    assert payload["assigned_object_type"] == "dcim.device"
    assert payload["assigned_object_id"] == RECORD_ID
    assert payload["kind"] == "success"
    assert "Outcome: synced" in payload["comments"]
    assert "Zabbix instance: https://zabbix.example" in payload["comments"]


def test_does_not_repeat_an_unchanged_outcome():
    report, _ = _report()
    endpoint = MagicMock()
    endpoint.filter.return_value = []
    writer = _writer(endpoint)
    writer.write(report)
    prior_comments = endpoint.create.call_args.args[0]["comments"]
    endpoint.create.reset_mock()
    endpoint.filter.return_value = [SimpleNamespace(comments=prior_comments)]

    writer.write(report)

    endpoint.create.assert_not_called()


def test_records_result_transitions_and_changed_failure_reasons():
    report, key = _report()
    endpoint = MagicMock()
    endpoint.filter.return_value = []
    writer = _writer(endpoint)
    writer.write(report)
    previous_comments = endpoint.create.call_args.args[0]["comments"]

    report.set_result(key, "failed", "no primary IP")
    endpoint.filter.return_value = [SimpleNamespace(comments=previous_comments)]
    writer.write(report)
    failed_comments = endpoint.create.call_args.args[0]["comments"]
    assert endpoint.create.call_args.args[0]["kind"] == "danger"
    assert "Reason: no primary IP" in failed_comments

    endpoint.create.reset_mock()
    report.set_result(key, "failed", "external NetBox or Zabbix sync error")
    endpoint.filter.return_value = [SimpleNamespace(comments=failed_comments)]
    writer.write(report)
    changed_reason_comments = endpoint.create.call_args.args[0]["comments"]
    assert "Reason: external NetBox or Zabbix sync error" in changed_reason_comments

    endpoint.create.reset_mock()
    endpoint.filter.return_value = [SimpleNamespace(comments=changed_reason_comments)]
    writer.write(report)
    endpoint.create.assert_not_called()

    report.set_result(key, "synced")
    endpoint.filter.return_value = [SimpleNamespace(comments=changed_reason_comments)]
    writer.write(report)
    assert endpoint.create.call_args.args[0]["kind"] == "success"
    assert "Outcome: synced" in endpoint.create.call_args.args[0]["comments"]


def test_other_zabbix_instance_does_not_suppress_status():
    report, _ = _report()
    first_endpoint = MagicMock()
    first_endpoint.filter.return_value = []
    _writer(first_endpoint, "zabbix-a").write(report)
    prior_comments = first_endpoint.create.call_args.args[0]["comments"]

    second_endpoint = MagicMock()
    second_endpoint.filter.return_value = [SimpleNamespace(comments=prior_comments)]
    _writer(second_endpoint, "zabbix-b").write(report)

    second_endpoint.create.assert_called_once()


def test_oob_and_primary_targets_are_journaled_separately_on_one_device():
    report = SyncStatusReport()
    record = SimpleNamespace(id=123)
    for target_type, name in (("device", "router-01"), ("device_oob", "router-oob")):
        report.add_target(
            target_type=target_type,
            name=name,
            netbox_id=123,
            sync_result="synced",
            source_record=record,
        )
    endpoint = MagicMock()
    endpoint.filter.return_value = []

    _writer(endpoint).write(report)

    assert endpoint.filter.call_count == EXPECTED_TARGET_COUNT
    assert endpoint.create.call_count == EXPECTED_TARGET_COUNT
    comments = [call.args[0]["comments"] for call in endpoint.create.call_args_list]
    assert comments[0].splitlines()[0] != comments[1].splitlines()[0]


@pytest.mark.parametrize(
    ("target_type", "content_type"),
    [
        ("device_oob", "dcim.device"),
        ("vm", "virtualization.virtualmachine"),
        ("ontap_cluster", "virtualization.cluster"),
        ("azure_subscription", "tenancy.tenant"),
    ],
)
def test_source_types_use_their_netbox_journal_content_types(target_type, content_type):
    report, _ = _report(target_type=target_type)
    endpoint = MagicMock()
    endpoint.filter.return_value = []

    _writer(endpoint).write(report)

    assert endpoint.filter.call_args.kwargs["assigned_object_type"] == content_type
    assert endpoint.create.call_args.args[0]["assigned_object_type"] == content_type


def test_skipped_outcome_is_not_journaled():
    report, _ = _report(result="skipped", reason="missing Zabbix context")
    endpoint = MagicMock()

    _writer(endpoint).write(report)

    endpoint.filter.assert_not_called()
    endpoint.create.assert_not_called()


def test_journal_lookup_failure_is_logged_without_raising():
    report, _ = _report()
    endpoint = MagicMock()
    endpoint.filter.side_effect = ConnectionError("NetBox unavailable")
    logger = MagicMock()

    SyncStatusJournal(endpoint, "zabbix-a", logger).write(report)

    endpoint.create.assert_not_called()
    logger.error.assert_called_once()


def test_journal_write_failure_is_logged_without_raising():
    report, _ = _report()
    endpoint = MagicMock()
    endpoint.filter.return_value = []
    endpoint.create.side_effect = ConnectionError("NetBox unavailable")
    logger = MagicMock()

    SyncStatusJournal(endpoint, "zabbix-a", logger).write(report)

    logger.error.assert_called_once()


def test_instance_identity_sanitizes_endpoint_and_uses_optional_label():
    syncer = Sync()
    syncer.zabbix_host = (
        "https://user:password@Zabbix.Example:8443/zabbix/api_jsonrpc.php?token=secret"
    )

    assert (
        syncer._sync_status_instance_id()
        == "https://zabbix.example:8443/zabbix/api_jsonrpc.php"
    )

    labeled_syncer = Sync({"sync_status_instance_id": " production zabbix "})
    assert labeled_syncer._sync_status_instance_id() == "production zabbix"


def test_sync_writes_journals_alongside_the_json_status_report():
    report, _ = _report()
    journal_endpoint = MagicMock()
    journal_endpoint.filter.return_value = []
    syncer = Sync()
    syncer.netbox = SimpleNamespace(
        extras=SimpleNamespace(journal_entries=journal_endpoint)
    )
    syncer.zabbix = MagicMock()
    syncer.zabbix_host = "https://zabbix.example"

    with patch.object(report, "write") as write_report:
        syncer._write_status_report(report)

    journal_endpoint.create.assert_called_once()
    write_report.assert_called_once()
