#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - PENARIK & PENAPIS VIDEO PEXELS 9:16 (X14)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/live-engine-01/X14_pexels_video_fetcher.py
#
# FUNGSI UTAMA (EDISI NAIK TARAF):
# 1. Penjana Kata Kunci AI Pintar (X06 Qwen3.5-4B): Menjana calon kata kunci B-Roll
#    pendek (2-3 perkataan Inggeris) yang fokus pada objek, pencahayaan retro,
#    suasana tenang, atau pandangan belakang/sisi tanpa wajah hadapan.
# 2. Guardrail Kata Kunci Berlapis (X04 Redis + X05 Upstash Vector):
#    - Memori 15 kata kunci terakhir + kunci tepat 14 hari di Redis (`ps2n_reels`).
#    - Penapis semantik Vector (skor >= 0.84 dalam 7 hari disekat) supaya
#      tema carian Pexels sentiasa segar dan tidak berulang rapat.
# 3. Penapis Ketat Bebas Wajah & Orang Ramai: Menolak potret, selfie, dan
#    kumpulan ramai, tetapi membenarkan objek tulen & pandangan belakang/sisi.
# 4. Penjara Anti-Ulangan Video 6 Bulan (180 Hari): Menyemak ID video Pexels &
#    cincangan SHA-256 terhadap Upstash Redis (`ps2n_reels`) serta SQLite.
# 5. Pembersih Stream FFmpeg (`-map 0:v:0 -c:v copy -an`): Membuang trek data
#    `tmcd` dan audio asal yang sering menyebabkan MoviePy gagal membaca MP4.
# ==============================================================================

import hashlib
import importlib
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import imageio_ffmpeg
import requests
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# ------------------------------------------------------------------------------
# IMPORT MODUL TERAS PROJEK (X01, X03, X04, X05, X06)
# ------------------------------------------------------------------------------
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for p in [CURRENT_DIR, PROJECT_ROOT]:
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

cfg: Any = importlib.import_module("X01_config")
_b2_mod: Any = importlib.import_module("X03_b2_proxy_client")
_redis_mod: Any = importlib.import_module("X04_redis_sharded")
_vector_mod: Any = importlib.import_module("X05_vector_sharded")
_llm_mod: Any = importlib.import_module("X06_local_llm_engine")

compute_file_sha256: Any = getattr(_b2_mod, "compute_file_sha256")
PS2RedisShardedManager: Any = getattr(_redis_mod, "PS2RedisShardedManager")
PS2VectorShardedManager: Any = getattr(_vector_mod, "PS2VectorShardedManager")
llm_engine: Any = getattr(_llm_mod, "llm_engine")

# Instans Redis & Vector khusus ruang nama Reels
reels_redis = PS2RedisShardedManager(namespace="ps2n_reels")
reels_vector = PS2VectorShardedManager(namespace="ps2n_reels")

# ==============================================================================
# KONFIGURASI PEXELS & PENJARA ANTI-ULANGAN 6 BULAN (180 HARI)
# ==============================================================================
DATA_DIR: Path = Path(getattr(cfg, "DATA_DIR", PROJECT_ROOT / "data"))
TEMP_DIR: Path = Path(getattr(cfg, "TEMP_DIR", PROJECT_ROOT / "temp"))
REELS_HISTORY_DB_PATH: Path = DATA_DIR / "ps2_reels_posts_history.db"

VIDEO_COOLDOWN_DAYS: int = 180
VIDEO_COOLDOWN_SECONDS: int = VIDEO_COOLDOWN_DAYS * 86400  # 6 Bulan (15,552,000 saat)
KEYWORD_EXACT_COOLDOWN_DAYS: int = 14
KEYWORD_SEMANTIC_COOLDOWN_DAYS: int = 7

