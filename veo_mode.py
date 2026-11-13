"""Explicit VEO new-client-only mode; keep membership and linking available."""
import os
import datetime as dt


def new_clients_only() -> bool:
    return (os.environ.get("VEO_WORKSPACE_ENABLED", "").strip().lower() == "true"
            and os.environ.get("VEO_NEW_CLIENTS_ONLY", "").strip().lower() == "true")


def intake_action(action: dict) -> bool:
    payload = action.get("payload") or {}
    return (any(payload.get(key) for key in ("veo_intake_request_id", "veo_intake_offer_id",
            "veo_intake_notice_id", "veo_intake_help"))
            and not any(payload.get(key) for key in ("veo_submission_id", "veo_report_date", "veo_availability_date")))


def require_non_intake_enabled() -> None:
    if new_clients_only():
        raise ValueError("Jaucian handles new clients only; record deals and reports in VEO")


def approved_deals_enabled() -> bool:
    return os.environ.get("VEO_APPROVED_DEALS_ENABLED", "").strip().lower() == "true"


def availability_enabled() -> bool:
    return not new_clients_only() or os.environ.get("VEO_AVAILABILITY_ENABLED", "").strip().lower() == "true"


def availability_action(action: dict) -> bool:
    p = action.get("payload") or {}
    return availability_enabled() and bool(p.get("veo_availability_date") and p.get("veo_tenant_id")) and not any(
        p.get(k) for k in ("veo_submission_id", "veo_report_date", "veo_intake_request_id", "veo_intake_offer_id", "veo_intake_notice_id", "veo_intake_help"))


def reviewed_action(action: dict) -> bool:
    p = action.get("payload") or {}
    return (approved_deals_enabled() and bool((p.get("veo_submission_id") and p.get("veo_deal_review_id")) or (p.get("veo_incentive_notice") and p.get("veo_tenant_id")))
            and not any(p.get(k) for k in ("veo_report_date", "veo_availability_date", "veo_intake_request_id",
                                         "veo_intake_offer_id", "veo_intake_notice_id", "veo_intake_help")))


def mode_name() -> str:
    return ("new_clients_and_reviewed_deals" if approved_deals_enabled() else "new_clients_only") if new_clients_only() else "workspace"


def delivery_cutoff() -> str | None:
    raw = os.environ.get("VEO_NEW_CLIENTS_SINCE", "").strip()
    if not new_clients_only() or not raw:
        return None
    value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("The new-client cutoff requires a timezone")
    return value.astimezone(dt.timezone.utc).isoformat()


def delivery_in_window(action: dict) -> bool:
    cutoff = delivery_cutoff()
    if not cutoff:
        return True
    value = dt.datetime.fromisoformat(str(action.get("scheduled_for") or "").replace("Z", "+00:00"))
    return value.tzinfo is not None and value >= dt.datetime.fromisoformat(cutoff)
