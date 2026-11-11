"""Explicit VEO new-client-only mode; keep membership and linking available."""
import os


def new_clients_only() -> bool:
    return (os.environ.get("VEO_WORKSPACE_ENABLED", "").lower() == "true"
            and os.environ.get("VEO_NEW_CLIENTS_ONLY", "").lower() == "true")


def intake_action(action: dict) -> bool:
    payload = action.get("payload") or {}
    return (any(payload.get(key) for key in ("veo_intake_request_id", "veo_intake_offer_id",
            "veo_intake_notice_id", "veo_intake_help"))
            and not any(payload.get(key) for key in ("veo_submission_id", "veo_report_date", "veo_availability_date")))


def require_non_intake_enabled() -> None:
    if new_clients_only():
        raise ValueError("Jaucian handles new clients only; record deals and reports in VEO")


def approved_deals_enabled() -> bool:
    return os.environ.get("VEO_APPROVED_DEALS_ENABLED", "").lower() == "true"


def reviewed_action(action: dict) -> bool:
    p = action.get("payload") or {}
    return (approved_deals_enabled() and bool(p.get("veo_submission_id") and p.get("veo_deal_review_id"))
            and not any(p.get(k) for k in ("veo_report_date", "veo_availability_date", "veo_intake_request_id",
                                         "veo_intake_offer_id", "veo_intake_notice_id", "veo_intake_help")))


def mode_name() -> str:
    return ("new_clients_and_reviewed_deals" if approved_deals_enabled() else "new_clients_only") if new_clients_only() else "workspace"
