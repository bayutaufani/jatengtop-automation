#!/usr/bin/env python3
"""
Otomasi Berita Jateng TOP
- Mode scheduled: mengambil kandidat trending (Google Trends/Google News RSS + RSS media)
  lalu menghasilkan 5 draft.
- Mode breaking: berjalan tiap 15 menit, hanya membuat artikel jika ada kandidat baru
  yang memenuhi ambang tren/kata kunci darurat.
- LLM gratis: Gemini API atau Groq API (opsional). Tanpa API key, artikel tetap
  dibuat dengan summarizer ekstraktif dari sumber.
"""

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# KONFIGURASI
# ---------------------------------------------------------------------------
SITE_BASE_URL = os.getenv("SITE_BASE_URL", "").rstrip("/")
AUTOMATION_TOKEN = os.getenv("AUTOMATION_TOKEN", "")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "auto").lower()

NEWS_COUNT = int(os.getenv("NEWS_COUNT", "5"))
BREAKING_MAX = int(os.getenv("BREAKING_MAX", "2"))
BREAKING_TRAFFIC_THRESHOLD = int(os.getenv("BREAKING_TRAFFIC_THRESHOLD", "200"))

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (compatible; JatengTopAutoNews/1.0; +https://jawatengah.top)",
)

GOOGLE_NEWS_RSS = "https://news.google.com/rss?hl=id&gl=ID&ceid=ID:id"
GOOGLE_TRENDS_RSS = "https://trends.google.com/trending/rss?geo=ID"
GOOGLE_BREAKING_RSS = (
    "https://news.google.com/rss/search?q=when%3A1h&hl=id&gl=ID&ceid=ID:id"
)

DIRECT_FEEDS = [
    ("CNN Indonesia", "https://www.cnnindonesia.com/rss"),
    ("Antara", "https://www.antaranews.com/rss/terkini.xml"),
    ("Tempo", "https://rss.tempo.co/"),
    ("Detik", "https://rss.detik.com/index.php/detikcom"),
]

BREAKING_KEYWORDS = [
    "gempa", "tsunami", "banjir", "longsor", "kebakaran", "ledakan",
    "kecelakaan", "tewas", "meninggal", "darurat", "evakuasi", "kerusuhan",
    "demo", "makar", "teror", "pesawat jatuh", "kapal tenggelam", "ditangkap",
    "kabur", "perang", "konflik", "pemilu", "curang", "resmi", "diumumkan",
]

ALLOWED_TAGS = {
    "p", "h2", "h3", "h4", "ul", "ol", "li", "strong", "b", "em", "i",
    "a", "blockquote", "br", "figure", "figcaption",
}

TZ_WIB = timezone(timedelta(hours=7))

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "id-ID,id;q=0.9"})


# ---------------------------------------------------------------------------
# UTILITAS
# ---------------------------------------------------------------------------
def log(level, message):
    now = datetime.now(TZ_WIB).strftime("%Y-%m-%d %H:%M:%S WIB")
    print(f"[{now}] [{level}] {message}", flush=True)


def local_name(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def child_text(element, name):
    for child in list(element):
        if local_name(child.tag) == name:
            return (child.text or "").strip()
    return ""


def parse_traffic(value):
    digits = re.sub(r"[^0-9]", "", value or "")
    return int(digits) if digits else 0


def parse_date(value):
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ_WIB)
    except (TypeError, ValueError, OverflowError):
        return None


def strip_html(value, limit=0):
    text = BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "..."
    return text


def extract_image_from_html(value):
    match = re.search(r"<img[^>]+src=[\"']([^\"']+)", value or "", re.IGNORECASE)
    return html.unescape(match.group(1)) if match else ""


def normalize_url(value):
    value = (value or "").strip()
    if not value:
        return ""
    parts = urlparse(value)
    if not parts.netloc:
        return value.lower()

    scheme = "https" if parts.scheme in ("http", "https") else parts.scheme
    query = []
    for key, val in parse_qsl(parts.query, keep_blank_values=False):
        if re.match(r"^(utm_|fbclid$|gclid$|oc$|hl$|gl$|ceid$)", key, re.IGNORECASE):
            continue
        query.append((key, val))

    return urlunparse((scheme, parts.netloc.lower(), parts.path or "/", "", urlencode(query), ""))


