# webhook-server/tests/test_compliance.py
import pytest
from unittest.mock import MagicMock
from compliance import ComplianceGate, ComplianceBlock, ActionType

def make_contact(sms_consent=True, call_consent=True, dnc_status="clear", tags=None):
    return {
        "id": "contact_123",
        "phone": "+17035550000",
        "customFields": [
            {"fieldKey": "contact.sms_consent",  "value": "true" if sms_consent else "false"},
            {"fieldKey": "contact.call_consent",  "value": "true" if call_consent else "false"},
            {"fieldKey": "contact.dnc_status",    "value": dnc_status},
        ],
        "tags": tags or [],
    }

def test_sms_blocked_when_no_consent():
    gate = ComplianceGate(supabase_client=None)
    contact = make_contact(sms_consent=False)
    with pytest.raises(ComplianceBlock) as exc:
        gate.check(contact, ActionType.SMS)
    assert "sms_consent" in str(exc.value)

def test_call_blocked_when_no_consent():
    gate = ComplianceGate(supabase_client=None)
    contact = make_contact(call_consent=False)
    with pytest.raises(ComplianceBlock) as exc:
        gate.check(contact, ActionType.CALL)
    assert "call_consent" in str(exc.value)

def test_blocked_when_dnc_status_set():
    gate = ComplianceGate(supabase_client=None)
    contact = make_contact(dnc_status="dnc")
    with pytest.raises(ComplianceBlock) as exc:
        gate.check(contact, ActionType.SMS)
    assert "dnc_status" in str(exc.value)

def test_blocked_when_dnc_tag_present():
    gate = ComplianceGate(supabase_client=None)
    contact = make_contact(tags=["dnc"])
    with pytest.raises(ComplianceBlock) as exc:
        gate.check(contact, ActionType.CALL)
    assert "dnc_tag" in str(exc.value)

def test_passes_when_all_clear():
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value.data = []
    gate = ComplianceGate(supabase_client=mock_sb)
    contact = make_contact()
    gate.check(contact, ActionType.SMS)
    gate.check(contact, ActionType.CALL)

def test_blocked_when_in_supabase_optout():
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        {"phone": "+17035550000"}
    ]
    gate = ComplianceGate(supabase_client=mock_sb)
    contact = make_contact()
    with pytest.raises(ComplianceBlock) as exc:
        gate.check(contact, ActionType.SMS)
    assert "opt_out" in str(exc.value)
