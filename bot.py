#!/usr/bin/env python3
"""
Telegram news aggregator (public channels -> your channel), designed to run on GitHub Actions.

Flow per run:
  1. Read the public web preview (https://t.me/s/<channel>) of every source in sources.txt
  2. Collect posts newer than the last processed one per channel (state.json), oldest first
  3. Clean them (handles, t.me links, hashtags), drop near-identical repeats with a free text check
  4. ONE Gemini request per batch does: topic filter + duplicate check against recently published news
     + urgency + English->Persian translation (Persian posts are only cleaned, never translated).
     Falls back to the next Gemini model, then to keywords (+ Google Translate) when no AI answer exists
  5. Post relevant items with the official Bot API (text, or photos + caption); rejected items and
     duplicates (with a one-line reason) go to an optional private review channel

No personal Telegram account is used anywhere.
"""
import difflib
import html
import io
import json
import os
import re
import sys
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import web_sources


# ----------------------------------------------------------------- config ---
def env(name, default=""):
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip()


def env_bool(name, default=False):
    return env(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


LEGACY_CHANNEL = env("SOURCE_CHANNEL", "tabzlive").lstrip("@")   # owner of an old single-channel state.json
BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
TARGET = env("TARGET_CHAT")                       # @my_channel or -100123456789
GEMINI_KEY = env("GEMINI_API_KEY")                # optional
GEMINI_MODELS = [m.strip() for m in env("GEMINI_MODELS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite").split(",") if m.strip()]
DRY_RUN = env_bool("DRY_RUN", False)
VERBOSE_LOG = env_bool("VERBOSE_LOG", False)      # public repos: logs are public, so channel names are hidden unless on
INITIAL_BACKFILL = int(env("INITIAL_BACKFILL", "0"))   # posts to publish on the very first run of a channel
MAX_POSTS_PER_RUN = int(env("MAX_POSTS_PER_RUN", "80"))
MAX_PAGES = int(env("MAX_PAGES", "4"))
ADD_SOURCE_LINK = env_bool("ADD_SOURCE_LINK", False)   # off: no source line under posts
# Text added at the very end of every post, e.g. "@my_channel". Defaults to TARGET_CHAT when that is an @username.
FOOTER = env("CHANNEL_FOOTER", TARGET if TARGET.startswith("@") else "")
SKIP_VIDEO_POSTS = env_bool("SKIP_VIDEO_POSTS", False)  # true: every post with a video is skipped (caption too)
# Videos are sent by giving Telegram the video URL (Telegram downloads it itself, nothing passes through the runner).
# A video that has no URL, or that Telegram rejects (e.g. too big), is skipped together with its caption.
VIDEO_DOWNLOAD = env_bool("VIDEO_DOWNLOAD", True)       # when Telegram cannot fetch the video URL itself: download it here and upload it
VIDEO_MAX_MB = float(env("VIDEO_MAX_MB", "20"))          # bigger videos are skipped (Bot API upload limit is 50 MB)
REVIEW_CHAT = env("REVIEW_CHAT")                  # optional private channel/chat that receives rejected posts
PUBLISH_PHOTO_ONLY = env_bool("PUBLISH_PHOTO_ONLY", False)  # photos without any text cannot be judged -> skipped by default
SEND_PAUSE = float(env("SEND_PAUSE", "3"))

TOPICS_FILE = Path(env("TOPICS_FILE", "topics.txt"))
KEYWORDS_FILE = Path(env("KEYWORDS_FILE", "keywords.txt"))
SOURCES_FILE = Path(env("SOURCES_FILE", "sources.txt"))
WEB_SOURCES_FILE = Path(env("WEB_SOURCES_FILE", "sources_web.txt"))
WEB_MAX_PER_SOURCE = int(env("WEB_MAX_PER_SOURCE", "10"))   # new articles taken from one site per run
REWRITE_BATCH = 3         # articles per rewrite request
REWRITE_CHARS = 12000
REWRITE_BODY_MAX = 6000   # characters of one article sent to the rewrite step
WEB_SEEN_KEEP = 400
WEB_FILTER_CHARS = 700    # how much of an article the relevance filter sees (title + first lines)
WEB_PHOTOS = env_bool("WEB_PHOTOS", False)   # news-agency photos are licensed: off by default
STATE_FILE = Path(env("STATE_FILE", "state.json"))
GLOSSARY_FILE = Path(env("GLOSSARY_FILE", "glossary.txt"))

BATCH_SIZE = 12           # posts per Gemini request
BATCH_CHARS = 9000        # max characters per Gemini request
MAX_FAILS = 5             # after this many failed runs a post is skipped
RECENT_KEEP = 60          # published items remembered for duplicate detection
RECENT_HOURS = 12
RECENT_IN_PROMPT = 30     # how many of them the AI sees
NEAR_DUP = 0.88           # text similarity that counts as "the same post"

UA = {"User-Agent": "Mozilla/5.0 (compatible; ChannelAggregator/1.0)"}
PERSIAN_RE = re.compile(r"[؀-ۿ]")
LETTER_RE = re.compile(r"[A-Za-z]")


def log(*a):
    print(*a, flush=True)


# ------------------------------------------------------------ config files ---
def load_lines(env_name, path):
    """Lines from an environment variable (if set) or from a file; blanks and # comments removed."""
    raw = env(env_name)
    if raw:
        lines = raw.replace("\\n", "\n").splitlines()
    else:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            lines = []
    lines = [l.rstrip() for l in lines]
    return [l for l in lines if l.strip() and not l.lstrip().startswith("#")]


def load_sources():
    out = []
    for l in load_lines("SOURCE_CHANNELS", SOURCES_FILE):
        for part in re.split(r"[,\s]+", l.strip()):
            part = re.sub(r"^(?:https?://)?(?:t\.me/)?(?:s/)?@?", "", part).strip("/")
            if part and part not in out:
                out.append(part)
    return out or [LEGACY_CHANNEL]


def load_glossary():
    return [l.strip() for l in load_lines("GLOSSARY_TEXT", GLOSSARY_FILE) if "=" in l]


# ------------------------------------------------------------------ state ---
def load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
    except Exception:
        data = {}
    chans = data.get("channels")
    if not isinstance(chans, dict):          # old single-channel format
        chans = {}
        if data.get("last_id") is not None:
            chans[LEGACY_CHANNEL] = {"last_id": data["last_id"]}
    fails = {k: v for k, v in (data.get("fails") or {}).items() if "/" in k}
    web = data.get("web") if isinstance(data.get("web"), dict) else {}
    return {"channels": chans, "fails": fails, "web": web,
            "recent": list(data.get("recent") or []), "usage": dict(data.get("usage") or {}),
            "flip": int(data.get("flip") or 0), "quote_n": int(data.get("quote_n") or 0)}


def save_state(state):
    if DRY_RUN:
        return
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- scraping ---
def fetch_page(channel, before=None):
    url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def bg_url(tag):
    m = re.search(r"url\(['\"]?([^'\")]+)", tag.get("style", "") or "")
    return m.group(1) if m else None


def id_from_href(href):
    m = re.search(r"/(\d+)(?:\?.*)?$", href or "")
    return int(m.group(1)) if m else None


def extract_text(node):
    if node is None:
        return ""
    for br in node.find_all("br"):
        br.replace_with("\n")
    for bq in node.find_all("blockquote"):
        bq.insert_after("\n")
    txt = node.get_text()
    txt = txt.replace("\xa0", " ")
    txt = re.sub(r"[ \t]+\n", "\n", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


def parse_messages(soup, channel=""):
    """Return {id: post} for every message found on a preview page."""
    out = {}
    for w in soup.select("div.tgme_widget_message[data-post]"):
        try:
            main_id = int(w["data-post"].rsplit("/", 1)[-1])
        except (ValueError, KeyError):
            continue
        ids = {main_id}

        photos = []
        for a in w.select("a.tgme_widget_message_photo_wrap"):
            u = bg_url(a)
            if u and u not in photos:
                photos.append(u)
            i = id_from_href(a.get("href"))
            if i:
                ids.add(i)

        has_video = False
        for el in w.select("a.tgme_widget_message_video_player, div.tgme_widget_message_video_wrap, "
                           "i.tgme_widget_message_video_thumb, video, a.tgme_widget_message_roundvideo_player"):
            has_video = True
            i = id_from_href(el.get("href")) if el.name == "a" else None
            if i:
                ids.add(i)

        # main text (not the one inside the "reply to" preview)
        text_nodes = [d for d in w.select("div.tgme_widget_message_text")
                      if not d.find_parent(class_="tgme_widget_message_reply")]
        text = extract_text(text_nodes[0]) if text_nodes else ""

        ts = 0.0
        t = w.select_one("a.tgme_widget_message_date time[datetime]")
        if t:
            try:
                ts = datetime.fromisoformat(t["datetime"]).timestamp()
            except (ValueError, KeyError):
                ts = 0.0

        video_url = ""
        for vel in w.select("video[src], video source[src], a.tgme_widget_message_video_player[data-video], "
                            "a.tgme_widget_message_roundvideo_player[data-video]"):
            u = vel.get("src") or vel.get("data-video") or ""
            if u.startswith("//"):
                u = "https:" + u
            if u.startswith("http"):
                video_url = u
                break

        pid = max(ids)
        out[pid] = {"id": pid, "link_id": main_id, "text": text, "photos": photos[:10],
                    "has_video": has_video, "video_url": video_url, "channel": channel, "ts": ts}
    return out


def collect_posts(channel, last_id):
    posts = {}
    page = parse_messages(fetch_page(channel), channel)
    posts.update(page)
    pages = 1
    while last_id is not None and page and pages < MAX_PAGES and min(page) > last_id + 1:
        page = parse_messages(fetch_page(channel, before=min(page)), channel)
        if not page:
            break
        posts.update(page)
        pages += 1
    return posts


# --------------------------------------------------------------- cleaning ---
URL_TME_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:t|telegram)\.me/\S*", re.I)
HANDLE_RE = re.compile(r"(?<![\w.])@[A-Za-z][A-Za-z0-9_]{3,}")
HASHTAG_RE = re.compile(r"(?<!\w)#[\w‌]+")
LEAD_DECOR_RE = re.compile("^[ \t]*(?:[\U0001F53A\U0001F53B\U0001F539\U0001F538▪◾◽][️]?[ \t]*)+")


def is_persian(text):
    return len(PERSIAN_RE.findall(text)) > len(LETTER_RE.findall(text))


def clean_text(text, strip_decor=False):
    """Remove channel handles, t.me links and hashtags. A line that held nothing else disappears."""
    out = []
    for line in text.splitlines():
        new = URL_TME_RE.sub("", line)
        new = HANDLE_RE.sub("", new)
        new = HASHTAG_RE.sub("", new)
        if strip_decor:
            new = LEAD_DECOR_RE.sub("", new)
        if line.strip() and new != line and not re.sub(r"[\W_]+", "", new):
            continue                      # the line was only a credit / tag / link
        out.append(re.sub(r"[ \t]{2,}", " ", new).rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def norm(text):
    t = URL_TME_RE.sub(" ", text)
    t = re.sub(r"https?://\S+", " ", t)
    t = HANDLE_RE.sub(" ", t)
    t = HASHTAG_RE.sub(" ", t)
    t = t.lower().replace("ي", "ی").replace("ك", "ک").replace("‌", " ")
    t = re.sub(r"[^\w\s]|_", " ", t)
    return re.sub(r"\s+", " ", t).strip()[:400]


def near_dup(a, b):
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) < 25 or len(b) < 25:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= NEAR_DUP


# ----------------------------------------------------------- AI processing ---
SYSTEM_PROMPT = """You are the editor and translator of a Persian (Farsi) Telegram news channel about wars, geopolitics, Iran, the Middle East, US/Europe foreign policy, and markets.

Input is a JSON object: {"recent": [{"ref": "R1", "fa": "<Persian text already published>"}], "items": [{"id": <number>, "text": "<post text>"}]}
"recent" is what the channel published in the last hours. "items" are new posts, oldest first.
Return a JSON array with exactly one entry per item:
{"id": <same number>, "relevant": true|false, "urgent": true|false, "kind": "<kind>", "duplicate_of": "<ref or item id or empty>", "update": true|false, "new_fact": "<what is new, or empty>", "reason": "<one short Persian sentence>", "fa": "<Persian translation, or empty string>"}

STEP 1 - relevance. Decide with the TOPICS list below.
- relevant=true when the post is about (or quotes) anything in the list. When unsure, choose true: missing important news is worse than publishing a borderline item.
- For the two broad categories "AI news" and "important world news", publish everything that is genuine news.
- relevant=false for advertisements, channel promotion, giveaways, memes, entertainment and sports, and anything clearly outside the list.
- "reason": one short Persian sentence. For rejected items say why; for accepted items name the matching topic.

STEP 2 - duplicates (only for relevant items).
- If the item reports the SAME event or the SAME statement as a "recent" entry, set duplicate_of to its ref (for example "R3"). If it repeats an EARLIER item of this same list, set duplicate_of to that item's id as a string (for example "2"). Otherwise leave duplicate_of empty.
- update=true ONLY when the new item states a specific new fact that the earlier item does not contain: a changed number, a new development, a new official reaction. Then keep duplicate_of and write ONE short Persian sentence in "new_fact" that names exactly what is new.
- NOT an update (set update=false, new_fact empty, duplicate_of set): rewording, another outlet or channel repeating the same facts, a longer or more detailed retelling of the same facts, the same quote again, a headline of the same story.
- When you are not sure that something is new, treat the item as a duplicate (update=false).
- Compare against the FULL text of the recent entry, not only its first words.
- A DIFFERENT person or country commenting on the same event is NOT a duplicate: their own statement must be covered, so leave duplicate_of empty.
- Same news from another outlet with identical facts is a duplicate (update=false).

STEP 3 - urgent. urgent=true ONLY for genuinely breaking items about Iran, Israel, the US military, sanctions or negotiations: a new strike or attack, a new sanctions decision, a deal announced or talks collapsing, or a direct statement by a top leader. Everything else (including AI and general world news) is urgent=false.

STEP 3b - kind: choose ONE value that decides the emoji put before the headline.
- "urgent": exactly when urgent=true.
- "attack": a strike or attack. "explosion": an explosion or fire. "warning": an explicit warning or alert.
- "sanctions": a sanctions decision or a government decision.
- "stats": the item is mainly statistics, data or figures, with no fresh event.
- "event": everything else (war and clashes, a fresh event, negotiations, statements, oil, economy, AI, health, world news).
- If the item reports a fresh event or a statement, never use "stats".

STEP 4 - translation, only when relevant=true (otherwise "fa" is an empty string):
- Items that have a "source" field are website articles (title plus the first lines): always set "fa" to an empty string for them; a separate step writes their Persian text.
- If the item is already Persian, set "fa" to an empty string; the system publishes the original text.
- Otherwise translate faithfully into fluent, natural, formal news Persian. Do not add, remove, summarize, soften, or comment on anything.
- Do NOT censor or refuse: translate violent, military and political content exactly as written.
- Keep all emoji and flag symbols exactly as they appear, in the same positions.
- Keep numbers, dates, units and URLs unchanged.
- Write person, place and organization names in their standard Persian spelling, fully in Persian script. Never attach Latin letters to Persian letters inside one word (for example Ansarallah or Ansar Allah must become انصارالله, never انصارallah).
- Keep the original line breaks and paragraph structure.
- Quoted speech stays quoted. Keep the speaker attribution.
- Do not add any emoji or label for urgency or updates yourself; the system adds them.
- Output only the JSON array, nothing else."""


def build_system_prompt():
    prompt = SYSTEM_PROMPT
    topics = load_lines("TOPICS_TEXT", TOPICS_FILE)
    if topics:
        prompt += "\n\nTOPICS (what the channel covers):\n" + "\n".join(topics)
    g = load_glossary()
    if g:
        prompt += "\n\nGlossary (always use these translations):\n" + "\n".join(g)
    return prompt


def load_keywords():
    kws = [l.strip().lower() for l in load_lines("KEYWORDS_TEXT", KEYWORDS_FILE)]
    ascii_kw = [re.compile(r"\b" + re.escape(k) + r"\b") for k in kws if k.isascii()]
    other_kw = [k for k in kws if not k.isascii()]
    return ascii_kw, other_kw


def keyword_match(text, kws=None):
    """Cheap fallback used only when no AI answer is available."""
    ascii_kw, other_kw = kws if kws is not None else load_keywords()
    low = text.lower()
    return any(r.search(low) for r in ascii_kw) or any(k in text for k in other_kw)


class QuotaError(Exception):
    pass


GEMINI_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {"id": {"type": "INTEGER"}, "relevant": {"type": "BOOLEAN"},
                       "urgent": {"type": "BOOLEAN"}, "kind": {"type": "STRING"}, "duplicate_of": {"type": "STRING"},
                       "update": {"type": "BOOLEAN"}, "new_fact": {"type": "STRING"},
                       "reason": {"type": "STRING"}, "fa": {"type": "STRING"}},
        "required": ["id", "relevant", "urgent", "kind", "duplicate_of", "update", "new_fact", "reason", "fa"],
    },
}

