#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - DIAGNOSTIK REDDIT PS2 + VISION OCR + AI (EXP09)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/exp/exp09_reddit_ps2_vision_persona_debug.py
#
# FUNGSI UJIAN (KEMAS KINI PASCA-EXP10):
# 1. Menggunakan enjin hibrid 6-lapisan yang terbukti berjaya dalam EXP10:
#    - Enjin 1: Arctic Shift Mirror API (posts + top comments, bebas 429)
#    - Enjin 2: PullPush Mirror API (sandaran kedua bebas 429)
#    - Enjin 3 & 4: WWW & Old Multireddit JSON API (App User-Agent)
#    - Enjin 5 & 6: Multireddit Atom RSS XML & Google News Reddit RSS
# 2. Muat turun 1 hingga 4 gambar Reddit dengan 3-lapisan anti-403
#    (httpx HTTP/2 -> preview.redd.it -> wsrv.nl CDN proxy) + sandaran Unsplash.
# 3. Analisis Vision + OCR (< 30 KB) melalui X07_vision_ocr_analyzer.py.
# 4. Olahan cerita Persona AI ('admin'/'min', lelaki 35 tahun, 500-640 aksara,
#    sifar emoji) + padanan produk Shopee dari data/ps2_shopee_products.db.
# 5. Penghantaran 1-4 gambar & laporan lengkap ke Channel Telegram (X12), serta
#    rekod anti-ulangan supaya ujian 3-4 kali berturut-turut sentiasa dapat pos baru.
# ==============================================================================

import argparse
import html
import importlib
import json
import os
import random
import re
import sqlite3
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx
import psutil
import requests
from PIL import Image
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# ------------------------------------------------------------------------------
# KONFIGURASI LALUAN & IMPORT SEMUA MODUL TERAS (X01 - X12)
# ------------------------------------------------------------------------------
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
LIVE_ENGINE_DIR = PROJECT_ROOT / "live-engine-01"

for p in [CURRENT_DIR, LIVE_ENGINE_DIR, PROJECT_ROOT]:
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

IMPORT_AUDIT: List[Tuple[str, bool, str]] = []


def _safe_import(mod_name: str) -> Any:
    try:
        mod = importlib.import_module(mod_name)
        IMPORT_AUDIT.append((mod_name, True, "OK"))
        return mod
    except Exception as exc:
        IMPORT_AUDIT.append((mod_name, False, str(exc)))
        raise


cfg: Any = _safe_import("X01_config")
_ledger_mod: Any = _safe_import("X02_sqlite_ledger")
_b2_mod: Any = _safe_import("X03_b2_proxy_client")
_redis_mod: Any = _safe_import("X04_redis_sharded")
_vector_mod: Any = _safe_import("X05_vector_sharded")
_llm_mod: Any = _safe_import("X06_local_llm_engine")
_vision_mod: Any = _safe_import("X07_vision_ocr_analyzer")
_tg_cfg_mod: Any = _safe_import("X10_telegram_config")
_tg_not_mod: Any = _safe_import("X12_telegram_notifier")

ledger_db: Any = getattr(_ledger_mod, "ledger_db")
b2_proxy: Any = getattr(_b2_mod, "b2_proxy")
redis_sharded: Any = getattr(_redis_mod, "redis_sharded")
vector_sharded: Any = getattr(_vector_mod, "vector_sharded")
llm_engine: Any = getattr(_llm_mod, "llm_engine")
clean_and_scrub_bm_story: Any = getattr(_llm_mod, "clean_and_scrub_bm_story")
validate_story_quality: Any = getattr(_llm_mod, "validate_story_quality")
vision_analyzer: Any = getattr(_vision_mod, "vision_analyzer")
tg_config: Any = getattr(_tg_cfg_mod, "tg_config")
tg_notifier: Any = getattr(_tg_not_mod, "tg_notifier")


# ==============================================================================
# KONFIGURASI SUBREDDIT PS2 & TETAPAN UJIAN EXP09
# ==============================================================================
MYT = timezone(timedelta(hours=8))

DATA_DIR: Path = Path(getattr(cfg, "DATA_DIR", PROJECT_ROOT / "data"))
TEMP_DIR: Path = Path(getattr(cfg, "TEMP_DIR", PROJECT_ROOT / "temp"))
WORK_MEDIA_DIR: Path = TEMP_DIR / "exp09_reddit_media"
PREVIEW_JSON_PATH: Path = TEMP_DIR / "exp09_last_preview.json"
SEEN_IDS_PATH: Path = TEMP_DIR / "exp09_seen_reddit_ids.json"
SHOPEE_DB_PATH: Path = DATA_DIR / "ps2_shopee_products.db"
GAMES_DB_PATH: Path = DATA_DIR / "ps2_games_catalog.db"

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

