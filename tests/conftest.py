import pytest

from app import db
from app.config import Settings
from app.services import build_services


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "crustdata_mode": "sim",
        "crustdata_api_key": "sk-secret-test-key",
        "sim_latency": False,
        "anthropic_api_key": "",
        "app_base_url": "http://test",
        "db_path": str(tmp_path / "test.sqlite"),
        "replay_dir": str(tmp_path / "replay"),
        "email_live": False,
        "slack_live": False,
    }
    return Settings(_env_file=None, **{**values, **overrides})


@pytest.fixture
async def services(tmp_path):
    db.close()
    settings = make_settings(tmp_path)
    db.connect(settings.db_path)
    built = build_services(settings)
    yield built
    await built.client.close()
    db.close()
