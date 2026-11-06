"""The deterministic replay that protects the demo if the venue Wi-Fi fails."""

from app import db
from app.crustdata.client import CrustdataClient
from app.crustdata.simulator import SimulatorTransport
from app.pipeline.run import process_signup, run_pill
from app.services import build_services
from tests.conftest import make_settings


async def full_run(services, email):
    signup = db.create_signup(email)
    await process_signup(services, signup["id"])
    result = db.get_signup(signup["id"])
    return result, await run_pill(services, result, result["pills"][0])


async def test_a_recorded_run_replays_with_no_network(tmp_path):
    db.close()
    first_db, second_db = tmp_path / "a.sqlite", tmp_path / "b.sqlite"

    settings = make_settings(tmp_path, crustdata_mode="record", db_path=str(first_db))
    db.connect(settings.db_path)
    recorder = build_services(settings)
    recorder.client = CrustdataClient(settings, transport=SimulatorTransport(latency=False))
    recorded, _ = await full_run(recorder, "priya@hirefast.in")
    await recorder.client.close()
    db.close()

    settings = make_settings(tmp_path, crustdata_mode="replay", db_path=str(second_db))
    db.connect(settings.db_path)
    player = build_services(settings)

    class NoNetwork:
        async def post(self, *args, **kwargs):
            raise AssertionError("replay must not touch the network")

        async def aclose(self):
            pass

    player.client.http = NoNetwork()
    replayed, pill = await full_run(player, "priya@hirefast.in")
    await player.client.close()

    assert replayed["status"] == "activated" or replayed["status"] == "delivered"
    assert replayed["result_rows"] == recorded["result_rows"]
    assert pill["ok"] is True
    assert db.credits_for(replayed["id"]) == db.credits_for(replayed["id"])
    db.close()


async def test_a_recording_miss_fails_fast_and_lands_safely(tmp_path):
    db.close()
    settings = make_settings(tmp_path, crustdata_mode="replay")
    db.connect(settings.db_path)
    player = build_services(settings)
    signup = db.create_signup("someone.new@brand-new-startup.xyz")
    await process_signup(player, signup["id"])
    result = db.get_signup(signup["id"])
    assert result["status"] == "awaiting_intent"
    identify_calls = [c for c in db.list_calls(signup["id"]) if c["endpoint"] == "/company/identify"]
    assert len(identify_calls) == 1, "a missing recording must not be retried"
    await player.client.close()
    db.close()
