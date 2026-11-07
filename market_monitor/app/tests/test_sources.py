import json
import zlib

from market_monitor.sources import congress_common as cc
from market_monitor.sources import house, news, prices, quiver, sec_form4, senate, x_posts


# --------------------------------------------------------------------------- SEC Form 4
def test_parse_edgar_feed_dedupes_and_keeps_amendments(fixture_text):
    filings = sec_form4.parse_feed(fixture_text("edgar_feed.xml"))
    accs = [f["accession"] for f in filings]
    assert accs == ["0001888888-26-000012", "0001777777-26-000003"]
    f = filings[0]
    assert f["dir_url"] == "https://www.sec.gov/Archives/edgar/data/1888888/000188888826000012"
    assert f["filed_at"] == "2026-11-06T21:05:12Z"
    assert filings[1]["form"] == "4/A"


def test_pick_xml_from_index():
    idx = {"directory": {"item": [
        {"name": "0001888888-26-000012-index-headers.html"},
        {"name": "0001888888-26-000012.txt"},
        {"name": "wk-form4_1762463112.xml"},
    ]}}
    assert sec_form4.pick_xml_from_index(idx) == "wk-form4_1762463112.xml"
    assert sec_form4.pick_xml_from_index({"directory": {"item": []}}) is None


def test_parse_form4_and_aggregate(fixture_text):
    parsed = sec_form4.parse_form4(fixture_text("form4.xml"))
    assert parsed["ticker"] == "EXRO"
    assert parsed["owners"][0]["name"] == "Jane A Doe"
    assert parsed["owners"][0]["role"] == "Chief Executive Officer, Director"
    assert parsed["planned"] is False
    trades = sec_form4.form4_to_trades(parsed, "0001888888-26-000012", "2026-11-06T21:05:12Z", "https://x")
    # Alleen P (koop); F (belasting) wordt genegeerd
    assert len(trades) == 1
    t = trades[0]
    assert t["tx_type"] == "buy"
    assert t["shares"] == 15000
    assert t["value_usd"] == 10000 * 52.10 + 5000 * 53.00
    assert round(t["price"], 2) == 52.40
    assert t["tx_date"] == "2026-11-04"
    assert t["company"] == "Example Robotics Corp"


def test_pretty_name_keeps_entities():
    assert sec_form4.pretty_name("MUSK ELON") == "Elon Musk"
    assert sec_form4.pretty_name("BERKSHIRE HATHAWAY INC") == "Berkshire Hathaway Inc"


def test_sec_source_run_with_fake_session(cfg, db, fixture_text):
    class Resp:
        def __init__(self, text=None, js=None):
            self.text, self._js, self.status_code, self.ok = text, js, 200, True

        def json(self):
            return self._js

        def raise_for_status(self):
            pass

    class FakeSession:
        def __init__(self):
            self.urls = []

        def request(self, method, url, timeout=None, **kw):
            self.urls.append(url)
            assert "test@example.com" in kw["headers"]["User-Agent"]
            if "getcurrent" in url:
                return Resp(text=fixture_text("edgar_feed.xml"))
            if url.endswith("index.json"):
                return Resp(js={"directory": {"item": [{"name": "form4.xml"}]}})
            return Resp(text=fixture_text("form4.xml"))

    s = FakeSession()
    src = sec_form4.SecForm4Source(cfg, db, s)
    assert src.run() == 2  # twee filings met elk een koop (zelfde fixture)
    assert src.run() == 0  # tweede keer: alles al gezien
    assert db.query_one("SELECT COUNT(*) n FROM trades")["n"] == 2


# --------------------------------------------------------------------------- Congress
def test_parse_amount_ranges():
    assert cc.parse_amount("$1,001 - $15,000") == (1001.0, 15000.0, 8000.5)
    assert cc.parse_amount("Over $50,000,000") == (50000001.0, None, 50000001.0)
    assert cc.parse_amount("$250,001 -\n$500,000")[1] == 500000.0
    assert cc.parse_amount(None) == (None, None, None)


def test_house_index(fixture_text):
    filings = house.parse_index_xml(fixture_text("house_index.xml").encode())
    assert [f["doc_id"] for f in filings] == ["20099001", "8220099"]  # alleen FilingType P
    assert filings[0]["name"] == "Alex Example"
    assert filings[0]["filing_date"] == "2026-11-02"


def test_house_ptr_text(fixture_text):
    txs = house.parse_ptr_text(fixture_text("house_ptr.txt"))
    assert len(txs) == 4
    a, b, c, d = txs
    assert (a["ticker"], a["tx_type"], a["tx_date"], a["value_low"], a["value_high"]) == (
        "EXRO", "buy", "2026-10-14", 250001.0, 500000.0)
    assert a["asset"] == "Example Robotics Corp. - Common Stock"
    assert a["asset_type"] == "aandeel"
    assert (b["ticker"], b["tx_type"], b["value_high"]) == ("SMPL", "sell", 50000.0)
    assert b["asset"] == "Sample Software Inc. Class A"
    assert (c["ticker"], c["asset_type"], c["value_low"]) == ("EXRO", "optie", 1000001.0)
    assert d["ticker"] is None and d["asset_type"] == "staatsobligatie" and d["value_high"] is None