def source_hash(value):
    return hashlib.sha256(normalize_url(value).encode("utf-8")).hexdigest()


def normalize_title(value):
    value = unicodedata.normalize("NFKD", value or "").lower()
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    stopwords = {
        "yang", "dan", "di", "ke", "dari", "untuk", "dengan", "pada", "dalam",
        "ini", "itu", "akan", "tidak", "juga", "kata", "soal", "usai", "jadi",
        "bisa", "ada", "lebih", "baru", "terbaru", "the", "and", "of", "to",
    }
    words = [word for word in value.split() if word and word not in stopwords]
    return " ".join(words)


def title_fingerprint(value):
    normalized = normalize_title(value)
    return " ".join(normalized.split()[:14])


def extract_keywords(text, limit=4):
    text = normalize_title(text)
    words = [word for word in text.split() if len(word) > 3]
    seen = []
    for word in words:
        if word not in seen:
            seen.append(word)
    seen.sort(key=len, reverse=True)
    return seen[:limit]


def now_wib_string():
    return datetime.now(TZ_WIB).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# HTTP & RSS
# ---------------------------------------------------------------------------
def http_get(url, timeout=20):
    return SESSION.get(url, timeout=timeout, allow_redirects=True)


def parse_feed(xml_bytes, feed_name):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        log("ERROR", f"Gagal parse RSS {feed_name}: {exc}")
        return []

    items = []
    for element in root.iter():
        if local_name(element.tag) != "item":
            continue

        title = child_text(element, "title")
        link = child_text(element, "link")
        description = child_text(element, "description")
        published_raw = child_text(element, "pubDate")
        published = parse_date(published_raw)

        source_element = None
        for child in list(element):
            if local_name(child.tag) == "source":
                source_element = child
                break

        source_name = (source_element.text or "").strip() if source_element is not None else feed_name
        source_url = source_element.attrib.get("url", "") if source_element is not None else ""
        traffic = parse_traffic(child_text(element, "approx_traffic"))
        feed_image = child_text(element, "picture")

        news_items = []
        for child in list(element):
            if local_name(child.tag) != "news_item":
                continue
            news_url = child_text(child, "news_item_url")
            if not news_url:
                continue
            news_items.append({
                "title": child_text(child, "news_item_title"),
                "url": news_url,
                "source": child_text(child, "news_item_source") or feed_name,
                "image": child_text(child, "news_item_picture"),
                "snippet": child_text(child, "news_item_snippet"),
            })

        if not link and news_items:
            link = news_items[0]["url"]

        if not title:
            continue

        items.append({
            "feed": feed_name,
            "title": title,
            "url": link,
            "description": description,
            "snippet": strip_html(description, 600),
            "published_raw": published_raw,
            "published": published,
            "source_name": source_name,
            "source_url": source_url,
            "traffic": traffic,
            "feed_image": feed_image or extract_image_from_html(description),
            "news_items": news_items,
        })

    return items


def fetch_feed(name, url):
    try:
        response = http_get(url)
        response.raise_for_status()
        return parse_feed(response.content, name)
    except Exception as exc:
        log("WARN", f"Feed {name} gagal: {exc}")
        return []


