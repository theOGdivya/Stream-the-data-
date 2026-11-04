import time

from aqserver.models import Reading
from aqserver.plausibility import odd_against
from aqserver.worker import process_pending
from render import render_scene
from test_api import heartbeat, jpeg, upload
from test_strip import _setup


def test_isolated_jumps_are_odd_but_real_changes_are_not():
    assert odd_against(2, [812, 810, 815], [816, 813], "co2") is not None      # dropped digits
    assert odd_against(812, [12, 13, 12], [13, 12], "pm25") is not None        # ghost digit
    assert odd_against(8120, [812, 810], [815], "co2") is not None             # extra digit
    assert odd_against(60, [15, 15, 15], [150, 280], "pm25") is None          # incense: rising
    assert odd_against(900, [500, 510, 505], [905, 910], "co2") is None        # a step that lasts
    assert odd_against(31, [14, 16, 15], [14, 17], "pm25") is None            # noise at low levels
    assert odd_against(24.0, [23.1, 23.2, 23.2], [23.3, 23.2], "temp") is None
    assert odd_against(32.0, [23.1, 23.2, 23.2], [23.3, 23.2], "temp") is not None
    assert odd_against(2, [812, 810], [], "co2") is None                       # no "after" yet: wait


def test_misread_in_a_series_goes_to_review(client, agent_client, app, settings):
    t0 = int(time.time() * 1000) - 600_000
    _setup(client, agent_client, app, settings, pm="37", co2="812", t=t0)
    seq = [("38", "815"), ("37", "2"), ("39", "814"), ("38", "816")]  # the reader "saw" 2 once
    ids = []
    for i, (pm, co2) in enumerate(seq, 1):
        frame, _ = render_scene(pm, co2)
        ids.append(upload(agent_client, jpeg(frame), captured_ms=t0 + i * 10_000).json()["id"])
        process_pending(app.state.Session, settings)
    with app.state.Session() as db:
        co2 = {r.frame_id: r for r in db.query(Reading).filter(Reading.metric == "co2")}
        pm = {r.frame_id: r for r in db.query(Reading).filter(Reading.metric == "pm25")}
    odd = co2[ids[1]]
    assert odd.value == 2 and odd.status == "review" and odd.final_value is None
    assert odd.note == "unusual jump: 2 between readings of about 814"
    assert all(co2[i].status == "ok" for i in (ids[0], ids[2], ids[3]))
    assert all(r.status == "ok" for r in pm.values())
    # A person confirms or corrects it in Review as usual.
    r = client.post(f"/api/readings/{odd.id}/review", json={"action": "correct", "value": 813}).json()
    assert r["status"] == "corrected"
