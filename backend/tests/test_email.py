"""Email (FEATURE_EMAIL, M8): invitations and resets by email, "Forgot your
password?", the test email, and emailed dashboards, against a local SMTP server."""

from __future__ import annotations

import re
import socket
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from email import message_from_bytes, policy
from email.message import EmailMessage
from typing import Any

import pytest
from aiosmtpd.controller import Controller
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from opsis.db.models import AuditLog, Job, Schedule
from opsis.semantic.calendar import CalendarConfig
from opsis.services import schedules, subscriptions
from opsis.settings import ConfigError
from tests.conftest import make_settings
from tests.test_auth import ADMIN, PASSWORD, SIGN_IN, admin_client, simple_spec
from tests.test_ingestion import ok, run_jobs, settings_of
from tests.test_query_api import MODEL_YAML, load
from tests.test_query_api import source as source  # noqa: F401  (fixture)

ClientFor = Callable[..., TestClient]
SENDER = "Opsis <opsis@example.test>"


class Inbox:
    """An SMTP server's handler that keeps every message it is given."""

    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    async def handle_DATA(self, server: Any, session: Any, envelope: Any) -> str:  # noqa: N802
        parsed = message_from_bytes(envelope.content, policy=policy.default)
        assert isinstance(parsed, EmailMessage)
        self.messages.append(parsed)
        return "250 OK"

    def to(self, address: str) -> list[EmailMessage]:
        return [m for m in self.messages if m["To"] == address]

    @staticmethod
    def text(message: EmailMessage) -> str:
        part = message.get_body(("plain",))
        assert part is not None
        return str(part.get_content())  # type: ignore[attr-defined]

    @staticmethod
    def html(message: EmailMessage) -> str:
        part = message.get_body(("html",))
        assert part is not None
        return str(part.get_content())  # type: ignore[attr-defined]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def smtp() -> Iterator[tuple[Inbox, dict[str, Any]]]:
    inbox = Inbox()
    port = free_port()
    controller = Controller(inbox, hostname="127.0.0.1", port=port)
    controller.start()
    email = {
        "feature_email": True,
        "smtp_host": "127.0.0.1",
        "smtp_port": port,
        "smtp_security": "none",
        "smtp_from": SENDER,
    }
    try:
        yield inbox, email
    finally:
        controller.stop()


def invited(
    client_for: ClientFor,
    admin: TestClient,
    inbox: Inbox,
    email: dict[str, Any],
    address: str,
    role: str = "viewer",
    attributes: dict[str, Any] | None = None,
) -> tuple[TestClient, str]:
    """Invite someone by email and accept it from the email; returns (client, user id)."""
    made = ok(admin.post("/api/users", json={
        "email": address, "role": role, "attributes": attributes or {}}), 201)  # fmt: skip
    token = link_in(Inbox.text(inbox.to(address)[-1])).rsplit("/", 1)[1]
    client = client_for(**SIGN_IN, **email)
    ok(client.post(f"/api/auth/links/{token}", json={"password": PASSWORD}))
    return client, made["user"]["id"]


def link_in(text: str) -> str:
    found = re.search(r"http://localhost:8080/set-password/[A-Za-z0-9_-]+", text)
    assert found, text
    return found.group(0)


def test_email_settings_fail_fast() -> None:
    with pytest.raises(ConfigError) as info:
        make_settings(feature_email=True)
    assert "SMTP_HOST" in str(info.value)
    with pytest.raises(ConfigError) as info:
        make_settings(feature_email=True, smtp_host="mail.example.test", smtp_from="nobody")
    assert "SMTP_FROM" in str(info.value)
    settings = make_settings(
        feature_email=True, smtp_host="mail.example.test", smtp_from=SENDER, smtp_password=" "
    )
    assert settings.smtp_password is None and settings.smtp_security == "starttls"


