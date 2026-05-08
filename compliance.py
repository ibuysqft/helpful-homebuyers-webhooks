# webhook-server/compliance.py
"""Compliance gate — blocks all outbound SMS and calls that fail consent/DNC checks.

Usage:
    from compliance import ComplianceGate, ComplianceBlock, ActionType

    gate = ComplianceGate(supabase_client=sb)
    try:
        gate.check(contact_dict, ActionType.SMS)
    except ComplianceBlock as e:
        logger.warning(f"Compliance block: {e}")
        return  # do not send
"""

from enum import Enum
from typing import Any


class ActionType(Enum):
    SMS = "sms"
    CALL = "call"


class ComplianceBlock(Exception):
    """Raised when an outbound action is blocked by compliance rules."""
    pass


class ComplianceGate:
    def __init__(self, supabase_client: Any):
        self._sb = supabase_client

    def check(self, contact: dict, action: ActionType) -> None:
        """Run all compliance checks. Raises ComplianceBlock if any check fails."""
        self._check_dnc_tag(contact)
        self._check_dnc_status(contact)
        self._check_consent(contact, action)
        if self._sb is not None:
            self._check_supabase_optout(contact)

    def _check_dnc_tag(self, contact: dict) -> None:
        tags = [t.lower() for t in contact.get("tags", [])]
        if "dnc" in tags:
            raise ComplianceBlock(
                f"dnc_tag: contact {contact.get('id')} has DNC tag"
            )

    def _check_dnc_status(self, contact: dict) -> None:
        status = self._get_field(contact, "dnc_status")
        if status and status.lower() not in ("", "clear", "none"):
            raise ComplianceBlock(
                f"dnc_status: contact {contact.get('id')} dnc_status={status}"
            )

    def _check_consent(self, contact: dict, action: ActionType) -> None:
        if action == ActionType.SMS:
            consent = self._get_field(contact, "sms_consent")
            if consent not in ("true", "1", "yes", True):
                raise ComplianceBlock(
                    f"sms_consent: contact {contact.get('id')} sms_consent not granted"
                )
        elif action == ActionType.CALL:
            consent = self._get_field(contact, "call_consent")
            if consent not in ("true", "1", "yes", True):
                raise ComplianceBlock(
                    f"call_consent: contact {contact.get('id')} call_consent not granted"
                )

    def _check_supabase_optout(self, contact: dict) -> None:
        phone = contact.get("phone", "")
        if not phone:
            return
        result = (
            self._sb.table("opt_outs")
            .select("phone")
            .eq("phone", phone)
            .execute()
        )
        if result.data:
            raise ComplianceBlock(
                f"opt_out: phone {phone} found in opt_outs table"
            )

    @staticmethod
    def _get_field(contact: dict, key: str) -> str:
        """Extract a custom field value by bare key name (e.g. 'sms_consent')."""
        for cf in contact.get("customFields", []):
            field_key = cf.get("fieldKey", "")
            # GHL returns keys as "contact.field_name" — match on suffix
            if field_key == f"contact.{key}" or field_key == key:
                return cf.get("value", "")
        return ""