def collect_candidates(mode):
    candidates = []
    seen_titles = set()

    def add(candidate):
        candidate["title"] = re.sub(r"\s+", " ", str(candidate.get("title", ""))).strip()
        fingerprint = title_fingerprint(candidate.get("title", ""))
        if not fingerprint or fingerprint in seen_titles:
            return
        seen_titles.add(fingerprint)
        candidates.append(candidate)

    # 1. Google Trends Indonesia (sumber tren + URL berita langsung)
    for trend in fetch_feed("Google Trends ID", GOOGLE_TRENDS_RSS):
        news_items = trend.get("news_items") or []
        if news_items:
            for index, news in enumerate(news_items[:3]):
                add({
                    "title": news.get("title") or trend["title"],
                    "source_url": news.get("url") or trend["url"],
                    "source_name": news.get("source") or trend.get("source_name") or "Google Trends",
                    "description": news.get("snippet") or trend.get("snippet", ""),
                    "published": trend.get("published"),
                    "traffic": trend.get("traffic", 0),
                    "feed_image": news.get("image") or trend.get("feed_image", ""),
                    "origin": "trends",
                    "trend": trend["title"],
                    "is_google_news": False,
                })
        elif trend.get("snippet"):
            add({
                "title": trend["title"],
                "source_url": trend["url"],
                "source_name": trend.get("source_name") or "Google Trends",
                "description": trend.get("snippet", ""),
                "published": trend.get("published"),
                "traffic": trend.get("traffic", 0),
                "feed_image": trend.get("feed_image", ""),
                "origin": "trends",
                "trend": trend["title"],
                "is_google_news": False,
            })

    # 2. RSS media Indonesia (link langsung + gambar + teks)
    for feed_name, feed_url in DIRECT_FEEDS:
        for item in fetch_feed(feed_name, feed_url):
            if not item.get("url"):
                continue
            add({
                "title": item["title"],
                "source_url": item["url"],
                "source_name": item.get("source_name") or feed_name,
                "description": item.get("snippet", ""),
                "published": item.get("published"),
                "traffic": item.get("traffic", 0),
                "feed_image": item.get("feed_image", ""),
                "origin": "direct",
                "trend": "",
                "is_google_news": False,
            })

    # 3. Google News Indonesia (judul populer; link berupa redirect Google)
    google_url = GOOGLE_BREAKING_RSS if mode == "breaking" else GOOGLE_NEWS_RSS
    for item in fetch_feed("Google News ID", google_url):
        if not item.get("url"):
            continue
        title = re.sub(r"\s+-\s+[^-]{2,60}$", "", item["title"]).strip()
        add({
            "title": title,
            "source_url": item["url"],
            "source_name": item.get("source_name") or "Google News",
            "description": item.get("snippet", ""),
            "published": item.get("published"),
            "traffic": item.get("traffic", 0),
            "feed_image": item.get("feed_image", ""),
            "origin": "google_news",
            "trend": "",
            "is_google_news": True,
        })

    if mode == "breaking":
        filtered = []
        cutoff = datetime.now(TZ_WIB) - timedelta(hours=3)
        for candidate in candidates:
            if candidate.get("is_google_news"):
                continue
            published = candidate.get("published")
            if published and published < cutoff:
                continue

            haystack = (candidate.get("title", "") + " " + candidate.get("description", "")).lower()
            has_keyword = any(keyword in haystack for keyword in BREAKING_KEYWORDS)
            if candidate.get("traffic", 0) >= BREAKING_TRAFFIC_THRESHOLD or has_keyword:
                filtered.append(candidate)

        # Prioritaskan tren tertinggi
        filtered.sort(key=lambda row: row.get("traffic", 0), reverse=True)
        return filtered

    # Mode scheduled: prioritaskan trends, lalu media langsung, lalu Google News
    priority = {"trends": 0, "direct": 1, "google_news": 2}
    candidates.sort(key=lambda row: (priority.get(row.get("origin"), 9), -row.get("traffic", 0)))
    return candidates


# ---------------------------------------------------------------------------
# API PHP
# ---------------------------------------------------------------------------
def api_call(action, method="GET", payload=None, params=None, timeout=90):
    if not SITE_BASE_URL or not AUTOMATION_TOKEN:
        raise RuntimeError("SITE_BASE_URL dan AUTOMATION_TOKEN wajib diisi.")

    url = f"{SITE_BASE_URL}/api_automation.php"
    query = {"action": action}
    if params:
        query.update(params)

    headers = {
        "X-Automation-Token": AUTOMATION_TOKEN,
        "Accept": "application/json",
    }

    last_error = None
    for attempt in range(1, 4):
        try:
            if method == "GET":
                response = SESSION.get(url, params=query, headers=headers, timeout=timeout)
            else:
                response = SESSION.post(url, params=query, json=payload, headers=headers, timeout=timeout)

            if response.status_code >= 500 and attempt < 3:
                last_error = RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
                time.sleep(3 * attempt)
                continue

            if response.status_code != 200:
                raise RuntimeError(f"API {action} HTTP {response.status_code}: {response.text[:300]}")

            data = response.json()
            if data.get("status") == "error":
                raise RuntimeError(data.get("message", "API error"))
            return data
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(3 * attempt)

    raise RuntimeError(f"API {action} gagal: {last_error}")


