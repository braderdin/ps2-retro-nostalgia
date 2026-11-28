#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - PENARIK & PENAPIS VIDEO PEXELS 9:16 (X14)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/live-engine-01/X14_pexels_video_fetcher.py
#
# FUNGSI UTAMA:
# 1. Menghantar MAKSIMUM 1 permintaan HTTP ke Pexels Videos API (`per_page=80`,
#    `orientation=portrait`) setiap kali dijalankan bagi menjimatkan kuota API.
# 2. 10 kategori topik luas & menarik (Konsol Retro/PS2, CRT/Arkedia, Setup RGB,
#    Kontroler/Pandangan Belakang & Sisi, Perkakasan/USB/HDD, Malam Hujan,
#    Neon Cyber, Alam Semula Jadi Tenang, Santai Lewat Malam, Audio Retro).
# 3. Tapisan Wajah Pintar: Menolak potret/selfie hadapan & haiwan sensitif,
#    tetapi MEMBENARKAN pandangan orang dari belakang, sisi, atau tangan sahaja.
# 4. Penjara Anti-Ulangan 6 Bulan (180 Hari): Menyemak ID video Pexels & cincangan
#    SHA-256 terhadap Upstash Redis (`ps2n_reels`) serta SQLite tempatan.
# 5. Pembersih Stream FFmpeg (`-map 0:v:0 -c:v copy -an`): Membuang trek data
#    `tmcd` dan audio asal yang sering menyebabkan MoviePy gagal membaca MP4.
# ==============================================================================

import hashlib
import importlib
import os
import random
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import imageio_ffmpeg
import requests
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# ------------------------------------------------------------------------------
# IMPORT MODUL TERAS PROJEK (X01, X03, X04)
# ------------------------------------------------------------------------------
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for p in [CURRENT_DIR, PROJECT_ROOT]:
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

cfg: Any = importlib.import_module("X01_config")
_b2_mod: Any = importlib.import_module("X03_b2_proxy_client")
_redis_mod: Any = importlib.import_module("X04_redis_sharded")

compute_file_sha256: Any = getattr(_b2_mod, "compute_file_sha256")
PS2RedisShardedManager: Any = getattr(_redis_mod, "PS2RedisShardedManager")

# Instans Redis khusus ruang nama Reels
reels_redis = PS2RedisShardedManager(namespace="ps2n_reels")

# ==============================================================================
# KONFIGURASI PEXELS & PENJARA ANTI-ULANGAN 6 BULAN (180 HARI)
# ==============================================================================
DATA_DIR: Path = Path(getattr(cfg, "DATA_DIR", PROJECT_ROOT / "data"))
TEMP_DIR: Path = Path(getattr(cfg, "TEMP_DIR", PROJECT_ROOT / "temp"))
REELS_HISTORY_DB_PATH: Path = DATA_DIR / "ps2_reels_posts_history.db"

VIDEO_COOLDOWN_DAYS: int = 180
VIDEO_COOLDOWN_SECONDS: int = VIDEO_COOLDOWN_DAYS * 86400  # 6 Bulan (15,552,000 saat)