# 10 Kategori Topik Bebas & B-Roll Menarik (30% Retro/Konsol/Gaming + 70% Estetika/Suasana/Alam)
PEXELS_REELS_CATEGORIES: Dict[str, Dict[str, Any]] = {
    "PS2_RETRO_CONSOLE": {
        "is_gaming_promo_eligible": True,
        "ai_theme_hint": "close up shots of retro game consoles, controllers, glowing buttons, and classic gaming gear without faces",
        "queries": [
            "retro game console",
            "game controller neon",
            "classic video game",
            "playstation controller",
            "retro gaming desk",
        ],
    },
    "CRT_ARCADE_NOSTALGIA": {
        "is_gaming_promo_eligible": True,
        "ai_theme_hint": "empty vintage arcade cabinets, glowing arcade buttons, and retro CRT television screens in dark neon rooms",
        "queries": [
            "arcade machine neon",
            "retro television screen",
            "arcade buttons glow",
            "vintage tv static",
            "neon arcade room",
        ],
    },
    "GAMING_SETUP_RGB": {
        "is_gaming_promo_eligible": True,
        "ai_theme_hint": "aesthetic RGB gaming desks, glowing mechanical keyboards, monitors, and dark cozy room setups with no people",
        "queries": [
            "rgb gaming desk",
            "gaming room neon",
            "mechanical keyboard rgb",
            "pc gaming setup",
            "neon monitor desk",
        ],
    },
    "CONTROLLER_HANDS_BACKVIEW": {
        "is_gaming_promo_eligible": True,
        "ai_theme_hint": "macro close up of hands holding a game controller or over-the-shoulder back view of gaming in dim neon light",
        "queries": [
            "hands holding controller",
            "game controller closeup",
            "playing video game",
            "joystick neon light",
            "controller buttons macro",
        ],
    },
    "STORAGE_TECH_HARDWARE": {
        "is_gaming_promo_eligible": True,
        "ai_theme_hint": "macro technology shots of USB flash drives, external hard drives, circuit boards, and glowing cables",
        "queries": [
            "usb flash drive",
            "external hard drive",
            "circuit board macro",
            "computer hardware neon",
            "microchip technology glow",
        ],
    },
    "COZY_RAIN_NIGHT": {
        "is_gaming_promo_eligible": False,
        "ai_theme_hint": "calming rainy night windows, wet neon city streets, and warm cozy room lamps at night",
        "queries": [
            "rain on window",
            "rainy night neon",
            "cozy night lamp",
            "wet street reflections",
            "night window rain",
        ],
    },
    "CYBER_NEON_VIBE": {
        "is_gaming_promo_eligible": False,
        "ai_theme_hint": "atmospheric purple and blue neon lights, retro synthwave grids, and futuristic night cityscapes",
        "queries": [
            "cyberpunk neon lights",
            "purple neon glow",
            "neon city night",
            "synthwave retro grid",
            "blue neon reflection",
        ],
    },
    "NATURE_CALM_ESCAPE": {
        "is_gaming_promo_eligible": False,
        "ai_theme_hint": "serene nature scenery with misty green forests, flowing rivers, raindrops on leaves, and peaceful sunset clouds",
        "queries": [
            "misty forest morning",
            "calm forest river",
            "raindrops on leaves",
            "peaceful sunset clouds",
            "green forest sunlight",
        ],
    },
    "LATE_NIGHT_CHILL": {
        "is_gaming_promo_eligible": False,
        "ai_theme_hint": "quiet late night desk aesthetics with steaming coffee mugs, warm desk lamps, and peaceful city night views",
        "queries": [
            "coffee mug night",
            "warm desk lamp",
            "night city skyline",
            "cozy room evening",
            "steaming coffee cup",
        ],
    },
    "TECH_AUDIO_RETRO": {
        "is_gaming_promo_eligible": False,
        "ai_theme_hint": "vintage audio aesthetics like spinning vinyl records, retro cassette tapes, and glowing headphones on a desk",
        "queries": [
            "headphones gaming desk",
            "cassette tape retro",
            "vinyl record spinning",
            "retro radio speaker",
            "audio equalizer neon",
        ],
    },
}

# Kata kunci dilarang keras (wajah hadapan, potret, orang ramai, haiwan sensitif)
STRICT_FACE_CROWD_AND_SENSITIVE_TOKENS: Set[str] = {
    "face",
    "faces",
    "portrait",
    "selfie",
    "vlog",
    "vlogger",
    "headshot",
    "smiling",
    "smile",
    "laughing",
    "talking",
    "interview",
    "model",
    "posing",
    "makeup",
    "beauty",
    "crowd",
    "crowds",
    "audience",
    "party",
    "concert",
    "group",
    "team",
    "friends",
    "family",
    "couple",
    "kid",
    "kids",
    "child",
    "children",
    "teen",
    "teenager",
    "boy",
    "boys",
    "girl",
    "girls",
    "dog",
    "dogs",
    "puppy",
    "puppies",
    "canine",
    "pig",
    "pigs",
    "pork",
    "swine",
    "boar",
}

