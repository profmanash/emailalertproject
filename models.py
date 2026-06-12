from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
import bcrypt

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)

    # Password reset
    reset_token = db.Column(db.String(200), nullable=True)
    reset_token_expiry = db.Column(db.DateTime, nullable=True)

    # Per-user SMTP configuration (Gmail)
    smtp_email = db.Column(db.String(120), nullable=True)
    smtp_password = db.Column(db.String(200), nullable=True)  # Gmail App Password
    smtp_host = db.Column(db.String(100), default="smtp.gmail.com")
    smtp_port = db.Column(db.Integer, default=587)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    email_jobs = db.relationship("EmailJob", backref="user", lazy=True, cascade="all, delete-orphan")

    def set_password(self, password: str):
        self.password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    def check_password(self, password: str) -> bool:
        return bcrypt.checkpw(password.encode("utf-8"), self.password_hash.encode("utf-8"))

    def __repr__(self):
        return f"<User {self.username}>"


class EmailJob(db.Model):
    __tablename__ = "email_jobs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    subject = db.Column(db.String(300), nullable=False)
    body = db.Column(db.Text, nullable=False)
    recipients = db.Column(db.Text, nullable=False)  # comma-separated emails
    attachment_filename = db.Column(db.String(300), nullable=True)
    attachment_path = db.Column(db.String(500), nullable=True)

    # Tracks how many times this job has been sent (used for subject numbering)
    send_count = db.Column(db.Integer, default=0, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    schedules = db.relationship("Schedule", backref="email_job", lazy=True, cascade="all, delete-orphan")

    @property
    def recipient_list(self):
        return [r.strip() for r in self.recipients.split(",") if r.strip()]

    @property
    def pending_count(self):
        return sum(1 for s in self.schedules if s.status == "pending")

    @property
    def sent_count(self):
        return sum(1 for s in self.schedules if s.status == "sent")

    @property
    def failed_count(self):
        return sum(1 for s in self.schedules if s.status == "failed")

    def get_next_subject(self) -> str:
        """Return the subject for the next send, with auto-incremented prefix after first send."""
        count = self.send_count + 1
        if count == 1:
            return self.subject
        return f"Task Completion Reminder {count} : {self.subject}"

    def __repr__(self):
        return f"<EmailJob {self.id}: {self.subject}>"


class Schedule(db.Model):
    __tablename__ = "schedules"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("email_jobs.id"), nullable=False)

    scheduled_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default="pending")  # pending | sent | failed
    sent_at = db.Column(db.DateTime, nullable=True)
    error_msg = db.Column(db.Text, nullable=True)
    actual_subject_used = db.Column(db.String(300), nullable=True)

    def __repr__(self):
        return f"<Schedule {self.id}: job={self.job_id} at={self.scheduled_at} status={self.status}>"