def filter_new_candidates(candidates):
    if not candidates:
        return []

    payload = {
        "items": [
            {
                "title": item["title"],
                "source_url": item["source_url"],
                "source_name": item.get("source_name", ""),
                "description": item.get("description", ""),
                "snippet": item.get("snippet", ""),
                "published_raw": item.get("published_raw", ""),
                "feed_image": item.get("feed_image", ""),
                "traffic": item.get("traffic", 0),
                "origin": item.get("origin", ""),
                "trend": item.get("trend", ""),
                "is_google_news": bool(item.get("is_google_news")),
            }
            for item in candidates[:50]
        ]
    }

    data = api_call("filter_new", method="POST", payload=payload)
    fresh = data.get("items", [])
    log("INFO", f"Filter dedupe: {len(candidates)} kandidat -> {len(fresh)} baru.")
    return fresh


# ---------------------------------------------------------------------------
# PENGAMBILAN DETAIL ARTIKEL
# ---------------------------------------------------------------------------
def fetch_article_details(candidate):
    url = candidate.get("source_url", "")
    details = {
        "text": candidate.get("description", ""),
        "image": candidate.get("feed_image", ""),
        "canonical": url,
        "site_name": candidate.get("source_name", ""),
        "description": candidate.get("description", ""),
        "fetched": False,
    }

    if not url or candidate.get("is_google_news"):
        return details

    try:
        response = SESSION.get(url, timeout=20, allow_redirects=True)
        response.raise_for_status()
    except Exception as exc:
        log("WARN", f"Gagal membuka sumber {url[:80]}: {exc}")
        return details

    if "news.google.com" in urlparse(response.url).netloc:
        return details

    try:
        soup = BeautifulSoup(response.text[:2_000_000], "html.parser")
    except Exception:
        return details

    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "iframe"]):
        tag.decompose()

    image = ""
    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or "").lower()
        if key in ("og:image", "twitter:image", "twitter:image:src") and not image:
            image = meta.get("content", "")

    description = ""
    for meta in soup.find_all("meta"):
        key = (meta.get("name") or meta.get("property") or "").lower()
        if key in ("description", "og:description") and not description:
            description = meta.get("content", "")

    canonical = ""
    canonical_tag = soup.find("link", rel=lambda value: value and "canonical" in value)
    if canonical_tag:
        canonical = canonical_tag.get("href", "")
    if canonical:
        canonical = urljoin(response.url, canonical)
    if image:
        image = urljoin(response.url, image)

    paragraphs = []
    container = soup.find("article") or soup.body or soup
    for paragraph in container.find_all("p"):
        text = paragraph.get_text(" ", strip=True)
        if len(text) >= 45:
            paragraphs.append(text)

    text = "\n\n".join(paragraphs)
    if len(text) < 300:
        text = container.get_text(" ", strip=True)

    details.update({
        "text": text[:8000],
        "image": image or details["image"],
        "canonical": canonical or response.url,
        "description": description or details["description"],
        "fetched": True,
    })
    return details


# ---------------------------------------------------------------------------
# LLM GRATIS
# ---------------------------------------------------------------------------
def parse_json_response(text):
    if not text:
        return None

    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None

    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def call_gemini(system_prompt, user_prompt):
    if not GEMINI_API_KEY:
        return None

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + requests.utils.quote(GEMINI_MODEL, safe="")
        + ":generateContent?key="
        + requests.utils.quote(GEMINI_API_KEY, safe="")
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": system_prompt + "\n\n" + user_prompt}]}],
        "generationConfig": {
            "temperature": 0.6,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
        },
    }

    try:
        response = SESSION.post(url, json=payload, timeout=80)
        response.raise_for_status()
        data = response.json()
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts)
        return text or None
    except Exception as exc:
        log("WARN", f"Gemini gagal: {exc}")
        return None


def call_groq(system_prompt, user_prompt):
    if not GROQ_API_KEY:
        return None

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.6,
        "max_tokens": 4096,
        "response_format": {"type": "json_object"},
    }

    try:
        response = SESSION.post(
            "https://api.groq.com/openai/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            timeout=80,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content")
    except Exception as exc:
        log("WARN", f"Groq gagal: {exc}")
        return None


