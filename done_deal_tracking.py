"""Optional Veo paid-sale tracking; paused by default at the team's request."""
import os
import re


def enabled() -> bool:
    return os.environ.get("DONE_DEAL_TRACKING_ENABLED", "false").strip().lower() == "true"


PAUSED_NOTICE = "Done Deal tracking is paused. You do not need to post Done Deals or use sale codes. Keep your client updated and arrange a handover if needed."


def delivery_allowed(action: dict) -> bool:
    """Retire queued legacy sale outputs without touching assignment state."""
    if enabled():
        return True
    payload = action.get("payload") or {}
    key = action.get("dedupe_key") or ""
    if payload.get("client_template_assignment_id"):
        return False
    if key.startswith(("daily-reminder:", "daily-quota:", "daily-report:", "historical-report:")) and payload.get("done_deal_tracking") is not False:
        return False
    if key.startswith(("ready-instructions:v", "ready-rollout:v")) and not key.startswith(("ready-instructions:v7:", "ready-rollout:v7:")):
        return False
    if key.startswith("client-help:"):
        from client_workflow import guide
        import html
        payload["text"] = html.escape(guide(int(action["chat_id"])))
    if key.startswith("referral-instructions:") and not payload.get("delete_message_id"):
        from referral_incentives import PUBLIC_INSTRUCTIONS
        payload["text"] = PUBLIC_INSTRUCTIONS
    # Undelivered assignments retain their token, owner and printed deadline.
    for field in ("text", "new_client_reminder_text"):
        if field in payload:
            payload[field] = re.sub(r"\n\nWhen this client pays, include .*?so we can link the sale to this assignment\.", "", payload[field], flags=re.S)
    markup = payload.get("reply_markup") or {}
    if "inline_keyboard" in markup:
        markup["inline_keyboard"] = [[b for b in row if not str(b.get("callback_data", "")).startswith("cw:dd:")]
                                      for row in markup["inline_keyboard"]]
        markup["inline_keyboard"] = [row for row in markup["inline_keyboard"] if row]
    return True
