"""Email service — sends transactional email via Resend API.
Direct port of src/services/email.rs.
"""
import httpx
from app.config import RESEND_API_KEY, MAIL_FROM, BASE_URL, NOTIFY_EMAIL


def _portal_url() -> str:
    return "https://senzii.com/staff/login"


async def _send_via_resend(to: str, subject: str, html: str) -> dict:
    """Send email via Resend API. Returns {'sent': bool, 'reason': str|None}."""
    if not RESEND_API_KEY:
        print(f"[email] Would send to {to}: {subject}")
        return {"sent": False, "reason": "no_api_key"}

    payload = {
        "from": MAIL_FROM,
        "to": [to],
        "subject": subject,
        "html": html,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if 200 <= resp.status_code < 300:
        return {"sent": True, "reason": None}
    else:
        body = resp.text
        print(f"[email] Failed to send: {resp.status_code} {body}")
        return {"sent": False, "reason": f"http_{resp.status_code}"}


async def send_magic_link_email(to: str, magic_link: str, org_name: str = "your organization") -> dict:
    """Send a magic link email to a new admin post-checkout."""
    html = f"""
      <div style="font-family: 'DM Sans', Arial, sans-serif; max-width: 520px; margin: 0 auto; color: #1a1a2e;">
        <h2 style="color: #0b1220; font-size: 1.5rem; margin-bottom: 0.5rem;">Welcome to Senzii</h2>
        <p style="color: #555; font-size: 1rem; margin-bottom: 1.5rem;">
          Your Senzii account for <strong>{org_name}</strong> is ready. Set your password to access your scheduling dashboard.
        </p>
        <a href="{magic_link}" style="display:inline-block; background:#16a34a; color:#fff; padding:0.75rem 1.5rem; border-radius:8px; text-decoration:none; font-weight:600; font-size:0.95rem;">
          Set up your account →
        </a>
        <p style="color: #888; font-size: 0.8rem; margin-top: 1.5rem;">
          This link expires in 7 days. If you didn't sign up for Senzii, you can safely ignore this email.
        </p>
      </div>
    """
    return await _send_via_resend(to, "Welcome to Senzii — Set up your account", html)


async def send_staff_magic_link_email(to: str, staff_name: str, magic_link: str, org_name: str = "your organization") -> dict:
    """Send a magic link email to a staff member."""
    html = f"""
      <div style="font-family: 'DM Sans', Arial, sans-serif; max-width: 520px; margin: 0 auto; color: #1a1a2e;">
        <h2 style="color: #0b1220; font-size: 1.5rem; margin-bottom: 0.5rem;">Welcome to Senzii</h2>
        <p style="color: #555; font-size: 1rem; margin-bottom: 1.5rem;">
          Hi {staff_name}, your <strong>{org_name}</strong> scheduling account is ready. Click below to set your password and access your shifts.
        </p>
        <a href="{magic_link}" style="display:inline-block; background:#16a34a; color:#fff; padding:0.75rem 1.5rem; border-radius:8px; text-decoration:none; font-weight:600; font-size:0.95rem;">
          Set up your account →
        </a>
        <p style="color: #888; font-size: 0.8rem; margin-top: 1.5rem;">
          This link expires in 7 days. If you did not expect this invitation, you can safely ignore this email.
        </p>
      </div>
    """
    return await _send_via_resend(to, f"Welcome to Senzii — Set up your account, {staff_name}", html)


async def send_client_magic_link_email(to: str, client_name: str, magic_link: str, org_name: str = "your organization") -> dict:
    """Send a magic link email to a client."""
    html = f"""
      <div style="font-family: 'DM Sans', Arial, sans-serif; max-width: 520px; margin: 0 auto; color: #1a1a2e;">
        <h2 style="color: #0b1220; font-size: 1.5rem; margin-bottom: 0.5rem;">Welcome to Senzii</h2>
        <p style="color: #555; font-size: 1rem; margin-bottom: 1.5rem;">
          Hi {client_name}, <strong>{org_name}</strong> has invited you to their scheduling portal. Click below to set your password and get started.
        </p>
        <a href="{magic_link}" style="display:inline-block; background:#16a34a; color:#fff; padding:0.75rem 1.5rem; border-radius:8px; text-decoration:none; font-weight:600; font-size:0.95rem;">
          Set up your account →
        </a>
        <p style="color: #888; font-size: 0.8rem; margin-top: 1.5rem;">
          This link expires in 7 days. If you did not expect this invitation, you can safely ignore this email.
        </p>
      </div>
    """
    return await _send_via_resend(to, f"Welcome to Senzii — Set up your account, {client_name}", html)


async def send_password_reset_email(to: str, reset_link: str) -> dict:
    """Send a password reset email with 1-hour-expiry link."""
    html = f"""
      <div style="font-family: 'DM Sans', Arial, sans-serif; max-width: 520px; margin: 0 auto; color: #1a1a2e;">
        <h2 style="color: #0b1220; font-size: 1.5rem; margin-bottom: 0.5rem;">Reset Your Password</h2>
        <p style="color: #555; font-size: 1rem; margin-bottom: 1.5rem;">
          We received a request to reset the password for your Senzii account. Click the button below to set a new password.
        </p>
        <a href="{reset_link}" style="display:inline-block; background:#16a34a; color:#fff; padding:0.75rem 1.5rem; border-radius:8px; text-decoration:none; font-weight:600; font-size:0.95rem;">
          Reset Password →
        </a>
        <p style="color: #888; font-size: 0.8rem; margin-top: 1.5rem;">
          This link expires in 1 hour. If you didn't request a password reset, you can safely ignore this email.
        </p>
      </div>
    """
    return await _send_via_resend(to, "Senzii — Reset Your Password", html)


async def send_trial_notification_email(org_name: str, admin_email: str, admin_name: str, admin_phone: str, seats: int) -> dict:
    """Send a notification to NOTIFY_EMAIL when someone registers for a trial."""
    if not NOTIFY_EMAIL:
        print(f"[email] Trial registration: org={org_name}, email={admin_email}, seats={seats} — no NOTIFY_EMAIL set")
        return {"sent": False, "reason": "no_notify_email"}

    html = f"""
      <div style="font-family: 'DM Sans', Arial, sans-serif; max-width: 520px; margin: 0 auto; color: #1a1a2e;">
        <h2 style="color: #16a34a; font-size: 1.5rem; margin-bottom: 0.5rem;">New Trial Registration</h2>
        <p style="color: #555; font-size: 1rem; margin-bottom: 1.5rem;">
          A new organization just signed up for a Senzii trial.
        </p>
        <table style="width: 100%; border-collapse: collapse; font-size: 0.95rem;">
          <tr><td style="padding: 6px 0; color: #888; width: 120px;">Organization</td><td style="padding: 6px 0; color: #1a1a2e; font-weight: 600;">{org_name}</td></tr>
          <tr><td style="padding: 6px 0; color: #888;">Admin email</td><td style="padding: 6px 0; color: #1a1a2e; font-weight: 600;">{admin_email}</td></tr>
          <tr><td style="padding: 6px 0; color: #888;">Name</td><td style="padding: 6px 0; color: #1a1a2e;">{admin_name}</td></tr>
          <tr><td style="padding: 6px 0; color: #888;">Phone</td><td style="padding: 6px 0; color: #1a1a2e;">{admin_phone}</td></tr>
          <tr><td style="padding: 6px 0; color: #888;">Seats</td><td style="padding: 6px 0; color: #1a1a2e;">{seats}</td></tr>
        </table>
        <p style="color: #888; font-size: 0.8rem; margin-top: 1.5rem;">
          Sent from Senzii webhook handler.
        </p>
      </div>
    """
    return await _send_via_resend(NOTIFY_EMAIL, f"New Senzii Trial — {org_name}", html)


async def send_sales_inquiry_notification(
    name: str, email: str, phone: str | None, company_name: str | None,
    staff_count: int | None, industry: str | None, message: str | None,
):
    """Send a sales inquiry notification to NOTIFY_EMAIL."""
    if not NOTIFY_EMAIL:
        return
    if not RESEND_API_KEY:
        print("[email] Sales inquiry received — no Resend API key configured")
        return

    industry_map = {
        "healthcare": "Healthcare", "construction": "Construction",
        "logistics": "Logistics", "hospitality": "Hospitality",
        "retail": "Retail", "manufacturing": "Manufacturing",
        "security": "Security", "events": "Events", "other": "Other",
    }
    ind = industry_map.get(industry, "—")
    company = company_name or "—"
    phone_str = phone or "—"
    staff = f"{staff_count} staff" if staff_count else "—"
    msg = message or "No additional notes provided."
    subject = f"New lead: {name} ({company_name or 'no company'})"

    html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;background:#f4f5f7;margin:0;padding:24px;">
<div style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
  <div style="background:#16a34a;padding:20px 28px;">
    <h1 style="margin:0;color:#ffffff;font-size:18px;font-weight:700;">New Lead from Senzii.com</h1>
    <p style="margin:4px 0 0;color:#bbf7d0;font-size:13px;">A demo was just requested</p>
  </div>
  <div style="padding:28px;">
    <table style="width:100%;border-collapse:collapse;">
      <tr><td style="padding:6px 0;color:#6b7280;font-size:13px;font-weight:600;width:110px;vertical-align:top;">NAME</td><td style="padding:6px 0;color:#111827;font-size:15px;font-weight:600;">{name}</td></tr>
      <tr><td style="padding:6px 0;color:#6b7280;font-size:13px;font-weight:600;vertical-align:top;">EMAIL</td><td style="padding:6px 0;"><a href="mailto:{email}" style="color:#16a34a;font-size:15px;text-decoration:none;">{email}</a></td></tr>
      <tr><td style="padding:6px 0;color:#6b7280;font-size:13px;font-weight:600;vertical-align:top;">PHONE</td><td style="padding:6px 0;color:#111827;font-size:15px;">{phone_str}</td></tr>
      <tr><td style="padding:6px 0;color:#6b7280;font-size:13px;font-weight:600;vertical-align:top;">COMPANY</td><td style="padding:6px 0;color:#111827;font-size:15px;">{company}</td></tr>
      <tr><td style="padding:6px 0;color:#6b7280;font-size:13px;font-weight:600;vertical-align:top;">STAFF SIZE</td><td style="padding:6px 0;color:#111827;font-size:15px;">{staff}</td></tr>
      <tr><td style="padding:6px 0;color:#6b7280;font-size:13px;font-weight:600;vertical-align:top;">INDUSTRY</td><td style="padding:6px 0;color:#111827;font-size:15px;">{ind}</td></tr>
    </table>
    <div style="margin-top:20px;padding:16px;background:#f9fafb;border-radius:8px;border-left:3px solid #16a34a;">
      <p style="margin:0 0 6px;color:#6b7280;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.03em;">Their scheduling challenges</p>
      <p style="margin:0;color:#374151;font-size:14px;line-height:1.55;">{msg}</p>
    </div>
  </div>
</div>
</body></html>"""

    await _send_via_resend(NOTIFY_EMAIL, subject, html)


async def send_shift_confirmation_email(
    to: str, staff_name: str, org_name: str, site_name: str,
    shift_date: str, start_time: str, end_time: str,
) -> dict:
    """Send a shift confirmation email to a staff member."""
    portal_url = _portal_url()
    html = f"""
      <div style="font-family: 'DM Sans', Arial, sans-serif; max-width: 520px; margin: 0 auto; color: #1a1a2e;">
        <h2 style="color: #0b1220; font-size: 1.5rem; margin-bottom: 0.5rem;">Your Schedule Has Been Updated</h2>
        <p style="color: #555; font-size: 1rem; margin-bottom: 1.5rem;">
          Hi {staff_name}, your shift schedule at <strong>{org_name}</strong> has been updated. Here are the details:
        </p>
        <table style="width: 100%; border-collapse: collapse; font-size: 0.95rem; margin-bottom: 1.5rem;">
          <tr><td style="padding: 6px 0; color: #888; width: 100px;">Location</td><td style="padding: 6px 0; color: #1a1a2e; font-weight: 600;">{site_name}</td></tr>
          <tr><td style="padding: 6px 0; color: #888;">Date</td><td style="padding: 6px 0; color: #1a1a2e; font-weight: 600;">{shift_date}</td></tr>
          <tr><td style="padding: 6px 0; color: #888;">Time</td><td style="padding: 6px 0; color: #1a1a2e; font-weight: 600;">{start_time} – {end_time}</td></tr>
        </table>
        <p style="color: #555; font-size: 1rem; margin-bottom: 1.5rem;">
          Please log in to your staff portal to view the full schedule and manage your shifts.
        </p>
        <a href="{portal_url}" style="display:inline-block; background:#16a34a; color:#fff; padding:0.75rem 1.5rem; border-radius:8px; text-decoration:none; font-weight:600; font-size:0.95rem;">
          View Your Schedule →
        </a>
      </div>
    """
    return await _send_via_resend(to, f"Senzii — Schedule Update for {shift_date}", html)


async def send_shift_unassigned_email(
    to: str, staff_name: str, org_name: str, site_name: str,
    shift_date: str, start_time: str, end_time: str,
) -> dict:
    """Send a shift unassigned email to a staff member."""
    portal_url = _portal_url()
    html = f"""
      <div style="font-family: 'DM Sans', Arial, sans-serif; max-width: 520px; margin: 0 auto; color: #1a1a2e;">
        <h2 style="color: #0b1220; font-size: 1.5rem; margin-bottom: 0.5rem;">Your Schedule Has Been Updated</h2>
        <p style="color: #555; font-size: 1rem; margin-bottom: 1.5rem;">
          Hi {staff_name}, the following shift at <strong>{org_name}</strong> has been removed from your schedule:
        </p>
        <table style="width: 100%; border-collapse: collapse; font-size: 0.95rem; margin-bottom: 1.5rem;">
          <tr><td style="padding: 6px 0; color: #888; width: 100px;">Location</td><td style="padding: 6px 0; color: #1a1a2e; font-weight: 600;">{site_name}</td></tr>
          <tr><td style="padding: 6px 0; color: #888;">Date</td><td style="padding: 6px 0; color: #1a1a2e; font-weight: 600;">{shift_date}</td></tr>
          <tr><td style="padding: 6px 0; color: #888;">Time</td><td style="padding: 6px 0; color: #1a1a2e; font-weight: 600;">{start_time} – {end_time}</td></tr>
        </table>
        <p style="color: #555; font-size: 1rem; margin-bottom: 1.5rem;">
          You are no longer assigned to this shift. Log in to your staff portal to view your current schedule.
        </p>
        <a href="{portal_url}" style="display:inline-block; background:#16a34a; color:#fff; padding:0.75rem 1.5rem; border-radius:8px; text-decoration:none; font-weight:600; font-size:0.95rem;">
          View Your Schedule →
        </a>
      </div>
    """
    return await _send_via_resend(to, f"Senzii — Schedule Update for {shift_date}", html)