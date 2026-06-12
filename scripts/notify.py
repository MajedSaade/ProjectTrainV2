#!/usr/bin/env python3
"""Send pipeline status email via smtplib. All config comes from environment variables."""

from __future__ import annotations

import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return value


def build_message() -> MIMEMultipart:
    status = _require("BUILD_STATUS")
    job_name = _require("JOB_NAME")
    build_number = _require("BUILD_NUMBER")
    recipient = _require("NOTIFY_TO")
    build_url = os.environ.get("BUILD_URL", "").strip()
    image_name = os.environ.get("DOCKER_IMAGE_NAME", "").strip()

    subject = f"[Jenkins] {status}: {job_name} #{build_number}"

    lines = [
        f"Pipeline: {job_name}",
        f"Build:   #{build_number}",
        f"Status:  {status}",
    ]
    if image_name:
        lines.append(f"Image:   {image_name}")
    if build_url:
        lines.append(f"URL:     {build_url}")

    body = "\n".join(lines)

    message = MIMEMultipart()
    message["From"] = _require("SMTP_USER")
    message["To"] = recipient
    message["Subject"] = subject
    message.attach(MIMEText(body, "plain"))
    return message


def send_email(message: MIMEMultipart) -> None:
    smtp_user = _require("SMTP_USER")
    smtp_password = _require("SMTP_PASSWORD")
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    recipient = _require("NOTIFY_TO")

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, [recipient], message.as_string())

    print(f"Notification sent to {recipient}")


def main() -> None:
    send_email(build_message())


if __name__ == "__main__":
    main()
