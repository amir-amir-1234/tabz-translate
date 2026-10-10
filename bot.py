#!/usr/bin/env python3
"""
Telegram channel translator (EN -> FA), designed to run on GitHub Actions.

Flow per run:
  1. Read the public web preview of the source channel (https://t.me/s/<channel>)
  2. Find posts newer than the last processed one (state.json)
  3. AI topic filter + translation in ONE Gemini request (free tier), falling back to the next model,
     then to keyword matching + Google Translate when no AI answer is available
  4. Post relevant items to your channel with the official Bot API (text, or photos + caption);
     rejected items (with a one-line reason) go to an optional private review channel

No personal Telegram account is used anywhere.
"""
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------- config ---
def env(name, default=""):
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip()


def env_bool(name, default=False):
    return env(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


SOURCE = env("SOURCE_CHANNEL", "tabzlive").lstrip("@")
BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
TARGET = env("TARGET_CHAT")                       # @my_channel or -100123456789
GEMINI_KEY = env("GEMINI_API_KEY")                # optional
GEMINI_MODELS = [m.strip() for m in env("GEMINI_MODELS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite").split(",") if m.strip()]
DRY_RUN = env_bool("DRY_RUN", False)
INITIAL_BACKFILL = int(env("INITIAL_BACKFILL", "0"))   # posts to publish on the very first run
MAX_POSTS_PER_RUN = int(env("MAX_POSTS_PER_RUN", "40"))
MAX_PAGES = int(env("MAX_PAGES", "4"))
ADD_SOURCE_LINK = env_bool("ADD_SOURCE_LINK", False)   # off: no source line under posts
# Text added at the very end of every post, e.g. "@my_channel". Defaults to TARGET_CHAT when that is an @username.
FOOTER = env("CHANNEL_FOOTER", TARGET if TARGET.startswith("@") else "")
SKIP_VIDEO_POSTS = env_bool("SKIP_VIDEO_POSTS", False)  # True: skip posts that contain a video entirely
REVIEW_CHAT = env("REVIEW_CHAT")                  # optional private channel/chat that receives rejected posts
PUBLISH_PHOTO_ONLY = env_bool("PUBLISH_PHOTO_ONLY", False)  # photos without any text cannot be judged -> skipped by default
TOPICS_FILE = Path(env("TOPICS_FILE", "topics.txt"))
KEYWORDS_FILE = Path(env("KEYWORDS_FILE", "keywords.txt"))
SEND_PAUSE = float(env("SEND_PAUSE", "3"))

BATCH_SIZE = 8            # posts per Gemini request
BATCH_CHARS = 6000        # max characters per Gemini request
MAX_FAILS = 5             # after this many failed runs a post is skipped
STATE_FILE = Path(env("STATE_FILE", "state.json"))
GLOSSARY_FILE = Path(env("GLOSSARY_FILE", "glossary.txt"))

UA = {"User-Agent": "Mozilla/5.0 (compatible; ChannelTranslator/1.0)"}
PERSIAN_RE = re.compile(r"[؀-ۿ]")
LETTER_RE = re.compile(r"[A-Za-z]")


def log(*a):
    print(*a, flush=True)


# ------------------------------------------------------------------ state ---
def load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
    except Exception:
        data = {}
    data.setdefault("last_id", None)
    data.setdefault("fails", {})
    return data


def save_state(state):
    if DRY_RUN:
        return
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- scraping ---
def fetch_page(before=None):
    url = f"https://t.me/s/{SOURCE}" + (f"?before={before}" if before else "")
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


def parse_messages(soup):
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

        pid = max(ids)
        out[pid] = {"id": pid, "link_id": main_id, "text": text,
                    "photos": photos[:10], "has_video": has_video}
    return out


def collect_posts(last_id):
    posts = {}
    page = parse_messages(fetch_page())
    posts.update(page)
    pages = 1
    while last_id is not None and page and pages < MAX_PAGES and min(page) > last_id + 1:
        page = parse_messages(fetch_page(before=min(page)))
        if not page:
            break
        posts.update(page)
        pages += 1
    return posts


# ------------------------------------------------------------ translation ---
def load_glossary():
    try:
        lines = [l.strip() for l in GLOSSARY_FILE.read_text(encoding="utf-8").splitlines()]
        return [l for l in lines if l and not l.startswith("#") and "=" in l]
    except Exception:
        return []


SYSTEM_PROMPT = """You are the editor and translator of a Persian (Farsi) Telegram news channel about wars, geopolitics, Iran, the Middle East, US/Europe foreign policy, and markets.

You receive a JSON array: [{"id": <number>, "text": "<post text>"}].
Return a JSON array with exactly one entry per input item:
{"id": <same number>, "relevant": true|false, "urgent": true|false, "reason": "<one short Persian sentence>", "fa": "<Persian translation, or empty string>"}

STEP 1 - relevance. Decide with the TOPICS list below.
- relevant=true when the post is about (or quotes) anything in the list. When unsure, choose true: missing important news is worse than publishing a borderline item.
- For the two broad categories "AI news" and "important world news", publish everything that is genuine news.
- relevant=false for advertisements, channel promotion, giveaways, memes, entertainment and sports, and anything clearly outside the list.
- "reason": one short Persian sentence. For rejected items say why; for accepted items name the matching topic.

STEP 2 - urgent. urgent=true ONLY for genuinely breaking items about Iran, Israel, the US military, sanctions or negotiations: a new strike or attack, a new sanctions decision, a deal announced or talks collapsing, or a direct statement by a top leader. Everything else (including AI and general world news) is urgent=false.

STEP 3 - translation, only when relevant=true (otherwise "fa" is an empty string):
- Translate faithfully into fluent, natural, formal news Persian. Do not add, remove, summarize, soften, or comment on anything.
- Do NOT censor or refuse: translate violent, military and political content exactly as written.
- Keep all emoji and flag symbols exactly as they appear, in the same positions.
- Keep numbers, dates, units, URLs, @usernames and hashtags unchanged.
- Write person, place and organization names in their standard Persian spelling, fully in Persian script. Never attach Latin letters to Persian letters inside one word (for example Ansarallah or Ansar Allah must become انصارالله, never انصارallah).
- Keep the original line breaks and paragraph structure.
- Quoted speech stays quoted. Keep the speaker attribution.
- If an item is already Persian, return it unchanged.
- Do not put any emoji or label for urgency yourself; the system adds it.
- Output only the JSON array, nothing else."""


def load_text_file(path):
    try:
        lines = [l.rstrip() for l in path.read_text(encoding="utf-8").splitlines()]
        return [l for l in lines if l.strip() and not l.lstrip().startswith("#")]
    except Exception:
        return []


def build_system_prompt():
    prompt = SYSTEM_PROMPT
    topics = load_text_file(TOPICS_FILE)
    if topics:
        prompt += "\n\nTOPICS (what the channel covers):\n" + "\n".join(topics)
    g = load_glossary()
    if g:
        prompt += "\n\nGlossary (always use these translations):\n" + "\n".join(g)
    return prompt


def load_keywords():
    kws = [l.strip().lower() for l in load_text_file(KEYWORDS_FILE)]
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
                       "urgent": {"type": "BOOLEAN"}, "reason": {"type": "STRING"},
                       "fa": {"type": "STRING"}},
        "required": ["id", "relevant", "urgent", "reason", "fa"],
    },
}