# 10 Kategori Topik Bebas & Menarik (30% Retro/Konsol/Gaming + 70% Gaya Hidup/Estetika/Alam)
PEXELS_REELS_CATEGORIES: Dict[str, Dict[str, Any]] = {
    "PS2_RETRO_CONSOLE": {
        "is_gaming_promo_eligible": True,
        "queries": [
            "retro video game console",
            "playstation controller neon",
            "classic game controller",
            "retro gaming setup",
        ],
    },
    "CRT_ARCADE_NOSTALGIA": {
        "is_gaming_promo_eligible": True,
        "queries": [
            "arcade game machine neon",
            "retro television screen",
            "vintage arcade cabinet",
            "retro crt monitor",
        ],
    },
    "GAMING_SETUP_RGB": {
        "is_gaming_promo_eligible": True,
        "queries": [
            "gaming setup rgb desk",
            "gaming room neon lights",
            "dual monitor gaming desk",
            "mechanical keyboard rgb",
        ],
    },
    "CONTROLLER_POV_BACKVIEW": {
        "is_gaming_promo_eligible": True,
        "queries": [
            "hands holding game controller",
            "gamer playing from behind",
            "esports player back view",
            "playing video game controller",
        ],
    },
    "STORAGE_TECH_HARDWARE": {
        "is_gaming_promo_eligible": True,
        "queries": [
            "usb flash drive technology",
            "external hard drive desk",
            "computer hardware circuit",
            "retro electronics macro",
        ],
    },
    "COZY_RAIN_NIGHT": {
        "is_gaming_promo_eligible": False,
        "queries": [
            "rain on window night city",
            "rainy night street neon",
            "cozy bedroom night lamp",
            "moody night desk rain",
        ],
    },
    "CYBER_NEON_VIBE": {
        "is_gaming_promo_eligible": False,
        "queries": [
            "cyberpunk neon city night",
            "purple blue neon lights",
            "neon street reflection night",
            "synthwave neon grid",
        ],
    },
    "NATURE_CALM_ESCAPE": {
        "is_gaming_promo_eligible": False,
        "queries": [
            "misty forest morning trees",
            "calm river flowing nature",
            "raindrops on green leaves",
            "peaceful sunset clouds sky",
        ],
    },
    "LATE_NIGHT_CHILL": {
        "is_gaming_promo_eligible": False,
        "queries": [
            "coffee mug night desk",
            "study desk warm lamp night",
            "midnight city skyline lights",
            "warm cozy room evening",
        ],
    },
    "TECH_AUDIO_RETRO": {
        "is_gaming_promo_eligible": False,
        "queries": [
            "headphones on gaming desk",
            "cassette tape retro music",
            "vinyl record player vintage",
            "retro radio speaker",
        ],
    },
}

# Tolak potret/wajah hadapan & haiwan sensitif; benarkan pandangan belakang/sisi/tangan
STRICT_FACE_AND_SENSITIVE_SLUGS = {
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
    "talking-to-camera",
    "looking-at-camera",
    "closeup-of-face",
    "model-posing",
    "makeup",
    "beauty",
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

BACK_OR_SIDE_VIEW_HINTS = {
    "behind",
    "back",
    "rear",
    "side",
    "silhouette",
    "hands",
    "hand",
    "pov",
    "over-the-shoulder",
}


def is_pexels_clip_safe_faceless_or_backview(video_obj: Dict[str, Any]) -> bool:
    """
    Menolak video berunsur potret muka hadapan / selfie atau haiwan sensitif,
    tetapi membenarkan klip tanpa manusia serta klip pandangan belakang/sisi/tangan.
    """
    slug = str(video_obj.get("url", "")).lower()
    tokens = set(re.split(r"[^a-z0-9]+", slug))

    if tokens.intersection(STRICT_FACE_AND_SENSITIVE_SLUGS):
        return False

    # Jika perkataan umum manusia disebut, pastikan ia pandangan belakang/sisi/tangan
    generic_people = {"man", "men", "woman", "women", "person", "people", "guy", "girl", "gamer", "player"}
    if tokens.intersection(generic_people):
        if not tokens.intersection(BACK_OR_SIDE_VIEW_HINTS):
            return False

    return True


def _is_video_used_in_sqlite_180d(video_id: str, file_sha256: str = "") -> bool:
    """Semakan sandaran dalam SQLite tempatan sekiranya rekod wujud dalam tempoh 180 hari."""
    if not REELS_HISTORY_DB_PATH.exists():
        return False
    import time

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
) -> Tuple[str, str, bool]:
    """
    Memilih kategori dan kata kunci Pexels secara adil menggunakan Redis
    (70% kategori santai/umum/alam vs 30% kategori konsol retro/gaming/hardware).
    """
    if custom_query.strip():
        q_low = custom_query.lower()
        is_gaming = any(
            k in q_low
            for k in ("ps2", "playstation", "console", "retro", "controller", "arcade", "crt", "gaming", "usb", "hdd")
        )
        return "CUSTOM_TOPIC", custom_query.strip(), is_gaming

    # Tentukan sama ada giliran ini 30% Retro/Gaming atau 70% Umum/Santai
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
    queries: List[str] = list(cat_obj["queries"])

    # Elakkan kata kunci yang baru digunakan dalam 10 hari terakhir
    fresh_queries = []
    for q in queries:
        slug = re.sub(r"[^a-z0-9]+", "_", q.lower()).strip("_")
        used_q, _ = reels_redis.is_duplicate_fingerprint(f"pexels_query:{slug}")
        if not used_q:
            fresh_queries.append(q)

    chosen_query = random.choice(fresh_queries or queries)
    return picked_cat, chosen_query, bool(cat_obj["is_gaming_promo_eligible"])