def call_llm(system_prompt, user_prompt):
    if LLM_PROVIDER == "gemini":
        text = call_gemini(system_prompt, user_prompt)
        return ("gemini", text) if text else (None, None)

    if LLM_PROVIDER == "groq":
        text = call_groq(system_prompt, user_prompt)
        return ("groq", text) if text else (None, None)

    text = call_gemini(system_prompt, user_prompt)
    if text:
        return "gemini", text

    text = call_groq(system_prompt, user_prompt)
    if text:
        return "groq", text

    return None, None


def sanitize_html(raw_html):
    if not raw_html:
        return ""

    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "iframe", "object", "embed", "form", "input", "button", "textarea", "select", "svg", "math"]):
        tag.decompose()

    for tag in soup.find_all(True):
        if tag.name not in ALLOWED_TAGS:
            tag.unwrap()
            continue

        attributes = {}
        if tag.name == "a":
            href = tag.get("href", "")
            if href.startswith(("http://", "https://", "/")):
                attributes["href"] = href
            attributes["rel"] = "noopener"
        tag.attrs = attributes

    result = str(soup).strip()
    if result and "<p" not in result and "<h2" not in result:
        paragraphs = [p.strip() for p in re.split(r"\n{2,}", result) if p.strip()]
        result = "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)
    return result


def make_captions(title, excerpt, article_url, tags):
    hashtags = ["#JatengTOP", "#JawaTengah", "#BeritaTerkini", "#InfoJateng"]
    if isinstance(tags, str):
        tags = [tags]
    for tag in tags or []:
        cleaned = re.sub(r"[^A-Za-z0-9_]", "", str(tag))
        if cleaned:
            hashtags.append("#" + cleaned)
    hashtags = " ".join(dict.fromkeys(hashtags))

    instagram = f"{title}\n\n{excerpt}\n\nSelengkapnya: {article_url}\n\n{hashtags}"
    tiktok = f"{title} {hashtags} #fyp #beritaviral"
    return {"instagram": instagram[:2200], "tiktok": tiktok[:2200]}


# ---------------------------------------------------------------------------
# GENERATOR
# ---------------------------------------------------------------------------
def build_prompt(candidate, details, related, categories, mode):
    related_lines = []
    for index, ref in enumerate(related, start=1):
        related_lines.append(
            f"{index}. {ref.get('title')} ({ref.get('published_at') or '-'})"
            f"\n   URL: {ref.get('url')}"
            f"\n   Ringkasan: {ref.get('excerpt', '')[:500]}"
        )
    related_text = "\n\n".join(related_lines) or "Tidak ada berita internal terkait."

    category_names = ", ".join(categories) if categories else "Jawa Tengah, Nasional, Peristiwa, Ekonomi, Wisata, Politik, Bisnis, Daerah"

    system = (
        "Anda editor senior Jateng TOP, portal berita berbahasa Indonesia yang menjunjung akurasi. "
        "Tulis ulang informasi menjadi artikel orisinal bergaya jurnalistik, SEO-friendly, dan mudah dibaca. "
        "Jangan menyalin kalimat sumber. Jangan mengarang nama, angka, kutipan, atau kronologi yang tidak didukung fakta. "
        "Jika informasi terbatas, gunakan bahasa hati-hati dan sebutkan keterbatasan data. "
        "Output HARUS JSON valid tanpa markdown dan tanpa code fence."
    )

    length_instruction = "350-500 kata" if mode == "breaking" else "600-900 kata"

    user = f"""Topik berita: {candidate.get('title')}
Sumber: {candidate.get('source_name') or '-'} ({candidate.get('source_url')})
Waktu publikasi sumber: {candidate.get('published_raw') or '-'}
Tren/traffic: {candidate.get('traffic', 0)}
Ringkasan sumber: {details.get('description') or candidate.get('description') or '-'}

Isi sumber (bahan fakta, jangan disalin mentah):
{details.get('text', '')[:5000]}

Berita internal Jateng TOP yang relevan (untuk konteks dan internal linking):
{related_text}

Kategori tersedia: {category_names}

Tugas:
1. Tulis artikel {length_instruction} dalam bahasa Indonesia.
2. Struktur HTML: paragraf pembuka, minimal 2 subjudul <h2>, konteks, dampak, dan penutup.
3. Jika ada berita internal relevan, sertakan minimal 1 tautan internal memakai URL yang diberikan dengan anchor alami.
4. Judul maksimal 110 karakter, informatif, tidak clickbait berlebihan.
5. Meta description 150-160 karakter.
6. Pilih tepat satu kategori dari daftar kategori tersedia.
7. Buat caption Instagram dan TikTok siap pakai, boleh menyertakan hashtag.
8. Jangan mencantumkan instruksi ini atau menulis "sebagai AI".

Balas dengan skema JSON:
{{"title":"...","meta_description":"...","content":"<p>...</p>","category":"...","tags":["..."],"caption_instagram":"...","caption_tiktok":"..."}}"""

    return system, user