REQ_COUNT = 0        # Gemini HTTP requests made in this run


def _gemini_json(model, system_prompt, payload, schema):
    """One structured Gemini call -> parsed JSON array. Raises QuotaError on a hard rate limit."""
    global REQ_COUNT
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }
    headers = {"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"}
    last_err = None
    for attempt in range(3):
        REQ_COUNT += 1
        try:
            r = requests.post(url, headers=headers, json=body, timeout=120)
        except requests.RequestException as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429:
            if attempt == 0:           # maybe just the per-minute limit: wait once and retry
                time.sleep(20)
                continue
            raise QuotaError(f"{model}: rate limited")
        if r.status_code >= 500:
            last_err = RuntimeError(f"{model}: HTTP {r.status_code}")
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"{model}: HTTP {r.status_code} {r.text[:200]}")
        data = r.json()
        cand = (data.get("candidates") or [None])[0]
        if not cand or not cand.get("content"):
            reason = (data.get("promptFeedback") or {}).get("blockReason") or (cand or {}).get("finishReason")
            raise RuntimeError(f"{model}: no content ({reason})")
        text = "".join(p.get("text", "") for p in cand["content"].get("parts", []))
        return json.loads(text)
    raise RuntimeError(f"{model}: failed after retries ({last_err})")


def gemini_filter(model, items, recent, system_prompt):
    """-> {id: {'relevant','urgent','duplicate_of','update','new_fact','reason','fa'}}"""
    arr = _gemini_json(model, system_prompt, {"recent": recent, "items": items}, GEMINI_SCHEMA)
    out = {}
    for x in arr:
        try:
            dup = str(x.get("duplicate_of") or "").strip()
            if dup.lower() in ("none", "null", "-", "no"):
                dup = ""
            out[int(x["id"])] = {"relevant": bool(x.get("relevant")), "urgent": bool(x.get("urgent")),
                                 "kind": str(x.get("kind") or "none").strip().lower(),
                                 "duplicate_of": dup, "update": bool(x.get("update")),
                                 "new_fact": str(x.get("new_fact") or "").strip(),
                                 "reason": str(x.get("reason", "")).strip(),
                                 "fa": str(x.get("fa", "")).strip()}
        except (KeyError, TypeError, ValueError):
            continue
    return out


