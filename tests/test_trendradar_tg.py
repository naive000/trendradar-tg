import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import trendradar_tg as t

SRC = t.Source("demo", "示範榜", "🧪", page_items=2, index_items=1)


def payload(items):
    return {"status": "success", "items": items}


def test_clean_title_github():
    assert t.clean_title("php /      php-src") == "php/php-src"
    assert t.clean_title("  a   b ") == "a b"


def test_strip_tracking_keeps_other_params():
    url = "https://www.producthunt.com/products/x?utm_campaign=a&utm_source=b&ref=1"
    assert t.strip_tracking(url) == "https://www.producthunt.com/products/x?ref=1"
    assert t.strip_tracking("https://a.com/p") == "https://a.com/p"


def test_parse_board_drops_bad_items_and_reads_note():
    board = t.parse_board(SRC, payload([
        {"title": "好", "url": "https://a.com/1", "extra": {"info": "9 points"}},
        {"title": "", "url": "https://a.com/2"},
        {"title": "無連結"},
        {"title": "javascript", "url": "javascript:alert(1)"},
    ]))
    assert [i.title for i in board.items] == ["好"]
    assert board.items[0].note == "9 points"


def board(n=3):
    return t.Board(SRC, [t.Item(f"软件 {i}", f"https://a.com/{i}", "热度") for i in range(n)])


def test_index_text_converts_and_escapes():
    b = t.Board(SRC, [t.Item('软件 <b> & "x"', "https://a.com/?q=1&r=2")])
    text = t.build_index_text(dt.date(2026, 10, 23), [b])
    assert "軟體" in text and "&lt;b&gt;" in text and "&amp;" in text
    assert "科技熱點日報 · 2026-10-23" in text


def test_index_respects_index_items_and_skips_failed_boards():
    failed = t.Board(t.Source("bad", "壞榜", "x"), error="boom")
    text = t.build_index_text(dt.date(2026, 10, 23), [board(5), failed])
    assert text.count("・") == 1  # index_items=1
    assert "壞榜" not in text and "1 個榜單" in text


def test_page_nodes_and_failed_footer():
    failed = t.Board(t.Source("bad", "壞榜", "x"), error="boom")
    flat = t.serialize_page(dt.date(2026, 10, 23), [board(5), failed])
    assert flat.count('"tag":"li"') == 2  # page_items=2
    assert "軟體" in flat and "本次未取得：壞榜" in flat


def test_page_size_guard_shrinks_to_limit():
    big = t.Source("big", "大榜", "x", page_items=500)
    b = t.Board(big, [t.Item("标题" * 40, f"https://a.com/{i}" + "x" * 200, "n" * 50) for i in range(500)])
    out = t.serialize_page(dt.date(2026, 10, 23), [b] * 8)
    assert len(out.encode()) <= t.PAGE_LIMIT


def test_should_send_once_per_day():
    today = dt.date(2026, 10, 23)
    assert t.should_send({"sent_dates": []}, today)
    assert not t.should_send({"sent_dates": ["2026-10-23"]}, today)
    assert t.should_send({"sent_dates": ["2026-10-23"]}, today, force=True)
    assert t.should_send({"sent_dates": ["2026-10-22"]}, today)