GENERIC_HUMAN_TOKENS: Set[str] = {
    "man",
    "men",
    "woman",
    "women",
    "person",
    "people",
    "guy",
    "guys",
    "lady",
    "gamer",
    "gamers",
    "player",
    "players",
    "streamer",
    "actor",
}

BACK_SIDE_HANDS_HINTS: Set[str] = {
    "behind",
    "back",
    "rear",
    "side",
    "silhouette",
    "hands",
    "hand",
    "pov",
    "shoulder",
}


def sanitize_pexels_keyword(raw_kw: str, max_words: int = 3) -> str:
    """
    Membersihkan kata kunci carian Pexels supaya ringkas (2-3 perkataan Inggeris),
    bebas simbol pelik, dan bebas istilah wajah/orang ramai.
    """
    if not raw_kw:
        return ""
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", str(raw_kw).lower())
    words = [w.strip() for w in cleaned.split() if len(w.strip()) >= 2]
    if not words:
        return ""

    # Tolak jika mengandungi kata terlarang
    if set(words).intersection(STRICT_FACE_CROWD_AND_SENSITIVE_TOKENS):
        return ""
    if set(words).intersection(GENERIC_HUMAN_TOKENS) and not set(words).intersection(BACK_SIDE_HANDS_HINTS):
        return ""

    trimmed_words = words[:max_words]
    if len(trimmed_words) < 2:
        return ""
    return " ".join(trimmed_words)


def is_pexels_clip_safe_faceless_or_backview(video_obj: Dict[str, Any]) -> bool:
    """
    Menolak video berunsur potret muka hadapan, selfie, kumpulan ramai, atau haiwan sensitif,
    tetapi membenarkan klip objek/suasana tulen serta pandangan belakang/sisi/tangan.
    """
    slug = str(video_obj.get("url", "")).lower()
    tokens = set(re.split(r"[^a-z0-9]+", slug))

    if tokens.intersection(STRICT_FACE_CROWD_AND_SENSITIVE_TOKENS):
        return False

    if tokens.intersection(GENERIC_HUMAN_TOKENS):
        if not tokens.intersection(BACK_SIDE_HANDS_HINTS):
            return False

    return True


def _is_keyword_blocked_by_guardrails(
    candidate_kw: str,
    recent_kw_records: List[Dict[str, Any]],
    now_epoch: int,
) -> Tuple[bool, str]:
    """
    Menyemak sama ada kata kunci carian Pexels disekat oleh:
    1. Redis exact cooldown (14 hari).
    2. Sejarah 15 kata kunci terakhir.
    3. Upstash Vector semantic similarity (skor >= 0.84 dalam 7 hari).
    """
    slug = re.sub(r"[^a-z0-9]+", "_", candidate_kw.lower()).strip("_")
    dup_r, _ = reels_redis.is_duplicate_fingerprint(f"pexels_query:{slug}")
    if dup_r:
        return True, "Kata kunci dikunci dalam Redis (< 14 hari)"

    for rec in recent_kw_records[:15]:
        prev_kw = str(rec.get("keyword", "")).strip().lower()
        if prev_kw == candidate_kw.lower():
            return True, "Kata kunci ada dalam memori 15 carian terakhir"

    try:
        matches, _ = reels_vector.search_semantic_items(
            routing_key="pexels_keywords",
            query_text=candidate_kw,
            top_k=3,
            record_type="keyword",
        )
        for m in matches:
            score = float(m.get("score", 0.0))
            meta = m.get("metadata", {}) or {}
            prev_ts = int(meta.get("posted_epoch") or meta.get("created_at") or now_epoch)
            age_days = max(0.0, (now_epoch - prev_ts) / 86400.0)
            if score >= 0.84 and age_days < KEYWORD_SEMANTIC_COOLDOWN_DAYS:
                return True, f"Terlalu mirip secara semantik ({score*100:.0f}%) dengan '{meta.get('keyword', '')}'"
    except Exception:
        pass

    return False, "OK"