def fallback_article(candidate, details, related):
    title = strip_html(candidate.get("title") or "", 120) or "Perkembangan Terbaru"
    source_text = details.get("text") or candidate.get("description") or ""
    sentences = re.split(r"(?<=[.!?])\s+", strip_html(source_text))
    sentences = [sentence.strip() for sentence in sentences if len(sentence.strip()) > 60]

    lead = sentences[0] if sentences else f"Informasi awal mengenai {title} masih terus berkembang."
    body_sentences = sentences[1:6]

    content = f"<p>{html.escape(lead)}</p>"
    if body_sentences:
        content += "<h2>Kronologi dan Konteks</h2>"
        for sentence in body_sentences:
            content += f"<p>{html.escape(sentence)}</p>"

    if related:
        content += "<h2>Kaitan dengan Pemberitaan Sebelumnya</h2>"
        for ref in related[:4]:
            ref_title = html.escape(ref.get("title", ""))
            ref_url = html.escape(ref.get("url", ""))
            ref_excerpt = html.escape(strip_html(ref.get("excerpt", ""), 320))
            content += f"<h3>{ref_title}</h3>"
            content += f"<p>{ref_excerpt}</p>"
            content += f"<p><a href=\"{ref_url}\">Baca pemberitaan sebelumnya</a></p>"

    content += "<h2>Yang Perlu Dicermati</h2>"
    content += (
        "<p>Informasi ini masih dapat berkembang. Pembaca disarankan memantau pembaruan resmi "
        "dan membandingkan beberapa sumber sebelum mengambil kesimpulan.</p>"
    )

    excerpt = strip_html(lead, 155)
    category = related[0].get("category") if related else ""
    captions = make_captions(
        title,
        excerpt,
        SITE_BASE_URL + "/",
        extract_keywords(candidate.get("title", ""), 4),
    )
    return {
        "title": title,
        "meta_description": excerpt,
        "content": content,
        "category": category,
        "tags": extract_keywords(candidate.get("title", ""), 4),
        "caption_instagram": captions["instagram"],
        "caption_tiktok": captions["tiktok"],
        "engine": "fallback",
    }


