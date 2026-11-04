import time


def test_events_can_be_marked_edited_exported_and_removed(client):
    now = int(time.time() * 1000)
    a = client.post("/api/events", json={"label": "Incense lit", "t_ms": now - 600_000}).json()
    assert a["created_by"] == "tester" and a["end_ms"] is None
    b = client.post("/api/events", json={"label": "Window opened", "t_ms": now - 300_000, "end_ms": now - 60_000,
                                         "notes": "east window"}).json()
    c = client.post("/api/events", json={"label": "=cmd()"}).json()  # default time: now
    assert abs(c["t_ms"] - now) < 60_000
    assert client.post("/api/events", json={"label": "x", "t_ms": now, "end_ms": now - 1}).status_code == 400
    assert client.post("/api/events", json={"label": ""}).status_code == 422

    res = client.get("/api/events", params={"since_ms": now - 400_000, "until_ms": now + 1000}).json()
    assert "Incense lit" in res["presets"]
    assert [e["label"] for e in res["events"]] == ["Window opened", "=cmd()"]
    # A long event that started before the period still shows.
    assert [e["label"] for e in client.get("/api/events", params={"since_ms": now - 100_000, "until_ms": now - 90_000}).json()["events"]] == ["Window opened"]

    e = client.patch(f"/api/events/{a['id']}", json={"end_ms": now - 500_000}).json()
    assert e["end_ms"] == now - 500_000
    assert client.patch(f"/api/events/{a['id']}", json={"clear_end": True}).json()["end_ms"] is None
    assert client.patch(f"/api/events/{a['id']}", json={"end_ms": now - 700_000}).status_code == 400

    csv = client.get("/api/export/events.csv", params={"since_ms": now - 3600_000, "until_ms": now + 1000}).text.splitlines()
    assert csv[0].startswith("start_utc,start_ms,end_utc") and len(csv) == 4
    assert "'=cmd()" in csv[3] and "east window" in csv[2]

    assert client.delete(f"/api/events/{b['id']}").json() == {"deleted": b["id"]}
    assert client.delete(f"/api/events/{b['id']}").status_code == 404
    actions = [x["action"] for x in client.get("/api/audit").json()]
    assert {"event.add", "event.edit", "event.delete"} <= set(actions)
