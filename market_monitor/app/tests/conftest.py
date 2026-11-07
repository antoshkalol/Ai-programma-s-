import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from market_monitor.config import Config  # noqa: E402
from market_monitor.db import Database  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_text():
    def _read(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return _read


@pytest.fixture
def cfg(tmp_path):
    return Config(data_dir=str(tmp_path), stock_watchlist=["EXRO", "SMPL"], crypto_watchlist=["BTC", "ETH"],
                  sec_user_agent="Test test@example.com")


@pytest.fixture
def db(cfg):
    return Database(cfg.db_path)
