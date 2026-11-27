#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - DIAGNOSTIK ENJIN FALLBACK REDDIT PS2 (EXP10)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/exp/exp10_reddit_fallback_diagnostic.py
#
# FUNGSI UJIAN:
# 1. Menguji & membandingkan 6 enjin penarikan pos Reddit PS2 secara langsung:
#    - Engine 1: Multireddit Combined JSON (www.reddit.com dengan App User-Agent)
#    - Engine 2: Old Reddit Multireddit JSON (old.reddit.com)
#    - Engine 3: Arctic Shift Reddit Mirror API (bebas sekatan 429 Reddit)
#    - Engine 4: PullPush Reddit Mirror API (sandaran kedua bebas 429)
#    - Engine 5: Multireddit Atom RSS XML Feed (.rss)
#    - Engine 6: Google News RSS Site-Search (sandaran kecemasan IP CI)
# 2. Menguji penarikan 5 komen terbaik melalui 4 lapisan fallback komen.
# 3. Mengesahkan pengekstrakan 1 hingga 4 URL gambar sah (HTTP 200) daripada calon
#    terbaik sebelum digabungkan semula ke dalam exp09 & modul pengeluaran X13.
# ==============================================================================

import html
import importlib
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import requests
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# ------------------------------------------------------------------------------
# KONFIGURASI LALUAN & IMPORT MODUL ASAS
# ------------------------------------------------------------------------------
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
LIVE_ENGINE_DIR = PROJECT_ROOT / "live-engine-01"

for p in [CURRENT_DIR, LIVE_ENGINE_DIR, PROJECT_ROOT]:
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

cfg: Any = importlib.import_module("X01_config")
TEMP_DIR: Path = Path(getattr(cfg, "TEMP_DIR", PROJECT_ROOT / "temp"))
TEMP_DIR.mkdir(parents=True, exist_ok=True)
REPORT_JSON_PATH: Path = TEMP_DIR / "exp10_reddit_fallback_report.json"

MYT = timezone(timedelta(hours=8))