def _make_pdf(lines):
    """Minimale PDF met tekstregels, om de echte PDF-route (pypdf) te testen."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    content = "BT /F1 9 Tf 40 800 Td 12 TL\n" + "\n".join(f"({esc(ln)}) Tj T*" for ln in lines) + "\nET"
    stream = zlib.compress(content.encode("latin-1"))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" /Filter /FlateDecode >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


def test_house_pdf_roundtrip(fixture_text):
    lines = [ln.replace("•", "-") for ln in fixture_text("house_ptr.txt").splitlines()]
    text = house.extract_pdf_text(_make_pdf(lines))
    txs = house.parse_ptr_text(text)
    assert [t["ticker"] for t in txs] == ["EXRO", "SMPL", "EXRO", None]


def test_senate_parsing(fixture_text):
    rows = senate.parse_search_rows(json.loads(fixture_text("senate_search.json")))
    assert rows[0]["kind"] == "ptr" and rows[0]["name"] == "Casey Example"
    assert rows[0]["url"].endswith("/search/view/ptr/0a1b2c3d-1111-2222-3333-444455556666/")
    assert rows[1]["kind"] == "paper"
    txs = senate.parse_ptr_html(fixture_text("senate_ptr.html"))
    assert len(txs) == 3
    assert (txs[0]["ticker"], txs[0]["tx_type"], txs[0]["value_low"]) == ("EXRO", "buy", 50001.0)
    assert txs[0]["asset"] == "Example Robotics Corp & Co"
    assert txs[1]["tx_type"] == "sell"
    assert txs[2]["ticker"] is None and txs[2]["tx_type"] == "other"


def test_politician_id_is_shared_across_sources():
    a = cc.politician_trade_id("Casey Example", "EXRO", "2026-10-21", "buy", 50001)
    b = cc.politician_trade_id("Sen. Casey Example", "exro", "2026-10-21", "buy", 50001.0)
    assert a == b


def test_quiver_parse():
    rows = [{"Representative": "Casey Example", "House": "Senate", "Ticker": "exro", "Transaction": "Purchase",
             "Range": "$15,001 - $50,000", "TransactionDate": "2026-10-21", "ReportDate": "2026-10-28",
             "Party": "D"}]
    t = quiver.parse_quiver(rows)[0]
    assert t["ticker"] == "EXRO" and t["tx_type"] == "buy" and t["party"] == "D" and t["actor_role"] == "Senator"
    assert t["filed_at"] == "2026-10-28T00:00:00Z"


# --------------------------------------------------------------------------- nieuws / X / koersen
def test_all_feeds_down_is_reported_as_error(cfg):
    import pytest
    import requests

    class DownSession:
        def request(self, *a, **kw):
            raise requests.ConnectionError("down")

    src = news.RssSource(cfg, DownSession(), lambda items: len(items))
    with pytest.raises(RuntimeError, match="Geen enkele RSS-feed"):
        src.run()
    gn = news.GoogleNewsSource(cfg, DownSession(), lambda items: len(items))
    with pytest.raises(RuntimeError, match="Google News"):
        gn.run()



def test_google_news_source_and_title(fixture_text):
    items = news.parse_rss(fixture_text("google_news.xml"), "Google News", google_news=True)
    assert items[0]["domain"] == "reuters.com"
    assert items[0]["title"] == "Example Robotics shares jump after record quarter, raises guidance"
    assert items[0]["source"] == "Reuters"
    assert items[0]["published_at"] == "2026-11-07T13:20:00Z"


def test_gdelt_parse(fixture_text):
    items = news.parse_gdelt(json.loads(fixture_text("gdelt.json")))
    assert items[0]["domain"] == "ft.com"
    assert items[0]["published_at"] == "2026-11-07T13:15:00Z"


def test_x_queries_and_parse():
    accounts = [f"account{i:02d}" for i in range(40)]
    qs = x_posts.build_queries(accounts)
    assert all(len(q) <= 512 for q in qs)
    assert sum(q.count("from:") for q in qs) == 40
    data = {"data": [{"id": "123", "text": "Big news  today", "author_id": "9", "created_at": "2026-11-07T13:00:00.000Z"}],
            "includes": {"users": [{"id": "9", "username": "someone"}]}}
    item = x_posts.parse_tweets(data)[0]
    assert item["url"] == "https://x.com/someone/status/123"
    assert item["published_at"] == "2026-11-07T13:00:00Z"
    assert item["title"] == "Big news today"


def test_yahoo_and_stooq_parse():
    data = {"chart": {"result": [{"meta": {"currency": "USD", "regularMarketPrice": 12.5, "longName": "Example"},
                                  "timestamp": [1762300800, 1762387200, 1762473600],
                                  "indicators": {"quote": [{"close": [10.0, None, 12.0]}]}}]}}
    p = prices.parse_yahoo(data)
    assert p["series"] == [("2025-11-05", 10.0), ("2025-11-07", 12.0)]
    assert p["last_price"] == 12.5 and p["prev_close"] == 10.0
    s = prices.parse_stooq("Date,Open,High,Low,Close,Volume\n2026-11-05,1,1,1,10.5,100\n2026-11-06,1,1,1,11,100\n")
    assert s["last_price"] == 11.0 and s["prev_close"] == 10.5
