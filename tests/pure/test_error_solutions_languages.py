"""Pure tests for the multi-language error texts:
* XTL_ERROR_SOLUTIONS parity — 11 buckets × the same 25 codes, and (when
  the vendor bundle is on this machine) the 9 plugin-sourced buckets must
  match main.bundle's keywordN tables verbatim, via the bundle's own
  errorCodes map (code -> title/solution keyword);
* xtl_error_solutions() bucket routing (zh splits, pt->EN, junk->EN);
* the bubble in a non-FR/EN HA language (de): DE label + DE solution +
  EN chrome title + the official picture;
* translations/{ko,vi,zh-Hant}.json cover the 25 robot_error codes with the
  plugin titles.
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path
from types import SimpleNamespace

from .helpers import (
    FakePersistentNotification,
    load_error_events_module,
    load_xtl_module,
)
import pytest

PKG = Path(__file__).resolve().parents[2] / "custom_components" / "jonr_vac"
BUNDLE = (Path(__file__).resolve().parents[3]
          / "jonr_xiaomihome_plugin" / "1730143" / "android" / "main.bundle")

LANGS = ["en", "fr", "de", "es", "it", "pl", "ru", "zh_Hans", "zh_Hant",
         "ko", "vi"]


# --- bucket shape + parity vs the plugin bundle ------------------------------

def test_all_eleven_buckets_cover_the_same_codes(monkeypatch):
    xtl = load_xtl_module(monkeypatch)
    codes = sorted(xtl.XTL_ERROR_CODE_KEYS)
    assert sorted(xtl.XTL_ERROR_SOLUTIONS) == sorted(LANGS)
    for lang, table in xtl.XTL_ERROR_SOLUTIONS.items():
        assert sorted(table) == codes, lang


@pytest.mark.skipif(not BUNDLE.is_file(), reason="vendor bundle not here")
def test_plugin_buckets_match_the_bundle_verbatim(monkeypatch):
    """The 9 vendor buckets are the bundle's keywordN tables transcribed
    through its own errorCodes map — re-derive both sides here."""
    xtl = load_xtl_module(monkeypatch)
    txt = io.open(BUNDLE, encoding="utf-8", errors="replace").read()

    j = txt.find("var errorCodes = new Map([")
    assert j >= 0
    seg = txt[j:txt.find(";", j)]
    pairs = re.findall(
        r"\[(\d{4}),\s*\{\s*title:[^k]*\.keyword(\d+),\s*solution:"
        r"[^k]*\.keyword(\d+)\s*\}", seg)
    err_map = {int(c): (int(t), int(s)) for c, t, s in pairs}
    assert sorted(err_map) == sorted(xtl.XTL_ERROR_CODE_KEYS)

    tables = {}
    for site in re.finditer(r'keyword0:\s*"', txt):
        pos = site.start()
        d = {}
        while True:
            m = re.match(
                r'\s*keyword(\d+):\s*"((?:[^"\\]|\\.)*)"\s*,?\s*',
                txt[pos:pos + 200000])
            if not m:
                break
            d["keyword" + m.group(1)] = json.loads('"' + m.group(2) + '"')
            pos += m.end()
        tables[site.start()] = d
    assert len(tables) == 11, len(tables)
    # the 11 literal tables sit in the bundle's own language order (the
    # same order the extraction pass walked): en, zh-Hans, zh-Hant, ko,
    # ru, es, fr, it, de, pl, vi
    bundle_order = ["en", "zh_Hans", "zh_Hant", "ko", "ru", "es", "fr",
                    "it", "de", "pl", "vi"]
    by_lang = dict(zip(bundle_order, (tables[s] for s in sorted(tables, key=int))))

    for lang in LANGS[2:]:                       # en/fr keep OUR wording
        want = {
            code: by_lang[lang]["keyword%d" % err_map[code][1]].strip()
            for code in sorted(err_map)}
        got = xtl.XTL_ERROR_SOLUTIONS[lang]
        for code in sorted(err_map):
            assert got[code] == want[code], (lang, code)


# --- routing ------------------------------------------------------------------

def test_solutions_bucket_routing(monkeypatch):
    xtl = load_xtl_module(monkeypatch)
    cases = {
        "de": "de", "de-DE": "de", "DE": "de",
        "zh-Hant": "zh_Hant", "zh-HK": "zh_Hant", "zh-TW": "zh_Hant",
        "zh-Hans": "zh_Hans", "zh-CN": "zh_Hans", "zh": "zh_Hans",
        "pt": "en", "pt-BR": "en", "ja": "en", "nl": "en",
        "": "en", None: "en", "xx": "en", "ko-KR": "ko", "vi_VN": "vi",
        "fr-FR": "fr", "ru-RU": "ru", "pl": "pl",
    }
    for tag, bucket in cases.items():
        assert xtl._solution_bucket(tag) == bucket, tag
        assert len(xtl.xtl_error_solutions(tag)) == 25


# --- the DE bubble: vendor label + vendor solution + picture ------------------

class FakeBus:
    def __init__(self) -> None:
        self.fired: list[tuple[str, dict]] = []

    def async_fire(self, event_type: str, payload: dict) -> None:
        self.fired.append((event_type, payload))


def _notifier(monkeypatch, language):
    mod = load_error_events_module(monkeypatch)
    hass = SimpleNamespace(bus=FakeBus(), config=SimpleNamespace(language=language))
    entry = SimpleNamespace(entry_id="e1", unique_id="44:87:63:ce:c2:07",
                            options={})
    from .helpers import FakeCoordinator
    return mod, mod.JonrErrorNotifier(hass, entry, FakeCoordinator())


def test_bubble_in_german_carries_vendor_texts_and_en_chrome(monkeypatch):
    _, notifier = _notifier(monkeypatch, "de-DE")
    notifier.async_process({"robot": 4012, "station": None})

    created = FakePersistentNotification.created[-1]
    # chrome title: no plugin wording exists for our bubble chrome -> EN
    assert created["title"] == "JONR Vacuum: robot error"
    # content: DE label (translations/de.json, plugin title wording) +
    # DE solution (bundle keyword verbatim) + the official picture
    assert "Roboter steckt fest" in created["message"]
    assert "4012" in created["message"]
    assert "entfernen" in created["message"]
    assert "/jonr-vac-card/images/faults/RobotTrapped.png" in created["message"]


def test_bubble_label_language_follows_hass_language(monkeypatch):
    mod = load_error_events_module(monkeypatch)
    xtl = load_xtl_module(monkeypatch)
    for lang, bucket in [("zh-Hant", "zh-Hant"), ("ko", "ko"),
                         ("vi", "vi"), ("de", "de"), ("fr", "fr"),
                         ("pt-BR", "pt-BR"), ("pt", "pt")]:
        labels = mod._labels(lang)
        assert set(labels) == set(xtl.XTL_ERROR_CODE_KEYS)
        states = json.loads(
            (PKG / "translations" / f"{bucket}.json")
            .read_text(encoding="utf-8")
        )["entity"]["sensor"]["robot_error"]["state"]
        for code, slug in xtl.XTL_ERROR_CODE_KEYS.items():
            assert labels[code] == states[slug], (lang, slug)
    # an unknown HA language falls back to the EN table
    assert mod._labels("sv") is mod._labels("en")
    assert mod._labels(None) is mod._labels("en")


# --- the 3 new locales ship the plugin's own titles ---------------------------

def test_new_locales_cover_every_code_with_plugin_titles(monkeypatch):
    xtl = load_xtl_module(monkeypatch)
    codes = sorted(xtl.XTL_ERROR_CODE_KEYS)
    for fname, bucket in [("ko", "ko"), ("vi", "vi"), ("zh-Hant", "zh_Hant")]:
        doc = json.loads(
            (PKG / "translations" / f"{fname}.json").read_text(encoding="utf-8"))
        re_states = doc["entity"]["sensor"]["robot_error"]["state"]
        se_states = doc["entity"]["sensor"]["station_error"]["state"]
        assert re_states == se_states            # mirrors the other locales
        for code, slug in xtl.XTL_ERROR_CODE_KEYS.items():
            text = re_states[slug]
            assert text and text != slug, (fname, slug)
            if fname == "ko":                     # every ko title carries Hangul
                assert any("가" <= ch <= "힣" for ch in text), (fname, slug)
        assert re_states["none"] == "No error"   # plugin ships none -> EN
        assert len(re_states) == 26              # 25 codes + none
    # the shape stays en.json-complete (HA does not merge partial files)
    en = json.loads((PKG / "translations" / "en.json").read_text(encoding="utf-8"))

    def keys(o, p=()):
        if isinstance(o, dict):
            for k, v in o.items():
                yield from keys(v, p + (k,))
        else:
            yield p
    want = set(keys(en))
    for fname in ("ko", "vi", "zh-Hant"):
        doc = json.loads((PKG / "translations" / f"{fname}.json")
                         .read_text(encoding="utf-8"))
        assert set(keys(doc)) == want, fname
