"""
Background scheduler that polls the database every 60 seconds
and sends any pending scheduled emails via Gmail SMTP.
"""
from __future__ import annotations

import smtplib
import logging
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone="Asia/Kolkata")


def send_email_via_smtp(
    user,
    subject: str,
    body: str,
    recipients: list[str],
    attachment_path: str | None,
    attachment_filename: str | None,
) -> None:
    """Send an email using the user's stored SMTP credentials."""

    has_attachment = attachment_path and os.path.exists(attachment_path)

    # Use "mixed" when there is an attachment so both parts are preserved
    msg = MIMEMultipart("mixed" if has_attachment else "alternative")
    msg["Subject"] = subject
    msg["From"] = user.smtp_email
    msg["To"] = ", ".join(recipients)

    # Wrap HTML body in its own alternative part so it renders correctly
    if has_attachment:
        body_part = MIMEMultipart("alternative")
        body_part.attach(MIMEText(body, "html", "utf-8"))
        msg.attach(body_part)
    else:
        msg.attach(MIMEText(body, "html", "utf-8"))

    # Attach file
    if has_attachment:
        with open(attachment_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        fname = attachment_filename or os.path.basename(attachment_path)
        part.add_header("Content-Disposition", f'attachment; filename="{fname}"')
        msg.attach(part)

    with smtplib.SMTP(user.smtp_host, user.smtp_port, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(user.smtp_email, user.smtp_password)
        server.sendmail(user.smtp_email, recipients, msg.as_string())


def process_due_schedules(app):
    """
    Called by APScheduler every 60 seconds.
    Finds all pending schedules whose local time has arrived and sends them.

    IMPORTANT: We compare using datetime.now() (local/naive) because the
    browser datetime-local input sends local time which we store as-is.
    Using utcnow() would create a 5h 30m offset for IST users and emails
    would never fire at the expected time.
    """
    with app.app_context():
        from models import db, Schedule, EmailJob, User

        # Use local time — matches how scheduled_at is stored
        now = datetime.now()

        due = (
            Schedule.query
            .filter(Schedule.status == "pending", Schedule.scheduled_at <= now)
            .order_by(Schedule.scheduled_at.asc())
            .all()
        )

        if not due:
            logger.debug(f"[Scheduler] No due schedules at {now.strftime('%H:%M:%S')}")
            return

        logger.info(f"[Scheduler] Found {len(due)} due schedule(s) at {now.strftime('%Y-%m-%d %H:%M:%S')}")

        for schedule in due:
            job: EmailJob = schedule.email_job
            user: User = job.user

            if not user.smtp_email or not user.smtp_password:
                schedule.status = "failed"
                schedule.error_msg = "SMTP credentials not configured. Go to Settings → SMTP Config."
                schedule.sent_at = datetime.now()
                db.session.commit()
                logger.warning(f"[Scheduler] Schedule {schedule.id} failed: no SMTP credentials.")
                continue

            # Compute the subject BEFORE incrementing send_count
            subject = job.get_next_subject()
            recipients = job.recipient_list

            try:
                send_email_via_smtp(
                    user=user,
                    subject=subject,
                    body=job.body,
                    recipients=recipients,
                    attachment_path=job.attachment_path,
                    attachment_filename=job.attachment_filename,
                )
                schedule.status = "sent"
                schedule.sent_at = datetime.now()
                schedule.actual_subject_used = subject
                job.send_count += 1
                db.session.commit()
                logger.info(f"[Scheduler] ✅ Sent schedule {schedule.id} to {recipients} — Subject: '{subject}'")

            except smtplib.SMTPAuthenticationError:
                schedule.status = "failed"
                schedule.error_msg = (
                    "SMTP authentication failed. Check your Gmail App Password in Settings. "
                    "Make sure 2-Step Verification is enabled and you are using an App Password."
                )
                schedule.sent_at = datetime.now()
                db.session.commit()
                logger.error(f"[Scheduler] ❌ Auth failed for schedule {schedule.id}")

            except smtplib.SMTPRecipientsRefused as exc:
                schedule.status = "failed"
                schedule.error_msg = f"Recipients refused: {exc}"
                schedule.sent_at = datetime.now()
                db.session.commit()
                logger.error(f"[Scheduler] ❌ Recipients refused for schedule {schedule.id}: {exc}")

            except Exception as exc:
                schedule.status = "failed"
                schedule.error_msg = str(exc)
                schedule.sent_at = datetime.now()
                db.session.commit()
                logger.error(f"[Scheduler] ❌ Failed schedule {schedule.id}: {exc}")


def init_scheduler(app):
    """Start the background scheduler, tied to the Flask app context."""
    scheduler.add_job(
        func=process_due_schedules,
        trigger=IntervalTrigger(seconds=60),
        args=[app],
        id="email_dispatcher",
        name="Email Dispatcher",
        replace_existing=True,
        misfire_grace_time=120,
    )
    scheduler.start()
    logger.info("[Scheduler] APScheduler started — checking every 60 seconds.")