REDDIT_APP_HEADERS: Dict[str, str] = {
    "User-Agent": "linux:ps2-retro-nostalgia-curator:v1.0.0 (by /u/ps2retronostalgia)",
    "Accept": "application/json, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

IMAGE_FETCH_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/jpeg,image/png,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

UNSPLASH_PS2_FALLBACK_QUERIES: List[str] = [
    "playstation dualshock controller",
    "playstation retro console gaming",
    "retro crt tv video game",
    "usb flash drive hard drive desk",
]


def get_myt_context() -> Dict[str, Any]:
    now = datetime.now(tz=MYT)
    days_bm = ["Isnin", "Selasa", "Rabu", "Khamis", "Jumaat", "Sabtu", "Ahad"]
    months_bm = [
        "Januari", "Februari", "Mac", "April", "Mei", "Jun",
        "Julai", "Ogos", "September", "Oktober", "November", "Disember",
    ]
    h = now.hour
    if 5 <= h < 12:
        period = "Pagi"
        mood_style = "TECH_INSIDER (Segar, perkongsian ilmu perkakasan & fakta menarik PS2)"
        forbidden_times = "petang ni, malam ni, tengah malam"
    elif 12 <= h < 15:
        period = "Tengah Hari"
        mood_style = "DEBATE_HOST (Santai waktu rehat, kupasan topik hangat komuniti)"
        forbidden_times = "pagi yang cerah, malam ni, tengah malam"
    elif 15 <= h < 19:
        period = "Petang"
        mood_style = "COLLECTOR_SHOWCASE (Santai petang lepas kerja, mengulas setup & koleksi)"
        forbidden_times = "pagi yang cerah, selamat pagi, pagi ini, tengah malam"
    else:
        period = "Malam"
        mood_style = "STORYTELLER_NOSTALGIA (Tenang malam, penceritaan mendalam mengimbau zaman 2000-an)"
        forbidden_times = "pagi yang cerah, selamat pagi, pagi ini, tengah hari"

    full_str = (
        f"{days_bm[now.weekday()]}, {now.day:02d} {months_bm[now.month - 1]} {now.year} | "
        f"{now.strftime('%I:%M %p')} MYT (Waktu {period})"
    )
    return {
        "now_iso": now.isoformat(),
        "now_epoch": int(now.timestamp()),
        "period": period,
        "mood_style": mood_style,
        "forbidden_times": forbidden_times,
        "full_timestamp": full_str,
    }


def load_seen_reddit_ids(reset: bool = False) -> set:
    if reset and SEEN_IDS_PATH.exists():
        SEEN_IDS_PATH.unlink(missing_ok=True)
        return set()
    if SEEN_IDS_PATH.exists():
        try:
            data = json.loads(SEEN_IDS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return set(str(x) for x in data)
        except Exception:
            pass
    return set()


def save_seen_reddit_id(post_id: str) -> None:
    if not post_id:
        return
    seen = load_seen_reddit_ids(reset=False)
    seen.add(str(post_id))
    SEEN_IDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_IDS_PATH.write_text(json.dumps(sorted(seen)[-200:], indent=2), encoding="utf-8")


# ==============================================================================
# 1. ENJIN PENARIKAN REDDIT 6-LAPISAN (ARCTIC SHIFT + PULLPUSH + REDDIT + RSS)
# ==============================================================================
def _fetch_url_text(url: str, headers: Dict[str, str], timeout: float = 15.0) -> Tuple[int, str, str]:
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


def clean_reddit_body(raw_text: Any, max_chars: int = 1600) -> str:
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


def extract_reddit_image_candidates(post_data: Dict[str, Any], max_images: int = 4) -> List[Dict[str, Any]]:
    """
    Mengekstrak sehingga 4 slot gambar daripada pos Reddit, di mana setiap slot
    menyimpan senarai URL alternatif (direct URL + preview URL) supaya jika
    `i.redd.it` disekat 403, pemuat turun boleh terus mencuba `preview.redd.it`.
    """
    slots: List[Dict[str, Any]] = []
    seen_primary = set()

    def _add_slot(primary_u: Any, backup_u: Any = "") -> None:
        p_clean = html.unescape(str(primary_u or "")).strip().replace("&amp;", "&")
        b_clean = html.unescape(str(backup_u or "")).strip().replace("&amp;", "&")
        if not p_clean.startswith(("http://", "https://")):
            return
        if any(ext in p_clean.lower() for ext in (".mp4", ".gifv", "v.redd.it")):
            return
        if p_clean in seen_primary:
            return
        seen_primary.add(p_clean)
        alt_list = [p_clean]
        if b_clean.startswith(("http://", "https://")) and b_clean != p_clean:
            alt_list.append(b_clean)
        slots.append({"urls": alt_list})

    # 1. Galeri berbilang gambar (`is_gallery` + `media_metadata`)
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
            if len(slots) >= max_images:
                break
            m_info = media_meta.get(mid) or {}
            if m_info.get("status") != "valid":
                continue
            previews = m_info.get("p") or []
            prev_u = ""
            if isinstance(previews, list) and previews and isinstance(previews[-1], dict):
                prev_u = str(previews[-1].get("u") or "")
            src_u = str((m_info.get("s") or {}).get("u") or "")
            _add_slot(prev_u or src_u, src_u if prev_u else "")

    # 2. Pautan terus imej + pratonton CDN Reddit (`preview.images`)
    direct_url = str(post_data.get("url") or "").strip()
    preview_backup = ""
    preview_obj = post_data.get("preview")
    if isinstance(preview_obj, dict):
        imgs = preview_obj.get("images") or []
        if imgs and isinstance(imgs[0], dict):
            res_list = imgs[0].get("resolutions") or []
            if isinstance(res_list, list) and res_list and isinstance(res_list[-1], dict):
                preview_backup = str(res_list[-1].get("url") or "")
            if not preview_backup:
                preview_backup = str((imgs[0].get("source") or {}).get("url") or "")

    if any(direct_url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")) or (
        "i.redd.it" in direct_url or "i.imgur.com" in direct_url
    ):
        _add_slot(direct_url, preview_backup)
    elif preview_backup and not slots:
        _add_slot(preview_backup)

    return slots[:max_images]


def score_ps2_candidate(cand: Dict[str, Any]) -> float:
    score = float(cand.get("score") or 0)
    comments = float(cand.get("num_comments") or 0)
    title = str(cand.get("title") or "").lower()
    body = str(cand.get("cleaned_text") or "")
    img_cnt = int(cand.get("image_count") or 0)

    eng_pts = min(score, 2500) * 0.6 + (comments * 4.5)
    img_pts = (220.0 + (img_cnt * 45.0)) if img_cnt > 0 else 0.0
    hooks = (
        "ps2", "playstation 2", "opl", "fmcb", "opentuna", "memory card",
        "dualshock", "crt", "matrix", "modchip", "hdd", "usb", "collection",
        "childhood", "nostalgia", "restored", "setup", "favorite", "best",
    )
    hook_pts = sum(55.0 for kw in hooks if kw in title)
    body_pts = 90.0 if len(body) >= 120 else 0.0
    return round(eng_pts + image_pts_calc(img_pts) + hook_pts + body_pts, 1)


def image_pts_calc(val: float) -> float:
    return val


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

    img_slots = extract_reddit_image_candidates(p, max_images=4)
    cleaned_body = clean_reddit_body(p.get("selftext") or "")
    sub = str(p.get("subreddit") or "ps2")
    permalink = str(p.get("permalink") or "")
    if permalink and not permalink.startswith("http"):
        permalink = f"https://www.reddit.com{permalink}"

    cand = {
        "post_id": str(p.get("id") or ""),
        "subreddit": sub,
        "title": title,
        "cleaned_text": cleaned_body,
        "image_slots": img_slots,
        "image_count": len(img_slots),
        "score": score,
        "num_comments": num_comments,
        "author": author or "reddit_user",
        "permalink": permalink,
        "source_engine": engine_name,
    }
    cand["curation_score"] = score_ps2_candidate(cand)
    return cand


# --- ENJIN 1: ARCTIC SHIFT MIRROR API (TERBUKTI HTTP 200 DALAM EXP10) ---
def engine_arctic_shift() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    t0 = time.perf_counter()
    posts: List[Dict[str, Any]] = []
    last_code = 0
    client_used = "httpx"

    for sub in CORE_PS2_SUBS:
        url = f"https://arctic-shift.photon-reddit.com/api/posts/search?subreddit={sub}&limit=25&sort=desc"
        code, body, cl = _fetch_url_text(url, REDDIT_APP_HEADERS, timeout=16.0)
        last_code = code
        client_used = cl
        if code == 200 and body:
            try:
                items = json.loads(body).get("data", [])
                for it in items:
                    norm = normalize_raw_submission(it, "E1_ARCTIC_SHIFT")
                    if norm:
                        posts.append(norm)
            except Exception:
                pass
        time.sleep(0.35)

    ms = round((time.perf_counter() - t0) * 1000, 1)
    return posts, {
        "engine": "1. Arctic Shift Mirror API",
        "http": last_code,
        "client": client_used,
        "latency_ms": ms,
        "posts": len(posts),
        "with_img": sum(1 for x in posts if x["image_count"] > 0),
        "status": "BERJAYA" if posts else "GAGAL",
    }


# --- ENJIN 2: PULLPUSH MIRROR API (TERBUKTI HTTP 200 DALAM EXP10) ---
def engine_pullpush_mirror() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    t0 = time.perf_counter()
    posts: List[Dict[str, Any]] = []
    last_code = 0
    client_used = "httpx"

    for sub in ("ps2", "ps2homebrew"):
        url = f"https://api.pullpush.io/reddit/search/submission/?subreddit={sub}&size=25&sort=desc&sort_type=created_utc"
        code, body, cl = _fetch_url_text(url, REDDIT_APP_HEADERS, timeout=16.0)
        last_code = code
        client_used = cl
        if code == 200 and body:
            try:
                items = json.loads(body).get("data", [])
                for it in items:
                    norm = normalize_raw_submission(it, "E2_PULLPUSH_API")
                    if norm:
                        posts.append(norm)
            except Exception:
                pass
        time.sleep(0.35)

    ms = round((time.perf_counter() - t0) * 1000, 1)
    return posts, {
        "engine": "2. PullPush Mirror API",
        "http": last_code,
        "client": client_used,
        "latency_ms": ms,
        "posts": len(posts),
        "with_img": sum(1 for x in posts if x["image_count"] > 0),
        "status": "BERJAYA" if posts else "GAGAL",
    }


# --- ENJIN 3 & 4: MULTIREDDIT JSON (WWW & OLD) ---
def engine_multireddit_json() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    t0 = time.perf_counter()
    multi = "+".join(CORE_PS2_SUBS)
    posts: List[Dict[str, Any]] = []
    last_code = 0
    client_used = "httpx"

    for tag, host in (("E3_WWW_JSON", "www.reddit.com"), ("E3_OLD_JSON", "old.reddit.com")):
        url = f"https://{host}/r/{multi}/hot.json?limit=25&raw_json=1"
        code, body, cl = _fetch_url_text(url, REDDIT_APP_HEADERS, timeout=12.0)
        last_code = code
        client_used = cl
        if code == 200 and body:
            try:
                children = json.loads(body).get("data", {}).get("children", [])
                for ch in children:
                    norm = normalize_raw_submission(ch.get("data", {}), tag)
                    if norm:
                        posts.append(norm)
                if posts:
                    break
            except Exception:
                pass

    ms = round((time.perf_counter() - t0) * 1000, 1)
    return posts, {
        "engine": "3. Direct Multireddit JSON",
        "http": last_code,
        "client": client_used,
        "latency_ms": ms,
        "posts": len(posts),
        "with_img": sum(1 for x in posts if x["image_count"] > 0),
        "status": "BERJAYA" if posts else "GAGAL",
    }


# --- ENJIN 5: MULTIREDDIT ATOM RSS XML FALLBACK ---
def engine_multireddit_rss() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    t0 = time.perf_counter()
    multi = "+".join(CORE_PS2_SUBS)
    url = f"https://www.reddit.com/r/{multi}/hot.rss?limit=25"
    code, xml_body, client_used = _fetch_url_text(url, REDDIT_APP_HEADERS, timeout=12.0)
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
                raw_html = c_el.text if c_el is not None and c_el.text else ""
                img_urls = re.findall(r'href="(https?://i\.redd\.it/[^"]+)"', raw_html, re.I)
                if not img_urls:
                    img_urls = re.findall(r'src="([^"]+\.(?:jpg|jpeg|png|webp))"', raw_html, re.I)
                slots = [{"urls": [html.unescape(u).replace("&amp;", "&")]} for u in dict.fromkeys(img_urls)][:4]

                cand = {
                    "post_id": id_el.text.split("_")[-1] if id_el is not None and id_el.text else f"rss_{idx}",
                    "subreddit": cat_el.attrib.get("term", "ps2") if cat_el is not None else "ps2",
                    "title": title,
                    "cleaned_text": clean_reddit_body(raw_html),
                    "image_slots": slots,
                    "image_count": len(slots),
                    "score": max(250 - (idx * 10), 25),
                    "num_comments": 12,
                    "author": "reddit_user",
                    "permalink": l_el.attrib.get("href", "") if l_el is not None else "",
                    "source_engine": "E4_MULTI_RSS_XML",
                }
                cand["curation_score"] = score_ps2_candidate(cand)
                posts.append(cand)
        except Exception:
            pass

    return posts, {
        "engine": "4. Multireddit Atom RSS",
        "http": code,
        "client": client_used,
        "latency_ms": ms,
        "posts": len(posts),
        "with_img": sum(1 for x in posts if x["image_count"] > 0),
        "status": "BERJAYA" if posts else "GAGAL",
    }


def fetch_top_reddit_comments(subreddit: str, post_id: str, max_comments: int = 5) -> Tuple[List[str], str]:
    """Menarik 5 komen teratas melalui Arctic Shift Comments API + Reddit JSON fallback."""
    if not post_id or post_id.startswith("rss_"):
        return [], "TIADA_ID"

    # 1. Arctic Shift Comments API (terbukti HTTP 200 dalam EXP10)
    as_url = f"https://arctic-shift.photon-reddit.com/api/comments/search?link_id=t3_{post_id}&limit=12"
    code_as, body_as, _ = _fetch_url_text(as_url, REDDIT_APP_HEADERS, timeout=14.0)
    if code_as == 200 and body_as:
        try:
            items = json.loads(body_as).get("data", [])
            out_as: List[str] = []
            for cd in items:
                if str(cd.get("author") or "") in ("[deleted]", "AutoModerator"):
                    continue
                txt = clean_reddit_body(cd.get("body") or "", max_chars=250)
                if len(txt) >= 18:
                    out_as.append(f"[{int(cd.get('score') or 0)} upvotes] {txt}")
                if len(out_as) >= max_comments:
                    break
            if out_as:
                return out_as, "ARCTIC_SHIFT_COMMENTS"
        except Exception:
            pass

    # 2. Fallback: Reddit JSON Comments
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
                        txt = clean_reddit_body(cd.get("body") or "", max_chars=250)
                        if len(txt) >= 18:
                            out.append(f"[{int(cd.get('score') or 0)} upvotes] {txt}")
                        if len(out) >= max_comments:
                            break
                    if out:
                        return out, label
            except Exception:
                pass

    return [], "TIADA_KOMEN_DITEMUI"


# ==============================================================================
# 2. MUAT TURUN 1-4 GAMBAR REDDIT ANTI-403 + SANDARAN UNSPLASH & OCR (X07)
# ==============================================================================
def _download_single_image_resilient(candidate_urls: List[str]) -> Optional[bytes]:
    """
    Memuat turun 1 gambar menggunakan 3 teknik bertingkat bagi mengatasi sekatan
    `i.redd.it HTTP 403`:
      1. httpx HTTP/2 dengan Accept: image/* (tanpa Referer bercanggah)
      2. requests HTTP/1.1 dengan Referer: https://www.reddit.com/
      3. Proksi imej CDN global wsrv.nl (memintas sekatan hotlink i.redd.it)
    """
    for raw_u in candidate_urls:
        clean_u = html.unescape(raw_u).strip().replace("&amp;", "&")
        if not clean_u.startswith(("http://", "https://")):
            continue

        # Kaedah 1: httpx HTTP/2 terus
        try:
            with httpx.Client(headers=IMAGE_FETCH_HEADERS, follow_redirects=True, timeout=18.0) as client:
                r = client.get(clean_u)
                if r.status_code == 200 and len(r.content) > 2048:
                    return r.content
        except Exception:
            pass

        # Kaedah 2: requests dengan Referer Reddit
        try:
            h2 = dict(IMAGE_FETCH_HEADERS)
            h2["Referer"] = "https://www.reddit.com/"
            r2 = requests.get(clean_u, headers=h2, timeout=18.0, allow_redirects=True)
            if r2.status_code == 200 and len(r2.content) > 2048:
                return r2.content
        except Exception:
            pass

        # Kaedah 3: Proksi CDN wsrv.nl (sangat berkesan untuk URL i.redd.it)
        try:
            proxy_u = f"https://wsrv.nl/?url={quote(clean_u, safe='')}&w=1280&output=jpg"
            with httpx.Client(headers=IMAGE_FETCH_HEADERS, follow_redirects=True, timeout=22.0) as client:
                r3 = client.get(proxy_u)
                if r3.status_code == 200 and len(r3.content) > 2048:
                    return r3.content
        except Exception:
            pass

    return None


def download_reddit_images_to_local(
    image_slots: List[Dict[str, Any]],
    post_id: str,
    max_images: int = 4,
) -> List[Path]:
    WORK_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    saved_paths: List[Path] = []

    for idx, slot in enumerate(image_slots[:max_images], 1):
        urls = slot.get("urls") or []
        img_bytes = _download_single_image_resilient(urls)
        if not img_bytes:
            continue
        try:
            im = Image.open(BytesIO(img_bytes)).convert("RGB")
            if max(im.size) > 1280:
                im.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
            out_file = WORK_MEDIA_DIR / f"reddit_{post_id}_{idx:02d}.jpg"
            im.save(out_file, format="JPEG", quality=88, optimize=True)
            saved_paths.append(out_file)
        except Exception:
            continue

    return saved_paths


def fetch_unsplash_fallback_images(topic_title: str, count: int = 2) -> List[Path]:
    WORK_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    api_key = getattr(cfg, "PS2N_UNSPLASH_ACCESS_KEY", "") or os.getenv("PS2N_UNSPLASH_ACCESS_KEY", "")
    if not api_key:
        local_pool = PROJECT_ROOT / "temp" / "test_media_pool_50" / "raw"
        if local_pool.exists():
            return sorted(local_pool.glob("*.jpg"))[:count]
        return []

    low_t = topic_title.lower()
    if any(w in low_t for w in ("usb", "hdd", "hard drive", "opl", "fmcb", "pendrive")):
        query = "usb flash drive hard drive desk"
    elif any(w in low_t for w in ("crt", "tv", "setup", "console")):
        query = "retro crt tv video game"
    else:
        query = random.choice(UNSPLASH_PS2_FALLBACK_QUERIES[:2])

    saved: List[Path] = []
    try:
        resp = requests.get(
            "https://api.unsplash.com/search/photos",
            headers={"Authorization": f"Client-ID {api_key}", "Accept-Version": "v1"},
            params={"query": query, "per_page": 8, "orientation": "landscape"},
            timeout=20,
        )
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            random.shuffle(results)
            for idx, item in enumerate(results[: count + 3], 1):
                if len(saved) >= count:
                    break
                dl_url = item.get("urls", {}).get("regular")
                if not dl_url:
                    continue
                r_img = requests.get(dl_url, timeout=20)
                if r_img.status_code == 200 and len(r_img.content) > 2048:
                    im = Image.open(BytesIO(r_img.content)).convert("RGB")
                    out_p = WORK_MEDIA_DIR / f"unsplash_fb_{idx:02d}_{item.get('id', idx)}.jpg"
                    im.save(out_p, format="JPEG", quality=88)
                    saved.append(out_p)
    except Exception:
        pass

    return saved


# ==============================================================================
# 3. PADANAN PRODUK SHOPEE SEBENAR & OLAHAN PERSONA AI (OCR + IDEA REDDIT)
# ==============================================================================
def match_shopee_product_for_reddit_topic(combined_text: str) -> Dict[str, Any]:
    low = combined_text.lower()
    if "matrix" in low or "modchip" in low:
        target_compat = "MATRIX_ONLY"
    elif any(k in low for k in ("fmcb", "opentuna", "fortuna", "code breaker", "memory card")):
        target_compat = "MOD_CARD_ONLY"
    elif any(k in low for k in ("opl", "laser", "disc read", "lens", "fat", "slim", "scph", "hdd", "usb", "xfer")):
        target_compat = "NON_MATRIX_OPL_SET"
    else:
        target_compat = "UNIVERSAL_STORAGE"

    if not SHOPEE_DB_PATH.exists():
        return {
            "product_code": "SHP_010",
            "product_name": "Playstation 2 Games PENDRIVE / HDD With Full Games (Can Choose Game)",
            "shopee_url": "https://s.shopee.com.my/2Vtc7TVFJw",
            "compatibility": "UNIVERSAL_STORAGE",
            "storage_tier": "PENDRIVE_OR_HDD_MULTI",
            "game_count_hint": "32GB hingga 500GB",
        }

    conn = sqlite3.connect(str(SHOPEE_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM shopee_products WHERE is_active = 1 AND compatibility = ? ORDER BY times_promoted ASC, id ASC",
                (target_compat,),
            ).fetchall()
        ]
        if not rows:
            rows = [
                dict(r)
                for r in conn.execute("SELECT * FROM shopee_products WHERE is_active = 1 ORDER BY id ASC").fetchall()
            ]
        return random.choice(rows[:2]) if len(rows) >= 2 else rows[0]
    finally:
        conn.close()


def generate_reddit_ocr_persona_story(
    reddit_post: Dict[str, Any],
    top_comments: List[str],
    vision_items: List[Dict[str, Any]],
    image_source_type: str,
    product_rec: Dict[str, Any],
    myt_ctx: Dict[str, Any],
) -> Tuple[bool, Dict[str, Any]]:
    self_ref = random.choice(["admin", "min"])

    ocr_notes: List[str] = []
    ocr_texts_found: List[str] = []
    for idx, v in enumerate(vision_items, 1):
        objs = ", ".join(v.get("detected_objects", [])) or "perkakasan PS2"
        ocr_t = str(v.get("ocr_text", "None")).strip()
        angle = str(v.get("retro_nostalgia_angle", "")).strip()
        if ocr_t and ocr_t.lower() != "none":
            ocr_texts_found.append(ocr_t)
        ocr_notes.append(f"- Visual {idx}: {angle} (Objek: {objs} | OCR: {ocr_t})")

    vision_summary = "\n".join(ocr_notes) if ocr_notes else "- Visual perkakasan klasik PlayStation 2."
    comments_block = "\n".join(f"- {c}" for c in top_comments[:4]) if top_comments else "- Ramai peminat PS2 berkongsi kenangan zaman 2000-an."

    system_prompt = (
        f"Kau ialah '{self_ref}', seorang lelaki Malaysia berumur 35 tahun yang terpelajar, matang, "
        "dan mengendalikan halaman Facebook 'PS2 Retro Nostalgia'. Kau sangat arif tentang konsol PlayStation 2, "
        "perkakasan PS2 Fat/Slim, modchip Matrix, Mod Card FMCB/OpenTuna, serta sejarah game retro.\n\n"
        "TUGASAN UTAMA (OLAHAN ASLI — BUKAN TERJEMAHAN):\n"
        "1. Gabungkan intipati perbincangan komuniti luar negara dengan apa yang dilihat pada GAMBAR & TEKS OCR.\n"
        "2. Jangan terjemah bulat-bulat. Olah semula menjadi cerita asli dalam Bahasa Melayu Malaysia yang hidup, "
        "santai, dan dekat dengan pengalaman gamer PS2 tempatan.\n"
        f"3. Bahasakan diri HANYA sebagai '{self_ref}' (DILARANG guna 'aku' atau 'saya') dan sapa pembaca sebagai 'korang' atau 'geng'.\n"
        f"4. Mood semasa ialah {myt_ctx['mood_style']}. DILARANG menyebut waktu bercanggah ({myt_ctx['forbidden_times']}).\n"
        "5. Selitkan secara halus (soft-sell) keseronokan menghidupkan semula PS2 lama guna pendrive/hardisk atau mod card "
        "supaya pembaca tertarik menyemak ruangan komen.\n\n"
        "KAWALAN KETAT:\n"
        "- Panjang cerita MESTI antara 500 hingga 640 aksara sahaja.\n"
        "- DILARANG menulis tajuk/header di baris pertama, DILARANG sebut 'Gambar 1/2' atau 'Reddit', DILARANG emoji, "
        "DILARANG perkataan Indonesia (bisa, banget, nggak, ngak, bikin, gampang, cobain, cocok, yuk, kalian, anda), "
        "dan DILARANG letak harga RM atau link Shopee dalam cerita utama.\n"
        "- Pastikan ayat berakhir dengan tanda noktah (.) yang lengkap."
    )

    user_prompt = (
        f"MASA SIARAN: {myt_ctx['full_timestamp']}\n"
        f"SUMBER IDEA KOMUNITI (r/{reddit_post['subreddit']}):\n"
        f"- Tajuk Asal: {reddit_post['title']}\n"
        f"- Huraian Asal: {reddit_post['cleaned_text'][:650] or 'Perkongsian visual setup & koleksi PS2.'}\n"
        f"- Reaksi Komen Teratas Komuniti:\n{comments_block}\n\n"
        f"DAPATAN VISION & OCR DARIPADA GAMBAR LAMPIRAN ({image_source_type}):\n"
        f"{vision_summary}\n\n"
        f"KONTEKS PRODUK KOMEN PERTAMA (untuk selitan halus sahaja): {product_rec['product_name']}\n\n"
        f"Olah penceritaan asli daripada sudut pandang '{self_ref}' sepanjang 500 hingga 640 aksara tanpa emoji:"
    )

    last_meta: Dict[str, Any] = {}
    for attempt in range(1, 5):
        adj_temp = min(0.92, round(0.74 + ((attempt - 1) * 0.04), 2))
        inf = llm_engine.generate_chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=adj_temp,
            max_tokens=390,
        )

        cleaned = clean_and_scrub_bm_story(inf["raw_text"], max_chars=645)
        cleaned = re.sub(r"^(cikgu|tuan|puan|bos)[,\s:!]+", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"\baku\b", self_ref, cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\bsaya\b", self_ref, cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(rf"(^|[.!?]\s+){self_ref}\b", lambda m: f"{m.group(1)}{self_ref.capitalize()}", cleaned)
        cleaned = re.sub(r"[^\x20-\x7E\n]", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        ok_q, q_msg = validate_story_quality(cleaned, min_len=480, max_len=655)
        last_meta = {
            "attempt": attempt,
            "elapsed_sec": inf["elapsed_sec"],
            "tok_per_sec": inf["tok_per_sec"],
            "story_chars": len(cleaned),
            "status": q_msg,
        }

        if ok_q:
            hashtags = "#PS2RetroNostalgia #PS2Malaysia #RetroGaming #PlayStation2 #Nostalgia2000an"
            post_caption = f"{cleaned}\n\n{hashtags}"
            first_comment = (
                f"🎮 Nak hidupkan semula konsol PS2 kat rumah tanpa pening kepala CD calar atau lens rosak? "
                f"Tekan link Shopee di bawah untuk pilih koleksi game atau mod card yang padan dengan PS2 korang:\n"
                f"👉 {product_rec['shopee_url']}\n\n"
                f"📌 {product_rec['compatibility']} | {product_rec['storage_tier'].replace('_', ' ')} ({product_rec['game_count_hint']})"
            )

            return True, {
                "persona_ref": self_ref,
                "story_bm": cleaned,
                "story_chars": len(cleaned),
                "post_caption": post_caption,
                "first_comment": first_comment,
                "ocr_texts_found": list(dict.fromkeys(ocr_texts_found)),
                "vision_summary": vision_summary,
                "metrics": last_meta,
            }

    return False, {"error": "Gagal menjana olahan cerita sah selepas 4 percubaan.", "metrics": last_meta}


# ==============================================================================
# 4. PENGHANTAR ALBUM 1-4 GAMBAR & LAPORAN LENGKAP KE CHANNEL TELEGRAM
# ==============================================================================
def send_media_and_report_to_telegram(
    image_paths: List[Path],
    reddit_post: Dict[str, Any],
    image_source_type: str,
    vision_items: List[Dict[str, Any]],
    story_pkg: Dict[str, Any],
    product_rec: Dict[str, Any],
    myt_ctx: Dict[str, Any],
) -> Dict[str, Any]:
    ok_cfg, errs = tg_config.validate()
    if not ok_cfg:
        raise RuntimeError(f"Kredensial Telegram tidak sah: {'; '.join(errs)}")

    channel_id = tg_config.get_channel_id("default")
    base_url = tg_config.bot_api_base_url
    sent_media_ids: List[int] = []

    media_caption = (
        f"🧪 <b>EXP09 :: UJIAN REDDIT PS2 + VISION OCR + PERSONA AI</b>\n"
        f"📡 <b>Sumber:</b> r/{html.escape(reddit_post['subreddit'])} ({html.escape(reddit_post['source_engine'])})\n"
        f"🖼️ <b>Media:</b> {len(image_paths)} gambar ({html.escape(image_source_type)})\n"
        f"🕒 <b>Masa MYT:</b> {html.escape(myt_ctx['full_timestamp'])}"
    )

    if len(image_paths) == 1:
        res_p = tg_notifier.send_photo(
            image_path=image_paths[0],
            html_caption=media_caption,
            chat_id=channel_id,
        )
        sent_media_ids.append(int(res_p["message_id"]))
    else:
        media_array: List[Dict[str, Any]] = []
        files_payload: Dict[str, Any] = {}

        for idx, img_p in enumerate(image_paths[:4], 1):
            attach_key = f"photo_{idx}"
            entry: Dict[str, Any] = {
                "type": "photo",
                "media": f"attach://{attach_key}",
            }
            if idx == 1:
                entry["caption"] = media_caption[:1000]
                entry["parse_mode"] = "HTML"
            media_array.append(entry)
            files_payload[attach_key] = (img_p.name, open(img_p, "rb"), "image/jpeg")

        try:
            resp = requests.post(
                f"{base_url}/sendMediaGroup",
                data={
                    "chat_id": channel_id,
                    "media": json.dumps(media_array),
                },
                files=files_payload,
                timeout=45,
            )
            data = resp.json()
            if resp.status_code != 200 or not data.get("ok"):
                raise RuntimeError(f"Gagal hantar album sendMediaGroup ke Telegram: {json.dumps(data)}")
            for m_obj in data.get("result", []):
                sent_media_ids.append(int(m_obj.get("message_id", 0)))
        finally:
            for _, f_tuple in files_payload.items():
                try:
                    f_tuple[1].close()
                except Exception:
                    pass

    time.sleep(0.8)

    ocr_lines_html: List[str] = []
    for v in vision_items:
        objs_str = ", ".join(v.get("detected_objects", [])) or "-"
        ocr_lines_html.append(
            f"• <b>Imej #{v.get('image_number')}:</b> Objek=[<code>{html.escape(objs_str)}</code>] | "
            f"OCR=[<code>{html.escape(str(v.get('ocr_text', 'None')))}</code>] | "
            f"Platform=[{html.escape(str(v.get('console_platform', '-')))}]"
        )

    report_html = "\n".join(
        [
            "📋 <b>LAPORAN OLAHAN AI (VISION OCR + IDEA REDDIT)</b>",
            f"• <b>Sumber Reddit:</b> r/{html.escape(reddit_post['subreddit'])} "
            f"(⬆️ {reddit_post['score']} | 💬 {reddit_post['num_comments']})",
            f"• <b>Tajuk Asal:</b> <i>{html.escape(reddit_post['title'][:160])}</i>",
            f"• <b>Pautan Reddit:</b> {html.escape(reddit_post['permalink'])}",
            "",
            "👁️ <b>HASIL BACAAN VISION &amp; OCR (X07)</b>",
            *ocr_lines_html,
            "",
            f"✍️ <b>AYAT OLAHAN PERSONA ({html.escape(story_pkg['persona_ref'])} | {story_pkg['story_chars']} aksara)</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            html.escape(story_pkg["post_caption"]),
            "━━━━━━━━━━━━━━━━━━━━",
            "",
            f"🛒 <b>KOMEN PERTAMA &amp; PRODUK SHOPEE ({html.escape(product_rec['product_code'])})</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            html.escape(story_pkg["first_comment"]),
            "━━━━━━━━━━━━━━━━━━━━",
        ]
    )

    msg_parts = tg_notifier.send_message(report_html, chat_id=channel_id, disable_preview=True)
    return {
        "channel_id": channel_id,
        "media_message_ids": sent_media_ids,
        "report_message_ids": [p["message_id"] for p in msg_parts],
    }


# ==============================================================================
# FUNGSI UTAMA DIAGNOSTIK EXP09
# ==============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="EXP09: Ujian Subreddit PS2 + Vision OCR + Persona AI -> Telegram")
    parser.add_argument("--force-unsplash", action="store_true", help="Uji laluan kecemasan Unsplash walaupun pos Reddit ada gambar.")
    parser.add_argument("--max-images", type=int, default=4, help="Had maksimum gambar diuji (1..4).")
    parser.add_argument("--reset-seen", action="store_true", help="Kosongkan rekod post_id yang sudah diuji.")
    args = parser.parse_args()

    current_stage = "Inisialisasi Sistem & Audit Modul"
    myt_ctx = get_myt_context()
    vm = psutil.virtual_memory()
    seen_ids = load_seen_reddit_ids(reset=args.reset_seen)

    try:
        console.print(
            Panel.fit(
                "[bold cyan]PS2-RETRO-NOSTALGIA :: REDDIT PS2 + VISION OCR + PERSONA AI TEST (EXP09)[/bold cyan]\n"
                f"[dim]Masa MYT: {myt_ctx['full_timestamp']}\n"
                f"Mood Slot: {myt_ctx['mood_style']} | RAM Tersedia: {vm.available / (1024**3):.2f} GB | "
                f"Rekod Sudah Diuji: {len(seen_ids)} pos[/dim]",
                border_style="cyan",
            )
        )

        # 1. Audit Modul & Pangkalan Data
        audit_tbl = Table(title="1. Audit Modul Pengeluaran & Pangkalan Data SQLite3", box=box.ROUNDED, header_style="bold magenta")
        audit_tbl.add_column("Komponen / Modul", style="bold white")
        audit_tbl.add_column("Butiran / Status Sambungan", style="cyan")
        audit_tbl.add_column("Status", justify="center")

        for mod_name, ok_m, msg_m in IMPORT_AUDIT:
            audit_tbl.add_row(mod_name, msg_m, "[bold green]IMPORT OK[/bold green]" if ok_m else "[bold red]GAGAL[/bold red]")

        shopee_cnt = 0
        if SHOPEE_DB_PATH.exists():
            c = sqlite3.connect(str(SHOPEE_DB_PATH))
            shopee_cnt = int(c.execute("SELECT COUNT(*) FROM shopee_products WHERE is_active=1").fetchone()[0])
            c.close()

        games_cnt = 0
        if GAMES_DB_PATH.exists():
            c = sqlite3.connect(str(GAMES_DB_PATH))
            games_cnt = int(c.execute("SELECT COUNT(*) FROM ps2_games").fetchone()[0])
            c.close()

        tg_ok, _ = tg_config.validate()
        audit_tbl.add_row("ps2_shopee_products.db", f"{shopee_cnt} produk Shopee aktif", "[bold green]ADA[/bold green]" if shopee_cnt else "[bold red]TIADA[/bold red]")
        audit_tbl.add_row("ps2_games_catalog.db", f"{games_cnt} judul game PS2", "[bold green]ADA[/bold green]" if games_cnt else "[bold red]TIADA[/bold red]")
        audit_tbl.add_row("X10_telegram_config", f"Mod: {tg_config.source_mode} | Channel: {tg_config.default_channel_id}", "[bold green]SAH[/bold green]" if tg_ok else "[bold red]RALAT[/bold red]")
        console.print(audit_tbl)

        # 2. Tarik Calon Pos Reddit Menggunakan Enjin Fallback Berperingkat
        current_stage = "Penarikan Subreddit PS2 & Mirror Fallback Engine"
        merged_by_id: Dict[str, Dict[str, Any]] = {}
        engine_stats: List[Dict[str, Any]] = []

        with console.status("[bold cyan]Menarik calon pos PS2 dari Arctic Shift, PullPush & Reddit Fallback...[/bold cyan]"):
            for idx, eng_fn in enumerate(
                [
                    engine_arctic_shift,
                    engine_pullpush_mirror,
                    engine_multireddit_json,
                    engine_multireddit_rss,
                ]
            ):
                posts, st = eng_fn()
                engine_stats.append(st)
                for p in posts:
                    pid = p.get("post_id") or p.get("title")
                    if pid not in merged_by_id:
                        merged_by_id[pid] = p
                    elif p["image_count"] > merged_by_id[pid]["image_count"]:
                        merged_by_id[pid] = p
                # Jika enjin utama (Arctic Shift) sudah dapat >= 15 pos, hanya jalankan PullPush sebagai pengkaya
                if idx == 1 and len(merged_by_id) >= 15:
                    break
                time.sleep(0.5)

        eng_tbl = Table(title="2. Status Enjin Sumber Reddit PS2", box=box.ROUNDED, header_style="bold green")
        eng_tbl.add_column("Enjin Sumber", style="bold white")
        eng_tbl.add_column("HTTP", justify="center", style="yellow")
        eng_tbl.add_column("Klien", justify="center", style="dim")
        eng_tbl.add_column("Latensi", justify="right", style="cyan")
        eng_tbl.add_column("Pos Sah", justify="right", style="white")
        eng_tbl.add_column("Ada Gambar", justify="right", style="green")
        eng_tbl.add_column("Status", justify="center")

        for st in engine_stats:
            badge = "[bold green]BERJAYA[/bold green]" if st["status"] == "BERJAYA" else "[bold yellow]FALLBACK SKIP[/bold yellow]"
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

        all_candidates = list(merged_by_id.values())
        if not all_candidates:
            raise RuntimeError("Tiada pos sah berjaya ditarik daripada mana-mana enjin Reddit.")

        # Tapis keluar pos yang sudah pernah diuji dalam larian sebelum ini
        fresh_candidates = [c for c in all_candidates if c.get("post_id") not in seen_ids]
        working_candidates = fresh_candidates if fresh_candidates else all_candidates

        working_candidates.sort(
            key=lambda x: (1 if x["image_count"] > 0 else 0, x["curation_score"], x["num_comments"]),
            reverse=True,
        )

        # Papar Top 6 calon baharu
        top_tbl = Table(
            title=f"3. Top 6 Calon Baharu Belum Diuji ({len(working_candidates)} Calon Tersedia)",
            box=box.ROUNDED,
            header_style="bold yellow",
        )
        top_tbl.add_column("#", justify="center", style="cyan", width=3)
        top_tbl.add_column("Subreddit", style="magenta", width=15)
        top_tbl.add_column("⬆️ / 💬", justify="center", style="yellow", width=12)
        top_tbl.add_column("Gambar", justify="center", style="green", width=8)
        top_tbl.add_column("Skor", justify="right", style="cyan", width=7)
        top_tbl.add_column("Tajuk Pos Reddit", style="white")

        for idx, c in enumerate(working_candidates[:6], 1):
            short_t = c["title"] if len(c["title"]) <= 56 else f"{c['title'][:53]}..."
            top_tbl.add_row(
                str(idx),
                f"r/{c['subreddit']}",
                f"{c['score']} / {c['num_comments']}",
                f"{c['image_count']} img",
                str(c["curation_score"]),
                short_t,
            )
        console.print(top_tbl)

        # Pilih secara rawak daripada 3 calon teratas supaya setiap ujian 3-4x terasa segar
        top_pool = working_candidates[: min(3, len(working_candidates))]
        chosen_post = random.choice(top_pool)
        top_comments, comment_src = fetch_top_reddit_comments(
            chosen_post["subreddit"],
            chosen_post["post_id"],
            max_comments=5,
        )

        console.print(
            Panel(
                f"[bold yellow]Subreddit:[/bold yellow] r/{chosen_post['subreddit']} | "
                f"[bold yellow]Enjin:[/bold yellow] {chosen_post['source_engine']} | "
                f"[bold yellow]Upvotes:[/bold yellow] {chosen_post['score']} | "
                f"[bold yellow]Komen:[/bold yellow] {chosen_post['num_comments']}\n"
                f"[bold white]Tajuk:[/bold white] {chosen_post['title']}\n"
                f"[dim]Komen Teratas: {len(top_comments)} komen ({comment_src}) | URL: {chosen_post['permalink']}[/dim]",
                title="4. Calon Reddit Terpilih & Komen Komuniti",
                border_style="cyan",
            )
        )

        # 3. Muat Turun 1-4 Gambar (3-Lapisan Anti-403 Reddit atau Unsplash Fallback) & OCR (X07)
        current_stage = "Muat Turun Media & Analisis Vision OCR (X07)"
        max_imgs = max(1, min(4, args.max_images))
        local_images: List[Path] = []
        image_source_type = "REDDIT_ORIGINAL"

        if not args.force_unsplash and chosen_post["image_slots"]:
            with console.status(f"[bold cyan]Memuat turun hingga {max_imgs} gambar Reddit (Anti-403 Multi-Path)...[/bold cyan]"):
                local_images = download_reddit_images_to_local(
                    chosen_post["image_slots"],
                    post_id=chosen_post["post_id"],
                    max_images=max_imgs,
                )

        if not local_images:
            image_source_type = "UNSPLASH_FALLBACK"
            with console.status("[bold yellow]Menggunakan sandaran gambar Unsplash untuk topik ini...[/bold yellow]"):
                local_images = fetch_unsplash_fallback_images(chosen_post["title"], count=min(2, max_imgs))

        if not local_images:
            raise RuntimeError("Gagal mendapatkan sebarang gambar daripada Reddit mahupun sandaran Unsplash.")

        with console.status(f"[bold cyan]Menjalankan Vision + OCR (X07 < 30KB) ke atas {len(local_images)} gambar...[/bold cyan]"):
            v_ok, v_items, v_meta = vision_analyzer.analyze_images(
                local_images,
                context_hint=chosen_post["title"],
                auto_unload=True,
                skip_failed=True,
            )

        if not v_ok or not v_items:
            raise RuntimeError(f"Enjin Vision/OCR X07 gagal menganalisis gambar: {v_meta}")

        # Tapis gambar tidak relevan PS2 jika datang dari sandaran Unsplash
        if image_source_type == "UNSPLASH_FALLBACK":
            filtered_imgs: List[Path] = []
            filtered_v: List[Dict[str, Any]] = []
            for idx, v_obj in enumerate(v_items):
                if v_obj.get("is_ps2_relevant", True) and not v_obj.get("has_human_face", False):
                    if idx < len(local_images):
                        filtered_imgs.append(local_images[idx])
                        filtered_v.append(v_obj)
            if filtered_imgs:
                local_images = filtered_imgs
                v_items = filtered_v

        ocr_tbl = Table(
            title=f"5. Hasil Bacaan Vision & OCR X07 ({image_source_type} | {v_meta['inference_sec']}s | {v_meta['tok_per_sec']} tok/s)",
            box=box.ROUNDED,
            header_style="bold cyan",
        )
        ocr_tbl.add_column("#", justify="center", style="cyan", width=3)
        ocr_tbl.add_column("Saiz Mampat", justify="right", style="green", width=12)
        ocr_tbl.add_column("Objek Dikesan", style="white")
        ocr_tbl.add_column("Teks OCR", style="yellow")
        ocr_tbl.add_column("Platform / Kategori", style="magenta")

        for v in v_items:
            ocr_tbl.add_row(
                str(v.get("image_number")),
                f"{v.get('compressed_kb', 0)} KB",
                ", ".join(v.get("detected_objects", [])) or "-",
                str(v.get("ocr_text", "None")),
                f"{v.get('console_platform')} ({v.get('visual_category')})",
            )
        console.print(ocr_tbl)

        # 4. Padankan Produk Shopee & Olah Cerita Persona AI (OCR + Idea Reddit)
        current_stage = "Olahan Persona AI (X06) & Padanan Shopee"
        product_rec = match_shopee_product_for_reddit_topic(
            f"{chosen_post['title']} {chosen_post['cleaned_text']} {' '.join(top_comments)}"
        )

        with console.status("[bold cyan]Mengolah cerita Persona AI ('admin'/'min') daripada OCR + idea Reddit...[/bold cyan]"):
            s_ok, story_pkg = generate_reddit_ocr_persona_story(
                reddit_post=chosen_post,
                top_comments=top_comments,
                vision_items=v_items,
                image_source_type=image_source_type,
                product_rec=product_rec,
                myt_ctx=myt_ctx,
            )
        llm_engine.unload()

        if not s_ok:
            raise RuntimeError(f"Gagal menjana cerita olahan AI: {story_pkg}")

        bm = story_pkg["metrics"]
        console.print(
            Panel(
                f"[bold yellow]KAPSYEN OLAHAN AI (Persona: {story_pkg['persona_ref']} | {story_pkg['story_chars']} aksara | Sifar Emoji):[/bold yellow]\n"
                f"[white]{story_pkg['post_caption']}[/white]\n\n"
                f"[bold green]KOMEN PERTAMA SHOPEE ({product_rec['product_code']}):[/bold green]\n"
                f"[cyan]{story_pkg['first_comment']}[/cyan]\n\n"
                f"[dim]Metrik LLM: {bm['elapsed_sec']}s ({bm['tok_per_sec']} tok/s, cubaan #{bm['attempt']})[/dim]",
                title="6. Pratonton Olahan Cerita AI (OCR + Reddit)",
                border_style="green",
            )
        )

        # 5. Hantar 1-4 Gambar & Laporan ke Channel Telegram (X12)
        current_stage = "Penghantaran Gambar & Laporan ke Channel Telegram (X12)"
        with console.status(f"[bold cyan]Menghantar {len(local_images)} gambar & laporan olahan AI ke Channel Telegram...[/bold cyan]"):
            tg_res = send_media_and_report_to_telegram(
                image_paths=local_images,
                reddit_post=chosen_post,
                image_source_type=image_source_type,
                vision_items=v_items,
                story_pkg=story_pkg,
                product_rec=product_rec,
                myt_ctx=myt_ctx,
            )

        # Simpan post_id ke rekod ujian supaya larian seterusnya pilih pos baharu
        save_seen_reddit_id(chosen_post["post_id"])

        console.print(
            Panel.fit(
                f"[bold green]✅ UJIAN EXP09 BERJAYA 100% & DIHANTAR KE CHANNEL TELEGRAM![/bold green]\n"
                f"• Channel ID: [cyan]{tg_res['channel_id']}[/cyan]\n"
                f"• Mesej Gambar/Album ({len(local_images)} keping): [yellow]{tg_res['media_message_ids']}[/yellow]\n"
                f"• Mesej Laporan Olahan AI: [yellow]{tg_res['report_message_ids']}[/yellow]",
                border_style="green",
            )
        )

        PREVIEW_JSON_PATH.write_text(
            json.dumps(
                {
                    "myt_timestamp": myt_ctx["full_timestamp"],
                    "reddit_post": chosen_post,
                    "top_comments": top_comments,
                    "image_source_type": image_source_type,
                    "local_images": [str(p) for p in local_images],
                    "vision_results": v_items,
                    "story_package": story_pkg,
                    "shopee_product": product_rec,
                    "telegram_delivery": tg_res,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        console.print(f"[dim]Fail log JSON disimpan di: {PREVIEW_JSON_PATH}[/dim]")

    except Exception as exc:
        err_trace = traceback.format_exc()
        console.print(
            Panel(
                f"[bold red]❌ RALAT DIKESAN PADA PERINGKAT: {current_stage}[/bold red]\n"
                f"[yellow]{exc}[/yellow]\n\n[dim]{err_trace[-1200:]}[/dim]",
                title="Laporan Ralat EXP09",
                border_style="red",
            )
        )
        try:
            tg_notifier.notify_workflow_error(
                platform_label="EXP09 - Reddit PS2 Vision & Persona Debug",
                failed_step=current_stage,
                error_detail=str(exc),
                log_excerpt=err_trace,
                channel_alias="default",
                also_dm=False,
            )
            console.print("[yellow]📨 Amaran ralat turut dihantar ke Channel Telegram.[/yellow]")
        except Exception as tg_err:
            console.print(f"[red]Gagal hantar laporan ralat ke Telegram: {tg_err}[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()