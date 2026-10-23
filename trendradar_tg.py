"""科技熱點日報 → Telegram。

資料來源：NewsNow 公共 API（https://github.com/ourongxing/newsnow），一次抓多個科技榜單的當下快照。
流程：每個榜單取前 N 名 → 標題簡轉繁（OpenCC s2twp）→ 建 Telegraph 全文頁 → 發頻道索引訊息
（標題＋各榜前幾名＋「閱讀全文」按鈕）→ 成功才寫回 state.json。每個台北日期只發一次。無 LLM。
靈感來自 sansan0/TrendRadar（GPL-3.0）；本專案為獨立重寫，未使用其程式碼。
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from opencc import OpenCC

NEWSNOW_API = "https://newsnow.busiyi.world/api/s"
NEWSNOW_URL = "https://github.com/ourongxing/newsnow"
TELEGRAPH_API = "https://api.telegra.ph"
AUTHOR_NAME = "科技熱點日報"
AUTHOR_URL = NEWSNOW_URL
STATE_PATH = Path(__file__).with_name("state.json")
TAIPEI = dt.timezone(dt.timedelta(hours=8))
# NewsNow 擋預設 UA（Cloudflare），須帶瀏覽器 UA
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")


@dataclass(frozen=True)
class Source:
    id: str  # NewsNow 榜單 id
    name: str
    icon: str
    page_items: int = 15  # Telegraph 全文列幾筆
    index_items: int = 3  # 索引訊息列幾筆


# 科技向榜單；要增減直接改這裡。不含政治／娛樂熱搜（知乎、微博等）。
SOURCES: list[Source] = [
    Source("hackernews", "Hacker News", "🟧"),
    Source("github-trending-today", "GitHub 今日趨勢", "🐙"),
    Source("producthunt", "Product Hunt", "🚀"),
    Source("v2ex-share", "V2EX 分享", "💬"),
    Source("sspai", "少數派", "📱"),
    Source("juejin", "掘金", "⛏️"),
    Source("ithome", "IT 之家", "🖥️"),
    Source("solidot", "Solidot", "🔬"),
]

PAGE_LIMIT = 60000  # Telegraph 內容上限 64KB
TITLE_LIMIT = 200
TEASER_LIMIT = 46  # 索引每則標題字數
SEND_INTERVAL = 3.0
FETCH_RETRIES = 3
MIN_OK_SOURCES = 3  # 成功榜單少於此數就不發，避免發出殘缺日報
SENT_DATES_KEEP = 60

_cc = OpenCC("s2twp")


def to_tw(text: str) -> str:
    return _cc.convert(text)


# ---------- HTTP ----------

def _request(url: str, *, data: bytes | None = None, headers: dict | None = None, label: str) -> bytes:
    """所有對外請求的唯一出口。錯誤訊息只帶 label，避免 token 隨 URL 進公開 log。"""
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            body = json.loads(e.read())
            detail = f" ({body.get('description') or body.get('error') or body.get('message')})"
        except Exception:
            pass
        raise RuntimeError(f"{label}: HTTP {e.code}{detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"{label}: {e.reason}") from None


def with_flood_retry(fn, attempts: int = 4):
    """碰到 Telegram 'retry after n' 就等待後重試。"""
    for i in range(attempts):
        try:
            return fn()
        except RuntimeError as e:
            m = re.search(r"retry after (\d+)", str(e))
            if not m or i == attempts - 1:
                raise
            wait = int(m.group(1)) + 1
            print(f"rate limited, sleep {wait}s")
            time.sleep(wait)


# ---------- Fetch / parse ----------

@dataclass
class Item:
    title: str
    url: str
    note: str = ""  # 熱度、星數等附註


@dataclass
class Board:
    source: Source
    items: list[Item] = field(default_factory=list)
    error: str | None = None


def clean_title(raw: str) -> str:
    """GitHub 榜單標題長得像 `php /      php-src`；壓成 `php/php-src`。"""
    text = re.sub(r"\s*/\s+", "/", raw.strip())
    return re.sub(r"\s+", " ", text)


def strip_tracking(url: str) -> str:
    """移除 utm_* 追蹤參數（Product Hunt 連結帶 NewsNow 的 utm），其餘 query 保留。"""
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if not k.startswith("utm_")]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def parse_board(source: Source, payload: dict) -> Board:
    """NewsNow 回應 → Board。缺標題或連結的項目丟棄；只收 http(s) 連結。"""
    board = Board(source)
    for raw in payload.get("items") or []:
        title = clean_title(str(raw.get("title") or ""))
        url = str(raw.get("url") or "")
        if not title or not re.match(r"https?://", url):
            continue
        url = strip_tracking(url)
        extra = raw.get("extra") or {}
        board.items.append(Item(title[:TITLE_LIMIT], url, clean_title(str(extra.get("info") or ""))))
    return board


def fetch_board(source: Source) -> Board:
    last = ""
    for attempt in range(FETCH_RETRIES):
        try:
            raw = _request(f"{NEWSNOW_API}?id={urllib.parse.quote(source.id)}",
                           headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                           label=f"newsnow {source.id}")
            payload = json.loads(raw)
            if payload.get("status") not in ("success", "cache"):
                raise RuntimeError(f"newsnow {source.id}: status {payload.get('status')}")
            board = parse_board(source, payload)
            if not board.items:
                raise RuntimeError(f"newsnow {source.id}: empty")
            return board
        except (RuntimeError, ValueError) as e:
            last = str(e)
            time.sleep(2 * (attempt + 1))
    return Board(source, error=last)


def fetch_all(sources: list[Source] = SOURCES) -> list[Board]:
    boards = []
    for s in sources:
        boards.append(fetch_board(s))
        time.sleep(1.0)  # 對公共 API 客氣一點
    return boards


# ---------- Render ----------

def day_title(date: dt.date) -> str:
    return f"科技熱點日報 · {date.isoformat()}"


def teaser(title: str) -> str:
    text = to_tw(title)
    return text[:TEASER_LIMIT].rstrip() + ("…" if len(text) > TEASER_LIMIT else "")


def build_index_text(date: dt.date, boards: list[Board]) -> str:
    """頻道索引訊息：標題＋各榜前幾名（標題可點）。"""
    ok = [b for b in boards if b.items]
    lines = [f"<b>📡 {html.escape(day_title(date))}</b>", f"{len(ok)} 個榜單的今日焦點：", ""]
    for b in ok:
        lines.append(f"<b>{b.source.icon} {html.escape(b.source.name)}</b>")
        for it in b.items[: b.source.index_items]:
            lines.append(f'・<a href="{html.escape(it.url, quote=True)}">{html.escape(teaser(it.title))}</a>')
        lines.append("")
    return "\n".join(lines).rstrip()


def _node(tag: str, attrs: dict | None = None, children: list | None = None) -> dict:
    node: dict = {"tag": tag}
    if attrs:
        node["attrs"] = attrs
    if children:
        node["children"] = children
    return node


def _link(label: str, href: str) -> dict:
    return _node("a", {"href": href}, [label])


def board_nodes(b: Board, limit: int) -> list:
    nodes = [_node("h4", children=[f"{b.source.icon} {b.source.name}"])]
    lis = []
    for it in b.items[:limit]:
        children: list = [_link(to_tw(it.title), it.url)]
        if it.note:
            children.append(f"　{to_tw(it.note)}")
        lis.append(_node("li", children=children))
    nodes.append(_node("ol", children=lis))
    return nodes


def serialize_page(date: dt.date, boards: list[Board]) -> str:
    """Telegraph 頁面內容（JSON nodes）。超過 64KB 就逐步減少每榜筆數。"""
    ok = [b for b in boards if b.items]
    failed = [b.source.name for b in boards if not b.items]
    for limit in (15, 10, 5, 3):
        nodes: list = [_node("p", children=[
            f"{date.isoformat()} 抓取的 {len(ok)} 個科技榜單。標題經簡轉繁，英文原樣保留。資料來自 ",
            _link("NewsNow", NEWSNOW_URL), "。"])]
        for b in ok:
            nodes += board_nodes(b, min(limit, b.source.page_items))
        if failed:
            nodes.append(_node("p", children=[_node("em", children=["本次未取得：" + "、".join(failed)])]))
        nodes.append(_node("hr"))
        nodes.append(_node("p", children=["各榜單內容版權屬原站；本頁僅列標題與連結，點入閱讀原文。"]))
        out = json.dumps(nodes, ensure_ascii=False, separators=(",", ":"))
        if len(out.encode()) <= PAGE_LIMIT:
            return out
    return out


# ---------- Telegraph / Telegram ----------

def telegraph_call(method: str, **params: str) -> dict:
    body = urllib.parse.urlencode(params).encode()
    raw = _request(f"{TELEGRAPH_API}/{method}", data=body,
                   headers={"Content-Type": "application/x-www-form-urlencoded"}, label=f"telegraph {method}")
    resp = json.loads(raw)
    if not resp.get("ok"):
        raise RuntimeError(f"telegraph {method}: {resp.get('error')}")
    return resp["result"]


def telegraph_create_account() -> str:
    return telegraph_call("createAccount", short_name="tech-hot", author_name=AUTHOR_NAME,
                          author_url=AUTHOR_URL)["access_token"]


def telegraph_create_page(token: str, date: dt.date, boards: list[Board]) -> str:
    result = telegraph_call("createPage", access_token=token, title=day_title(date)[:256], author_name=AUTHOR_NAME,
                            author_url=AUTHOR_URL, content=serialize_page(date, boards), return_content="false")
    return result["url"]


def telegram_send_index(bot_token: str, chat_id: str, date: dt.date, boards: list[Board], page_url: str) -> None:
    params = {
        "chat_id": chat_id,
        "text": build_index_text(date, boards),
        "parse_mode": "HTML",
        "reply_markup": json.dumps({"inline_keyboard": [[{"text": "閱讀全文", "url": page_url}]]}, ensure_ascii=False),
        "link_preview_options": json.dumps({"is_disabled": True}),
    }
    raw = _request(f"https://api.telegram.org/bot{bot_token}/sendMessage", data=urllib.parse.urlencode(params).encode(),
                   headers={"Content-Type": "application/x-www-form-urlencoded"}, label="telegram sendMessage")
    if not json.loads(raw).get("ok"):
        raise RuntimeError("telegram sendMessage: not ok")


# ---------- State ----------

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"last_date": None, "sent_dates": [], "pages": {}, "last_sent": None}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def should_send(state: dict, today: dt.date, force: bool = False) -> bool:
    """每個台北日期只發一次；--force 才重發。"""
    return force or today.isoformat() not in state.get("sent_dates", [])


def _require_env(*names: str) -> dict:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        sys.exit(f"missing env: {', '.join(missing)}")
    return {n: os.environ[n] for n in names}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="今天已發過也重發（會重用已建的 Telegraph 頁）")
    ap.add_argument("--create-telegraph-account", action="store_true", help="建立 Telegraph 帳號並印出 token")
    ap.add_argument("--dry-run", action="store_true", help="只印訊息，不發送、不寫 state")
    args = ap.parse_args(argv)

    if args.create_telegraph_account:
        sys.stdout.write(telegraph_create_account())
        return 0
    today = dt.datetime.now(TAIPEI).date()
    state = load_state()
    if not args.dry_run and not should_send(state, today, args.force):
        print(f"today={today} already sent, nothing to do")
        return 0

    boards = fetch_all()
    for b in boards:
        print(f"{b.source.id}: {len(b.items)} items" + (f" ERROR {b.error}" if b.error else ""))
    ok = sum(1 for b in boards if b.items)
    if ok < MIN_OK_SOURCES:
        print(f"only {ok} sources ok (< {MIN_OK_SOURCES}), abort without sending")
        return 1
    if args.dry_run:
        page = serialize_page(today, boards)
        print(f"--- index ---\n{build_index_text(today, boards)}\n--- page ({len(page.encode())} bytes) ---\n{page[:500]} ...")
        return 0

    env = _require_env("TG_BOT_TOKEN", "TG_CHAT_ID", "TELEGRAPH_TOKEN")
    key = today.isoformat()
    pages = state.setdefault("pages", {})
    if key not in pages:  # Telegraph 頁建了刪不掉：發送失敗重試時重用，不重建
        pages[key] = with_flood_retry(lambda: telegraph_create_page(env["TELEGRAPH_TOKEN"], today, boards))
        save_state(state)
    with_flood_retry(lambda: telegram_send_index(env["TG_BOT_TOKEN"], env["TG_CHAT_ID"], today, boards, pages[key]))
    state["sent_dates"] = sorted(set(state.get("sent_dates", [])) | {key})[-SENT_DATES_KEEP:]
    state["pages"] = {k: v for k, v in pages.items() if k in state["sent_dates"]}
    state["last_date"] = key
    state["last_sent"] = key
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