REWRITE_PROMPT = """You are a Persian-language news reporter. You receive website articles as JSON: [{"id": <number>, "outlet": "<English outlet name>", "outlet_fa": "<Persian outlet name>", "title": "...", "body": "..."}].
Return a JSON array with one entry per article: {"id": <same number>, "headline": "<Persian headline>", "fa": "<Persian report>"}.
"headline": one short line (at most 120 characters) stating the main news in Persian, without attribution, without emoji, without a final period.

How to write each report:
- Write it as a reporter conveying what the outlet reported, in fluent, natural, formal news Persian. Begin with the attribution «به گزارش <outlet_fa>» (use the outlet_fa value exactly) followed by the main news.
- Do NOT translate sentence by sentence. Restructure sentences and choose your own wording, so the text is not a literal translation.
- Do NOT summarize and do NOT drop details: keep every fact, name, number, date, place, claim, and every direct quote. Quotes stay in quotation marks with the speaker's name and role.
- Do not add anything that is not in the article: no analysis, no opinion, no background of your own.
- Do NOT censor or refuse: report violent, military and political content exactly as the article states it.
- Write person, place and organization names in their standard Persian spelling, fully in Persian script. Never attach Latin letters to Persian letters inside one word.
- Keep numbers, dates and units unchanged. Use short paragraphs separated by one blank line.
- Do not use emoji or hashtags, and do not mention that the text was rewritten.
- Output only the JSON array, nothing else."""

