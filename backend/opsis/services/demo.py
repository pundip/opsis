"""Demo data: something to look at on a fresh install, with no source to connect.

`seed` makes three CSV datasets of made-up giving data, their semantic models
and a published dashboard:

  demo_gifts    15,000 gifts over the last three years, more in June and December
  demo_donors    1,500 donors, each in a state
  demo_regions   the eight states and territories

gifts join to donors and donors to regions, so the dashboard's table by state
uses a field two joins away. The data is generated, not real: the names are
first and last names put together at random, and the same day always gives the
same rows. Run it with `python -m opsis seed-demo` or from Settings (admins).
Running it again finishes a half-made demo and otherwise changes nothing;
delete the dashboard and the three datasets to remove it.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import random
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsis.auth.principal import Principal, Role
from opsis.db.models import Connection, Dashboard, Dataset, Job, User
from opsis.errors import ApiError, JobError
from opsis.jobs import queue
from opsis.services import connections, dashboards, datasets, models, refresh, users
from opsis.services.datasets import REFRESH_JOB
from opsis.settings import Settings

log = logging.getLogger("opsis.demo")

DEMO_JOB = "demo.seed"
TITLE = "Giving overview (demo)"
GIFTS, DONORS, REGIONS = "demo_gifts", "demo_donors", "demo_regions"
GIFT_ROWS, DONOR_ROWS = 15_000, 1_500

STATES = (
    ("ACT", "Australian Capital Territory", 2),
    ("NSW", "New South Wales", 30),
    ("NT", "Northern Territory", 1),
    ("QLD", "Queensland", 20),
    ("SA", "South Australia", 7),
    ("TAS", "Tasmania", 2),
    ("VIC", "Victoria", 27),
    ("WA", "Western Australia", 11),
)
FUNDS = (
    ("Scholarships", 28),
    ("Medical research", 22),
    ("Where most needed", 20),
    ("Library", 12),
    ("Sport", 10),
    ("Arts", 8),
)
CHANNELS = (
    ("Online", 42),
    ("Direct mail", 20),
    ("Phone", 15),
    ("Events", 13),
    ("Workplace giving", 8),
    ("Bequest", 2),
)
# Tier, share of donors, how often they give (relative), typical gift (lognormal mu, sigma).
TIERS = (
    ("Regular", 80, 1.0, (3.9, 0.7)),
    ("Mid-level", 16, 1.6, (6.0, 0.5)),
    ("Major", 4, 2.2, (8.4, 0.6)),
)
FIRST = (
    "Alex", "Sam", "Jordan", "Taylor", "Chris", "Morgan", "Jamie", "Casey", "Riley", "Avery",
    "Charlie", "Drew", "Emerson", "Finley", "Harper", "Hayden", "Jesse", "Kai", "Logan", "Quinn",
)  # fmt: skip
LAST = (
    "Nguyen", "Smith", "Williams", "Brown", "Jones", "Patel", "Wilson", "Taylor", "Anderson",
    "Thomas", "Lee", "Martin", "Chen", "White", "Walker", "Hall", "Young", "King", "Wright",
    "Scott",
)  # fmt: skip
# Gifts per month, relative: the end of the financial year (June) and December are busy.
MONTH_WEIGHT = {6: 2.2, 12: 1.6, 5: 1.2, 11: 1.1}


def generate(today: date) -> dict[str, bytes]:
    """The three CSV files, the same for the same day."""
    rng = random.Random(f"opsis-demo-{today.isoformat()}")
    start = today - timedelta(days=3 * 365)

    regions = io.StringIO()
    writer = csv.writer(regions, lineterminator="\n")
    writer.writerow(["code", "state"])
    writer.writerows((code, name) for code, name, _ in STATES)

    donors = io.StringIO()
    writer = csv.writer(donors, lineterminator="\n")
    writer.writerow(["donor_id", "donor_name", "tier", "state_code", "joined"])
    tiers: list[tuple[Any, ...]] = []
    for donor_id in range(1, DONOR_ROWS + 1):
        tier = rng.choices(TIERS, weights=[t[1] for t in TIERS])[0]
        tiers.append(tier)
        state = rng.choices(STATES, weights=[s[2] for s in STATES])[0]
        joined = start - timedelta(days=rng.randrange(0, 5 * 365))
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        writer.writerow([donor_id, name, tier[0], state[0], joined.isoformat()])

    days = [start + timedelta(days=n) for n in range((today - start).days + 1)]
    day_weights = [MONTH_WEIGHT.get(d.month, 1.0) for d in days]
    donor_weights = [t[2] for t in tiers]
    gifts = io.StringIO()
    writer = csv.writer(gifts, lineterminator="\n")
    writer.writerow(["gift_id", "donor_id", "gift_date", "fund", "channel", "amount"])
    for gift_id in range(1, GIFT_ROWS + 1):
        donor = rng.choices(range(1, DONOR_ROWS + 1), weights=donor_weights)[0]
        mu, sigma = tiers[donor - 1][3]
        amount = max(5.0, round(math.exp(rng.gauss(mu, sigma)), 2))
        when = rng.choices(days, weights=day_weights)[0]
        fund = rng.choices(FUNDS, weights=[f[1] for f in FUNDS])[0][0]
        channel = rng.choices(CHANNELS, weights=[c[1] for c in CHANNELS])[0][0]
        writer.writerow([gift_id, donor, when.isoformat(), fund, channel, f"{amount:.2f}"])

    return {
        REGIONS: regions.getvalue().encode(),
        DONORS: donors.getvalue().encode(),
        GIFTS: gifts.getvalue().encode(),
    }


MODELS: dict[str, dict[str, Any]] = {
    REGIONS: {
        "label": "Demo regions",
        "dimensions": {
            "code": {"column": "code", "type": "string", "label": "State code"},
            "state": {"column": "state", "type": "string", "label": "State"},
        },
        "measures": {"regions": {"agg": "count", "label": "Regions"}},
    },
    DONORS: {
        "label": "Demo donors",
        "dimensions": {
            "donor_name": {"column": "donor_name", "type": "string", "label": "Donor"},
            "tier": {"column": "tier", "type": "string", "label": "Tier"},
            "state_code": {"column": "state_code", "type": "string", "label": "State code"},
            "joined": {"column": "joined", "type": "time", "label": "Joined"},
        },
        "measures": {"donor_count": {"agg": "count", "label": "Donors"}},
        "joins": [
            {
                "to": REGIONS,
                "type": "left",
                "on": {"state_code": "code"},
                "relationship": "many_to_one",
            },
        ],  # fmt: skip
    },
    GIFTS: {
        "label": "Demo gifts",
        "dimensions": {
            "gift_date": {"column": "gift_date", "type": "time", "label": "Gift date"},
            "fund": {"column": "fund", "type": "string", "label": "Fund"},
            "channel": {"column": "channel", "type": "string", "label": "Channel"},
            "amount": {"column": "amount", "type": "number", "label": "Amount"},
        },
        "measures": {
            "total_given": {
                "agg": "sum",
                "column": "amount",
                "format": "currency_aud",
                "label": "Total given",
            },
            "gifts": {"agg": "count", "label": "Gifts"},
            "donors": {"agg": "count_distinct", "column": "donor_id", "label": "Donors"},
            "average_gift": {
                "expr": "{total_given} / NULLIF({gifts}, 0)",
                "format": "currency_aud",
                "label": "Average gift",
            },
        },
        "segments": {
            "major_gifts": {"filter": "amount >= 1000", "label": "Gifts of $1,000 or more"}
        },
        "joins": [
            {
                "to": DONORS,
                "type": "left",
                "on": {"donor_id": "donor_id"},
                "relationship": "many_to_one",
            },
        ],
    },  # fmt: skip
}
LABELS = {GIFTS: "Demo gifts", DONORS: "Demo donors", REGIONS: "Demo regions"}


def _kpi(item_id: str, x: int, measure: str, title: str, compare: bool) -> dict[str, Any]:
    return {
        "id": item_id,
        "visual": "kpi",
        "title": title,
        "position": {"x": x, "y": 0, "w": 3, "h": 3},
        "dataset": GIFTS,
        "bindings": {"value": [f"{GIFTS}.{measure}"]},
        "config": {"comparison": "previous_year" if compare else "none"},
    }


def dashboard_spec() -> dict[str, Any]:
    g = GIFTS
    total = f"{g}.total_given"
    return {
        "specVersion": 1,
        "title": TITLE,
        "description": "Made-up gifts over the last three years, from the demo data. "
        "Delete it, and the three demo datasets, any time.",
        "layout": {"columns": 12, "rowHeight": 40},
        "filters": [
            {
                "id": "period",
                "type": "dateRange",
                "field": f"{g}.gift_date",
                "label": "Period",
                "default": {"relative": "last_12_months"},
            },
            {"id": "fund", "type": "multiSelect", "field": f"{g}.fund", "label": "Fund"},
        ],  # fmt: skip
        "items": [
            _kpi("total", 0, "total_given", "Total given", True),
            _kpi("gift_count", 3, "gifts", "Gifts", True),
            _kpi("donor_count", 6, "donors", "Donors", False),
            _kpi("average", 9, "average_gift", "Average gift", False),
            {
                "id": "by_month",
                "visual": "line",
                "title": "Given each month",
                "position": {"x": 0, "y": 3, "w": 8, "h": 7},
                "dataset": g,
                "bindings": {"x": [{"field": f"{g}.gift_date", "grain": "month"}], "y": [total]},
            },
            {
                "id": "by_channel",
                "visual": "pie",
                "title": "By channel",
                "position": {"x": 8, "y": 3, "w": 4, "h": 7},
                "dataset": g,
                "bindings": {"category": [f"{g}.channel"], "value": [total]},
                "config": {"donut": True},
            },
            {
                "id": "by_fund",
                "visual": "bar",
                "title": "By fund",
                "position": {"x": 0, "y": 10, "w": 6, "h": 7},
                "dataset": g,
                "bindings": {"category": [f"{g}.fund"], "value": [total]},
                "sort": [{"field": total, "dir": "desc"}],
                "config": {"orientation": "horizontal"},
            },
            {
                "id": "by_state",
                "visual": "table",
                "title": "By state",
                "position": {"x": 6, "y": 10, "w": 6, "h": 7},
                "dataset": g,
                "bindings": {"columns": [f"{REGIONS}.state", total, f"{g}.gifts", f"{g}.donors"]},
                "sort": [{"field": total, "dir": "desc"}],
            },
        ],  # fmt: skip
        "interactions": {"crossFilter": True},
    }


def _actor(db: Session, settings: Settings, user_id: uuid.UUID | None) -> Principal:
    """Who the demo is made by: the admin who asked, or the first admin."""
    user = db.get(User, user_id) if user_id else None
    if user is None:
        # Local mode has the built-in admin; with sign-in, the first real admin.
        user = db.scalar(
            select(User)
            .where(User.role == Role.ADMIN.value, User.is_active)
            .where(User.is_builtin == (not settings.sign_in))
            .order_by(User.created_at)
        )
    if user is None:
        raise JobError("There is no admin account yet. Start the app once, then try again.")
    return users.principal_for(db, user, "demo")


def _refresh_now(db: Session, settings: Settings, actor: Principal, dataset: Dataset) -> None:
    """Run the first refresh here and now, recorded as a job like any other."""
    job = Job(
        type=REFRESH_JOB,
        payload={"dataset_id": str(dataset.id)},
        status="running",
        locked_by="demo",
        started_at=datetime.now(UTC),
        attempts=1,
        created_by=actor.user_id,
    )
    db.add(job)
    db.commit()
    try:
        result = refresh.run_refresh(db, job, settings)
    except Exception as exc:
        db.rollback()
        queue.finish(db, job, error=str(exc)[:500])
        raise JobError(f"The demo dataset {dataset.name} could not be refreshed: {exc}") from exc
    queue.finish(db, job, result=result)
    refresh.prune_snapshots(db, job, settings)


def seed(db: Session, settings: Settings, user_id: uuid.UUID | None = None) -> dict[str, Any]:
    actor = _actor(db, settings, user_id)
    existing = db.scalar(select(Dashboard).where(Dashboard.title == TITLE))
    names = (REGIONS, DONORS, GIFTS)
    made = {d.name: d for d in db.scalars(select(Dataset).where(Dataset.name.in_(names)))}
    if existing is not None and len(made) == len(names):
        raise JobError(
            "The demo data is already loaded. To load it again, delete the dashboard "
            f"{TITLE!r} and the datasets {', '.join(names)} first."
        )
    files = generate(settings.today())
    for name in names:
        dataset = made.get(name)
        if dataset is None:
            upload = uuid.uuid4().hex
            folder = connections.uploads_dir(settings) / upload
            folder.mkdir(parents=True)
            (folder / f"{name}.csv").write_bytes(files[name])
            connection_name = f"{LABELS[name]} (file)"
            connection = db.scalar(select(Connection).where(Connection.name == connection_name))
            if connection is None:
                connection = connections.create(
                    db, settings, actor, name=connection_name, connector_id="file",
                    config={"file": f"{upload}/{name}.csv"},
                )  # fmt: skip
            stream = connections.discover(settings, connection)[0].name
            dataset = datasets.create(
                db, settings, actor, name=name, label=LABELS[name], connection_id=connection.id,
                stream=stream, refresh_mode="full", cursor_field=None, primary_key=[],
                columns=None,
            )  # fmt: skip
            made[name] = dataset
        if dataset.current_snapshot_id is None:
            _refresh_now(db, settings, actor, dataset)
        db.refresh(dataset)
        record = models.get_record(db, dataset)
        if record is None or record.is_starter:
            models.save(db, settings, actor, dataset, MODELS[name], message="Demo data")
    if existing is None:
        try:
            existing = dashboards.create(
                db, settings, actor, title=TITLE, spec=dashboard_spec(), message="Demo data"
            )
        except ApiError as exc:
            found = "; ".join(f"{d['path']}: {d['message']}" for d in exc.details[:5])
            raise JobError(f"The demo dashboard could not be made: {exc.message} {found}") from exc
    if existing.published_version is None:
        dashboards.publish(db, actor, existing, None)
    url = f"{settings.public_base_url}/dashboards/{existing.id}"
    log.info("Demo data loaded: %s", url)
    return {
        "datasets": {name: str(made[name].id) for name in names},
        "dashboardId": str(existing.id),
        "url": url,
        "rows": {REGIONS: len(STATES), DONORS: DONOR_ROWS, GIFTS: GIFT_ROWS},
    }


def run_demo(db: Session, job: Job, settings: Settings) -> dict[str, Any]:
    """The job behind Settings > Load demo data."""
    requested = job.payload.get("requestedBy")
    return seed(db, settings, uuid.UUID(str(requested)) if requested else None)