# User-Agent patuh garis panduan API Reddit (mengelak sekatan 429 pada Mozilla/5.0)
REDDIT_APP_HEADERS: Dict[str, str] = {
    "User-Agent": "linux:ps2-retro-nostalgia-curator:v1.0.0 (by /u/ps2retronostalgia)",
    "Accept": "application/json, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Header pelayar untuk pengesahan imej & suapan Google News
WEB_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

CORE_PS2_SUBS: List[str] = ["ps2", "ps2homebrew", "PlayStation2"]
GENERAL_RETRO_SUBS: List[str] = ["crtgaming", "retrogaming", "gaming", "nostalgia"]

LOW_STORY_TITLE_PATTERNS = (
    r"\bhow much is this worth\b",
    r"\bprice check\b",
    r"\bis this fake\b",
    r"\bauthenticity check\b",
    r"\bfor sale\b",
    r"\bthrift haul\b",
)


def get_myt_now_str() -> str:
    now = datetime.now(tz=MYT)
    return now.strftime("%Y-%m-%d %I:%M:%S %p MYT")


def clean_reddit_text(raw_text: Any, max_chars: int = 1500) -> str:
    if not raw_text:
        return ""
    text = html.unescape(str(raw_text))
    text = re.sub(r"<[^>]+>", " ", text)
    if text.strip() in ("[removed]", "[deleted]"):
        return ""
    text = re.sub(r">!([\s\S]*?)!<", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_~`#]", "", text)
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned[:max_chars]


def extract_image_urls_safe(post_data: Dict[str, Any], max_images: int = 4) -> List[str]:
    """Mengekstrak 1-4 URL gambar daripada pos Reddit dengan perlindungan nilai null."""
    urls: List[str] = []
    seen = set()

    def _add(u: Any) -> None:
        if not u:
            return
        clean_u = html.unescape(str(u)).strip().replace("&amp;", "&")
        if clean_u.startswith(("http://", "https://")) and clean_u not in seen:
            if any(ext in clean_u.lower() for ext in (".mp4", ".gifv", "v.redd.it")):
                return
            seen.add(clean_u)
            urls.append(clean_u)

    # 1. Galeri berbilang gambar
    media_meta = post_data.get("media_metadata")
    if post_data.get("is_gallery") and isinstance(media_meta, dict):
        gallery_data = post_data.get("gallery_data") or {}
        gallery_items = gallery_data.get("items") or []
        ordered_ids = [
            it.get("media_id")
            for it in gallery_items
            if isinstance(it, dict) and it.get("media_id")
        ] or list(media_meta.keys())

        for mid in ordered_ids:
            if len(urls) >= max_images:
                break
            m_info = media_meta.get(mid) or {}
            if m_info.get("status") != "valid":
                continue
            previews = m_info.get("p") or []
            chosen_u = ""
            if isinstance(previews, list) and previews:
                last_p = previews[-1]
                if isinstance(last_p, dict):
                    chosen_u = str(last_p.get("u") or "")
            if not chosen_u:
                s_obj = m_info.get("s") or {}
                if isinstance(s_obj, dict):
                    chosen_u = str(s_obj.get("u") or "")
            _add(chosen_u)

    # 2. Pautan terus imej
    direct_url = str(post_data.get("url") or "").strip()
    if any(direct_url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")) or (
        "i.redd.it" in direct_url or "i.imgur.com" in direct_url
    ):
        _add(direct_url)

    # 3. Pratonton imej
    preview_obj = post_data.get("preview")
    if not urls and isinstance(preview_obj, dict):
        imgs = preview_obj.get("images") or []
        for img_item in imgs[:max_images]:
            if not isinstance(img_item, dict):
                continue
            res_list = img_item.get("resolutions") or []
            if isinstance(res_list, list) and res_list and isinstance(res_list[-1], dict):
                _add(res_list[-1].get("url"))
            else:
                src_obj = img_item.get("source") or {}
                if isinstance(src_obj, dict):
                    _add(src_obj.get("url"))

    return urls[:max_images]


def score_ps2_candidate(cand: Dict[str, Any]) -> float:
    score = float(cand.get("score") or 0)
    comments = float(cand.get("num_comments") or 0)
    title = str(cand.get("title") or "").lower()
    body = str(cand.get("cleaned_text") or "")
    img_cnt = len(cand.get("image_urls") or [])

    eng_pts = min(score, 2500) * 0.6 + (comments * 4.5)
    img_pts = (220.0 + (img_cnt * 45.0)) if img_cnt > 0 else 0.0
    hooks = (
        "ps2", "playstation 2", "opl", "fmcb", "opentuna", "memory card",
        "dualshock", "crt", "matrix", "modchip", "hdd", "usb", "collection",
        "childhood", "nostalgia", "restored", "setup", "favorite", "best",
    )
    hook_pts = sum(55.0 for kw in hooks if kw in title)
    body_pts = 90.0 if len(body) >= 120 else 0.0
    return round(eng_pts + img_pts + hook_pts + body_pts, 1)


def normalize_raw_submission(p: Dict[str, Any], engine_name: str, require_ps2_kw: bool = False) -> Optional[Dict[str, Any]]:
    if not isinstance(p, dict):
        return None
    title = str(p.get("title") or "").strip()
    author = str(p.get("author") or "").strip()
    if not title or p.get("over_18") or p.get("stickied") or author in ("[deleted]", "AutoModerator"):
        return None

    low_t = title.lower()
    if any(re.search(pat, low_t) for pat in LOW_STORY_TITLE_PATTERNS):
        return None

    if require_ps2_kw:
        if not any(k in low_t for k in ("ps2", "playstation 2", "dualshock 2", "fmcb", "opl", "memory card")):
            return None

    num_comments = int(p.get("num_comments") or 0)
    score = int(p.get("score") or 0)
    if num_comments < 3 and score < 15:
        return None

    img_urls = extract_image_urls_safe(p, max_images=4)
    cleaned_body = clean_reddit_text(p.get("selftext") or "")
    sub = str(p.get("subreddit") or "ps2")
    permalink = str(p.get("permalink") or "")
    if permalink and not permalink.startswith("http"):
        permalink = f"https://www.reddit.com{permalink}"

    cand = {
        "post_id": str(p.get("id") or ""),
        "subreddit": sub,
        "title": title,
        "cleaned_text": cleaned_body,
        "image_urls": img_urls,
        "image_count": len(img_urls),
        "score": score,
        "num_comments": num_comments,
        "author": author or "reddit_user",
        "permalink": permalink,
        "source_engine": engine_name,
    }
    cand["curation_score"] = score_ps2_candidate(cand)
    return cand


def _fetch_url_text(url: str, headers: Dict[str, str], timeout: float = 14.0) -> Tuple[int, str, str]:
    """Mencuba httpx dahulu dan beralih kepada requests jika gagal."""
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=timeout) as client:
            r = client.get(url)
            if r.status_code == 200 and r.text:
                return 200, r.text, "httpx"
            first_code = r.status_code
    except Exception:
        first_code = 0

    try:
        r2 = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        return r2.status_code, r2.text, "requests"
    except Exception as exc:
        return first_code, str(exc), "error"


# ==============================================================================
# 6 ENJIN FALLBACK PENARIKAN REDDIT PS2
# ==============================================================================
def engine_1_multireddit_www_json() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    t0 = time.perf_counter()
    multi = "+".join(CORE_PS2_SUBS)
    url = f"https://www.reddit.com/r/{multi}/hot.json?limit=30&raw_json=1"
    code, body, client_used = _fetch_url_text(url, REDDIT_APP_HEADERS)
    ms = round((time.perf_counter() - t0) * 1000, 1)

    posts: List[Dict[str, Any]] = []
    if code == 200 and body:
        try:
            children = json.loads(body).get("data", {}).get("children", [])
            for ch in children:
                norm = normalize_raw_submission(ch.get("data", {}), "E1_WWW_MULTI_JSON")
                if norm:
                    posts.append(norm)
        except Exception:
            pass

    return posts, {
        "engine": "1. WWW Multireddit JSON",
        "endpoint": f"www.reddit.com/r/{multi}/hot.json",
        "http": code,
        "client": client_used,
        "latency_ms": ms,
        "posts": len(posts),
        "with_img": sum(1 for x in posts if x["image_count"] > 0),
        "status": "BERJAYA" if posts else "GAGAL",
    }


def engine_2_multireddit_old_json() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    t0 = time.perf_counter()
    multi = "+".join(GENERAL_RETRO_SUBS)
    url = f"https://old.reddit.com/r/{multi}/search.json?q=PS2+OR+%22PlayStation+2%22&restrict_sr=1&sort=top&t=month&limit=30&raw_json=1"
    code, body, client_used = _fetch_url_text(url, REDDIT_APP_HEADERS)
    ms = round((time.perf_counter() - t0) * 1000, 1)

    posts: List[Dict[str, Any]] = []
    if code == 200 and body:
        try:
            children = json.loads(body).get("data", {}).get("children", [])
            for ch in children:
                norm = normalize_raw_submission(ch.get("data", {}), "E2_OLD_SEARCH_JSON", require_ps2_kw=True)
                if norm:
                    posts.append(norm)
        except Exception:
            pass

    return posts, {
        "engine": "2. Old Reddit Search JSON",
        "endpoint": f"old.reddit.com/r/{multi}/search.json",
        "http": code,
        "client": client_used,
        "latency_ms": ms,
        "posts": len(posts),
        "with_img": sum(1 for x in posts if x["image_count"] > 0),
        "status": "BERJAYA" if posts else "GAGAL",
    }


def engine_3_arctic_shift_mirror() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    t0 = time.perf_counter()
    posts: List[Dict[str, Any]] = []
    last_code = 0
    client_used = "httpx"

    for sub in ("ps2", "ps2homebrew"):
        url = f"https://arctic-shift.photon-reddit.com/api/posts/search?subreddit={sub}&limit=20&sort=desc"
        code, body, cl = _fetch_url_text(url, REDDIT_APP_HEADERS)
        last_code = code
        client_used = cl
        if code == 200 and body:
            try:
                items = json.loads(body).get("data", [])
                for it in items:
                    norm = normalize_raw_submission(it, "E3_ARCTIC_SHIFT")
                    if norm:
                        posts.append(norm)
            except Exception:
                pass
        time.sleep(0.4)

    ms = round((time.perf_counter() - t0) * 1000, 1)
    return posts, {
        "engine": "3. Arctic Shift Mirror API",
        "endpoint": "arctic-shift.photon-reddit.com/api/posts/search",
        "http": last_code,
        "client": client_used,
        "latency_ms": ms,
        "posts": len(posts),
        "with_img": sum(1 for x in posts if x["image_count"] > 0),
        "status": "BERJAYA" if posts else "GAGAL",
    }


def engine_4_pullpush_mirror() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    t0 = time.perf_counter()
    posts: List[Dict[str, Any]] = []
    last_code = 0
    client_used = "httpx"

    for sub in ("ps2", "ps2homebrew"):
        url = f"https://api.pullpush.io/reddit/search/submission/?subreddit={sub}&size=20&sort=desc&sort_type=created_utc"
        code, body, cl = _fetch_url_text(url, REDDIT_APP_HEADERS)
        last_code = code
        client_used = cl
        if code == 200 and body:
            try:
                items = json.loads(body).get("data", [])
                for it in items:
                    norm = normalize_raw_submission(it, "E4_PULLPUSH_API")
                    if norm:
                        posts.append(norm)
            except Exception:
                pass
        time.sleep(0.4)

    ms = round((time.perf_counter() - t0) * 1000, 1)
    return posts, {
        "engine": "4. PullPush Mirror API",
        "endpoint": "api.pullpush.io/reddit/search/submission",
        "http": last_code,
        "client": client_used,
        "latency_ms": ms,
        "posts": len(posts),
        "with_img": sum(1 for x in posts if x["image_count"] > 0),
        "status": "BERJAYA" if posts else "GAGAL",
    }


def engine_5_multireddit_rss_feed() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    t0 = time.perf_counter()
    multi = "+".join(CORE_PS2_SUBS)
    url = f"https://www.reddit.com/r/{multi}/hot.rss?limit=30"
    code, xml_body, client_used = _fetch_url_text(url, REDDIT_APP_HEADERS)
    ms = round((time.perf_counter() - t0) * 1000, 1)

    posts: List[Dict[str, Any]] = []
    if code == 200 and xml_body:
        try:
            root = ET.fromstring(xml_body.encode("utf-8", errors="ignore"))
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for idx, entry in enumerate(root.findall("atom:entry", ns)):
                t_el = entry.find("atom:title", ns)
                l_el = entry.find("atom:link", ns)
                c_el = entry.find("atom:content", ns)
                id_el = entry.find("atom:id", ns)
                cat_el = entry.find("atom:category", ns)

                title = t_el.text.strip() if t_el is not None and t_el.text else ""
                if not title:
                    continue
                low_t = title.lower()
                if any(re.search(pat, low_t) for pat in LOW_STORY_TITLE_PATTERNS):
                    continue

                raw_html = c_el.text if c_el is not None and c_el.text else ""
                img_urls = re.findall(r'href="(https?://i\.redd\.it/[^"]+)"', raw_html, re.I)
                if not img_urls:
                    img_urls = re.findall(r'src="([^"]+\.(?:jpg|jpeg|png|webp))"', raw_html, re.I)
                img_urls = list(dict.fromkeys(html.unescape(u).replace("&amp;", "&") for u in img_urls))[:4]

                sub_name = cat_el.attrib.get("term", "ps2") if cat_el is not None else "ps2"
                raw_id = id_el.text.split("_")[-1] if id_el is not None and id_el.text else f"rss_{idx}"

                cand = {
                    "post_id": raw_id,
                    "subreddit": sub_name,
                    "title": title,
                    "cleaned_text": clean_reddit_text(raw_html),
                    "image_urls": img_urls,
                    "image_count": len(img_urls),
                    "score": max(300 - (idx * 10), 30),
                    "num_comments": 15,
                    "author": "reddit_user",
                    "permalink": l_el.attrib.get("href", "") if l_el is not None else "",
                    "source_engine": "E5_MULTI_RSS_XML",
                }
                cand["curation_score"] = score_ps2_candidate(cand)
                posts.append(cand)
        except Exception:
            pass

    return posts, {
        "engine": "5. Multireddit RSS XML",
        "endpoint": f"www.reddit.com/r/{multi}/hot.rss",
        "http": code,
        "client": client_used,
        "latency_ms": ms,
        "posts": len(posts),
        "with_img": sum(1 for x in posts if x["image_count"] > 0),
        "status": "BERJAYA" if posts else "GAGAL",
    }


def engine_6_google_news_reddit_rss() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    t0 = time.perf_counter()
    url = (
        "https://news.google.com/rss/search?"
        "q=site:reddit.com/r/ps2+OR+site:reddit.com/r/ps2homebrew&hl=en-US&gl=US&ceid=US:en"
    )
    code, xml_body, client_used = _fetch_url_text(url, WEB_HEADERS)
    ms = round((time.perf_counter() - t0) * 1000, 1)

    posts: List[Dict[str, Any]] = []
    if code == 200 and xml_body:
        try:
            root = ET.fromstring(xml_body.encode("utf-8", errors="ignore"))
            for idx, item in enumerate(root.findall(".//item")[:20]):
                t_el = item.find("title")
                l_el = item.find("link")
                d_el = item.find("description")
                title = (t_el.text or "").strip() if t_el is not None else ""
                link = (l_el.text or "").strip() if l_el is not None else ""
                desc = (d_el.text or "").strip() if d_el is not None else ""
                if not title or "reddit.com" not in link:
                    continue

                title_clean = re.sub(r"\s*-\s*Reddit\s*$", "", title, flags=re.I).strip()
                sub_m = re.search(r"reddit\.com/r/([^/]+)", link, re.I)
                id_m = re.search(r"/comments/([a-z0-9]+)/", link, re.I)

                cand = {
                    "post_id": id_m.group(1) if id_m else f"gnews_{idx}",
                    "subreddit": sub_m.group(1) if sub_m else "ps2",
                    "title": title_clean,
                    "cleaned_text": clean_reddit_text(desc),
                    "image_urls": [],
                    "image_count": 0,
                    "score": 120,
                    "num_comments": 15,
                    "author": "reddit_user",
                    "permalink": link,
                    "source_engine": "E6_GOOGLE_NEWS_RSS",
                }
                cand["curation_score"] = score_ps2_candidate(cand)
                posts.append(cand)
        except Exception:
            pass

    return posts, {
        "engine": "6. Google News Reddit RSS",
        "endpoint": "news.google.com/rss/search (site:reddit.com/r/ps2)",
        "http": code,
        "client": client_used,
        "latency_ms": ms,
        "posts": len(posts),
        "with_img": 0,
        "status": "BERJAYA" if posts else "GAGAL",
    }


# ==============================================================================
# 4-LAPISAN PENARIK KOMEN & PENGESAH URL IMEJ
# ==============================================================================
def fetch_comments_4_tier(subreddit: str, post_id: str) -> Tuple[List[str], str]:
    if not post_id or post_id.startswith(("rss_", "gnews_")):
        return [], "TIADA_ID"

    # Tier 1 & 2: www.reddit.com & old.reddit.com JSON
    for label, host in (("WWW_COMMENTS_JSON", "www.reddit.com"), ("OLD_COMMENTS_JSON", "old.reddit.com")):
        url = f"https://{host}/r/{subreddit}/comments/{post_id}.json?sort=top&limit=10&raw_json=1"
        code, body, _ = _fetch_url_text(url, REDDIT_APP_HEADERS, timeout=12.0)
        if code == 200 and body:
            try:
                parsed = json.loads(body)
                if isinstance(parsed, list) and len(parsed) >= 2:
                    children = parsed[1].get("data", {}).get("children", [])
                    out: List[str] = []
                    for ch in children:
                        if ch.get("kind") != "t1":
                            continue
                        cd = ch.get("data") or {}
                        if str(cd.get("author") or "") in ("[deleted]", "AutoModerator"):
                            continue
                        txt = clean_reddit_text(cd.get("body") or "", max_chars=240)
                        if len(txt) >= 18:
                            out.append(f"[{int(cd.get('score') or 0)} upvotes] {txt}")
                        if len(out) >= 5:
                            break
                    if out:
                        return out, label
            except Exception:
                pass
        time.sleep(0.5)

    # Tier 3: Arctic Shift Comments Mirror API
    as_url = f"https://arctic-shift.photon-reddit.com/api/comments/search?link_id=t3_{post_id}&limit=10"
    code_as, body_as, _ = _fetch_url_text(as_url, REDDIT_APP_HEADERS, timeout=12.0)
    if code_as == 200 and body_as:
        try:
            items = json.loads(body_as).get("data", [])
            out_as: List[str] = []
            for cd in items:
                if str(cd.get("author") or "") in ("[deleted]", "AutoModerator"):
                    continue
                txt = clean_reddit_text(cd.get("body") or "", max_chars=240)
                if len(txt) >= 18:
                    out_as.append(f"[{int(cd.get('score') or 0)} upvotes] {txt}")
                if len(out_as) >= 5:
                    break
            if out_as:
                return out_as, "ARCTIC_SHIFT_COMMENTS"
        except Exception:
            pass

    return [], "GAGAL_SEMUA_TIER_KOMEN"


def verify_image_url_live(img_url: str) -> Tuple[bool, str]:
    headers = dict(WEB_HEADERS)
    headers["Referer"] = "https://www.reddit.com/"
    try:
        r = requests.get(img_url, headers=headers, timeout=15, stream=True, allow_redirects=True)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        sample = r.raw.read(2048)
        if len(sample) < 512:
            return False, "Saiz fail terlalu kecil"
        return True, f"HTTP 200 ({r.headers.get('Content-Type', 'image')})"
    except Exception as exc:
        return False, str(exc)


def main() -> None:
    console.print(
        Panel.fit(
            "[bold cyan]PS2-RETRO-NOSTALGIA :: UJIAN 6-ENJIN FALLBACK REDDIT PS2 (EXP10)[/bold cyan]\n"
            f"[dim]Masa MYT: {get_myt_now_str()} | User-Agent Patuh API Reddit + Mirror APIs[/dim]",
            border_style="cyan",
        )
    )

    engines = [
        engine_1_multireddit_www_json,
        engine_2_multireddit_old_json,
        engine_3_arctic_shift_mirror,
        engine_4_pullpush_mirror,
        engine_5_multireddit_rss_feed,
        engine_6_google_news_reddit_rss,
    ]

    all_posts_by_id: Dict[str, Dict[str, Any]] = {}
    benchmark_rows: List[Dict[str, Any]] = []

    with console.status("[bold cyan]Menguji kesemua 6 enjin fallback Reddit PS2 secara berperingkat...[/bold cyan]"):
        for idx, eng_fn in enumerate(engines):
            posts, stats = eng_fn()
            benchmark_rows.append(stats)
            for p in posts:
                pid = p.get("post_id") or p.get("title")
                if pid not in all_posts_by_id:
                    all_posts_by_id[pid] = p
                else:
                    # Gabungkan imej jika enjin lain menjumpai gambar lebih banyak
                    if p["image_count"] > all_posts_by_id[pid]["image_count"]:
                        all_posts_by_id[pid] = p
            if idx < len(engines) - 1:
                time.sleep(1.2)  # Jeda selamat 1.2s antara enjin supaya tidak kena 429

    # 1. Paparan Jadual Perbandingan 6 Enjin Fallback
    eng_tbl = Table(title="1. Keputusan Ujian 6 Enjin Fallback Reddit PS2", box=box.ROUNDED, header_style="bold green")
    eng_tbl.add_column("Enjin Fallback", style="bold white")
    eng_tbl.add_column("HTTP", justify="center", style="yellow")
    eng_tbl.add_column("Klien", justify="center", style="dim")
    eng_tbl.add_column("Latensi", justify="right", style="cyan")
    eng_tbl.add_column("Pos Sah", justify="right", style="white")
    eng_tbl.add_column("Ada Gambar", justify="right", style="green")
    eng_tbl.add_column("Status", justify="center")

    working_engines = 0
    for st in benchmark_rows:
        if st["status"] == "BERJAYA":
            working_engines += 1
        badge = "[bold green]BERJAYA[/bold green]" if st["status"] == "BERJAYA" else "[bold red]GAGAL[/bold red]"
        eng_tbl.add_row(
            st["engine"],
            str(st["http"]),
            st["client"],
            f"{st['latency_ms']} ms",
            str(st["posts"]),
            str(st["with_img"]),
            badge,
        )
    console.print(eng_tbl)

    merged_posts = list(all_posts_by_id.values())
    if not merged_posts:
        console.print("[bold red]❌ Semua 6 enjin gagal menarik data Reddit. Sila semak sambungan rangkaian.[/bold red]")
        sys.exit(1)

    merged_posts.sort(
        key=lambda x: (1 if x["image_count"] > 0 else 0, x["curation_score"], x["num_comments"]),
        reverse=True,
    )

    # 2. Paparan Top 8 Calon Unik Terkumpul
    cand_tbl = Table(
        title=f"2. Top 8 Calon Unik PS2 Terkumpul ({len(merged_posts)} Pos Unik | {working_engines}/6 Enjin Aktif)",
        box=box.ROUNDED,
        header_style="bold yellow",
    )
    cand_tbl.add_column("#", justify="center", style="cyan", width=3)
    cand_tbl.add_column("Subreddit", style="magenta", width=15)
    cand_tbl.add_column("Enjin Sumber", style="cyan", width=18)
    cand_tbl.add_column("⬆️ / 💬", justify="center", style="yellow", width=12)
    cand_tbl.add_column("Imej", justify="center", style="green", width=7)
    cand_tbl.add_column("Tajuk Pos", style="white")

    for idx, c in enumerate(merged_posts[:8], 1):
        short_t = c["title"] if len(c["title"]) <= 54 else f"{c['title'][:51]}..."
        cand_tbl.add_row(
            str(idx),
            f"r/{c['subreddit']}",
            c["source_engine"],
            f"{c['score']} / {c['num_comments']}",
            f"{c['image_count']} img",
            short_t,
        )
    console.print(cand_tbl)

    # 3. Uji Penarikan Komen & Pengesahan Gambar pada Calon #1
    top_pick = merged_posts[0]
    with console.status(f"[bold cyan]Menguji 4-lapisan penarik komen & pengesahan gambar untuk r/{top_pick['subreddit']}...[/bold cyan]"):
        comments, comment_engine = fetch_comments_4_tier(top_pick["subreddit"], top_pick["post_id"])
        img_checks: List[Tuple[str, bool, str]] = []
        for u in top_pick["image_urls"][:4]:
            ok_i, msg_i = verify_image_url_live(u)
            img_checks.append((u, ok_i, msg_i))

    detail_lines = [
        f"[bold yellow]Subreddit:[/bold yellow] r/{top_pick['subreddit']} | [bold yellow]Enjin:[/bold yellow] {top_pick['source_engine']}",
        f"[bold white]Tajuk:[/bold white] {top_pick['title']}",
        f"[bold cyan]Enjin Komen:[/bold cyan] {comment_engine} ({len(comments)} komen ditarik)",
    ]
    for cm in comments[:3]:
        detail_lines.append(f"  [dim]• {cm[:130]}...[/dim]")

    detail_lines.append(f"[bold green]Pengesahan Gambar ({len(img_checks)} URL):[/bold green]")
    for u, ok_i, msg_i in img_checks:
        st_txt = "[green]SAH[/green]" if ok_i else "[red]GAGAL[/red]"
        detail_lines.append(f"  • {st_txt} ({msg_i}) -> [dim]{u[:75]}...[/dim]")

    console.print(Panel("\n".join(detail_lines), title="3. Pengesahan End-to-End Calon #1", border_style="green"))

    REPORT_JSON_PATH.write_text(
        json.dumps(
            {
                "tested_at_myt": get_myt_now_str(),
                "working_engines_count": working_engines,
                "engine_benchmarks": benchmark_rows,
                "top_pick": top_pick,
                "sample_comments": comments,
                "comment_engine": comment_engine,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    console.print(f"[dim]Laporan diagnostik disimpan di: {REPORT_JSON_PATH}[/dim]")


if __name__ == "__main__":
    main()