def fetch_and_prepare_pexels_clips(
    work_dir: Path,
    desired_count: int = 4,
    custom_query: str = "",
    force_gaming_promo: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Menarik calon video menegak 9:16 melalui 1 permintaan Pexels API,
    menapis menggunakan Redis & SQLite (180 hari / 6 bulan), memuat turun
    3-5 klip terbaik, membuang trek `tmcd`/audio via FFmpeg, dan mengira SHA-256.
    """
    api_key = (
        getattr(cfg, "PS2N_PEXELS_API_KEY", "")
        or os.getenv("PS2N_PEXELS_API_KEY", "")
    ).strip()
    if not api_key:
        raise RuntimeError("Kunci PS2N_PEXELS_API_KEY tiada dalam persekitaran.")

    work_dir.mkdir(parents=True, exist_ok=True)
    desired_count = max(3, min(5, desired_count))

    category_name, query_str, is_gaming_eligible = choose_balanced_category_and_query(
        custom_query=custom_query,
        force_gaming_promo=force_gaming_promo,
    )

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
        if not vid_id:
            continue

        if not is_pexels_clip_safe_faceless_or_backview(vid):
            skipped_face += 1
            continue

        # Penjara 6 Bulan (180 Hari) pada Redis & SQLite
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

    prepared_clips: List[Dict[str, Any]] = []
    seen_batch_sha256 = set()

    for cand in top_pool:
        if len(prepared_clips) >= desired_count:
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

            # Buang trek data `tmcd` & audio asal supaya 100% serasi dengan MoviePy
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
            cand["local_path"] = clean_path
            cand["sha256"] = sha256_hex
            cand["size_mb"] = round(clean_path.stat().st_size / (1024 * 1024), 2)
            prepared_clips.append(cand)
        except Exception:
            raw_path.unlink(missing_ok=True)
            clean_path.unlink(missing_ok=True)

    return {
        "category": category_name,
        "query": query_str,
        "is_gaming_promo_eligible": is_gaming_eligible,
        "clips": prepared_clips,
        "stats": {
            "api_requests_used": 1,
            "raw_candidates": len(raw_videos),
            "skipped_face_filter": skipped_face,
            "skipped_180d_dedup": skipped_180d,
            "selected_count": len(prepared_clips),
        },
    }


def mark_pexels_clips_used_6_months(
    clips: List[Dict[str, Any]],
    category: str,
    query_str: str,
    post_uid: str,
) -> None:
    """Mengunci ID video Pexels & SHA-256 selama 180 hari (6 bulan) dalam Redis."""
    q_slug = re.sub(r"[^a-z0-9]+", "_", query_str.lower()).strip("_")
    reels_redis.mark_dedup_fingerprint(
        f"pexels_query:{q_slug}",
        post_uid=post_uid,
        ttl_seconds=10 * 86400,
    )
    reels_redis.increment_rotation_score("reels_topic_categories", category)

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
    res = fetch_and_prepare_pexels_clips(work_dir=test_dir, desired_count=3)
    tbl = Table(
        title=f"Ujian Kendiri X14 :: {res['category']} ('{res['query']}')",
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