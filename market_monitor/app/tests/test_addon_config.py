"""Controleert dat de add-on definitie (config.yaml) en de code bij elkaar passen."""

import re
from dataclasses import fields
from pathlib import Path

from market_monitor import __version__
from market_monitor.config import Config

ADDON = Path(__file__).resolve().parents[2]


def _top_level_keys(section: str, text: str) -> list[str]:
    block = ("\n" + text).split(f"\n{section}:\n", 1)[1]
    keys = []
    for line in block.splitlines():
        if line and not line.startswith(" "):
            break
        m = re.match(r"^  ([a-z_0-9]+):", line)
        if m:
            keys.append(m.group(1))
    return keys


def test_options_match_config_dataclass():
    text = (ADDON / "config.yaml").read_text(encoding="utf-8")
    options = set(_top_level_keys("options", text))
    schema = set(_top_level_keys("schema", text))
    code = {f.name for f in fields(Config)} - {"data_dir", "port"}
    assert options == schema
    assert options == code


def test_version_matches():
    text = (ADDON / "config.yaml").read_text(encoding="utf-8")
    assert f'version: "{__version__}"' in text


def test_translations_cover_all_options():
    text = (ADDON / "config.yaml").read_text(encoding="utf-8")
    options = set(_top_level_keys("options", text))
    for lang in ("nl", "en"):
        tr = (ADDON / "translations" / f"{lang}.yaml").read_text(encoding="utf-8")
        keys = set(_top_level_keys("configuration", tr))
        assert options == keys, (lang, options ^ keys)