def test_invitations_and_reset_links_go_by_email(
    client_for: ClientFor, smtp: tuple[Inbox, dict[str, Any]]
) -> None:
    inbox, email = smtp
    admin = admin_client(client_for, **email)
    made = ok(admin.post("/api/users", json={"email": "vera@example.test", "displayName": "Vera"}), 201)  # fmt: skip
    assert (made["emailed"], made["link"]) == (True, None)
    [invite] = inbox.to("vera@example.test")
    assert invite["From"] == SENDER and invite["Subject"] == "You are invited to Opsis"
    assert invite["Message-ID"].endswith("@example.test>")
    url = link_in(Inbox.text(invite))
    assert f'href="{url}"' in Inbox.html(invite)
    assert "Choose a password" in Inbox.html(invite)
    # The link in the email is the invitation: it works once.
    vera = client_for(**SIGN_IN, **email)
    ok(vera.post(f"/api/auth/links/{url.rsplit('/', 1)[1]}", json={"password": PASSWORD}))
    assert ok(vera.get("/api/me"))["email"] == "vera@example.test"

    sent = ok(admin.post(f"/api/users/{made['user']['id']}/password-link"))
    assert sent == {"link": None, "emailed": True}
    reset = inbox.to("vera@example.test")[-1]
    assert reset["Subject"] == "Set a new password for Opsis"
    assert "your password stays as it is" in Inbox.text(reset)

    # When the mail server cannot be reached, the admin gets the link to pass on.
    down = admin_client(client_for, **{**email, "smtp_port": free_port()})
    fallback = ok(down.post("/api/users", json={"email": "walt@example.test"}), 201)
    assert fallback["emailed"] is False and fallback["link"].startswith("http://localhost:8080/")
    assert "could not be sent through 127.0.0.1" in fallback["emailError"]
    # The messages are never stored: only the fact that one went out is audited.
    assert not inbox.to("walt@example.test")


def test_forgot_password(
    client_for: ClientFor, smtp: tuple[Inbox, dict[str, Any]], db: Session
) -> None:
    inbox, email = smtp
    admin = admin_client(client_for, **email)
    ok(admin.post("/api/users", json={"email": "vera@example.test", "displayName": "Vera"}), 201)
    ok(client_for(**SIGN_IN, **email).post(
        f"/api/auth/links/{link_in(Inbox.text(inbox.messages[-1])).rsplit('/', 1)[1]}",
        json={"password": PASSWORD},
    ))  # fmt: skip
    inbox.messages.clear()

    browser = client_for(**SIGN_IN, **email)
    unknown = ok(browser.post("/api/auth/forgot-password", json={"email": "nobody@example.test"}), 202)  # fmt: skip
    known = ok(browser.post("/api/auth/forgot-password", json={"email": "Vera@Example.test"}), 202)
    assert unknown == known  # the same answer whether or not there is an account
    [reset] = inbox.messages
    assert reset["To"] == "vera@example.test" and reset["Subject"] == "Set a new password for Opsis"
    token = link_in(Inbox.text(reset)).rsplit("/", 1)[1]
    assert ok(browser.get(f"/api/auth/links/{token}"))["purpose"] == "reset"
    ok(browser.post(f"/api/auth/links/{token}", json={"password": "Another-Pass-Word-31"}))
    assert db.scalar(select(AuditLog).where(AuditLog.action == "user.reset_requested"))

    # Three requests for one address in a while; then it waits, for known and unknown alike.
    for _ in range(2):
        ok(browser.post("/api/auth/forgot-password", json={"email": "vera@example.test"}), 202)
    refused = browser.post("/api/auth/forgot-password", json={"email": "vera@example.test"})
    assert refused.status_code == 429
    assert refused.json()["error"]["message"].startswith("Too many requests.")
    for _ in range(2):
        ok(browser.post("/api/auth/forgot-password", json={"email": "nobody@example.test"}), 202)
    assert (
        browser.post("/api/auth/forgot-password", json={"email": "nobody@example.test"}).status_code
        == 429
    )
    assert len(inbox.messages) == 3

    # Without email there is no such route: an admin makes reset links instead.
    plain = client_for(**SIGN_IN)
    assert plain.post("/api/auth/forgot-password", json={"email": ADMIN}).status_code == 404
    assert ok(plain.get("/api/config"))["features"]["email"] is False