def generate_ai_pexels_keywords(
    category_name: str,
    cat_info: Dict[str, Any],
    recent_kw_records: List[Dict[str, Any]],
) -> List[str]:
    """
    Menggunakan LLM tempatan X06 (Qwen3.5-4B) untuk menjana 8 calon kata kunci
    B-Roll pendek (2-3 perkataan Inggeris) yang estetik dan bebas wajah manusia.
    """
    recent_list = [str(r.get("keyword", "")).strip() for r in recent_kw_records[:12] if r.get("keyword")]
    recent_str = ", ".join(recent_list) if recent_list else "none"
    seed_examples = ", ".join(cat_info.get("queries", [])[:4])

    system_prompt = (
        "You are an expert B-Roll stock video search strategist for vertical 9:16 social media reels.\n"
        "Generate EXACTLY 8 short English search queries for Pexels Video Search.\n"
        "STRICT RULES:\n"
        "1. Each query MUST be 2 to 3 plain English words ONLY (never longer than 3 words, so Pexels returns many results).\n"
        "2. Focus ONLY on objects, lighting, retro hardware, screens, hands, or calm scenery.\n"
        "3. NEVER include words related to faces, portraits, selfies, crowds, kids, or animals.\n"
        "4. Return ONLY a valid JSON array of 8 strings. No markdown, no explanations."
    )

    user_prompt = (
        f"Category: {category_name}\n"
        f"Visual Style: {cat_info.get('ai_theme_hint', '')}\n"
        f"Reference Examples (2-3 words): {seed_examples}\n"
        f"Recently Used Queries (DO NOT REPEAT): {recent_str}\n\n"
        "Output JSON array of 8 fresh 2-3 word queries:"
    )

    valid_ai_queries: List[str] = []
    try:
        res = llm_engine.generate_chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.74,
            max_tokens=220,
            enforce_natural_bm=False,
        )
        raw_out = str(res.get("raw_text", ""))
        cleaned = re.sub(r"<think>[\s\S]*?(?:</think>|$)", "", raw_out, flags=re.DOTALL)
        arr_match = re.search(r"\[[\s\S]*?\]", cleaned)
        if arr_match:
            parsed = json.loads(arr_match.group(0))
            if isinstance(parsed, list):
                for item in parsed:
                    sanitized = sanitize_pexels_keyword(str(item), max_words=3)
                    if sanitized and sanitized not in valid_ai_queries:
                        valid_ai_queries.append(sanitized)
    except Exception as exc:
        console.print(f"[yellow]⚠️ Amaran Penjana Kata Kunci AI X14: {exc}[/yellow]")
    finally:
        llm_engine.unload()

    return valid_ai_queries


def _is_video_used_in_sqlite_180d(video_id: str, file_sha256: str = "") -> bool:
    """Semakan sandaran dalam SQLite tempatan sekiranya rekod wujud dalam tempoh 180 hari."""
    if not REELS_HISTORY_DB_PATH.exists():
        return False

    cutoff = int(time.time()) - VIDEO_COOLDOWN_SECONDS
    conn = sqlite3.connect(str(REELS_HISTORY_DB_PATH))
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='reels_post_history'"
        ).fetchone()
        if not cur:
            return False

        row = conn.execute(
            """
            SELECT id FROM reels_post_history
            WHERE posted_epoch > ?
              AND (pexels_ids_json LIKE ? OR (? != '' AND video_sha256_json LIKE ?))
            LIMIT 1
            """,
            (cutoff, f'%"{video_id}"%', file_sha256, f'%"{file_sha256}"%'),
        ).fetchone()
        return row is not None
    except Exception:
        return False
    finally:
        conn.close()


def sanitize_mp4_strip_tmcd_and_audio(raw_mp4: Path, clean_mp4: Path) -> bool:
    """
    Menyalin trek video tulen sahaja (`-map 0:v:0 -c:v copy -an`) menggunakan FFmpeg
    bagi membuang trek masa `tmcd` dan audio asal yang boleh merosakkan MoviePy.
    """
    ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg_bin,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(raw_mp4),
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-an",
        str(clean_mp4),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return proc.returncode == 0 and clean_mp4.exists() and clean_mp4.stat().st_size > 40_000
    except Exception:
        return False


