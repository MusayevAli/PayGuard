"""A model-sized callback cannot hide clock state or prevent an operator pause."""

from threading import Event, Thread

from starlette.testclient import TestClient

from api.main import create_app


def test_state_and_keyed_pause_respond_during_a_keyed_step(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNER", "stub")
    monkeypatch.setenv("PAYGUARD_CONTROLLER_TOKEN", "")
    world = tmp_path / "world"
    world.mkdir()
    (world / "world.jsonl").write_text("")
    app = create_app(world_dir=world, db_path=tmp_path / "events.db", desk=False)
    entered, release, responsive = Event(), Event(), Event()
    responses = {}

    def slow_agent(_events):
        entered.set()
        assert release.wait(4)

    app.state.clock.after_advance = slow_agent
    with TestClient(app) as client:

        def advance():
            responses["step"] = client.post("/clock/step", headers={"Idempotency-Key": "slow-step"})

        def inspect_and_pause():
            responses["state"] = client.get("/state")
            responses["pause"] = client.post(
                "/clock/pause", headers={"Idempotency-Key": "operator-pause"}
            )
            responsive.set()

        step, controls = Thread(target=advance), Thread(target=inspect_and_pause)
        step.start()
        try:
            assert entered.wait(1)
            controls.start()
            assert responsive.wait(1), "state/pause waited behind a slow agent callback"
            assert step.is_alive(), "the test callback must still be running"
            assert responses["state"].status_code == 200
            assert responses["state"].json()["day_index"] == 1
            assert responses["pause"].status_code == 200
            assert responses["pause"].json()["running"] is False
        finally:
            release.set()
            step.join(4)
            if controls.ident is not None:
                controls.join(4)
        assert not step.is_alive() and not controls.is_alive()
        assert responses["step"].status_code == 200
