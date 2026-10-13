#!/usr/bin/env python3
"""Hourly price message: Brent, USD, global gold ounce, 18k gold, Emami coin.

Reads the latest posts of three public Telegram channels (web preview, no login),
extracts the numbers exactly as written and posts ONE text message to the channel
through the official Bot API.

Environment:
  TELEGRAM_BOT_TOKEN, TARGET_CHAT   required (unless DRY_RUN)
  CHANNEL_FOOTER                    default: TARGET_CHAT if it is an @username
  DRY_RUN=true                      print only, send nothing
  FORCE=true                        ignore the Tehran hour window (08:00-01:59)
"""
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


def env(name, default=""):
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip()


def env_bool(name, default=False):
    return env(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
TARGET = env("TARGET_CHAT")
FOOTER = env("CHANNEL_FOOTER", TARGET if TARGET.startswith("@") else "")
if FOOTER and ord(FOOTER[0]) < 0x2000:
    FOOTER = "🤖 " + FOOTER                       # "🤖 @channel"
STATE_FILE = env("STATE_FILE", "state.json")      # written by bot.py (key "risk")
RISK_SALT = env("RISK_SALT", "khabre12")
RISK_MAX_AGE = 12 * 3600                           # an older estimate is not shown
DRY_RUN = env_bool("DRY_RUN", False)
FORCE = env_bool("FORCE", False)
MAX_PAGES = int(env("PRICE_MAX_PAGES", "6"))     # preview pages to look back (about 20 posts each)
HOURS = set(range(8, 24)) | {0, 1}                # 08:00 ... 01:59 Tehran time
UA = {"User-Agent": "Mozilla/5.0 (compatible; PriceBot/1.0)"}
RLM = "\u200f"

CH_BRENT = "oilpriced"
CH_OUNCE = "onsaanlain"
CH_RATES = "Mrgif"

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def log(*a):
    print(*a, flush=True)


# ------------------------------------------------------------------ scraping ---
def extract_text(node):
    for br in node.find_all("br"):
        br.replace_with("\n")
    txt = node.get_text().replace("\xa0", " ")
    txt = re.sub(r"[ \t]+\n", "\n", txt)
    return txt.strip()


def parse_texts(html):
    """[(id, text)] for every message on a preview page that has text, any order."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for w in soup.select("div.tgme_widget_message[data-post]"):
        try:
            mid = int(w["data-post"].rsplit("/", 1)[-1])
        except (ValueError, KeyError):
            continue
        nodes = [d for d in w.select("div.tgme_widget_message_text")
                 if not d.find_parent(class_="tgme_widget_message_reply")]
        if not nodes:
            continue
        forwarded = bool(w.select_one(".tgme_widget_message_forwarded_from"))
        out.append((mid, extract_text(nodes[0]), forwarded))
    return out


def fetch_html(channel, before=None):
    url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            return r.text
        except Exception as e:                      # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def posts_newest_first(channel, fetch=fetch_html):
    """Generator over (id, text, forwarded), newest first, paging back through the channel."""
    before = None
    seen = set()
    for _ in range(MAX_PAGES):
        posts = parse_texts(fetch(channel, before))
        posts = [p for p in posts if p[0] not in seen]
        if not posts:
            return
        posts.sort(key=lambda p: p[0], reverse=True)
        for p in posts:
            seen.add(p[0])
            yield p
        before = min(p[0] for p in posts)


# ------------------------------------------------------------------ parsers ---
NUM = r"\d[\d,]*(?:\.\d+)?"


def parse_brent(text):
    if "trading closed" in text.lower() or "⛔" in text:
        return None
    m = re.search(rf"Brent\s*[=:]\s*({NUM})", text, re.I)
    return m.group(1) if m else None


_OUNCE_RE = re.compile(rf"^[^\w]*({NUM})(?:\s+[^\w\s]{{0,3}}\s*[\d:/\-.\s]{{3,25}})?\s*$")


def parse_ounce(text):
    """Strict: the whole post must be '<emoji> number [timestamp]' (any emoji, e.g. 🔴/🔵),
    and the number must look like a gold ounce price. Closed-market / other posts are skipped,
    so the search goes back to the last real price (same rule as Brent)."""
    t = text.strip().translate(_DIGITS)
    m = _OUNCE_RE.match(t)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return m.group(1) if 1000 <= v <= 20000 else None


def _line_number(line):
    """First number after the label on a line, written exactly as in the source."""
    # remove digits that belong to the label itself ("18 عیار")
    line = re.sub(r"[\d۰-۹]+\s*عیار", "عیار", line)
    m = re.search(r"([\d۰-۹][\d۰-۹,٬،]*(?:[.٫][\d۰-۹]+)?)", line)
    return m.group(1) if m else None


def parse_rates(text):
    """Return dict with any of: usd, gold18, coin found in this post."""
    out = {}
    for raw in text.splitlines():
        line = raw.translate(_DIGITS)
        plain = re.sub(r"\s+", " ", line)
        num = _line_number(raw)                      # keep the source's own digits
        if not num:
            continue
        if "usd" not in out and "دلار آمریکا" in plain and "فروش" in plain:
            out["usd"] = num
        elif "gold18" not in out and "طلای 18 عیار" in plain:
            out["gold18"] = num
        elif "coin" not in out and "سکه امامی" in plain:
            out["coin"] = num
    return out


# ------------------------------------------------------------------ gathering ---
def find_first(channel, extract, fetch=fetch_html):
    """Latest post (scanning back) for which extract(text) returns a value."""
    seen = []
    for _mid, text, forwarded in posts_newest_first(channel, fetch):
        if len(seen) < 4:
            seen.append(("FWD " if forwarded else "") + text[:60].replace("\n", " "))
        if forwarded:
            continue
        v = extract(text)
        if v:
            return v
    log(f"  {channel}: nothing matched; newest posts seen: {seen if seen else 'NONE (page empty or blocked)'}")
    return None


def find_rates(fetch=fetch_html):
    """Each field independently from the newest post that contains it."""
    got = {}
    for _mid, text, forwarded in posts_newest_first(CH_RATES, fetch):
        if forwarded:
            continue
        for k, v in parse_rates(text).items():
            got.setdefault(k, v)
        if len(got) == 3:
            break
    return got


def gather(fetch=fetch_html):
    result = {}
    for key, fn in (("brent", lambda: find_first(CH_BRENT, parse_brent, fetch)),
                    ("ounce", lambda: find_first(CH_OUNCE, parse_ounce, fetch))):
        try:
            result[key] = fn()
        except Exception as e:                      # noqa: BLE001
            log(f"  {key}: failed ({type(e).__name__}: {e})")
            result[key] = None
    try:
        rates = find_rates(fetch)
    except Exception as e:                          # noqa: BLE001
        log(f"  rates: failed ({type(e).__name__}: {e})")
        rates = {}
    result.update(usd=rates.get("usd"), gold18=rates.get("gold18"), coin=rates.get("coin"))
    return result


_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def tehran_idx(ts):
    return int((ts + 12600) // 3600)               # hour counter in Tehran local time


def shown_risk(risk, now):
    """The number shown now. bot.py stores the Gemini estimate (base) every 3 hours; at the top of each
    following Tehran hour the shown number moves by exactly one point up or down, chosen at random
    (a hash of the hour, so no state is needed), and never strays more than 3 points from the base.
    After a direct attack the number is 100."""
    base = int(risk["base"])
    if risk.get("attack") or base >= 100:
        return 100
    start = tehran_idx(risk["ts"])
    steps = max(0, min(tehran_idx(now) - start, 12))
    off = 0
    for k in range(1, steps + 1):
        d = 1 if hashlib.sha256(f"{RISK_SALT}:{start + k}".encode()).digest()[0] & 1 else -1
        if abs(off + d) > 3:
            d = -d
        off += d
    return max(1, min(99, base + off))


def read_risk(now=None):
    """-> number to show, or None when there is no (fresh) estimate."""
    now = now or time.time()
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            risk = (json.load(f) or {}).get("risk") or {}
        if not risk.get("ts") or now - float(risk["ts"]) > RISK_MAX_AGE:
            return None
        return shown_risk(risk, now)
    except Exception as e:                          # noqa: BLE001
        log(f"  risk: not available ({type(e).__name__})")
        return None


def build_message(p, footer=FOOTER):
    lines = []
    if p.get("brent"):
        lines.append(f"{RLM}🛢برنت : {p['brent']}")
    if p.get("usd"):
        lines.append(f"{RLM}💵 دلار آمریکا : {p['usd']}")
    if p.get("ounce"):
        lines.append(f"{RLM}⚜انس جهانی : {p['ounce']}")
    if p.get("gold18"):
        lines.append(f"{RLM}🏅طلای 18 عیار : {p['gold18']}")
    if p.get("coin"):
        lines.append(f"{RLM}🌕سکه امامی : {p['coin']}")
    if not lines:
        return ""
    body = "\n\n".join(lines)
    tail = []
    if p.get("risk") is not None:
        tail.append(f"{RLM}📈برآورد جمنای از احتمال حمله در ۷ روز آینده: {str(p['risk']).translate(_FA_DIGITS)}٪")
    if footer:
        tail.append(footer)
    return body + ("\n\n" + "\n".join(tail) if tail else "")


# ------------------------------------------------------------------ telegram ---
def send(text):
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for _ in range(4):
        r = requests.post(api, data={"chat_id": TARGET, "text": text,
                                     "disable_web_page_preview": "true"}, timeout=60)
        try:
            j = r.json()
        except Exception:                           # noqa: BLE001
            j = {"ok": False, "description": r.text[:200]}
        if j.get("ok"):
            return
        if r.status_code == 429:
            time.sleep(int((j.get("parameters") or {}).get("retry_after", 5)) + 1)
            continue
        raise RuntimeError(f"telegram sendMessage: {j.get('description')}")
    raise RuntimeError("telegram sendMessage: too many rate limits")


def main():
    now = datetime.now(ZoneInfo("Asia/Tehran"))
    if not FORCE and now.hour not in HOURS:
        log(f"outside the 08:00-01:59 Tehran window ({now:%H:%M}), nothing to do")
        return 0
    if not DRY_RUN and (not BOT_TOKEN or not TARGET):
        log("TELEGRAM_BOT_TOKEN and TARGET_CHAT are required")
        return 1

    prices = gather()
    prices["risk"] = read_risk()
    log("found:", {k: (v if v else "-") for k, v in prices.items()})
    msg = build_message(prices)
    if not msg:
        log("no prices found at all, nothing sent")
        return 1
    log("--- message ---\n" + msg + "\n---------------")
    if DRY_RUN:
        log("dry run: not sent")
        return 0
    send(msg)
    log("sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