def choose_balanced_category_and_query(
    custom_query: str = "",
    force_gaming_promo: Optional[bool] = None,
    use_ai_keywords: bool = True,
) -> Tuple[str, str, bool, str]:
    """
    Memilih kategori (70% Santai/Estetika/Alam vs 30% Konsol/Retro/Gaming) dan
    menjana kata kunci pendek 2-3 perkataan menggunakan AI X06 + tapisan Redis/Vector.
    Memulangkan: (category_name, chosen_query, is_gaming_eligible, keyword_source)
    """
    if custom_query.strip():
        q_low = custom_query.lower()
        is_gaming = any(
            k in q_low
            for k in ("ps2", "playstation", "console", "retro", "controller", "arcade", "crt", "gaming", "usb", "hdd")
        )
        return "CUSTOM_TOPIC", custom_query.strip(), is_gaming, "MANUAL_CUSTOM"

    if force_gaming_promo is None:
        want_gaming = random.random() < 0.32
    else:
        want_gaming = force_gaming_promo

    eligible_cats = [
        cat
        for cat, info in PEXELS_REELS_CATEGORIES.items()
        if bool(info["is_gaming_promo_eligible"]) == want_gaming
    ]
    if not eligible_cats:
        eligible_cats = list(PEXELS_REELS_CATEGORIES.keys())

    picked_cat = reels_redis.pick_least_used_item(
        pool_name="reels_topic_categories",
        candidate_ids=eligible_cats,
    ) or random.choice(eligible_cats)

    cat_obj = PEXELS_REELS_CATEGORIES[picked_cat]
    is_gaming_eligible = bool(cat_obj["is_gaming_promo_eligible"])
    recent_kw_records = reels_redis.get_rolling_history(stream="pexels_keywords", limit=15)
    now_ep = int(time.time())

    # 1. Cuba kata kunci janaan AI X06 dahulu
    if use_ai_keywords:
        ai_candidates = generate_ai_pexels_keywords(
            category_name=picked_cat,
            cat_info=cat_obj,
            recent_kw_records=recent_kw_records,
        )
        for cand_kw in ai_candidates:
            blocked, _ = _is_keyword_blocked_by_guardrails(cand_kw, recent_kw_records, now_ep)
            if not blocked:
                return picked_cat, cand_kw, is_gaming_eligible, "AI_GENERATED_X06"

    # 2. Sandaran daripada bank benih B-Roll kategori yang melepasi guardrail Redis/Vector
    seed_list: List[str] = list(cat_obj["queries"])
    random.shuffle(seed_list)
    for seed_kw in seed_list:
        blocked, _ = _is_keyword_blocked_by_guardrails(seed_kw, recent_kw_records, now_ep)
        if not blocked:
            return picked_cat, seed_kw, is_gaming_eligible, "SEED_BANK_VETTED"

    return picked_cat, random.choice(seed_list), is_gaming_eligible, "SEED_BANK_FALLBACK"