def gemini_filter(model, items, system_prompt):
    """-> {id: {'relevant': bool, 'urgent': bool, 'reason': str, 'fa': str}}"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(items, ensure_ascii=False)}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "responseSchema": GEMINI_SCHEMA,
        },
    }
    headers = {"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"}
    last_err = None
    for attempt in range(3):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=90)
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
        arr = json.loads(text)
        out = {}
        for x in arr:
            try:
                out[int(x["id"])] = {"relevant": bool(x.get("relevant")), "urgent": bool(x.get("urgent")),
                                     "reason": str(x.get("reason", "")).strip(),
                                     "fa": str(x.get("fa", "")).strip()}
            except (KeyError, TypeError, ValueError):
                continue
        return out
    raise RuntimeError(f"{model}: failed after retries ({last_err})")


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


def make_batches(posts):
    batches, cur, size = [], [], 0
    for p in posts:
        n = len(p["text"])
        if cur and (len(cur) >= BATCH_SIZE or size + n > BATCH_CHARS):
            batches.append(cur)
            cur, size = [], 0
        cur.append(p)
        size += n
    if cur:
        batches.append(cur)
    return batches


def process_posts(posts):
    """posts: list of {'id','text'} -> {id: {'relevant','urgent','reason','fa','ai'}}.
    Missing ids = nothing worked (will be retried on the next run)."""
    result = {}
    system_prompt = build_system_prompt()
    kws = load_keywords()
    dead = set()      # models that hit quota/auth problems during this run

    def try_models(need):
        for model in GEMINI_MODELS:
            if not need:
                return
            if model in dead:
                continue
            try:
                got = gemini_filter(model, [{"id": i, "text": t} for i, t in need.items()], system_prompt)
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
        # no AI answer: keyword match decides, Google Translate translates
        for i, t in list(need.items()):
            if i in result:
                continue
            if not keyword_match(t, kws):
                result[i] = {"relevant": False, "urgent": False, "fa": "", "ai": False,
                             "reason": "هوش مصنوعی در دسترس نبود و کلیدواژه‌ای پیدا نشد"}
                continue
            try:
                fa = google_translate(t)
                if looks_ok(t, fa):
                    result[i] = {"relevant": True, "urgent": False, "fa": fa, "ai": False,
                                 "reason": "بدون هوش مصنوعی (کلیدواژه)"}
                else:
                    log(f"  post {i}: google translate output rejected")
            except Exception as e:
                log(f"  post {i}: google translate failed: {str(e)[:200]}")
    return result


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


def split_message(text, limit=4096):
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


def download(url):
    try:
        r = requests.get(url, headers=UA, timeout=30, stream=True)
        r.raise_for_status()
        buf = r.content
        if len(buf) > 10 * 1024 * 1024:
            return None
        return buf
    except Exception as e:
        log("  photo download failed:", str(e)[:100])
        return None


def send_post(post, fa_text):
    parts = []
    if fa_text:
        parts.append(fa_text)
    if ADD_SOURCE_LINK:
        parts.append(f"📎 منبع: https://t.me/{SOURCE}/{post['link_id']}")
    if FOOTER:
        parts.append(FOOTER)
    text = "\n\n".join(parts)

    photos = []
    for u in post["photos"]:
        b = download(u)
        if b:
            photos.append(b)

    if DRY_RUN:
        log(f"  [dry-run] post {post['id']}: {len(photos)} photo(s)\n" + "\n".join("    " + l for l in text.splitlines()))
        return

    if not photos:
        for part in split_message(text):
            tg("sendMessage", data={"chat_id": TARGET, "text": part, "disable_web_page_preview": "true"})
        return

    caption_ok = len(text) <= 1024
    if len(photos) == 1:
        data = {"chat_id": TARGET}
        if caption_ok:
            data["caption"] = text
        tg("sendPhoto", data=data, files={"photo": ("photo.jpg", io.BytesIO(photos[0]))})
    else:
        media, files = [], {}
        for n, b in enumerate(photos):
            item = {"type": "photo", "media": f"attach://p{n}"}
            if n == 0 and caption_ok:
                item["caption"] = text
            media.append(item)
            files[f"p{n}"] = (f"p{n}.jpg", io.BytesIO(b))
        tg("sendMediaGroup", data={"chat_id": TARGET, "media": json.dumps(media)}, files=files)
    if not caption_ok:
        for part in split_message(text):
            tg("sendMessage", data={"chat_id": TARGET, "text": part, "disable_web_page_preview": "true"})


def send_review(post, verdict):
    """Rejected posts go to a private review channel so you can check the filter. Never blocks the run."""
    if not REVIEW_CHAT:
        return
    note = "" if verdict.get("ai", True) else "\n(بدون هوش مصنوعی)"
    text = f"⛔ {verdict.get('reason') or 'رد شد'}{note}\n\n{post['text'][:1500]}\n\nhttps://t.me/{SOURCE}/{post['link_id']}"
    if DRY_RUN:
        log(f"  [dry-run] REJECTED post {post['id']}: {verdict.get('reason')}\n    " + post["text"][:120].replace("\n", " "))
        return
    try:
        tg("sendMessage", data={"chat_id": REVIEW_CHAT, "text": text, "disable_web_page_preview": "true"})
        time.sleep(1.2)
    except Exception as e:
        log(f"  review channel send failed: {str(e)[:150]}")


# -------------------------------------------------------------------- main ---
def main():
    if not DRY_RUN and (not BOT_TOKEN or not TARGET):
        log("ERROR: TELEGRAM_BOT_TOKEN and TARGET_CHAT must be set (or use DRY_RUN=true).")
        return 1
    if GEMINI_KEY:
        log("Gemini key found; models:", ", ".join(GEMINI_MODELS))
    else:
        log("No Gemini key: using Google Translate only.")

    state = load_state()
    last_id = state["last_id"]

    try:
        posts = collect_posts(last_id)
    except Exception as e:
        log("ERROR: could not read the source channel:", str(e)[:200])
        return 1
    if not posts:
        log("ERROR: no posts found on the preview page (channel private, or preview disabled?).")
        return 1
    ids = sorted(posts)
    log(f"Fetched {len(ids)} posts (ids {ids[0]}..{ids[-1]}), last processed: {last_id}")

    if last_id is None:
        if INITIAL_BACKFILL > 0:
            last_id = ids[-INITIAL_BACKFILL - 1] if len(ids) > INITIAL_BACKFILL else ids[0] - 1
        else:
            state["last_id"] = ids[-1]
            save_state(state)
            log(f"First run: starting from post {ids[-1]}; nothing published. New posts will be copied from now on.")
            return 0

    new = [posts[i] for i in ids if i > last_id][:MAX_POSTS_PER_RUN]
    if not new:
        log("No new posts.")
        return 0

    # posts with text go through the AI filter; photos without any text cannot be judged
    to_check = [p for p in new if p["text"] and not (SKIP_VIDEO_POSTS and p["has_video"])]
    log(f"{len(new)} new post(s), checking {len(to_check)}")
    verdicts = process_posts(to_check) if to_check else {}

    sent = rejected = 0
    for p in new:
        pid = p["id"]
        skip = (SKIP_VIDEO_POSTS and p["has_video"]) or (not p["text"] and not (p["photos"] and PUBLISH_PHOTO_ONLY))
        if skip:
            state["last_id"] = pid
            save_state(state)
            continue

        if not p["text"]:
            fa = ""
        else:
            v = verdicts.get(pid)
            if v is None:
                n = state["fails"].get(str(pid), 0) + 1
                state["fails"][str(pid)] = n
                save_state(state)
                if n >= MAX_FAILS:
                    log(f"post {pid}: processing failed {n} times, skipping it")
                    state["last_id"] = pid
                    state["fails"].pop(str(pid), None)
                    save_state(state)
                    continue
                log(f"post {pid}: processing failed ({n}/{MAX_FAILS}), will retry next run")
                break
            if not v["relevant"]:
                send_review(p, v)
                rejected += 1
                state["last_id"] = pid
                state["fails"].pop(str(pid), None)
                save_state(state)
                continue
            fa = ("🔴 " if v["urgent"] else "") + v["fa"]
        try:
            send_post(p, fa)
        except Exception as e:
            n = state["fails"].get(str(pid), 0) + 1
            state["fails"][str(pid)] = n
            log(f"post {pid}: send failed ({n}/{MAX_FAILS}): {str(e)[:200]}")
            if n >= MAX_FAILS:
                state["last_id"] = pid
                state["fails"].pop(str(pid), None)
            save_state(state)
            if n < MAX_FAILS:
                break
            continue
        sent += 1
        state["last_id"] = pid
        state["fails"].pop(str(pid), None)
        save_state(state)
        if not DRY_RUN:
            time.sleep(SEND_PAUSE)

    log(f"Done. {'Previewed' if DRY_RUN else 'Sent'} {sent} post(s), {rejected} rejected by the filter.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