def test_the_test_email(client_for: ClientFor, smtp: tuple[Inbox, dict[str, Any]]) -> None:
    inbox, email = smtp
    admin = admin_client(client_for, **email)
    assert ok(admin.post("/api/admin/email/test")) == {"sentTo": ADMIN}
    assert ok(admin.post("/api/admin/email/test", json={"to": "ops@example.test"})) == {
        "sentTo": "ops@example.test"
    }
    assert [m["Subject"] for m in inbox.messages] == ["Test email from Opsis"] * 2
    assert "Email works." in Inbox.text(inbox.messages[0])
    viewer, _ = invited(client_for, admin, inbox, email, "viewer@example.test")
    assert viewer.post("/api/admin/email/test").status_code == 403

    down = admin_client(client_for, **{**email, "smtp_port": free_port()})
    failed = down.post("/api/admin/email/test")
    assert failed.status_code == 502 and failed.json()["error"]["code"] == "email_failed"
    assert admin_client(client_for).post("/api/admin/email/test").status_code == 404


def test_emailed_dashboards(
    client_for: ClientFor,
    smtp: tuple[Inbox, dict[str, Any]],
    source: dict[str, Any],  # noqa: F811
    db: Session,
) -> None:
    inbox, email = smtp
    email = {**email, "feature_scheduled_refresh": True}  # for the Schedules page
    admin = admin_client(client_for, **email)
    load(admin, source)
    ok(admin.put("/api/models/gifts", json={"yaml": MODEL_YAML}))
    viewer, viewer_id = invited(
        client_for, admin, inbox, email, "viewer@example.test", "viewer", {"regions": ["VIC"]}
    )
    editor, _ = invited(client_for, admin, inbox, email, "editor@example.test", "editor")
    inbox.messages.clear()
    note = {
        "id": "note", "visual": "text", "position": {"x": 0, "y": 9, "w": 12, "h": 2},
        "config": {"markdown": "## About\n\nGifts **by region**, from [the CRM](https://crm.example.test)."},
    }  # fmt: skip
    spec = simple_spec("Giving <weekly>", more=[note])
    spec["items"][0]["title"] = "Total given"
    dashboard = ok(admin.post("/api/dashboards", json={"title": "Giving", "spec": spec}), 201)["id"]
    path = f"/api/dashboards/{dashboard}/subscription"

    # Not shared with the viewer yet: no such dashboard for them.
    assert viewer.get(path).status_code == 404
    ok(admin.put(f"/api/dashboards/{dashboard}/access", json={
        "shares": [{"grantee": f"user:{viewer_id}", "role": "viewer"}]}))  # fmt: skip
    ok(admin.post(f"/api/dashboards/{dashboard}/publish"))
    nothing = ok(viewer.get(path))
    assert (nothing["subscription"], nothing["email"]) == (None, "viewer@example.test")
    assert nothing["presets"] == list(schedules.PRESETS)
    bad = viewer.put(path, json={"cron": "*/5 * * * *"})
    assert bad.status_code == 422 and "too often" in bad.json()["error"]["message"]
    made = ok(viewer.put(path, json={"cron": "0 7 * * 1"}))["subscription"]
    assert made["preset"] == "weekly" and made["timezone"] == "Australia/Melbourne"
    [mine] = ok(viewer.get("/api/me/subscriptions"))["subscriptions"]
    assert mine["dashboardId"] == dashboard and mine["cron"] == "0 7 * * 1"
    ok(admin.put(path, json={"cron": "0 6 * * *"}))  # the admin's own, separately
    assert len(ok(admin.get("/api/me/subscriptions"))["subscriptions"]) == 1
    listed = ok(admin.get("/api/schedules"))["schedules"]
    assert {r["label"] for r in listed if r["kind"] == "snapshot"} == {
        "Email of Giving <weekly> to viewer",
        "Email of Giving <weekly> to Admin",
    }
    # Other people see only their own.
    assert [r["kind"] for r in ok(editor.get("/api/schedules"))["schedules"]] == []

    # Due now: the scheduler queues it, and the runner builds it as the viewer.
    settings = settings_of(admin)
    db.execute(update(Schedule).values(next_run_at=datetime.now(UTC) - timedelta(minutes=1)))
    db.commit()
    assert schedules.tick(db, settings) == 2
    run_jobs(admin)
    [sent] = inbox.to("viewer@example.test")
    assert sent["Subject"].startswith("Giving <weekly> (")
    text, html = Inbox.text(sent), Inbox.html(sent)
    assert "Giving &lt;weekly&gt;" in html and "<weekly>" not in html  # escaped
    # Row-level security: the viewer's email has VIC only; the admin's has every region.
    assert "VIC" in text and "NSW" not in text and "QLD" not in text
    assert all(r in Inbox.text(inbox.to(ADMIN)[0]) for r in ("VIC", "NSW", "QLD"))
    total = float(source["conn"].execute(
        "SELECT sum(amount) FROM src.gifts WHERE region = 'VIC'").fetchone()[0])  # fmt: skip
    assert f"${total:,.2f}" in text and "Total given" in text
    assert f"http://localhost:8080/dashboards/{dashboard}" in text
    assert "<b>by region</b>" in html and "the CRM (https://crm.example.test)" in html
    assert "You asked for this email: Mondays at 07:00 (Australia/Melbourne)." in text
    job = db.scalars(select(Job).where(Job.type == schedules.SNAPSHOT_JOB)).first()
    assert job is not None and job.status == "succeeded"

    # Once the viewer can no longer open it, the next email stops instead.
    ok(admin.put(f"/api/dashboards/{dashboard}/access", json={"shares": []}))
    db.execute(update(Schedule).values(next_run_at=datetime.now(UTC) - timedelta(minutes=1)))
    db.commit()
    schedules.tick(db, settings)
    run_jobs(admin)
    assert len(inbox.to("viewer@example.test")) == 1
    owners = set(db.scalars(select(Schedule.owner_id).where(Schedule.kind == "snapshot")).all())
    assert uuid.UUID(viewer_id) not in owners and len(owners) == 1
    assert db.scalar(select(AuditLog).where(AuditLog.action == "dashboard.subscription_removed"))

    # Stopping it, and deleting the dashboard, remove the schedule.
    assert ok(admin.put(path, json={"cron": None}))["subscription"] is None
    ok(admin.put(path, json={"cron": "0 6 * * *"}))
    assert admin.delete(f"/api/dashboards/{dashboard}").status_code == 204
    assert db.scalar(select(Schedule).where(Schedule.kind == "snapshot")) is None
    # Without email, the routes do not exist.
    assert admin_client(client_for).get("/api/me/subscriptions").status_code == 404


def test_values_are_written_the_way_the_app_writes_them() -> None:
    calendar = CalendarConfig(fiscal_year_start_month=7, fiscal_year_label="fy_end_year")
    assert subscriptions.format_measure(1234.5, "currency_aud") == "$1,234.50"
    assert subscriptions.format_measure(-1234.5, "currency_usd") == "-USD 1,234.50"
    assert subscriptions.format_measure(0.1234, "percent") == "12.3%"
    assert subscriptions.format_measure(0.5, "percent") == "50%"
    assert subscriptions.format_measure(1500, None) == "1,500"
    assert subscriptions.format_measure(2.345, "number") == "2.35"
    label = subscriptions.period_label
    assert label("2026-10-01", "month", calendar) == "Oct 2026"
    assert label("2026-10-01", "quarter", calendar) == "Q4 2026"
    assert label("2026-10-01", "fiscal_quarter", calendar) == "Q2 FY2027"
    assert label("2026-07-01", "fiscal_year", calendar) == "FY2027"
    assert label("2026-11-23", "week", calendar) == "Week of 23 Nov 2026"
    other = CalendarConfig(fiscal_year_start_month=7, fiscal_year_label="year_span")
    assert label("2026-07-01", "fiscal_year", other) == "2026-27"