def _query_and_download_pexels_batch(
    api_key: str,
    query_str: str,
    work_dir: Path,
    desired_count: int,
    seen_batch_sha256: Set[str],
    existing_ids: Set[str],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Menjalankan 1 permintaan Pexels API dan memuat turun klip terbaik yang lulus tapisan."""
    resp = requests.get(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": api_key},
        params={
            "query": query_str,
            "orientation": "portrait",
            "per_page": 80,
            "size": "medium",
        },
        timeout=25,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Pexels API gagal (HTTP {resp.status_code}): {resp.text[:160]}")

    raw_videos = resp.json().get("videos", [])
    candidates: List[Dict[str, Any]] = []
    skipped_face = 0
    skipped_180d = 0

    for vid in raw_videos:
        vid_id = str(vid.get("id", "")).strip()
        if not vid_id or vid_id in existing_ids:
            continue

        if not is_pexels_clip_safe_faceless_or_backview(vid):
            skipped_face += 1
            continue

        dup_redis, _ = reels_redis.is_duplicate_fingerprint(f"pexels_vid:{vid_id}")
        if dup_redis or _is_video_used_in_sqlite_180d(vid_id):
            skipped_180d += 1
            continue

        dur = int(vid.get("duration") or 0)
        if dur < 4 or dur > 45:
            continue

        best_mp4 = None
        for vf in vid.get("video_files", []):
            if vf.get("file_type") == "video/mp4":
                w = int(vf.get("width") or 0)
                h = int(vf.get("height") or 0)
                if h >= w and h >= 720 and vf.get("link"):
                    best_mp4 = vf
                    break

        if not best_mp4:
            for vf in vid.get("video_files", []):
                if vf.get("file_type") == "video/mp4":
                    w = int(vf.get("width") or 0)
                    h = int(vf.get("height") or 0)
                    if h >= w and vf.get("link"):
                        best_mp4 = vf
                        break

        if best_mp4:
            h_val = int(best_mp4.get("height") or 720)
            dur_bonus = 35 if 8 <= dur <= 22 else 12
            candidates.append(
                {
                    "id": vid_id,
                    "pexels_url": str(vid.get("url", "")),
                    "duration": dur,
                    "width": int(best_mp4.get("width") or 720),
                    "height": h_val,
                    "download_link": best_mp4["link"],
                    "score": h_val + dur_bonus,
                }
            )

    candidates.sort(key=lambda x: x["score"], reverse=True)
    top_pool = candidates[: max(desired_count * 3, 12)]
    random.shuffle(top_pool)

    downloaded: List[Dict[str, Any]] = []
    for cand in top_pool:
        if len(downloaded) >= desired_count:
            break

        raw_path = work_dir / f"raw_{cand['id']}.mp4"
        clean_path = work_dir / f"clip_{cand['id']}.mp4"
        try:
            r_dl = requests.get(
                cand["download_link"],
                headers={"User-Agent": "Mozilla/5.0"},
                stream=True,
                timeout=35,
            )
            if r_dl.status_code != 200:
                continue

            with open(raw_path, "wb") as f_out:
                for chunk in r_dl.iter_content(chunk_size=1024 * 512):
                    if chunk:
                        f_out.write(chunk)

            if not raw_path.exists() or raw_path.stat().st_size < 50_000:
                raw_path.unlink(missing_ok=True)
                continue

            sanitized_ok = sanitize_mp4_strip_tmcd_and_audio(raw_path, clean_path)
            raw_path.unlink(missing_ok=True)
            if not sanitized_ok:
                clean_path.unlink(missing_ok=True)
                continue

            sha256_hex = compute_file_sha256(clean_path)
            dup_sha, _ = reels_redis.is_duplicate_fingerprint(f"pexels_sha256:{sha256_hex}")
            if dup_sha or sha256_hex in seen_batch_sha256 or _is_video_used_in_sqlite_180d(cand["id"], sha256_hex):
                clean_path.unlink(missing_ok=True)
                skipped_180d += 1
                continue

            seen_batch_sha256.add(sha256_hex)
            existing_ids.add(cand["id"])
            cand["local_path"] = clean_path
            cand["sha256"] = sha256_hex
            cand["size_mb"] = round(clean_path.stat().st_size / (1024 * 1024), 2)
            downloaded.append(cand)
        except Exception:
            raw_path.unlink(missing_ok=True)
            clean_path.unlink(missing_ok=True)

    batch_stats = {
        "raw_candidates": len(raw_videos),
        "skipped_face_filter": skipped_face,
        "skipped_180d_dedup": skipped_180d,
    }
    return downloaded, batch_stats


def fetch_and_prepare_pexels_clips(
    work_dir: Path,
    desired_count: int = 4,
    custom_query: str = "",
    force_gaming_promo: Optional[bool] = None,
    use_ai_keywords: bool = True,
) -> Dict[str, Any]:
    """
    Menarik calon video menegak 9:16 (1 request utama Pexels API dengan kata kunci AI),
    menapis menggunakan Redis & SQLite (180 hari / 6 bulan), dan memuat turun
    3-5 klip terbaik. Jika hasil < 3 klip, membuat 1 cubaan sandaran kata kunci benih.
    """
    api_key = (
        getattr(cfg, "PS2N_PEXELS_API_KEY", "")
        or os.getenv("PS2N_PEXELS_API_KEY", "")
    ).strip()
    if not api_key:
        raise RuntimeError("Kunci PS2N_PEXELS_API_KEY tiada dalam persekitaran.")

    work_dir.mkdir(parents=True, exist_ok=True)
    desired_count = max(3, min(5, desired_count))

    category_name, query_str, is_gaming_eligible, kw_source = choose_balanced_category_and_query(
        custom_query=custom_query,
        force_gaming_promo=force_gaming_promo,
        use_ai_keywords=use_ai_keywords,
    )

    seen_shas: Set[str] = set()
    seen_ids: Set[str] = set()
    api_requests_used = 1

    prepared_clips, st1 = _query_and_download_pexels_batch(
        api_key=api_key,
        query_str=query_str,
        work_dir=work_dir,
        desired_count=desired_count,
        seen_batch_sha256=seen_shas,
        existing_ids=seen_ids,
    )

    total_raw = st1["raw_candidates"]
    total_skip_face = st1["skipped_face_filter"]
    total_skip_180d = st1["skipped_180d_dedup"]

    # Jika kata kunci AI terlalu khusus (< 3 klip), tambah dari kata kunci benih kategori yang sama
    if len(prepared_clips) < 3 and category_name in PEXELS_REELS_CATEGORIES:
        backup_seeds = [q for q in PEXELS_REELS_CATEGORIES[category_name]["queries"] if q != query_str]
        if backup_seeds:
            backup_q = random.choice(backup_seeds)
            api_requests_used += 1
            extra_clips, st2 = _query_and_download_pexels_batch(
                api_key=api_key,
                query_str=backup_q,
                work_dir=work_dir,
                desired_count=desired_count - len(prepared_clips),
                seen_batch_sha256=seen_shas,
                existing_ids=seen_ids,
            )
            prepared_clips.extend(extra_clips)
            total_raw += st2["raw_candidates"]
            total_skip_face += st2["skipped_face_filter"]
            total_skip_180d += st2["skipped_180d_dedup"]

    return {
        "category": category_name,
        "query": query_str,
        "keyword_source": kw_source,
        "is_gaming_promo_eligible": is_gaming_eligible,
        "clips": prepared_clips,
        "stats": {
            "api_requests_used": api_requests_used,
            "raw_candidates": total_raw,
            "skipped_face_filter": total_skip_face,
            "skipped_180d_dedup": total_skip_180d,
            "selected_count": len(prepared_clips),
        },
    }


def mark_pexels_clips_used_6_months(
    clips: List[Dict[str, Any]],
    category: str,
    query_str: str,
    post_uid: str,
) -> None:
    """
    Mengunci:
    1. Kata kunci Pexels dalam Redis (14 hari), memori rolling 20 carian, & Upstash Vector.
    2. ID video Pexels & cincangan SHA-256 selama 180 hari (6 bulan) dalam Redis.
    """
    now_ep = int(time.time())
    q_slug = re.sub(r"[^a-z0-9]+", "_", query_str.lower()).strip("_")

    reels_redis.mark_dedup_fingerprint(
        f"pexels_query:{q_slug}",
        post_uid=post_uid,
        ttl_seconds=KEYWORD_EXACT_COOLDOWN_DAYS * 86400,
    )
    reels_redis.push_rolling_history(
        record={"keyword": query_str.strip(), "category": category, "posted_epoch": now_ep},
        stream="pexels_keywords",
        max_items=20,
        also_push_global=False,
    )
    reels_redis.increment_rotation_score("reels_topic_categories", category)

    try:
        kw_hash = hashlib.sha1(query_str.strip().lower().encode("utf-8")).hexdigest()[:12]
        reels_vector.upsert_semantic_item(
            item_id=f"kw_{kw_hash}_{now_ep}",
            routing_key="pexels_keywords",
            semantic_text=query_str.strip(),
            metadata={"keyword": query_str.strip(), "category": category, "posted_epoch": now_ep},
            record_type="keyword",
        )
    except Exception:
        pass

    for c in clips:
        vid_id = str(c.get("id", "")).strip()
        sha = str(c.get("sha256", "")).strip()
        if vid_id:
            reels_redis.mark_dedup_fingerprint(
                f"pexels_vid:{vid_id}",
                post_uid=post_uid,
                ttl_seconds=VIDEO_COOLDOWN_SECONDS,
            )
        if sha:
            reels_redis.mark_dedup_fingerprint(
                f"pexels_sha256:{sha}",
                post_uid=post_uid,
                ttl_seconds=VIDEO_COOLDOWN_SECONDS,
            )


if __name__ == "__main__":
    test_dir = TEMP_DIR / "x14_fetcher_probe"
    res = fetch_and_prepare_pexels_clips(work_dir=test_dir, desired_count=3, use_ai_keywords=True)
    tbl = Table(
        title=f"Ujian Kendiri X14 :: {res['category']} ('{res['query']}' | {res['keyword_source']})",
        box=box.ROUNDED,
        header_style="bold cyan",
    )
    tbl.add_column("ID Pexels", style="bold white")
    tbl.add_column("Resolusi", justify="center")
    tbl.add_column("Durasi", justify="right")
    tbl.add_column("Saiz Bersih", justify="right")
    tbl.add_column("SHA-256 (16 aksara)", style="dim")
    for c in res["clips"]:
        tbl.add_row(c["id"], f"{c['width']}x{c['height']}", f"{c['duration']}s", f"{c['size_mb']} MB", c["sha256"][:16])
    console.print(tbl)
    console.print(Panel(f"Statistik: {res['stats']}", border_style="green"))