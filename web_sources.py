"""
Website sources for the aggregator: RSS/Atom feeds and plain HTML listing pages.

Only what a site publishes openly is read: RSS fields (title, description, full text when the feed
itself carries it) or, for official government pages, the article page. Paywalled articles are never
fetched. robots.txt is respected.

Config line (sources_web.txt or env WEB_SOURCES):
    name | rss  | feed url                   | Persian outlet name
    name | html | listing url | link regex   | Persian outlet name
"""
import email.utils
import re
import time
import urllib.parse
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import datetime

import requests
from bs4 import BeautifulSoup

WEB_UA = "Mozilla/5.0 (compatible; NewsAggregatorBot/1.0; personal non-commercial use)"
HEADERS = {"User-Agent": WEB_UA, "Accept": "text/html,application/xhtml+xml,application/xml,application/rss+xml;q=0.9,*/*;q=0.8"}

NS = {"content": "http://purl.org/rss/1.0/modules/content/",
      "media": "http://search.yahoo.com/mrss/",
      "atom": "http://www.w3.org/2005/Atom",
      "dc": "http://purl.org/dc/elements/1.1/"}

BOILERPLATE_RE = re.compile(
    r"click here|download the fox news app|fox news app|sign up for|subscribe to|newsletter|"
    r"follow .{0,40} on (x|twitter|facebook|instagram)|all rights reserved|advertisement$|"
    r"^(read|watch|listen)( more| now)?:|this (story|article) (was|originally)", re.I)

_robots = {}


def allowed(url):
    """robots.txt check (cached per host). Any failure to read robots.txt counts as allowed."""
    p = urllib.parse.urlparse(url)
    host = f"{p.scheme}://{p.netloc}"
    if host not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = requests.get(host + "/robots.txt", headers=HEADERS, timeout=15)
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
            else:
                rp = None
        except Exception:
            rp = None
        _robots[host] = rp
    rp = _robots[host]
    return True if rp is None else rp.can_fetch(WEB_UA, url)


def fetch(url, timeout=30):
    if not allowed(url):
        raise RuntimeError("blocked by robots.txt")
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as e:                      # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


# ------------------------------------------------------------------ config ---
def parse_sources(lines):
    out = []
    for l in lines:
        parts = [x.strip() for x in l.split("|")]
        if len(parts) < 4:
            continue
        name, typ = parts[0], parts[1].lower()
        if typ == "rss" and len(parts) >= 4:
            out.append({"name": name, "type": "rss", "url": parts[2], "outlet_fa": parts[3], "regex": ""})
        elif typ == "html" and len(parts) >= 5:
            out.append({"name": name, "type": "html", "url": parts[2], "regex": parts[3], "outlet_fa": parts[4]})
    return out


# ------------------------------------------------------------------- text ---
def html_to_text(html):
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup(["script", "style", "noscript", "iframe", "figure", "aside", "form"]):
        t.decompose()
    blocks = []
    for el in soup.find_all(["p", "li", "blockquote", "h2", "h3"]):
        txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        if not txt:
            continue
        if BOILERPLATE_RE.search(txt):
            continue
        blocks.append(txt)
    if not blocks:
        txt = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
        return "" if BOILERPLATE_RE.search(txt) else txt
    out = []
    for b in blocks:                       # nested li/p can repeat the same text
        if not out or out[-1] != b:
            out.append(b)
    return "\n\n".join(out)


def _ts(value):
    if not value:
        return 0.0
    value = value.strip()
    try:
        return email.utils.parsedate_to_datetime(value).timestamp()
    except Exception:                               # noqa: BLE001
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:                               # noqa: BLE001
        return 0.0


def _text(el, path):
    if el is None:
        return ""
    found = el.find(path, NS)
    return (found.text or "").strip() if found is not None else ""


# --------------------------------------------------------------------- rss ---
def parse_rss(xml_text, src):
    """-> [item] newest first when the feed is ordered that way."""
    xml_text = xml_text.lstrip("﻿ \n\r\t")
    root = ET.fromstring(xml_text)
    items = []
    nodes = root.findall(".//item") or root.findall(".//atom:entry", NS)
    for it in nodes:
        title = html_to_text(_text(it, "title")) or _text(it, "title")
        link = _text(it, "link")
        if not link:
            a = it.find("atom:link", NS)
            link = (a.get("href") if a is not None else "") or ""
        key = _text(it, "guid") or _text(it, "atom:id") or link
        if not key or not title:
            continue
        full = _text(it, "content:encoded") or _text(it, "atom:content")
        desc = _text(it, "description") or _text(it, "atom:summary")
        body = html_to_text(full) if full else html_to_text(desc)
        if body.strip() == title.strip():
            body = ""
        ts = _ts(_text(it, "pubDate") or _text(it, "atom:updated") or _text(it, "atom:published") or _text(it, "dc:date"))
        image = ""
        for tag in ("media:content", "media:thumbnail"):
            m = it.find(tag, NS)
            if m is not None and m.get("url") and (m.get("medium", "image") == "image" or "image" in (m.get("type") or "image")):
                image = m.get("url")
                break
        if not image:
            enc = it.find("enclosure")
            if enc is not None and "image" in (enc.get("type") or "") and enc.get("url"):
                image = enc.get("url")
        items.append({"key": key, "title": title, "body": body, "link": link, "ts": ts,
                      "image": image, "outlet": src["name"], "outlet_fa": src["outlet_fa"]})
    return items


# -------------------------------------------------------------------- html ---
def list_links(html, base_url, regex):
    soup = BeautifulSoup(html, "html.parser")
    rx = re.compile(regex)
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        url = urllib.parse.urljoin(base_url, a["href"]).split("#")[0]
        if not rx.search(url) or url in seen:
            continue
        seen.add(url)
        out.append({"key": url, "link": url, "title": re.sub(r"\s+", " ", a.get_text(" ", strip=True))})
    return out


def parse_article(html):
    """Generic article extraction for official pages -> {'title','body','ts'}."""
    soup = BeautifulSoup(html, "html.parser")

    def meta(*names):
        for n in names:
            m = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if m and m.get("content"):
                return m["content"].strip()
        return ""

    title = meta("og:title", "twitter:title")
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else (soup.title.get_text(strip=True) if soup.title else "")
    ts = _ts(meta("article:published_time", "datePublished", "pubdate"))
    if not ts:
        t = soup.find("time", attrs={"datetime": True})
        ts = _ts(t["datetime"]) if t else 0.0
    for t in soup(["nav", "header", "footer"]):
        t.decompose()
    root = soup.find("article") or soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup
    return {"title": re.sub(r"\s+", " ", title).strip(), "body": html_to_text(str(root)), "ts": ts}


# ----------------------------------------------------------------- public ---
def list_items(src):
    """Network: current items of a source. RSS items are complete; HTML items are stubs (see materialize)."""
    text = fetch(src["url"])
    if src["type"] == "rss":
        return parse_rss(text, src)
    stubs = list_links(text, src["url"], src["regex"])
    for s in stubs:
        s.update(body="", ts=0.0, image="", outlet=src["name"], outlet_fa=src["outlet_fa"], stub=True)
    return stubs


def materialize(src, item):
    """Make a complete item. For HTML sources this fetches the article page."""
    if not item.get("stub"):
        return item
    art = parse_article(fetch(item["link"]))
    out = dict(item)
    out.update(title=art["title"] or item["title"], body=art["body"], ts=art["ts"], stub=False)
    if not out["title"]:
        raise RuntimeError("article without title")
    return out
