import random
import time

from aqserver.accuracy import pick_spot_checks, reading_accuracy, wilson
from aqserver.worker import process_pending
from render import render_scene
from test_api import jpeg, upload
from test_strip import _setup


def test_wilson_interval():
    assert wilson(0, 0) == (None, None)
    lo, hi = wilson(49, 50)
    assert 89 < lo < 90 and 99.5 < hi <= 100
    lo, hi = wilson(50, 50)
    assert hi == 100.0 and lo > 92


def _series(client, agent_client, app, settings, n=12):
    t0 = int(time.time() * 1000) - 3600_000
    _setup(client, agent_client, app, settings, pm="37", co2="812", t=t0)
    for i in range(1, n):
        frame, _ = render_scene(str(37 + i % 3), str(812 + i % 4))
        upload(agent_client, jpeg(frame), captured_ms=t0 + i * 10_000)
    process_pending(app.state.Session, settings, batch=50)
    return t0


def test_spot_checks_give_an_accuracy_with_a_confidence_range(client, agent_client, app, settings):
    t0 = _series(client, agent_client, app, settings)
    res = client.get("/api/spotcheck", params={"since_ms": t0 - 1, "until_ms": t0 + 600_000, "limit": 6}).json()
    assert res["target"] == 50 and len(res["items"]) == 6
    assert {i["metric"] for i in res["items"]} == {"pm25", "co2"}  # both numbers get checked
    assert len({i["id"] for i in res["items"]}) == 6
    first, second, third, *_ = res["items"]
    assert client.post(f"/api/readings/{first['id']}/spotcheck", json={"action": "right"}).json()["status"] == "verified"
    r = client.post(f"/api/readings/{second['id']}/spotcheck", json={"action": "wrong", "value": 1}).json()
    assert r["status"] == "corrected" and r["final_value"] == 1
    assert client.post(f"/api/readings/{third['id']}/spotcheck", json={"action": "unclear"}).json()["status"] == "ok"
    assert client.post(f"/api/readings/{first['id']}/spotcheck", json={"action": "right"}).status_code == 409
    assert client.post(f"/api/readings/{second['id']}/spotcheck", json={"action": "wrong"}).status_code == 409

    acc = client.get("/api/results", params={"since_ms": t0 - 1, "until_ms": t0 + 600_000, "avg_s": 60}).json()["accuracy"]
    checked = [a for a in acc if a["spot_checked"]]
    assert sum(a["spot_checked"] for a in acc) == 2 and sum(a["spot_right"] for a in acc) == 1
    assert all(a["ci_low"] is not None for a in checked)
    # Readings already checked are not offered again.
    again = client.get("/api/spotcheck", params={"since_ms": t0 - 1, "until_ms": t0 + 600_000, "limit": 50}).json()
    assert not {first["id"], second["id"], third["id"]} & {i["id"] for i in again["items"]}
    csv = client.get("/api/export/readings.csv", params={"since_ms": t0 - 1, "until_ms": t0 + 600_000}).text
    assert csv.splitlines()[0].endswith(",spot_check") and ",spot\r" in csv or ",spot\n" in csv


def test_review_queue_outcomes_are_counted_separately(client, agent_client, app, settings):
    t0 = _series(client, agent_client, app, settings, n=4)
    with app.state.Session() as db:
        picked = pick_spot_checks(db, t0 - 1, t0 + 600_000, limit=2, rng=random.Random(1))
        assert len(picked) == 2
    rid = picked[0].id
    client.post(f"/api/readings/{rid}/flag")
    client.post(f"/api/readings/{rid}/review", json={"action": "correct", "value": 5})
    with app.state.Session() as db:
        rows = reading_accuracy(db, t0 - 1, t0 + 600_000)
    assert sum(r["reviewed"] for r in rows) == 1 and sum(r["review_corrected"] for r in rows) == 1
    assert sum(r["spot_checked"] for r in rows) == 0