def generate_article(candidate, categories, mode):
    details = fetch_article_details(candidate)
    keywords = extract_keywords(candidate.get("title", "") + " " + candidate.get("trend", ""), 4)

    related = []
    try:
        context = api_call("context", method="GET", params={"q": " ".join(keywords)})
        related = context.get("items", [])
    except Exception as exc:
        log("WARN", f"Gagal mengambil konteks internal: {exc}")

    system, user = build_prompt(candidate, details, related, categories, mode)
    provider, raw = call_llm(system, user)
    article = None

    if raw:
        parsed = parse_json_response(raw)
        if parsed and parsed.get("title") and parsed.get("content"):
            article = {
                "title": str(parsed.get("title", "")).strip(),
                "meta_description": str(parsed.get("meta_description", "")).strip(),
                "content": sanitize_html(str(parsed.get("content", ""))),
                "category": str(parsed.get("category", "")).strip(),
                "tags": parsed.get("tags") if isinstance(parsed.get("tags"), list) else keywords,
                "caption_instagram": str(parsed.get("caption_instagram", "")).strip(),
                "caption_tiktok": str(parsed.get("caption_tiktok", "")).strip(),
                "engine": provider or "llm",
            }

    if article is None or not article.get("content") or not article.get("title"):
        article = fallback_article(candidate, details, related)

    article_url = SITE_BASE_URL + "/"
    if not article.get("caption_instagram") or not article.get("caption_tiktok"):
        captions = make_captions(article["title"], article["meta_description"], article_url, article.get("tags"))
        article["caption_instagram"] = article.get("caption_instagram") or captions["instagram"]
        article["caption_tiktok"] = article.get("caption_tiktok") or captions["tiktok"]

    final_url = details.get("canonical") or candidate.get("source_url", "")
    if final_url.startswith("https://news.google.com"):
        final_url = candidate.get("source_url", "")

    return {
        "title": article["title"][:255],
        "content": article["content"],
        "meta_description": article["meta_description"][:255],
        "category": article.get("category") or "",
        "categories": [article.get("category")] if article.get("category") else [],
        "tags": article.get("tags", []),
        "captions": {
            "instagram": article.get("caption_instagram", ""),
            "tiktok": article.get("caption_tiktok", ""),
        },
        "image_url": details.get("image") or candidate.get("feed_image", ""),
        "image_caption": f"Foto: {candidate.get('source_name')}" if candidate.get("source_name") else "",
        "source_url": final_url,
        "source_name": candidate.get("source_name", ""),
        "source_title": candidate.get("title", ""),
        "source_published": candidate.get("published_raw", ""),
        "related": [
            {"id": ref.get("id"), "title": ref.get("title"), "url": ref.get("url")}
            for ref in related[:4]
        ],
        "engine": article.get("engine", "fallback"),
    }


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Otomasi berita Jateng TOP")
    parser.add_argument("--mode", choices=["scheduled", "breaking"], default="scheduled")
    parser.add_argument("--dry-run", action="store_true", help="Cetak payload tanpa menyimpan draft.")
    args = parser.parse_args()

    if not SITE_BASE_URL:
        log("ERROR", "Environment SITE_BASE_URL belum diisi (contoh: https://jawatengah.top).")
        return 2
    if not AUTOMATION_TOKEN:
        log("ERROR", "Environment AUTOMATION_TOKEN belum diisi.")
        return 2

    log("INFO", f"Mulai mode {args.mode}. Sumber: Google Trends/News ID + RSS media.")
    log("INFO", "Server API: " + SITE_BASE_URL)

    try:
        ping = api_call("ping", timeout=30)
        if not ping.get("migration", {}).get("ready"):
            log("ERROR", "Migrasi database di server belum dijalankan. Buka admin/auto-news-install.php.")
            return 3
    except Exception as exc:
        log("ERROR", f"Tidak dapat menghubungi API: {exc}")
        return 1

    try:
        categories = [row["name"] for row in api_call("categories").get("items", [])]
    except Exception as exc:
        log("WARN", f"Gagal mengambil kategori: {exc}")
        categories = []

    candidates = collect_candidates(args.mode)
    if not candidates:
        log("INFO", "Tidak ada kandidat berita. Selesai tanpa membuat artikel.")
        return 0

    try:
        fresh = filter_new_candidates(candidates)
    except Exception as exc:
        log("ERROR", f"Gagal filter kandidat: {exc}")
        return 1

    limit = BREAKING_MAX if args.mode == "breaking" else NEWS_COUNT
    selected = fresh[:limit]

    if not selected:
        log("INFO", "Semua kandidat sudah pernah diberitakan. Tidak ada artikel baru.")
        return 0

    if args.mode == "scheduled" and len(selected) < NEWS_COUNT:
        log("WARN", f"Hanya {len(selected)} kandidat baru tersedia dari target {NEWS_COUNT}.")

    items = []
    for index, candidate in enumerate(selected, start=1):
        log("INFO", f"[{index}/{len(selected)}] Menulis: {candidate.get('title')}")
        try:
            items.append(generate_article(candidate, categories, args.mode))
        except Exception as exc:
            log("ERROR", f"Gagal menulis artikel: {exc}")
        time.sleep(1.5)

    if not items:
        log("ERROR", "Tidak ada artikel yang berhasil dibuat.")
        return 1

    if args.dry_run:
        log("INFO", "Dry-run: payload berikut tidak dikirim.")
        print(json.dumps({"run_type": args.mode, "items": items}, ensure_ascii=False, indent=2))
        return 0

    try:
        response = api_call(
            "store_drafts",
            method="POST",
            payload={"run_type": args.mode, "items": items},
            timeout=150,
        )
    except Exception as exc:
        log("ERROR", f"Gagal mengirim draft: {exc}")
        return 1

    created = response.get("created", 0)
    log("INFO", f"Selesai. Draft dibuat: {created} dari {len(items)} artikel.")
    for result in response.get("results", []):
        log("INFO", f"- {result.get('status')}: {result.get('title') or result.get('message')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
