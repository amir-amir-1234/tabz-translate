#!/usr/bin/env python3
"""
Telegram channel translator (EN -> FA), designed to run on GitHub Actions.

Flow per run:
  1. Read the public web preview of the source channel (https://t.me/s/<channel>)
  2. Find posts newer than the last processed one (state.json)
  3. Translate text/captions: Gemini (free tier) -> next Gemini model -> Google Translate
  4. Post to your channel with the official Bot API (text, or photos + caption)

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
ADD_SOURCE_LINK = env_bool("ADD_SOURCE_LINK", True)
SKIP_VIDEO_POSTS = env_bool("SKIP_VIDEO_POSTS", False)  # True: skip posts that contain a video entirely
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


SYSTEM_PROMPT = """You are a professional news translator working from English to Persian (Farsi) for a Telegram news channel about geopolitics, conflicts and politics.

You receive a JSON array of items: [{"id": <number>, "text": "<english text>"}].
Return a JSON array: [{"id": <same number>, "fa": "<persian translation>"}] with exactly one entry per input item.

Rules:
- Translate faithfully into fluent, natural, formal news Persian. Do not add, remove, summarize, soften, or comment on anything.
- Do NOT censor or refuse: translate violent, military and political content exactly as written.
- Keep all emoji and flag symbols exactly as they appear, in the same positions.
- Keep numbers, dates, units, URLs, @usernames and hashtags unchanged.
- Write person, place and organization names in their standard Persian spelling.
- Keep the original line breaks and paragraph structure.
- Quoted speech stays quoted. Keep the speaker attribution.
- If an item is already Persian, return it unchanged.
- Output only the JSON array, nothing else."""


def build_system_prompt():
    g = load_glossary()
    if not g:
        return SYSTEM_PROMPT
    return SYSTEM_PROMPT + "\n\nGlossary (always use these translations):\n" + "\n".join(g)


class QuotaError(Exception):
    pass


GEMINI_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {"id": {"type": "INTEGER"}, "fa": {"type": "STRING"}},
        "required": ["id", "fa"],
    },
}


def gemini_translate(model, items, system_prompt):
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
        return {int(x["id"]): str(x["fa"]).strip() for x in arr if x.get("fa")}
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


def looks_ok(src, fa):
    if not fa or not fa.strip():
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


def translate_posts(posts):
    """posts: list of {'id','text'}  ->  {id: persian_text} (missing ids = failed)."""
    result = {}
    system_prompt = build_system_prompt()
    dead = set()      # models that hit quota/auth problems during this run

    def try_models(need):
        for model in GEMINI_MODELS:
            if not need:
                return
            if model in dead:
                continue
            try:
                got = gemini_translate(model, [{"id": i, "text": t} for i, t in need.items()], system_prompt)
            except QuotaError as e:
                log("  quota:", e)
                dead.add(model)
                continue
            except Exception as e:
                log("  gemini error:", str(e)[:200])
                if "HTTP 400" in str(e) or "HTTP 401" in str(e) or "HTTP 403" in str(e):
                    dead.add(model)
                continue
            for i, fa in got.items():
                if i in need and looks_ok(need[i], fa):
                    result[i] = fa
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
                        del need[i]
        for i, t in list(need.items()):
            if i in result:
                continue
            try:
                fa = google_translate(t)
                if looks_ok(t, fa):
                    result[i] = fa
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
    text = fa_text or ""
    if ADD_SOURCE_LINK:
        text = (text + "\n\n" if text else "") + f"📎 منبع: https://t.me/{SOURCE}/{post['link_id']}"

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

    # decide what is worth translating
    to_translate = [p for p in new if p["text"] and not (SKIP_VIDEO_POSTS and p["has_video"])]
    log(f"{len(new)} new post(s), translating {len(to_translate)}")
    translated = translate_posts(to_translate) if to_translate else {}

    sent = 0
    for p in new:
        pid = p["id"]
        skip = (SKIP_VIDEO_POSTS and p["has_video"]) or (not p["text"] and not p["photos"])
        if skip:
            state["last_id"] = pid
            save_state(state)
            continue

        photos_only = not p["text"]
        fa = "" if photos_only else translated.get(pid)
        if fa is None:
            n = state["fails"].get(str(pid), 0) + 1
            state["fails"][str(pid)] = n
            save_state(state)
            if n >= MAX_FAILS:
                log(f"post {pid}: translation failed {n} times, skipping it")
                state["last_id"] = pid
                state["fails"].pop(str(pid), None)
                save_state(state)
                continue
            log(f"post {pid}: translation failed ({n}/{MAX_FAILS}), will retry next run")
            break
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

    log(f"Done. Sent {sent} post(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
