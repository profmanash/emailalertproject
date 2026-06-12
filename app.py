"""
Gmail Scheduler — Main Flask Application
"""

import os
import secrets
import logging
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from flask import (
    Flask, render_template, redirect, url_for, request,
    flash, jsonify, send_from_directory, abort
)
from flask_login import (
    LoginManager, login_user, logout_user,
    login_required, current_user
)
from werkzeug.utils import secure_filename

from config import Config
from models import db, User, EmailJob, Schedule
from scheduler import init_scheduler

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config.from_object(Config)

# Ensure required directories exist
os.makedirs(os.path.join(app.root_path, "instance"), exist_ok=True)
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access this page."
login_manager.login_message_category = "info"


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def allowed_file(filename: str) -> bool:
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_EXTENSIONS"]
    )


def send_reset_email(user: User, reset_url: str):
    """Send the password reset link to the user's registered email."""
    if not user.smtp_email or not user.smtp_password:
        raise RuntimeError("SMTP credentials not configured; cannot send reset email.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Password Reset — Gmail Scheduler"
    msg["From"] = user.smtp_email
    msg["To"] = user.email

    html = f"""
    <html><body style="font-family:Arial,sans-serif;background:#0f0f1a;color:#e0e0e0;padding:30px;">
      <div style="max-width:500px;margin:auto;background:#1a1a2e;border-radius:12px;padding:30px;
                  border:1px solid #6c63ff44;">
        <h2 style="color:#6c63ff;">Password Reset Request</h2>
        <p>Click the button below to reset your password. This link expires in <strong>1 hour</strong>.</p>
        <a href="{reset_url}"
           style="display:inline-block;margin:20px 0;padding:12px 28px;background:linear-gradient(135deg,#6c63ff,#a78bfa);
                  color:#fff;text-decoration:none;border-radius:8px;font-weight:bold;">
          Reset Password
        </a>
        <p style="font-size:12px;color:#888;">If you did not request this, ignore this email.</p>
      </div>
    </body></html>
    """
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(user.smtp_host, user.smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.login(user.smtp_email, user.smtp_password)
        server.sendmail(user.smtp_email, [user.email], msg.as_string())


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        password = request.form.get("password", "")
        remember = bool(request.form.get("remember"))

        user = User.query.filter(
            (User.username == identifier) | (User.email == identifier)
        ).first()

        if user and user.check_password(password):
            login_user(user, remember=remember)
            next_page = request.args.get("next")
            flash(f"Welcome back, {user.username}! 👋", "success")
            return redirect(next_page or url_for("dashboard"))
        else:
            flash("Invalid username/email or password.", "danger")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        errors = []
        if not username or len(username) < 3:
            errors.append("Username must be at least 3 characters.")
        if not email or "@" not in email:
            errors.append("Enter a valid email address.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")
        if User.query.filter_by(username=username).first():
            errors.append("Username already taken.")
        if User.query.filter_by(email=email).first():
            errors.append("Email already registered.")

        if errors:
            for e in errors:
                flash(e, "danger")
        else:
            user = User(username=username, email=email)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash("Account created! Please log in.", "success")
            return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter_by(email=email).first()

        if user:
            if not user.smtp_email or not user.smtp_password:
                flash(
                    "Your account has no SMTP credentials configured yet. "
                    "Please contact the administrator to reset your password.",
                    "warning",
                )
                return redirect(url_for("forgot_password"))

            token = secrets.token_urlsafe(32)
            user.reset_token = token
            user.reset_token_expiry = datetime.utcnow() + timedelta(hours=1)
            db.session.commit()

            reset_url = url_for("reset_password", token=token, _external=True)
            try:
                send_reset_email(user, reset_url)
                flash("A password reset link has been sent to your email.", "success")
            except Exception as exc:
                logger.error(f"Reset email error: {exc}")
                flash(f"Could not send reset email: {exc}", "danger")
        else:
            # Don't reveal whether email exists
            flash("If that email is registered, a reset link has been sent.", "info")

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    user = User.query.filter_by(reset_token=token).first()

    if not user or not user.reset_token_expiry or user.reset_token_expiry < datetime.utcnow():
        flash("This reset link is invalid or has expired.", "danger")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if len(password) < 8:
            flash("Password must be at least 8 characters.", "danger")
        elif password != confirm:
            flash("Passwords do not match.", "danger")
        else:
            user.set_password(password)
            user.reset_token = None
            user.reset_token_expiry = None
            db.session.commit()
            flash("Password reset successfully! Please log in.", "success")
            return redirect(url_for("login"))

    return render_template("reset_password.html", token=token)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def dashboard():
    jobs = EmailJob.query.filter_by(user_id=current_user.id).all()
    total_jobs = len(jobs)
    total_schedules = sum(len(j.schedules) for j in jobs)
    pending = sum(j.pending_count for j in jobs)
    sent = sum(j.sent_count for j in jobs)
    failed = sum(j.failed_count for j in jobs)

    # Upcoming schedules (next 10)
    upcoming = (
        Schedule.query
        .join(EmailJob)
        .filter(EmailJob.user_id == current_user.id, Schedule.status == "pending")
        .order_by(Schedule.scheduled_at.asc())
        .limit(10)
        .all()
    )

    # Recent sent schedules (last 10)
    recent_sent = (
        Schedule.query
        .join(EmailJob)
        .filter(EmailJob.user_id == current_user.id, Schedule.status == "sent")
        .order_by(Schedule.sent_at.desc())
        .limit(10)
        .all()
    )

    return render_template(
        "dashboard.html",
        total_jobs=total_jobs,
        total_schedules=total_schedules,
        pending=pending,
        sent=sent,
        failed=failed,
        upcoming=upcoming,
        recent_sent=recent_sent,
    )


# ---------------------------------------------------------------------------
# Email Jobs
# ---------------------------------------------------------------------------

@app.route("/compose", methods=["GET", "POST"])
@login_required
def compose():
    if request.method == "POST":
        subject = request.form.get("subject", "").strip()
        body = request.form.get("body", "").strip()
        recipients_raw = request.form.get("recipients", "").strip()
        schedule_times = request.form.getlist("schedule_times")

        errors = []
        if not subject:
            errors.append("Subject is required.")
        if not body:
            errors.append("Email body is required.")
        if not recipients_raw:
            errors.append("At least one recipient is required.")
        if not schedule_times:
            errors.append("At least one schedule time is required.")

        # Validate recipients
        recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
        for r in recipients:
            if "@" not in r:
                errors.append(f"Invalid email address: {r}")

        # Parse schedule times
        parsed_times = []
        for st in schedule_times:
            if st:
                try:
                    dt = datetime.strptime(st, "%Y-%m-%dT%H:%M")
                    if dt < datetime.now():
                        errors.append(f"Schedule time {st} is in the past.")
                    else:
                        parsed_times.append(dt)
                except ValueError:
                    errors.append(f"Invalid date/time format: {st}")

        if not parsed_times and not errors:
            errors.append("No valid schedule times provided.")

        # Handle attachment
        attachment_path = None
        attachment_filename = None
        file = request.files.get("attachment")
        if file and file.filename:
            if allowed_file(file.filename):
                fname = secure_filename(file.filename)
                # Unique prefix to avoid collisions
                unique_fname = f"{secrets.token_hex(8)}_{fname}"
                attachment_path = os.path.join(app.config["UPLOAD_FOLDER"], unique_fname)
                file.save(attachment_path)
                attachment_filename = fname
            else:
                errors.append("File type not allowed.")

        if errors:
            for e in errors:
                flash(e, "danger")
            now_str = datetime.now().strftime("%Y-%m-%dT%H:%M")
            return render_template("compose.html", form_data=request.form, now_str=now_str)

        # Create EmailJob
        job = EmailJob(
            user_id=current_user.id,
            subject=subject,
            body=body,
            recipients=", ".join(recipients),
            attachment_path=attachment_path,
            attachment_filename=attachment_filename,
        )
        db.session.add(job)
        db.session.flush()  # get job.id

        # Create Schedule rows
        for dt in sorted(parsed_times):
            schedule = Schedule(job_id=job.id, scheduled_at=dt)
            db.session.add(schedule)

        db.session.commit()
        flash(f"✅ Email job created with {len(parsed_times)} schedule(s)!", "success")
        return redirect(url_for("schedules"))

    now_str = datetime.now().strftime("%Y-%m-%dT%H:%M")
    return render_template("compose.html", form_data={}, now_str=now_str)


@app.route("/schedules")
@login_required
def schedules():
    jobs = (
        EmailJob.query
        .filter_by(user_id=current_user.id)
        .order_by(EmailJob.created_at.desc())
        .all()
    )
    return render_template("schedules.html", jobs=jobs)


@app.route("/job/<int:job_id>/delete", methods=["POST"])
@login_required
def delete_job(job_id):
    job = EmailJob.query.filter_by(id=job_id, user_id=current_user.id).first_or_404()
    # Remove attachment file
    if job.attachment_path and os.path.exists(job.attachment_path):
        os.remove(job.attachment_path)
    db.session.delete(job)
    db.session.commit()
    flash("Email job deleted.", "info")
    return redirect(url_for("schedules"))


@app.route("/schedule/<int:schedule_id>/delete", methods=["POST"])
@login_required
def delete_schedule(schedule_id):
    schedule = Schedule.query.get_or_404(schedule_id)
    job = EmailJob.query.filter_by(id=schedule.job_id, user_id=current_user.id).first_or_404()
    db.session.delete(schedule)
    db.session.commit()
    flash("Schedule removed.", "info")
    return redirect(url_for("schedules"))


@app.route("/job/<int:job_id>/add-schedule", methods=["POST"])
@login_required
def add_schedule(job_id):
    job = EmailJob.query.filter_by(id=job_id, user_id=current_user.id).first_or_404()
    dt_str = request.form.get("new_schedule_time", "")
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M")
        if dt < datetime.now():
            flash("Schedule time is in the past.", "danger")
        else:
            schedule = Schedule(job_id=job.id, scheduled_at=dt)
            db.session.add(schedule)
            db.session.commit()
            flash("New schedule added.", "success")
    except ValueError:
        flash("Invalid date/time format.", "danger")
    return redirect(url_for("schedules"))


@app.route("/schedule/<int:schedule_id>/send-now", methods=["POST"])
@login_required
def send_now(schedule_id):
    """Immediately send a pending schedule — useful for testing SMTP."""
    schedule = Schedule.query.get_or_404(schedule_id)
    job = EmailJob.query.filter_by(id=schedule.job_id, user_id=current_user.id).first_or_404()

    if schedule.status != "pending":
        flash("Only pending schedules can be sent immediately.", "warning")
        return redirect(url_for("schedules"))

    if not current_user.smtp_email or not current_user.smtp_password:
        flash("⚠️ SMTP credentials not configured. Go to Settings → SMTP Config.", "danger")
        return redirect(url_for("settings"))

    from scheduler import send_email_via_smtp
    import smtplib

    subject = job.get_next_subject()
    try:
        send_email_via_smtp(
            user=current_user,
            subject=subject,
            body=job.body,
            recipients=job.recipient_list,
            attachment_path=job.attachment_path,
            attachment_filename=job.attachment_filename,
        )
        schedule.status = "sent"
        schedule.sent_at = datetime.now()
        schedule.actual_subject_used = subject
        job.send_count += 1
        db.session.commit()
        flash(f"✅ Email sent immediately to {job.recipients} — Subject: '{subject}'", "success")
    except smtplib.SMTPAuthenticationError:
        flash(
            "❌ SMTP authentication failed. Make sure you are using a Gmail App Password "
            "(not your regular Gmail password). Enable 2-Step Verification first.",
            "danger",
        )
    except Exception as exc:
        flash(f"❌ Failed to send: {exc}", "danger")

    return redirect(url_for("schedules"))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "smtp":
            current_user.smtp_email = request.form.get("smtp_email", "").strip()
            current_user.smtp_password = request.form.get("smtp_password", "").strip()
            current_user.smtp_host = request.form.get("smtp_host", "smtp.gmail.com").strip()
            current_user.smtp_port = int(request.form.get("smtp_port", 587))
            db.session.commit()
            flash("✅ SMTP settings saved.", "success")

        elif action == "profile":
            new_username = request.form.get("username", "").strip()
            new_email = request.form.get("email", "").strip().lower()

            if new_username and new_username != current_user.username:
                if User.query.filter_by(username=new_username).first():
                    flash("Username already taken.", "danger")
                    return redirect(url_for("settings"))
                current_user.username = new_username

            if new_email and new_email != current_user.email:
                if User.query.filter_by(email=new_email).first():
                    flash("Email already registered.", "danger")
                    return redirect(url_for("settings"))
                current_user.email = new_email

            db.session.commit()
            flash("✅ Profile updated.", "success")

        elif action == "password":
            current_pw = request.form.get("current_password", "")
            new_pw = request.form.get("new_password", "")
            confirm_pw = request.form.get("confirm_password", "")

            if not current_user.check_password(current_pw):
                flash("Current password is incorrect.", "danger")
            elif len(new_pw) < 8:
                flash("New password must be at least 8 characters.", "danger")
            elif new_pw != confirm_pw:
                flash("Passwords do not match.", "danger")
            else:
                current_user.set_password(new_pw)
                db.session.commit()
                flash("✅ Password changed successfully.", "success")

        elif action == "test_smtp":
            try:
                import smtplib
                with smtplib.SMTP(current_user.smtp_host, current_user.smtp_port) as server:
                    server.ehlo()
                    server.starttls()
                    server.login(current_user.smtp_email, current_user.smtp_password)
                flash("✅ SMTP connection successful!", "success")
            except Exception as exc:
                flash(f"❌ SMTP test failed: {exc}", "danger")

    return render_template("settings.html")


# ---------------------------------------------------------------------------
# Attachment download
# ---------------------------------------------------------------------------

@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


# ---------------------------------------------------------------------------
# API: stats (for dashboard auto-refresh)
# ---------------------------------------------------------------------------

@app.route("/api/stats")
@login_required
def api_stats():
    jobs = EmailJob.query.filter_by(user_id=current_user.id).all()
    return jsonify({
        "pending": sum(j.pending_count for j in jobs),
        "sent": sum(j.sent_count for j in jobs),
        "failed": sum(j.failed_count for j in jobs),
    })


# ---------------------------------------------------------------------------
# Init DB + Start Scheduler
# ---------------------------------------------------------------------------

with app.app_context():
    db.create_all()
    logger.info("Database tables created/verified.")

init_scheduler(app)
'''
if __name__ == "__main__":
    app.run(debug=False, use_reloader=False, port=5000)

'''

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)