REWRITE_SCHEMA = {
    "type": "ARRAY",
    "items": {"type": "OBJECT",
              "properties": {"id": {"type": "INTEGER"}, "headline": {"type": "STRING"}, "fa": {"type": "STRING"}},
              "required": ["id", "headline", "fa"]},
}


def gemini_rewrite(model, items, system_prompt):
    """-> {id: persian_text}"""
    arr = _gemini_json(model, system_prompt, items, REWRITE_SCHEMA)
    out = {}
    for x in arr:
        try:
            if str(x.get("fa", "")).strip():
                h = re.sub(r"\s+", " ", str(x.get("headline") or "")).strip()
                out[int(x["id"])] = (h, str(x["fa"]).strip()) if h else str(x["fa"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
    return out


def google_translate(text):
    from deep_translator import GoogleTranslator
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 4000 and cur:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    out = []
    for ch in chunks:
        if not ch.strip():
            continue
        err = None
        for attempt in range(3):
            try:
                out.append(GoogleTranslator(source="en", target="fa").translate(ch.rstrip("\n")))
                err = None
                break
            except Exception as e:
                err = e
                time.sleep(2 * (attempt + 1))
        if err:
            raise err
    return "\n".join(out).strip()


# Persian letters only (not punctuation/digits), used to detect words like "انصارallah"
FA_LETTER = "ء-غف-يپچژکگی"
MIXED_SCRIPT_RE = re.compile(f"[{FA_LETTER}][A-Za-z]|[A-Za-z][{FA_LETTER}]")


def looks_ok(src, fa):
    if not fa or not fa.strip():
        return False
    if MIXED_SCRIPT_RE.search(fa):
        return False
    if LETTER_RE.search(src) and not PERSIAN_RE.search(fa):
        return False
    if len(fa) < 0.25 * len(src):
        return False
    return True


def make_batches(posts, max_items=None, max_chars=None, field="text"):
    max_items = max_items or BATCH_SIZE
    max_chars = max_chars or BATCH_CHARS
    batches, cur, size = [], [], 0
    for p in posts:
        n = len(p[field])
        if cur and (len(cur) >= max_items or size + n > max_chars):
            batches.append(cur)
            cur, size = [], 0
        cur.append(p)
        size += n
    if cur:
        batches.append(cur)
    return batches


def process_posts(posts, recent=None):
    """posts: list of {'id','text'} (text already cleaned); recent: [{'ref','fa'}].
    -> {id: {'relevant','urgent','duplicate_of','update','reason','fa','ai'}}.
    Missing ids = nothing worked (they are retried on the next run)."""
    result = {}
    recent = recent or []
    system_prompt = build_system_prompt()
    kws = load_keywords()
    dead = set()      # models that hit quota/auth problems during this run
    fa_ids = {p["id"] for p in posts if is_persian(p["text"])}
    src_of = {p["id"]: p["source"] for p in posts if p.get("source")}     # website articles

    def try_models(need):
        for model in GEMINI_MODELS:
            if not need:
                return
            if model in dead:
                continue
            try:
                got = gemini_filter(model, [{"id": i, "text": t, **({"source": src_of[i]} if i in src_of else {})}
                                            for i, t in need.items()], recent, system_prompt)
            except QuotaError as e:
                log("  quota:", e)
                dead.add(model)
                continue
            except Exception as e:
                log("  gemini error:", str(e)[:200])
                if "HTTP 400" in str(e) or "HTTP 401" in str(e) or "HTTP 403" in str(e):
                    dead.add(model)
                continue
            for i, v in got.items():
                if i not in need:
                    continue
                if not v["relevant"]:
                    result[i] = {**v, "fa": "", "ai": True}
                    del need[i]
                elif i in fa_ids:                      # Persian: publish the cleaned original
                    result[i] = {**v, "fa": need[i], "ai": True}
                    del need[i]
                elif i in src_of:                      # website article: rewritten in a separate step
                    result[i] = {**v, "fa": "", "ai": True}
                    del need[i]
                elif looks_ok(need[i], v["fa"]):
                    result[i] = {**v, "ai": True}
                    del need[i]

    for batch in make_batches(posts):
        need = {p["id"]: p["text"] for p in batch}
        if GEMINI_KEY:
            try_models(need)
            # a single sensitive post can make a whole batch fail: retry the rest one by one
            if len(need) > 1:
                for i in list(need):
                    try_models({i: need[i]})
                    if i in result:
                        need.pop(i, None)
        # no AI answer: keyword match decides, Google Translate translates (Persian is published as is)
        for i, t in list(need.items()):
            if i in result:
                continue
            base = {"relevant": True, "urgent": False, "kind": "none", "duplicate_of": "", "update": False, "new_fact": "", "ai": False}
            if not keyword_match(t, kws):
                result[i] = {**base, "relevant": False, "fa": "",
                             "reason": "هوش مصنوعی در دسترس نبود و کلیدواژه‌ای پیدا نشد"}
                continue
            if i in fa_ids:
                result[i] = {**base, "fa": t, "reason": "بدون هوش مصنوعی (کلیدواژه)"}
                continue
            if i in src_of:
                result[i] = {**base, "fa": "", "reason": "بدون هوش مصنوعی (کلیدواژه)"}
                continue
            try:
                fa = google_translate(t)
                if looks_ok(t, fa):
                    result[i] = {**base, "fa": fa, "reason": "بدون هوش مصنوعی (کلیدواژه)"}
                else:
                    log(f"  item {i}: google translate output rejected")
            except Exception as e:
                log(f"  item {i}: google translate failed: {str(e)[:200]}")
    return result


def is_update(v):
    """An update must name the new fact; otherwise it is treated as a plain duplicate."""
    return bool(v.get("update") and v.get("new_fact", "").strip())


TITLED = set()      # ids of rewritten articles whose first block is a headline


def rewrite_ok(src, fa, outlet_fa):
    if not fa or not PERSIAN_RE.search(fa) or MIXED_SCRIPT_RE.search(fa):
        return False
    return len(fa) >= 0.45 * len(src)          # "no summary": a short result means details were dropped


def rewrite_articles(items):
    """items: [{'id','outlet','outlet_fa','title','body'}] -> {id: persian text with attribution}.
    Gemini rewrites in a reporter's voice. There is deliberately no literal-translation fallback."""
    result = {}
    prompt = REWRITE_PROMPT
    g = load_glossary()
    if g:
        prompt += "\n\nGlossary (always use these translations):\n" + "\n".join(g)
    dead = set()
    for it in items:
        it["body"] = it["body"][:REWRITE_BODY_MAX]
        it["_len"] = len(it["title"]) + len(it["body"])
    by_id = {it["id"]: it for it in items}

    def src_len(i):
        return by_id[i]["_len"]

    def attribute(i, fa):
        o = by_id[i]["outlet_fa"]
        return fa if o in fa[:200] else f"به گزارش {o}:\n{fa}"

    def try_models(batch):
        need = {it["id"] for it in batch}
        for model in GEMINI_MODELS:
            if not need or not GEMINI_KEY:
                return
            if model in dead:
                continue
            payload = [{k: by_id[i][k] for k in ("id", "outlet", "outlet_fa", "title", "body")} for i in sorted(need)]
            try:
                got = gemini_rewrite(model, payload, prompt)
            except QuotaError as e:
                log("  quota:", e)
                dead.add(model)
                continue
            except Exception as e:
                log("  gemini rewrite error:", str(e)[:200])
                if "HTTP 400" in str(e) or "HTTP 401" in str(e) or "HTTP 403" in str(e):
                    dead.add(model)
                continue
            for i, fa in got.items():
                head = ""
                if isinstance(fa, tuple):
                    head, fa = fa
                if i in need and rewrite_ok("x" * src_len(i), fa, by_id[i]["outlet_fa"]):
                    body = attribute(i, fa)
                    if head and PERSIAN_RE.search(head) and not MIXED_SCRIPT_RE.search(head):
                        result[i] = head + "\n\n" + body
                        TITLED.add(i)
                    else:
                        result[i] = body
                    need.discard(i)

    for batch in make_batches([{**it, "text": it["title"] + it["body"]} for it in items], REWRITE_BATCH, REWRITE_CHARS):
        try_models(batch)
        for it in batch:
            i = it["id"]
            if i in result:
                continue
            if len(batch) > 1:
                try_models([by_id[i]])
        # no literal machine-translation fallback for website articles: missing ids are retried next run
    return result


def web_post(src, it, ts):
    """A website article in the same shape as a Telegram post."""
    head = (it["title"] + "\n\n" + it["body"][:WEB_FILTER_CHARS]).strip()
    return {"id": zlib.crc32(it["key"].encode("utf-8")), "link_id": 0, "key": it["key"], "link": it.get("link", ""),
            "text": head, "title": it["title"], "body": it["body"], "channel": f"web:{src['name']}",
            "ts": it.get("ts") or ts, "source": src["outlet_fa"], "outlet": src["name"],
            "photos": [it["image"]] if (WEB_PHOTOS and it.get("image")) else [], "has_video": False}


# ---------------------------------------------------------------- telegram ---
API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def tg(method, data=None, files=None):
    for attempt in range(4):
        r = requests.post(f"{API}/{method}", data=data, files=files, timeout=120)
        try:
            j = r.json()
        except Exception:
            j = {"ok": False, "description": r.text[:200]}
        if j.get("ok"):
            return j
        if r.status_code == 429:
            wait = int((j.get("parameters") or {}).get("retry_after", 5)) + 1
            log(f"  telegram rate limit, waiting {wait}s")
            time.sleep(wait)
            continue
        raise RuntimeError(f"telegram {method}: {j.get('description')}")
    raise RuntimeError(f"telegram {method}: too many rate limits")


def tg_html(method, data, files=None):
    """Send with parse_mode=HTML. If Telegram cannot parse the markup, resend the same post as plain text."""
    data = dict(data, parse_mode="HTML")
    if method == "sendMediaGroup":
        data["media"] = json.dumps([dict(m, parse_mode="HTML") for m in json.loads(data["media"])])
    try:
        return tg(method, data=data, files=files)
    except RuntimeError as e:
        if "parse entities" not in str(e).lower():
            raise
        log("  HTML markup rejected, sending as plain text")
        data.pop("parse_mode", None)
        for k in ("text", "caption"):
            if k in data:
                data[k] = strip_tags(data[k])
        if method == "sendMediaGroup":
            data["media"] = json.dumps([{k: (strip_tags(v) if k == "caption" else v) for k, v in m.items() if k != "parse_mode"}
                                        for m in json.loads(data["media"])])
        return tg(method, data=data, files=files)


def split_message(text, limit=4096):
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


def download(url, retry=False):
    headers = dict(UA)
    if retry:      # second attempt looks like a normal browser coming from t.me
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                                 "Chrome/124.0 Safari/537.36", "Referer": "https://t.me/"}
    try:
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        buf = r.content
        if len(buf) > 10 * 1024 * 1024 or not buf:
            return None
        return buf
    except Exception as e:
        log("  photo download failed:", str(e)[:100])
        return None


def _text_only(text):
    for part in split_message(text):
        tg_html("sendMessage", {"chat_id": TARGET, "text": part, "disable_web_page_preview": "true"})


# Emoji before the headline, by news kind. Everything not listed here (events, war, negotiations, statements,
# oil, economy, ...) gets the alternating 🔴 / ⭕️ pair.
KIND_EMOJI = {"urgent": "🚨", "attack": "🚨", "sanctions": "🚨", "warning": "🚨⚠️", "explosion": "🚨🔥", "stats": "📊"}
EVENT_EMOJIS = ("🔴", "⭕️")
QUOTE_EVERY = int(env("QUOTE_EVERY", "5"))   # every Nth short text-only post gets its body in a quote block (0 = never)
LONG_PARAGRAPHS = 3          # a post is "long" with at least this many paragraphs (headline not counted)
MARK_MIN_CHARS = 130         # a paragraph gets the marker from about 3 phone lines (~130 characters)
_EMOJI_START = re.compile("^[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2190-\u21FF\u203C\u2049\ufe0f\u200d]")


def pick_emoji(v, flip=0):
    """-> (emoji, used_flip). Urgent always wins; unlisted kinds alternate 🔴 / ⭕️ (flip counts them)."""
    if v.get("urgent"):
        return KIND_EMOJI["urgent"], False
    e = KIND_EMOJI.get(v.get("kind", "none"))
    if e:
        return e, False
    return EVENT_EMOJIS[flip % 2], True


def _split_blocks(body, titled):
    blocks = [b.strip() for b in re.split(r"\n\s*\n", body.strip()) if b.strip()]
    if titled is None:
        if len(blocks) == 1 and "\n" in blocks[0]:
            first, rest = blocks[0].split("\n", 1)
            blocks = [first.strip(), rest.strip()]
        titled = len(blocks) >= 2
    title = blocks.pop(0) if (titled and blocks) else None
    return title, blocks


def is_long_post(body, titled=None):
    return len(_split_blocks(body, titled)[1]) >= LONG_PARAGRAPHS


def format_post(body, emoji="", titled=None, quote_short=False):
    """HTML text for Telegram.
    Short posts (fewer than LONG_PARAGRAPHS paragraphs): plain text, only the emoji at the start.
    Long posts: bold headline, and every paragraph of at least MARK_MIN_CHARS characters inside a collapsible quote.
    titled: True = first block is a headline, False = none, None = decide from the structure.
    quote_short: a short post gets its body (everything after the headline) in one plain quote block.
    The text itself is never changed, only wrapped (and HTML-escaped)."""
    title, blocks = _split_blocks(body, titled)
    esc = lambda t: html.escape(t, quote=False)
    lead = lambda t: t if (not emoji or _EMOJI_START.match(t)) else f"{emoji} {t}"
    long_post = len(blocks) >= LONG_PARAGRAPHS
    if quote_short and not long_post and blocks:
        out = []
        if title is not None:
            out.append(lead(esc(title)))
            inner = "\n\n".join(esc(b) for b in blocks)
        else:
            inner = "\n\n".join([lead(esc(blocks[0]))] + [esc(b) for b in blocks[1:]])
        out.append(f"<blockquote>{inner}</blockquote>")
        return "\n\n".join(out)
    out = []
    if title is not None:
        t = lead(esc(title)) if not _EMOJI_START.match(title) else esc(title)
        out.append(f"<b>{t}</b>" if long_post else t)
    for i, b in enumerate(blocks):
        t = esc(b)
        if title is None and i == 0:
            t = lead(t) if not _EMOJI_START.match(b) else t
        elif long_post and len(b) >= MARK_MIN_CHARS and not _EMOJI_START.match(b):
            t = f"<blockquote expandable>{t}</blockquote>"
        out.append(t)
    return "\n\n".join(out)


def strip_tags(t):
    return html.unescape(re.sub(r"<[^>]+>", "", t))


def download_video(url):
    """Stream the video into memory; None when it is too big or cannot be read."""
    limit = int(VIDEO_MAX_MB * 1024 * 1024)
    try:
        with requests.get(url, headers={**UA, "Referer": "https://t.me/"}, timeout=60, stream=True) as r:
            r.raise_for_status()
            size = int(r.headers.get("Content-Length") or 0)
            if size > limit:
                log(f"  video too big: {size // 1024 // 1024} MB (limit {VIDEO_MAX_MB:g} MB)")
                return None
            buf = bytearray()
            for chunk in r.iter_content(256 * 1024):
                buf.extend(chunk)
                if len(buf) > limit:
                    log(f"  video too big (limit {VIDEO_MAX_MB:g} MB)")
                    return None
            return bytes(buf) or None
    except Exception as e:
        log("  video download failed:", str(e)[:100])
        return None


class VideoRejected(Exception):
    """Telegram would not take the video; the post (and its caption) is dropped, never sent as text."""


def send_post(post, fa_text, allow_text_only=False):
    """Text, or photos + caption. A post that has photos is never silently sent as text only:
    if the download fails, Telegram is given the photo URL; if that fails too, the error is raised
    (the post is retried next run) unless allow_text_only is set (last attempt: the news matters more)."""
    parts = []
    if fa_text:
        parts.append(fa_text)
    if ADD_SOURCE_LINK:
        parts.append(f"📎 منبع: https://t.me/{post.get('channel', LEGACY_CHANNEL)}/{post['link_id']}")
    if FOOTER:
        parts.append(FOOTER)
    text = "\n\n".join(parts)

    if post.get("has_video"):
        url = post.get("video_url", "")
        if not url:
            raise VideoRejected("no video URL in the preview")
        if DRY_RUN:
            log(f"  [dry-run] post {post['id']}: VIDEO {url[:80]}\n" + "\n".join("    " + l for l in text.splitlines()))
            return
        caption_ok = len(text) <= 1024
        data = {"chat_id": TARGET, "video": url, "supports_streaming": "true"}
        if caption_ok:
            data["caption"] = text
        try:
            tg_html("sendVideo", data)
        except Exception as e:
            err = str(e)[:150]
            if not VIDEO_DOWNLOAD:
                raise VideoRejected(err)
            log(f"  Telegram could not fetch the video URL ({err}); downloading it here")
            buf = download_video(url)
            if not buf:
                raise VideoRejected(f"{err}; local download failed or too big")
            data.pop("video")
            try:
                tg_html("sendVideo", data, {"video": ("video.mp4", io.BytesIO(buf))})
            except Exception as e2:
                raise VideoRejected(str(e2)[:150])
        if not caption_ok:
            try:
                _text_only(text)
            except Exception as e:
                log("  video sent, long caption part failed:", str(e)[:100])
        return

    photos = []                # bytes when downloaded, else the URL itself (Telegram then fetches it)
    for u in post["photos"]:
        b = download(u) or download(u, retry=True)
        photos.append(b if b else u)

    if DRY_RUN:
        got = sum(1 for p in photos if isinstance(p, bytes))
        log(f"  [dry-run] post {post['id']}: {len(photos)} photo(s), {got} downloaded\n" + "\n".join("    " + l for l in text.splitlines()))
        return

    if not photos:
        _text_only(text)
        return

    try:
        caption_ok = len(text) <= 1024
        if len(photos) == 1:
            data, files = {"chat_id": TARGET}, None
            if caption_ok:
                data["caption"] = text
            if isinstance(photos[0], bytes):
                files = {"photo": ("photo.jpg", io.BytesIO(photos[0]))}
            else:
                data["photo"] = photos[0]
            tg_html("sendPhoto", data, files)
        else:
            media, files = [], {}
            for n, p in enumerate(photos):
                item = {"type": "photo", "media": f"attach://p{n}" if isinstance(p, bytes) else p}
                if n == 0 and caption_ok:
                    item["caption"] = text
                media.append(item)
                if isinstance(p, bytes):
                    files[f"p{n}"] = (f"p{n}.jpg", io.BytesIO(p))
            tg_html("sendMediaGroup", {"chat_id": TARGET, "media": json.dumps(media)}, files or None)
    except Exception as e:
        if not allow_text_only:
            raise
        log(f"  photos could not be sent ({str(e)[:100]}); sending text only on the last attempt")
        _text_only(text)
        return
    if not caption_ok:
        _text_only(text)


def send_review(post, verdict):
    """Rejected posts go to a private review channel so you can check the filter. Never blocks the run."""
    if not REVIEW_CHAT:
        return
    note = "" if verdict.get("ai", True) else "\n(بدون هوش مصنوعی)"
    src = post.get("link") or f"https://t.me/{post.get('channel', LEGACY_CHANNEL)}/{post['link_id']}"
    text = f"⛔ {verdict.get('reason') or 'رد شد'}{note}\n\n{post['text'][:1500]}\n\n{src}"
    if DRY_RUN:
        log(f"  [dry-run] REJECTED post {post['id']}: {verdict.get('reason')}\n    " + post["text"][:120].replace("\n", " "))
        return
    try:
        tg("sendMessage", data={"chat_id": REVIEW_CHAT, "text": text, "disable_web_page_preview": "true"})
        time.sleep(1.2)
    except Exception as e:
        log(f"  review channel send failed: {str(e)[:150]}")


# ------------------------------------------------------------ recent items ---
def prune_recent(state, now=None):
    now = now or time.time()
    state["recent"] = [r for r in state["recent"]
                       if isinstance(r, dict) and r.get("t", 0) >= now - RECENT_HOURS * 3600][-RECENT_KEEP:]


def add_recent(state, post, cleaned, fa_text):
    snippet = re.sub(r"\s+", " ", strip_tags(fa_text)).strip()[:400]
    state["recent"].append({"t": post.get("ts") or time.time(), "n": norm(cleaned), "fa": snippet})
    state["recent"] = state["recent"][-RECENT_KEEP:]


# -------------------------------------------------------------------- main ---
def main():
    if not DRY_RUN and (not BOT_TOKEN or not TARGET):
        log("ERROR: TELEGRAM_BOT_TOKEN and TARGET_CHAT must be set (or use DRY_RUN=true).")
        return 1
    if GEMINI_KEY:
        log("Gemini key found; models:", ", ".join(GEMINI_MODELS))
    else:
        log("No Gemini key: keywords + Google Translate only.")

    sources = load_sources()
    state = load_state()
    prune_recent(state)

    web_srcs = web_sources.parse_sources(load_lines("WEB_SOURCES", WEB_SOURCES_FILE))
    labels = {c: f"#{i + 1}" for i, c in enumerate(sources)}
    labels.update({f"web:{w['name']}": f"web{i + 1}" for i, w in enumerate(web_srcs)})

    def tag(ch):
        return ch if (DRY_RUN or VERBOSE_LOG) else labels.get(ch, "?")

    # 1. collect new posts from every source
    all_new = []
    for ch in sources:
        cs = state["channels"].setdefault(ch, {"last_id": None})
        try:
            posts = collect_posts(ch, cs.get("last_id"))
        except Exception as e:
            log(f"[{tag(ch)}] cannot read channel: {str(e)[:150]}")
            continue
        if not posts:
            log(f"[{tag(ch)}] no posts found on the preview page (private channel, or preview disabled?)")
            continue
        ids = sorted(posts)
        last = cs.get("last_id")
        if last is None:
            if INITIAL_BACKFILL > 0:
                last = ids[-INITIAL_BACKFILL - 1] if len(ids) > INITIAL_BACKFILL else ids[0] - 1
            else:
                cs["last_id"] = ids[-1]
                log(f"[{tag(ch)}] first run: starting from post {ids[-1]}; nothing published from the past.")
                continue
        new = [posts[i] for i in ids if i > last]
        log(f"[{tag(ch)}] {len(new)} new post(s)")
        all_new.extend(new)

    # 1b. news websites and official pages (seen-list instead of last_id)
    for src in web_srcs:
        ch = f"web:{src['name']}"
        ws = state["web"].setdefault(src["name"], {"seen": None})
        try:
            items = web_sources.list_items(src)
        except Exception as e:
            log(f"[{tag(ch)}] cannot read source: {str(e)[:150]}")
            continue
        if not items:
            log(f"[{tag(ch)}] source returned no items")
            continue
        if ws.get("seen") is None:
            ws["seen"] = [it["key"] for it in items][-WEB_SEEN_KEEP:]
            log(f"[{tag(ch)}] first run: {len(items)} existing item(s) marked as seen; nothing published from the past.")
            continue
        seen = set(ws["seen"])
        new = [it for it in items if it["key"] not in seen]
        if any(it.get("ts") for it in new):
            new.sort(key=lambda it: it.get("ts") or 0)
        else:
            new.reverse()                                  # listing pages show the newest first
        if len(new) > WEB_MAX_PER_SOURCE:                  # a big backlog: keep the newest, mark the rest as seen
            ws["seen"] = (list(ws["seen"]) + [it["key"] for it in new[:-WEB_MAX_PER_SOURCE]])[-WEB_SEEN_KEEP:]
            new = new[-WEB_MAX_PER_SOURCE:]
        log(f"[{tag(ch)}] {len(new)} new article(s)")
        now_ts = time.time()
        for n, it in enumerate(new):
            try:
                full = web_sources.materialize(src, it)
            except Exception as e:
                log(f"[{tag(ch)}] article fetch failed: {str(e)[:120]}")
                continue
            all_new.append(web_post(src, full, now_ts + n * 0.001))
    save_state(state)

    if not all_new:
        log("No new posts.")
        return 0
    all_new.sort(key=lambda p: (p.get("ts", 0), p["channel"], p["id"]))
    all_new = all_new[:MAX_POSTS_PER_RUN]

    # 2. clean, free duplicate check, collect what the AI must judge
    recent_norms = [r.get("n", "") for r in state["recent"]]
    run_norms, entries, to_check = [], [], []
    for p in all_new:
        e = {"post": p, "kind": "check", "cleaned": ""}
        entries.append(e)
        if p["has_video"] and (SKIP_VIDEO_POSTS or not p.get("video_url")):
            e["kind"] = "skip"
            if not SKIP_VIDEO_POSTS:
                log(f"[{tag(p['channel'])}] post {p['id']}: video without a usable URL, skipped")
        elif not p["text"]:
            e["kind"] = "photo" if (p["photos"] and PUBLISH_PHOTO_ONLY) else "skip"
        else:
            cleaned = clean_text(p["text"], strip_decor=is_persian(p["text"]))
            e["cleaned"] = cleaned
            n = norm(cleaned)
            if not cleaned.strip():
                e["kind"] = "skip"
            elif any(near_dup(n, r) for r in recent_norms + run_norms):
                e["kind"] = "dup_text"
            else:
                run_norms.append(n)
                e["idx"] = len(to_check) + 1
                to_check.append({"id": e["idx"], "text": cleaned, **({"source": p["source"]} if p.get("source") else {})})

    recent_prompt = [{"ref": f"R{k}", "fa": r.get("fa", "")}
                     for k, r in enumerate(state["recent"][-RECENT_IN_PROMPT:], 1)]
    recent_refs = {x["ref"] for x in recent_prompt}
    log(f"{len(all_new)} post(s) to handle, {len(to_check)} go to the AI filter")
    verdicts = process_posts(to_check, recent_prompt) if to_check else {}

    # 2b. website articles that will be published: rewritten in a reporter's voice (separate step)
    def rewrite_item(e):
        p = e["post"]
        return {"id": e["idx"], "outlet": p["outlet"], "outlet_fa": p["source"],
                "title": p["title"], "body": clean_text(p["body"])}

    rewrites, attempted = {}, set()
    rw_entries = [e for e in entries
                  if e["kind"] == "check" and e["post"].get("source") and verdicts.get(e["idx"])
                  and verdicts[e["idx"]]["relevant"]
                  and (not verdicts[e["idx"]]["duplicate_of"] or is_update(verdicts[e["idx"]]))]
    if rw_entries and GEMINI_KEY:
        rewrites = rewrite_articles([rewrite_item(e) for e in rw_entries])
        attempted = {e["idx"] for e in rw_entries}

    def get_rewrite(e):
        if e["idx"] not in attempted and GEMINI_KEY:
            attempted.add(e["idx"])
            rewrites.update(rewrite_articles([rewrite_item(e)]))
        return rewrites.get(e["idx"], "")

    if GEMINI_KEY:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if state["usage"].get("date") != today:
            state["usage"] = {"date": today, "requests": 0}
        state["usage"]["requests"] = state["usage"].get("requests", 0) + REQ_COUNT
        log(f"Gemini requests: {REQ_COUNT} this run, {state['usage']['requests']} today (UTC)")

    # 3. decide, publish, remember
    sent = rejected = 0
    published_idx = set()
    blocked = set()
    for e in entries:
        p, ch = e["post"], e["post"]["channel"]
        if ch in blocked:
            continue
        key = f"{ch}/{p['id']}"

        def advance():
            if ch.startswith("web:"):
                ws = state["web"][ch[4:]]
                ws["seen"] = (list(ws.get("seen") or []) + [p["key"]])[-WEB_SEEN_KEEP:]
            else:
                state["channels"][ch]["last_id"] = p["id"]
            state["fails"].pop(key, None)
            save_state(state)

        def fail(what):
            n = state["fails"].get(key, 0) + 1
            state["fails"][key] = n
            if n >= MAX_FAILS:
                log(f"[{tag(ch)}] post {p['id']}: {what} failed {n} times, skipping it")
                advance()
            else:
                log(f"[{tag(ch)}] post {p['id']}: {what} failed ({n}/{MAX_FAILS}), will retry next run")
                blocked.add(ch)
                save_state(state)

        if e["kind"] == "skip":
            advance()
            continue
        if e["kind"] == "dup_text":
            send_review(p, {"reason": "تکراری (متن مشابه)", "ai": True})
            rejected += 1
            advance()
            continue

        used_flip = False
        quote_eligible = False
        if e["kind"] == "photo":
            fa = ""
        else:
            v = verdicts.get(e["idx"])
            if v is None:
                fail("processing")
                continue
            if not v["relevant"]:
                send_review(p, v)
                rejected += 1
                advance()
                continue
            dup = v.get("duplicate_of", "")
            valid_dup = bool(dup) and (dup in recent_refs or (dup.isdigit() and int(dup) in published_idx))
            if valid_dup and not is_update(v):
                send_review(p, {**v, "reason": f"تکراری: {v.get('reason', '')}"})
                rejected += 1
                advance()
                continue
            prefix = "🔄 به‌روزرسانی\n" if valid_dup and is_update(v) else ""
            if p.get("source"):
                body_fa = get_rewrite(e)
                if not body_fa:
                    fail("rewriting")
                    continue
                titled = e["idx"] in TITLED
            else:
                body_fa = v["fa"]
                titled = None
            emoji, used_flip = pick_emoji(v, state["flip"])
            quote_eligible = (QUOTE_EVERY > 0 and not p["photos"] and not p["has_video"]
                              and not is_long_post(body_fa, titled))
            quote = quote_eligible and state["quote_n"] % QUOTE_EVERY == 0
            fa = prefix + format_post(body_fa, emoji, titled, quote_short=quote)

        try:
            send_post(p, fa, allow_text_only=state["fails"].get(key, 0) >= MAX_FAILS - 1)
        except VideoRejected as ex:
            log(f"[{tag(ch)}] post {p['id']}: video rejected ({ex}), post skipped, caption not sent")
            send_review(p, {"reason": f"ویدیو ارسال نشد ({str(ex)[:80]}) و کپشن منتشر نشد", "ai": True})
            advance()
            continue
        except Exception as ex:
            log(f"[{tag(ch)}] post {p['id']}: send error: {str(ex)[:200]}")
            fail("sending")
            continue
        sent += 1
        if e["kind"] == "check" and quote_eligible:
            state["quote_n"] += 1
        if e["kind"] == "check" and used_flip:
            state["flip"] += 1
        if e["kind"] == "check":
            published_idx.add(e["idx"])
            add_recent(state, p, e["cleaned"], fa)
        advance()
        if not DRY_RUN:
            time.sleep(SEND_PAUSE)

    save_state(state)
    log(f"Done. {'Previewed' if DRY_RUN else 'Sent'} {sent} post(s), {rejected} rejected by the filter.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
