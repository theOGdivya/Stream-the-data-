

def test_spool_survives_its_folders_being_deleted(tmp_path):
    import shutil

    from aqagent.spool import Spool
    sp = Spool(tmp_path / "spool", keep_sent_days=1)
    shutil.rmtree(tmp_path / "spool")  # e.g. a cleanup tool, or someone tidying folders
    assert sp.pending_items() == [] and sp.backlog() == 0
    sp.prune_sent()
    meta = sp.put("cam", 1_700_000_000_000, b"jpeg", {"camera_id": "cam"})
    assert meta.exists() and sp.backlog() == 1
    sp.mark_sent(meta)
    assert sp.backlog() == 0


def test_uploader_keeps_going_after_an_unexpected_error(tmp_path):
    import threading

    from aqagent.config import AgentConfig
    from aqagent.net import Uploader
    from aqagent.spool import Spool

    stop = threading.Event()
    sp = Spool(tmp_path / "spool")
    cfg = AgentConfig.__new__(AgentConfig)
    cfg.server_url, cfg.agent_token, cfg.verify_tls = "http://127.0.0.1:9", "t" * 20, True
    up = Uploader(cfg, sp, stop)
    calls = {"n": 0}

    def flaky(limit=50):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("locked by antivirus")
        stop.set()
        return []

    sp.pending_items = flaky
    up.run()  # must not raise
    assert calls["n"] == 2 and "PermissionError" in up.last_error
