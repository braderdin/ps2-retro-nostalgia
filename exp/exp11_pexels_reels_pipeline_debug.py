#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - UJIAN PENUH PIPELINE PEXELS + OCR + REELS (EXP11)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/exp/exp11_pexels_reels_pipeline_debug.py
#
# TUJUAN UJIAN:
# 1. Menguji import & kesihatan modul asas (X01-X07, X09, X12) + 200+ muzik di
#    /home/braderdin/ps2-retro-nostalgia/assets/music/ (Mutagen + Librosa BPM).
# 2. Menarik video Pexels 9:16 (MAKSIMUM 1 REQUEST API per sesi) dengan topik
#    bebas (retro konsol, gaming, perkakasan, suasana malam/alam), menapis
#    guna Redis (namespace 'ps2n_reels'), dan memilih 3-5 klip terbaik.
# 3. Mengekstrak maksimum 10 gambar snapshot daripada klip terpilih, memampat
#    < 30 KB, dan menghantar ke X07 Vision/OCR secara berperingkat (MAKSIMUM 3-4
#    gambar per panggilan OCR) sambil menyimpan hasil demi hasil ke .json & .db
#    di dalam /home/braderdin/ps2-retro-nostalgia/temp/.
# 4. Memotong & mencantum 3-5 klip menjadi video menegak 1080x1920 berdurasi
#    30-50 saat berserta muzik latar dari assets/music/ (MoviePy v2).
# 5. Menjana kapsyen Persona AI santai (sedar waktu MYT, hari, mood & watak
#    berbeza) + semakan semantik X05 Vector.
# 6. Menguji aliran penerbitan: FB Reels dahulu -> jika berjaya langkau Feed;
#    jika Reels gagal -> fallback automatik ke FB Feed Video.
# ==============================================================================

import argparse
import hashlib
import hmac
import importlib
import json
import os
import random
import re
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests
from PIL import Image
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# ------------------------------------------------------------------------------
# PENYEDIAAN LALUAN PROJEK & IMPORT MODUL ASAS (X01 - X07, X09, X12)
# ------------------------------------------------------------------------------
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
ENGINE_DIR = PROJECT_ROOT / "live-engine-01"

for p in [ENGINE_DIR, PROJECT_ROOT]:
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _safe_import(mod_name: str) -> Tuple[Optional[Any], str]:
    try:
        mod = importlib.import_module(mod_name)
        return mod, "OK"
    except Exception as exc:
        return None, f"GAGAL ({exc})"


cfg, st_x01 = _safe_import("X01_config")
_x02, st_x02 = _safe_import("X02_sqlite_ledger")
_x03, st_x03 = _safe_import("X03_b2_proxy_client")
_x04, st_x04 = _safe_import("X04_redis_sharded")
_x05, st_x05 = _safe_import("X05_vector_sharded")
_x06, st_x06 = _safe_import("X06_local_llm_engine")
_x07, st_x07 = _safe_import("X07_vision_ocr_analyzer")
_x09, st_x09 = _safe_import("X09_media_harvester")
_x12, st_x12 = _safe_import("X12_telegram_notifier")

# Pustaka Audio & Video (MoviePy v2 + Mutagen + Librosa)
try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None

try:
    import librosa
    LIBROSA_OK = True
except ImportError:
    librosa = None
    LIBROSA_OK = False

try:
    from moviepy import AudioFileClip, VideoFileClip, concatenate_videoclips
    MOVIEPY_V2 = True
except ImportError:
    from moviepy.editor import AudioFileClip, VideoFileClip, concatenate_videoclips  # type: ignore
    MOVIEPY_V2 = False


# ==============================================================================
# KONFIGURASI & LALUAN UJIAN EXP11
# ==============================================================================
MYT = timezone(timedelta(hours=8))

TEMP_DIR: Path = Path(getattr(cfg, "TEMP_DIR", PROJECT_ROOT / "temp"))
DATA_DIR: Path = Path(getattr(cfg, "DATA_DIR", PROJECT_ROOT / "data"))
MUSIC_DIR: Path = PROJECT_ROOT / "assets" / "music"
WORK_DIR: Path = TEMP_DIR / "exp11_reels_work"

OCR_SESSION_JSON: Path = TEMP_DIR / "exp11_ocr_sessions.json"
DEBUG_STATE_DB: Path = TEMP_DIR / "exp11_reels_debug.db"
REPORT_OUTPUT_JSON: Path = TEMP_DIR / "exp11_last_reels_test.json"

# Tetapan Klip & OCR Berperingkat
MIN_CLIPS_PICK: int = 3
MAX_CLIPS_PICK: int = 5
MAX_IMAGES_PER_OCR_BATCH: int = 4   # Maksimum 3-4 gambar per panggilan Vision/OCR X07
MAX_TOTAL_SNAPSHOTS: int = 10       # Maksimum keseluruhan 10 gambar per video Reels
REEL_MIN_DURATION_SEC: int = 30
REEL_MAX_DURATION_SEC: int = 50
TARGET_WIDTH: int = 1080
TARGET_HEIGHT: int = 1920

# Katalog Topik Bebas Pexels (Gaming, Retro Konsol, Hardware, Suasana Santai/Alam)
PEXELS_TOPIC_POOLS: Dict[str, List[str]] = {
    "RETRO_CONSOLE": [
        "retro video game console",
        "playstation controller neon",
        "arcade game machine neon",
        "retro television gaming",
    ],
    "GAMING_HARDWARE": [
        "gaming setup rgb desk",
        "mechanical keyboard neon",
        "usb flash drive technology",
        "game controller close up",
    ],
    "NIGHT_CHILL_AESTHETIC": [
        "rain window night city",
        "neon street night rain",
        "cozy desk lamp night",
        "cyberpunk neon lights",
    ],
    "NATURE_RELAX": [
        "misty forest morning nature",
        "calm river forest trees",
        "sunset clouds peaceful sky",
        "raindrops leaves nature",
    ],
}

FORBIDDEN_SLUG_TERMS = {
    "man", "men", "woman", "women", "girl", "girls", "boy", "boys",
    "person", "people", "face", "faces", "portrait", "selfie", "vlog",
    "model", "posing", "smile", "smiling", "headshot", "streamer",
    "dog", "dogs", "puppy", "pig", "pigs", "pork",
}

# Pelbagai Watak & Karakter Mengikut Hari (7 Hari Seminggu Berbeza)
DAILY_CHARACTERS: List[Dict[str, str]] = [
    {
        "day_idx": "0",
        "char_id": "CHAR_ISNIN_TECH",
        "name": "Abang Bengkel Konsol (Santai & Praktikal)",
        "voice": "Gaya abang 35 tahun yang suka belek perkakasan, kabel, storan dan setup bilik game. Bercerita ringkas dan terus terang.",
    },
    {
        "day_idx": "1",
        "char_id": "CHAR_SELASA_NOSTALGIA",
        "name": "Kaki Kedai Game 2000-an",
        "voice": "Gaya member lama mengimbau kenangan zaman sekolah, berebut kontroler, dan keseronokan main game klasik lepas balik sekolah.",
    },
    {
        "day_idx": "2",
        "char_id": "CHAR_RABU_CHILL",
        "name": "Penglipur Lara Santai Tengah Minggu",
        "voice": "Nada tenang dan relaks, mengajak pembaca tarik nafas sekejap dari penat kerja sambil layan visual retro dan muzik latar.",
    },
    {
        "day_idx": "3",
        "char_id": "CHAR_KHAMIS_CURATOR",
        "name": "Kurator Koleksi & Budaya Retro",
        "voice": "Menghargai seni visual, bunyi klasik, dan keunikan teknologi era 90-an hingga 2000-an dengan bahasa yang hidup.",
    },
    {
        "day_idx": "4",
        "char_id": "CHAR_JUMAAT_MARATHON",
        "name": "Kaki Lepak Malam Minggu",
        "voice": "Bersemangat menyambut malam minggu dan hujung minggu, bercerita tentang suasana marathon game sampai lewat malam.",
    },
    {
        "day_idx": "5",
        "char_id": "CHAR_SABTU_SETUP",
        "name": "Peminat Setup Bilik & Estetika",
        "voice": "Suka memerhati pencahayaan neon, susun atur meja, dan suasana bilik yang selesa untuk melayan hobi.",
    },
    {
        "day_idx": "6",
        "char_id": "CHAR_AHAD_REFLEKSI",
        "name": "Abang Santai Hujung Minggu",
        "voice": "Nada mesra keluarga dan nostalgia santai hari Ahad sebelum masuk minggu kerja baharu.",
    },
]


# ==============================================================================
# 1. KONTEKS MASA MALAYSIA (MYT), WATAK HARIAN & MOOD
# ==============================================================================
def build_myt_persona_context() -> Dict[str, Any]:
    now = datetime.now(tz=MYT)
    days_bm = ["Isnin", "Selasa", "Rabu", "Khamis", "Jumaat", "Sabtu", "Ahad"]
    months_bm = [
        "Januari", "Februari", "Mac", "April", "Mei", "Jun",
        "Julai", "Ogos", "September", "Oktober", "November", "Disember",
    ]
    wd = now.weekday()
    hour = now.hour
    character = DAILY_CHARACTERS[wd % len(DAILY_CHARACTERS)]

    if 5 <= hour < 12:
        period = "Pagi"
        mood = "Segar, tenang & membangkitkan semangat pagi"
        allowed_cues = "pagi ni, awal pagi ni, hari ni"
        forbidden_cues = "tengah hari ni, petang ni, malam ni, tengah malam"
    elif 12 <= hour < 15:
        period = "Tengah Hari"
        mood = "Santai waktu rehat tengah hari"
        allowed_cues = "tengah hari ni, waktu rehat ni, hari ni"
        forbidden_cues = "pagi ni, selamat pagi, petang ni, malam ni, tengah malam"
    elif 15 <= hour < 19:
        period = "Petang"
        mood = "Relaks lepas habis kerja atau kelas"
        allowed_cues = "petang ni, lewat petang ni, hari ni"
        forbidden_cues = "pagi ni, selamat pagi, tengah hari ni, malam ni, tengah malam"
    elif 19 <= hour <= 23:
        period = "Malam"
        mood = "Nostalgia malam & lepak santai"
        allowed_cues = "malam ni, malam-malam macam ni"
        forbidden_cues = "pagi ni, selamat pagi, tengah hari ni, petang ni, tengah malam ni"
    else:
        period = "Tengah Malam"
        mood = "Sunyi, mendalam & layan perasaan lewat malam"
        allowed_cues = "lewat malam ni, tengah malam ni"
        forbidden_cues = "pagi yang cerah, selamat pagi, tengah hari ni, petang ni"

    ts_label = (
        f"{days_bm[wd]}, {now.day:02d} {months_bm[now.month - 1]} {now.year} | "
        f"{now.strftime('%I:%M %p')} MYT ({period})"
    )
    return {
        "now_iso": now.isoformat(),
        "now_epoch": int(now.timestamp()),
        "day_name": days_bm[wd],
        "period": period,
        "mood": mood,
        "allowed_cues": allowed_cues,
        "forbidden_cues": forbidden_cues,
        "timestamp_label": ts_label,
        "character": character,
    }


# ==============================================================================
# 2. PANGKALAN DATA SEMENTARA & JSON RUJUKAN OCR DI /temp/
# ==============================================================================
class TempOcrStateStore:
    """
    Menyimpan hasil analisis OCR gambar demi gambar & sesi demi sesi ke dalam:
    - /home/braderdin/ps2-retro-nostalgia/temp/exp11_ocr_sessions.json
    - /home/braderdin/ps2-retro-nostalgia/temp/exp11_reels_debug.db
    """

    def __init__(self, json_path: Path, db_path: Path) -> None:
        self.json_path = json_path
        self.db_path = db_path
        self.records: List[Dict[str, Any]] = []
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._flush_json()

    def _init_db(self) -> None:
        if self.db_path.exists():
            self.db_path.unlink()
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                """
                CREATE TABLE ocr_frame_insights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no INTEGER NOT NULL,
                    frame_no INTEGER NOT NULL,
                    clip_id TEXT NOT NULL,
                    timestamp_sec REAL NOT NULL,
                    compressed_kb REAL NOT NULL,
                    detected_objects TEXT NOT NULL,
                    ocr_text TEXT NOT NULL,
                    console_platform TEXT NOT NULL,
                    visual_category TEXT NOT NULL,
                    has_human_face INTEGER NOT NULL,
                    retro_nostalgia_angle TEXT NOT NULL,
                    recorded_at_iso TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _flush_json(self) -> None:
        payload = {
            "updated_at": datetime.now(tz=MYT).isoformat(),
            "total_frames_processed": len(self.records),
            "frames": self.records,
        }
        self.json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def append_frame_insight(self, item: Dict[str, Any]) -> None:
        self.records.append(item)
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                """
                INSERT INTO ocr_frame_insights (
                    batch_no, frame_no, clip_id, timestamp_sec, compressed_kb,
                    detected_objects, ocr_text, console_platform, visual_category,
                    has_human_face, retro_nostalgia_angle, recorded_at_iso
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["batch_no"],
                    item["frame_no"],
                    item["clip_id"],
                    item["timestamp_sec"],
                    item["compressed_kb"],
                    json.dumps(item["detected_objects"], ensure_ascii=False),
                    item["ocr_text"],
                    item["console_platform"],
                    item["visual_category"],
                    1 if item["has_human_face"] else 0,
                    item["retro_nostalgia_angle"],
                    item["recorded_at_iso"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
        self._flush_json()


# ==============================================================================
# 3. PEMILIH MUZIK DARI assets/music/ + ANALISIS RENTAK LIBROSA
# ==============================================================================
def select_and_analyze_music(
    reels_redis: Any,
    target_duration: int = 38,
) -> Tuple[Optional[Any], Dict[str, Any], int]:
    """
    Mengimbas folder /home/braderdin/ps2-retro-nostalgia/assets/music/,
    memilih 1 trek yang paling kurang digunakan (via Redis), mengekstrak metadata
    Mutagen, dan mengesan BPM serta ketukan rentak menggunakan Librosa.
    """
    supported_exts = (".mp3", ".mp4", ".m4a", ".wav", ".aac", ".ogg")
    if not MUSIC_DIR.exists():
        raise FileNotFoundError(f"Folder muzik tidak ditemui: {MUSIC_DIR}")

    all_tracks = sorted(
        [p for p in MUSIC_DIR.iterdir() if p.is_file() and p.suffix.lower() in supported_exts]
    )
    total_found = len(all_tracks)
    if total_found == 0:
        raise RuntimeError(f"Tiada fail audio/video muzik dijumpai dalam {MUSIC_DIR}")

    # Ambil sampel rawak 25 trek dan pilih yang paling jarang diguna di Redis
    sample_pool = random.sample(all_tracks, min(25, total_found))
    track_map = {p.name: p for p in sample_pool}
    picked_name = reels_redis.pick_least_used_item(
        pool_name="reels_music_tracks",
        candidate_ids=list(track_map.keys()),
    )
    chosen_path = track_map.get(picked_name or "", random.choice(sample_pool))

    # Ekstrak metadata ID3 menggunakan Mutagen
    clean_stem = re.sub(r"[_\-]+", " ", chosen_path.stem).strip().title()
    title = clean_stem or "Retro Nostalgia Ambient"
    artist = "Meta Sound Collection"
    genre = "Instrumental"

    if MutagenFile is not None:
        try:
            tag = MutagenFile(str(chosen_path))
            if tag and hasattr(tag, "get"):
                t_raw = tag.get("\xa9nam") or tag.get("TIT2") or tag.get("title")
                a_raw = tag.get("\xa9ART") or tag.get("TPE1") or tag.get("artist")
                g_raw = tag.get("\xa9gen") or tag.get("TCON") or tag.get("genre")
                if isinstance(t_raw, list):
                    t_raw = t_raw[0]
                if isinstance(a_raw, list):
                    a_raw = a_raw[0]
                if isinstance(g_raw, list):
                    g_raw = g_raw[0]
                if t_raw and not str(t_raw).strip().isdigit():
                    title = str(t_raw).strip()
                if a_raw and not str(a_raw).strip().isdigit():
                    artist = str(a_raw).strip()
                if g_raw:
                    genre = str(g_raw).strip()
        except Exception:
            pass

    # Baca audio dengan MoviePy & analisis tempo BPM dengan Librosa
    bpm_val = 0.0
    beat_times: List[float] = []
    audio_clip = AudioFileClip(str(chosen_path))
    full_audio_dur = float(audio_clip.duration or 0.0)

    if LIBROSA_OK and full_audio_dur > 2.0:
        try:
            sr = 22050
            scan_dur = min(full_audio_dur, float(target_duration + 6))
            sub_scan = (
                audio_clip.subclipped(0, scan_dur)
                if hasattr(audio_clip, "subclipped")
                else audio_clip.subclip(0, scan_dur)
            )
            arr = sub_scan.to_soundarray(fps=sr)
            sub_scan.close()
            mono_y = np.mean(arr, axis=1).astype(np.float32) if arr.ndim > 1 else arr.astype(np.float32)
            tempo, beat_frames = librosa.beat.beat_track(y=mono_y, sr=sr)
            bpm_val = round(float(tempo.item()) if hasattr(tempo, "item") else float(tempo), 1)
            beat_times = [round(float(t), 2) for t in librosa.frames_to_time(beat_frames, sr=sr).tolist()]
        except Exception as exc:
            console.print(f"[yellow]⚠️ Amaran Librosa BPM ({chosen_path.name}): {exc}[/yellow]")

    # Potong klip audio mengikut sasaran durasi video
    start_sec = 0.0
    if full_audio_dur > target_duration + 4:
        start_sec = float(random.randint(0, int(full_audio_dur - target_duration - 2)))
    end_sec = min(full_audio_dur, start_sec + float(target_duration))

    cut_audio = (
        audio_clip.subclipped(start_sec, end_sec)
        if hasattr(audio_clip, "subclipped")
        else audio_clip.subclip(start_sec, end_sec)
    )

    vibe = (
        f"Rentak Ceria & Dinamik ({bpm_val} BPM)"
        if bpm_val >= 115
        else f"Alunan Sederhana Santai ({bpm_val} BPM)"
        if bpm_val >= 85
        else f"Muzik Lo-Fi / Ambient Tenang ({bpm_val} BPM)"
    )

    music_meta = {
        "filename": chosen_path.name,
        "file_path": str(chosen_path),
        "title": title,
        "artist": artist,
        "genre": genre,
        "full_duration_sec": round(full_audio_dur, 1),
        "bpm": bpm_val,
        "beat_count": len(beat_times),
        "beat_timestamps": beat_times,
        "vibe": vibe,
    }
    return cut_audio, music_meta, total_found


# ==============================================================================
# 4. PENARIK PEXELS 1-REQUEST + TAPISAN REDIS + MUAT TURUN 3-5 KLIP TERBAIK
# ==============================================================================
def fetch_and_download_pexels_clips(
    reels_redis: Any,
    desired_clips: int = 4,
    custom_query: str = "",
) -> Tuple[str, str, List[Dict[str, Any]], Dict[str, Any]]:
    """
    Menghantar MAKSIMUM 1 permintaan HTTP ke Pexels Videos API (`per_page=80`),
    menapis video berwajah & ID yang sudah digunakan dalam Redis `ps2n_reels`,
    dan memuat turun 3 hingga 5 klip terbaik ke temp/.
    """
    api_key = (
        getattr(cfg, "PS2N_PEXELS_API_KEY", "")
        or os.getenv("PS2N_PEXELS_API_KEY", "")
    ).strip()
    if not api_key:
        raise RuntimeError("Kunci PS2N_PEXELS_API_KEY tiada dalam .env.local atau GitHub Secrets.")

    if custom_query.strip():
        category_name = "CUSTOM_QUERY"
        query_str = custom_query.strip()
    else:
        category_name = random.choice(list(PEXELS_TOPIC_POOLS.keys()))
        query_str = random.choice(PEXELS_TOPIC_POOLS[category_name])

    # Satu (1) permintaan API sahaja!
    t0 = time.perf_counter()
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
    api_ms = round((time.perf_counter() - t0) * 1000, 1)

    if resp.status_code != 200:
        raise RuntimeError(f"Pexels API HTTP {resp.status_code}: {resp.text[:160]}")

    raw_videos = resp.json().get("videos", [])
    vetted_candidates: List[Dict[str, Any]] = []
    skipped_face = 0
    skipped_redis = 0

    for vid in raw_videos:
        vid_id = str(vid.get("id", "")).strip()
        if not vid_id:
            continue

        slug = str(vid.get("url", "")).lower()
        tokens = set(re.split(r"[^a-z0-9]+", slug))
        if tokens.intersection(FORBIDDEN_SLUG_TERMS):
            skipped_face += 1
            continue

        is_used, _ = reels_redis.is_duplicate_fingerprint(f"pexels_vid:{vid_id}")
        if is_used:
            skipped_redis += 1
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
            # Skor kualiti klip (utamakan resolusi menegak HD & durasi ideal 8-20s)
            h_val = int(best_mp4.get("height") or 720)
            dur_bonus = 30 if 8 <= dur <= 22 else 10
            vetted_candidates.append(
                {
                    "id": vid_id,
                    "duration": dur,
                    "width": int(best_mp4.get("width") or 720),
                    "height": h_val,
                    "link": best_mp4["link"],
                    "quality_score": h_val + dur_bonus,
                }
            )

    # Susun terbaik & ambil 3-5 klip
    vetted_candidates.sort(key=lambda x: x["quality_score"], reverse=True)
    top_pool = vetted_candidates[: max(desired_clips * 3, 10)]
    random.shuffle(top_pool)

    downloaded_clips: List[Dict[str, Any]] = []
    for cand in top_pool:
        if len(downloaded_clips) >= desired_clips:
            break
        out_mp4 = WORK_DIR / f"pexels_{cand['id']}.mp4"
        try:
            r_dl = requests.get(cand["link"], headers={"User-Agent": "Mozilla/5.0"}, stream=True, timeout=35)
            if r_dl.status_code == 200:
                with open(out_mp4, "wb") as f_out:
                    for chunk in r_dl.iter_content(chunk_size=1024 * 512):
                        if chunk:
                            f_out.write(chunk)
                if out_mp4.exists() and out_mp4.stat().st_size > 50_000:
                    cand["local_path"] = out_mp4
                    cand["size_mb"] = round(out_mp4.stat().st_size / (1024 * 1024), 2)
                    downloaded_clips.append(cand)
        except Exception:
            out_mp4.unlink(missing_ok=True)

    fetch_stats = {
        "api_requests_used": 1,
        "api_latency_ms": api_ms,
        "raw_returned": len(raw_videos),
        "skipped_face_filter": skipped_face,
        "skipped_redis_dedup": skipped_redis,
        "vetted_pool": len(vetted_candidates),
        "downloaded_count": len(downloaded_clips),
    }
    return category_name, query_str, downloaded_clips, fetch_stats


# ==============================================================================
# 5. SNAPSHOT MAKSIMUM 10 GAMBAR & PROSES OCR BERPERINGKAT (3-4 GAMBAR / SESI)
# ==============================================================================
def extract_and_analyze_snapshots_in_batches(
    clips: List[Dict[str, Any]],
    topic_hint: str,
    state_store: TempOcrStateStore,
    vision_analyzer: Any,
    max_total_images: int = MAX_TOTAL_SNAPSHOTS,
    batch_size: int = MAX_IMAGES_PER_OCR_BATCH,
) -> List[Dict[str, Any]]:
    """
    Mengekstrak sehingga 10 gambar snapshot kronologi merentas 3-5 klip video,
    kemudian menghantar ke X07 Vision/OCR dalam kumpulan 3-4 gambar per sesi
    dan menyimpan hasil setiap sesi ke .json & .db di dalam /temp/.
    """
    # Agihkan kuota 10 snapshot secara sekata merentas klip
    n_clips = len(clips)
    base_per_clip = max_total_images // n_clips
    extra = max_total_images % n_clips
    quotas = [base_per_clip + (1 if i < extra else 0) for i in range(n_clips)]

    snapshot_tasks: List[Dict[str, Any]] = []
    frame_counter = 0

    for clip_obj, q_count in zip(clips, quotas):
        if q_count <= 0:
            continue
        v_clip = VideoFileClip(str(clip_obj["local_path"]))
        dur = float(v_clip.duration or 1.0)
        try:
            for j in range(q_count):
                # Titik masa sekata antara 15% hingga 85% durasi klip
                ratio = 0.15 + (0.70 * (j + 0.5) / q_count)
                t_sec = round(min(max(0.2, dur * ratio), max(0.3, dur - 0.2)), 2)
                frame_arr = v_clip.get_frame(t_sec)

                frame_counter += 1
                snap_path = WORK_DIR / f"snap_{frame_counter:02d}_clip_{clip_obj['id']}.jpg"
                im = Image.fromarray(frame_arr).convert("RGB")
                im.save(snap_path, format="JPEG", quality=85)

                snapshot_tasks.append(
                    {
                        "frame_no": frame_counter,
                        "clip_id": clip_obj["id"],
                        "timestamp_sec": t_sec,
                        "snap_path": snap_path,
                    }
                )
        finally:
            v_clip.close()

    all_ocr_insights: List[Dict[str, Any]] = []
    total_batches = (len(snapshot_tasks) + batch_size - 1) // batch_size

    try:
        for b_idx in range(total_batches):
            batch_slice = snapshot_tasks[b_idx * batch_size : (b_idx + 1) * batch_size]
            batch_paths = [item["snap_path"] for item in batch_slice]
            batch_no = b_idx + 1

            console.print(
                f"  [cyan]🔍 [Sesi OCR {batch_no}/{total_batches}][/cyan] "
                f"Memproses {len(batch_paths)} gambar (Bingkai #{batch_slice[0]['frame_no']}..#{batch_slice[-1]['frame_no']}) < 30 KB..."
            )

            ok_v, v_items, v_meta = vision_analyzer.analyze_images(
                image_sources=batch_paths,
                save_compressed_dir=WORK_DIR / f"ocr_batch_{batch_no:02d}",
                auto_unload=False,  # Kekal dalam RAM sepanjang 3 sesi batch
                context_hint=topic_hint,
                skip_failed=True,
            )

            if not ok_v or not v_items:
                console.print(f"  [yellow]⚠️ Amaran Sesi OCR {batch_no}: {v_meta.get('error', 'Tiada item sah')}[/yellow]")
                continue

            for local_i, v_res in enumerate(v_items):
                src_task = batch_slice[local_i] if local_i < len(batch_slice) else batch_slice[-1]
                insight_row = {
                    "batch_no": batch_no,
                    "frame_no": src_task["frame_no"],
                    "clip_id": src_task["clip_id"],
                    "timestamp_sec": src_task["timestamp_sec"],
                    "compressed_kb": v_res.get("compressed_kb", 0.0),
                    "detected_objects": v_res.get("detected_objects", []),
                    "ocr_text": v_res.get("ocr_text", "None"),
                    "console_platform": v_res.get("console_platform", "General Gaming"),
                    "visual_category": v_res.get("visual_category", "cat_reels_clip"),
                    "has_human_face": bool(v_res.get("has_human_face", False)),
                    "retro_nostalgia_angle": v_res.get("retro_nostalgia_angle", ""),
                    "recorded_at_iso": datetime.now(tz=MYT).isoformat(),
                }
                state_store.append_frame_insight(insight_row)
                all_ocr_insights.append(insight_row)

            console.print(
                f"  [green]✅ Sesi OCR {batch_no}/{total_batches} selesai[/green] "
                f"({v_meta.get('inference_sec', 0)}s | Max saiz: {v_meta.get('max_compressed_kb', 0)} KB) -> Disimpan ke JSON & DB."
            )
    finally:
        # Wajib kosongkan VRAM/RAM VLM sebelum memuatkan enjin LLM teks X06
        vision_analyzer.unload()

    return all_ocr_insights


# ==============================================================================
# 6. ENJIN SUNTINGAN & CANTUMAN VIDEO 9:16 (30-50 SAAT + MUZIK LATAR)
# ==============================================================================
def _fit_clip_to_vertical_9_16(clip: Any, target_w: int = 1080, target_h: int = 1920) -> Any:
    """Melaras skala video menegak 1080x1920 tanpa herotan (cover-scale + center-crop)."""
    cw, ch = clip.size
    scale = max(target_w / cw, target_h / ch)
    new_w = int(round(cw * scale))
    new_h = int(round(ch * scale))

    resized = clip.resized((new_w, new_h)) if hasattr(clip, "resized") else clip.resize((new_w, new_h))
    x1 = max(0, (new_w - target_w) // 2)
    y1 = max(0, (new_h - target_h) // 2)

    if hasattr(resized, "cropped"):
        return resized.cropped(x1=x1, y1=y1, width=target_w, height=target_h)
    return resized.crop(x1=x1, y1=y1, x2=x1 + target_w, y2=y1 + target_h)


def edit_and_stitch_reels_video(
    clips: List[Dict[str, Any]],
    audio_clip: Optional[Any],
    music_meta: Dict[str, Any],
    target_total_sec: int = 38,
) -> Tuple[Path, float]:
    """
    Memotong 3-5 klip mengikut ketukan rentak/pembahagian sekata supaya jumlah
    durasi berada dalam julat ketat 30 hingga 50 saat, membuang audio asal,
    memasang muzik latar dari assets/music/, dan mengeksport MP4 1080x1920.
    """
    target_total_sec = max(REEL_MIN_DURATION_SEC, min(REEL_MAX_DURATION_SEC, target_total_sec))
    n_clips = len(clips)
    base_seg = target_total_sec / n_clips
    beats = music_meta.get("beat_timestamps", [])

    # Selaraskan titik potongan pada ketukan rentak Librosa terdekat jika tersedia
    cut_durations: List[float] = []
    acc_time = 0.0
    for idx in range(n_clips):
        if idx == n_clips - 1:
            seg = max(4.0, round(target_total_sec - acc_time, 2))
        else:
            ideal_end = acc_time + base_seg
            snapped_end = ideal_end
            if beats:
                nearest = min(beats, key=lambda b: abs(b - ideal_end))
                if abs(nearest - ideal_end) <= 1.2:
                    snapped_end = nearest
            seg = max(4.5, round(snapped_end - acc_time, 2))
        cut_durations.append(seg)
        acc_time += seg

    loaded_segments: List[Any] = []
    try:
        for c_obj, seg_dur in zip(clips, cut_durations):
            vc = VideoFileClip(str(c_obj["local_path"]))
            v_dur = float(vc.duration or 1.0)
            use_dur = min(v_dur, seg_dur)

            sub = vc.subclipped(0, use_dur) if hasattr(vc, "subclipped") else vc.subclip(0, use_dur)
            no_aud = sub.without_audio() if hasattr(sub, "without_audio") else sub.set_audio(None)
            fitted = _fit_clip_to_vertical_9_16(no_aud, TARGET_WIDTH, TARGET_HEIGHT)
            loaded_segments.append(fitted)

        stitched = concatenate_videoclips(loaded_segments, method="compose")
        final_dur = float(stitched.duration or 0.0)

        # Jika durasi gabungan kurang dari 30s (kerana klip Pexels asal pendek), ulang klip terakhir
        if final_dur < REEL_MIN_DURATION_SEC and loaded_segments:
            pad_needed = (REEL_MIN_DURATION_SEC + 2.0) - final_dur
            extra_seg = loaded_segments[-1]
            if float(extra_seg.duration) > pad_needed:
                extra_seg = (
                    extra_seg.subclipped(0, pad_needed)
                    if hasattr(extra_seg, "subclipped")
                    else extra_seg.subclip(0, pad_needed)
                )
            stitched = concatenate_videoclips([stitched, extra_seg], method="compose")
            final_dur = float(stitched.duration or 0.0)

        if final_dur > REEL_MAX_DURATION_SEC:
            stitched = (
                stitched.subclipped(0, REEL_MAX_DURATION_SEC)
                if hasattr(stitched, "subclipped")
                else stitched.subclip(0, REEL_MAX_DURATION_SEC)
            )
            final_dur = float(stitched.duration or REEL_MAX_DURATION_SEC)

        if audio_clip is not None:
            a_dur = float(audio_clip.duration or final_dur)
            use_a_dur = min(a_dur, final_dur)
            trimmed_audio = (
                audio_clip.subclipped(0, use_a_dur)
                if hasattr(audio_clip, "subclipped")
                else audio_clip.subclip(0, use_a_dur)
            )
            stitched = (
                stitched.with_audio(trimmed_audio)
                if hasattr(stitched, "with_audio")
                else stitched.set_audio(trimmed_audio)
            )

        out_video = WORK_DIR / f"exp11_reels_final_{int(time.time())}.mp4"
        stitched.write_videofile(
            str(out_video),
            fps=24,
            codec="libx264",
            audio_codec="aac",
            preset="fast",
            threads=4,
            logger=None,
        )
        stitched.close()
        return out_video, round(final_dur, 2)
    finally:
        for seg in loaded_segments:
            try:
                seg.close()
            except Exception:
                pass
        if audio_clip is not None:
            try:
                audio_clip.close()
            except Exception:
                pass


# ==============================================================================
# 7. PENJANAAN KAPSYEN PERSONA AI SANTAI (X06 + X05 VECTOR)
# ==============================================================================
def generate_smart_reels_caption(
    ocr_insights: List[Dict[str, Any]],
    topic_category: str,
    pexels_query: str,
    music_meta: Dict[str, Any],
    cal_ctx: Dict[str, Any],
    llm_engine: Any,
    clean_fn: Any,
    reels_vector: Any,
) -> Tuple[str, str, Dict[str, Any]]:
    self_ref = random.choice(["admin", "min"])
    char_info = cal_ctx["character"]

    # Bina ringkasan kronologi daripada 10 bingkai OCR
    timeline_lines: List[str] = []
    for row in ocr_insights[:10]:
        objs = ", ".join(row.get("detected_objects", [])) or "visual estetik"
        ocr_t = row.get("ocr_text", "None")
        angle = row.get("retro_nostalgia_angle", "")
        timeline_lines.append(
            f"- Bingkai {row['frame_no']} (Klip {row['clip_id']} @ {row['timestamp_sec']}s): "
            f"{angle} [Objek: {objs} | OCR: {ocr_t}]"
        )
    ocr_timeline_str = "\n".join(timeline_lines)

    system_prompt = (
        f"Kau ialah '{self_ref}', pengendali halaman Facebook 'PS2 Retro Nostalgia' di Malaysia.\n"
        f"WATAK HARI INI ({cal_ctx['day_name']} — {char_info['name']}):\n"
        f"{char_info['voice']}\n\n"
        "TUGAS UTAMA:\n"
        "Tulis kapsyen video pendek Facebook Reels (340 - 520 aksara) dalam Bahasa Melayu Malaysia "
        "yang santai, bersahaja dan sedap dibaca berdasarkan gabungan jalan cerita visual video + suasana muzik latar.\n\n"
        "PERATURAN WAJIB:\n"
        "1. Mulakan dengan ayat pembuka (hook) yang menarik dalam 3 saat pertama.\n"
        f"2. Bahasakan diri sebagai '{self_ref}' (jangan guna 'aku' atau 'saya') dan sapa penonton sebagai 'korang' atau 'geng'.\n"
        f"3. MASA SEBENAR MALAYSIA: {cal_ctx['timestamp_label']} | Mood: {cal_ctx['mood']}.\n"
        f"   Jika sebut masa, HANYA guna '{cal_ctx['allowed_cues']}' dan DILARANG sebut '{cal_ctx['forbidden_cues']}'.\n"
        "4. Topik bebas (retro konsol, gaming, perkakasan atau suasana santai/alam) — kaitkan secara natural dengan "
        "jiwa peminat gaming & nostalgia tanpa memaksa.\n"
        "5. SIFAR emoji dalam perenggan cerita, tanpa tajuk di baris atas, tanpa link URL atau harga RM, "
        "dan tutup ayat dengan noktah lengkap."
    )

    user_prompt = (
        f"TEMA VIDEO PEXELS ({topic_category}): {pexels_query}\n"
        f"MUZIK LATAR REELS: '{music_meta['title']}' ({music_meta['vibe']})\n"
        f"DAPATAN 10 BINGKAI VISUAL & OCR VIDEO:\n{ocr_timeline_str}\n\n"
        f"Tulis kapsyen Reels santai daripada watak '{self_ref}' (340 - 520 aksara, tanpa emoji):"
    )

    best_story = ""
    llm_meta: Dict[str, Any] = {}
    vec_shard = 0

    for attempt in range(1, 4):
        adj_temp = min(0.84, round(0.68 + ((attempt - 1) * 0.05), 2))
        inf = llm_engine.generate_chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=adj_temp,
            max_tokens=360,
        )
        cleaned = clean_fn(inf["raw_text"], max_chars=540)
        cleaned = re.sub(r"\b(aku|saya)\b", self_ref, cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(rf"(^|[.!?]\s+){self_ref}\b", lambda m: f"{m.group(1)}{self_ref.capitalize()}", cleaned)

        if len(cleaned) < 240:
            llm_meta = {"attempt": attempt, "status": f"Terlalu pendek ({len(cleaned)} aksara)"}
            continue

        is_dup, dup_info, vec_shard = reels_vector.check_semantic_duplicate(
            game_title=f"reels:{topic_category}:{pexels_query}",
            candidate_story=cleaned,
            similarity_threshold=0.76,
            exact_threshold=0.95,
            exact_cooldown_days=60,
            similar_cooldown_days=5,
            now_epoch=cal_ctx["now_epoch"],
        )
        llm_meta = {
            "attempt": attempt,
            "elapsed_sec": inf["elapsed_sec"],
            "tok_per_sec": inf["tok_per_sec"],
            "story_chars": len(cleaned),
            "vector_shard": vec_shard,
            "semantic_status": "DISEKAT" if is_dup else "LULUS_UNIK",
        }
        if not is_dup:
            best_story = cleaned
            break
        console.print(f"[yellow]⚠️ Cubaan #{attempt} ditolak Vector: {dup_info}[/yellow]")

    llm_engine.unload()
    if not best_story:
        raise RuntimeError(f"Gagal menjana kapsyen Reels sah selepas 3 cubaan ({llm_meta}).")

    hashtags = "#PS2RetroNostalgia #ReelsMalaysia #RetroGaming #GamingMalaysia #Nostalgia"
    full_caption = f"{best_story}\n\n{hashtags}"
    return best_story, full_caption, llm_meta


# ==============================================================================
# 8. PENERBIT FACEBOOK REELS DAHULU -> FALLBACK KE FB FEED JIKA GAGAL
# ==============================================================================
def publish_fb_reels_with_feed_fallback(
    video_path: Path,
    caption: str,
) -> Dict[str, Any]:
    """
    Mencuba penerbitan ke Facebook Reels (/{page_id}/video_reels 3-fasa) terlebih dahulu:
    - Jika Reels berjaya -> terus pulang & LANGKAU muat naik Feed.
    - Jika Reels gagal -> beralih automatik ke Facebook Page Feed Video (/{page_id}/videos).
    """
    app_secret = (getattr(cfg, "PS2N_FB_APP_SECRET", "") or os.getenv("PS2N_FB_APP_SECRET", "")).strip()
    page_id = (getattr(cfg, "PS2N_FB_PAGE_ID", "") or os.getenv("PS2N_FB_PAGE_ID", "")).strip()
    page_token = (getattr(cfg, "PS2N_FB_PAGE_TOKEN", "") or os.getenv("PS2N_FB_PAGE_TOKEN", "")).strip()
    graph_ver = (getattr(cfg, "PS2N_FB_GRAPH_VERSION", "") or os.getenv("PS2N_FB_GRAPH_VERSION", "") or "v26.0").strip()

    if not page_id or not page_token:
        raise RuntimeError("Kredensial PS2N_FB_PAGE_ID atau PS2N_FB_PAGE_TOKEN tiada.")

    def _auth() -> Dict[str, str]:
        d = {"access_token": page_token}
        if app_secret:
            d["appsecret_proof"] = hmac.new(
                app_secret.encode("utf-8"),
                page_token.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        return d

    file_size = video_path.stat().st_size
    reels_endpoint = f"https://graph.facebook.com/{graph_ver}/{page_id}/video_reels"
    reels_error_reason = ""

    # --- LALUAN UTAMA: FACEBOOK REELS (3 FASA) ---
    try:
        console.print("[cyan]🎬 [FB REELS FASA 1/3] Memulakan sesi muat naik Reels...[/cyan]")
        p1 = _auth()
        p1["upload_phase"] = "start"
        r1 = requests.post(reels_endpoint, data=p1, timeout=30)
        j1 = r1.json()
        if r1.status_code != 200 or "video_id" not in j1:
            raise RuntimeError(f"Start gagal: {json.dumps(j1)}")

        video_id = str(j1["video_id"])
        upload_url = str(j1["upload_url"])

        console.print(f"[cyan]🎬 [FB REELS FASA 2/3] Memuat naik binary MP4 ({round(file_size / (1024*1024), 2)} MB) ke rupload...[/cyan]")
        with open(video_path, "rb") as vf:
            video_bytes = vf.read()

        r2 = requests.post(
            upload_url,
            headers={
                "Authorization": f"OAuth {page_token}",
                "offset": "0",
                "file_size": str(file_size),
                "Content-Type": "application/octet-stream",
            },
            data=video_bytes,
            timeout=150,
        )
        if r2.status_code != 200:
            raise RuntimeError(f"Rupload HTTP {r2.status_code}: {r2.text[:140]}")

        console.print("[cyan]🎬 [FB REELS FASA 3/3] Menerbitkan video ke Facebook Reels...[/cyan]")
        p3 = _auth()
        p3.update(
            {
                "upload_phase": "finish",
                "video_id": video_id,
                "video_state": "PUBLISHED",
                "description": caption,
            }
        )
        r3 = requests.post(reels_endpoint, data=p3, timeout=40)
        j3 = r3.json()
        if r3.status_code == 200 and j3.get("success"):
            return {
                "publish_mode": "FB_REELS",
                "fb_post_id": video_id,
                "permalink": f"https://www.facebook.com/reel/{video_id}",
                "feed_skipped": True,
            }
        raise RuntimeError(f"Finish gagal: {json.dumps(j3)}")

    except Exception as exc:
        reels_error_reason = str(exc)
        console.print(f"[bold yellow]⚠️ FB Reels tidak berjaya ({reels_error_reason}). Beralih ke fallback FB Feed Video...[/bold yellow]")

    # --- LALUAN SANDARAN: FACEBOOK PAGE FEED VIDEO ---
    feed_video_url = f"https://graph-video.facebook.com/{graph_ver}/{page_id}/videos"
    f_data = _auth()
    f_data["description"] = caption
    with open(video_path, "rb") as vf:
        r_feed = requests.post(
            feed_video_url,
            data=f_data,
            files={"source": (video_path.name, vf, "video/mp4")},
            timeout=150,
        )
    j_feed = r_feed.json()
    if r_feed.status_code in (200, 201) and "id" in j_feed:
        vid_post_id = str(j_feed["id"])
        return {
            "publish_mode": "FB_FEED_FALLBACK",
            "fb_post_id": vid_post_id,
            "permalink": f"https://www.facebook.com/{vid_post_id}",
            "feed_skipped": False,
            "reels_error": reels_error_reason,
        }

    raise RuntimeError(
        f"Kedua-dua FB Reels ({reels_error_reason}) dan FB Feed Fallback ({json.dumps(j_feed)}) gagal."
    )


# ==============================================================================
# FUNGSI UTAMA DIAGNOSTIK EXP11
# ==============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="EXP11: Ujian Lengkap Pexels + Muzik + OCR Berperingkat + FB Reels")
    parser.add_argument("--query", type=str, default="", help="Kata kunci carian Pexels khusus (pilihan).")
    parser.add_argument("--clips", type=int, default=4, choices=[3, 4, 5], help="Bilangan klip Pexels (3-5).")
    parser.add_argument("--duration", type=int, default=36, help="Sasaran durasi video Reels (30-50 saat).")
    parser.add_argument("--publish-fb", action="store_true", help="Terbitkan ke Facebook sebenar (Reels -> Feed Fallback).")
    parser.add_argument("--notify-tg", action="store_true", help="Hantar laporan ujian ke Telegram melalui X12.")
    args = parser.parse_args()

    try:
        cal_ctx = build_myt_persona_context()
        console.print(
            Panel.fit(
                "[bold cyan]PS2-RETRO-NOSTALGIA :: UJIAN PENUH PIPELINE REELS (EXP11)[/bold cyan]\n"
                f"[dim]{cal_ctx['timestamp_label']} | Watak: {cal_ctx['character']['name']}[/dim]",
                border_style="cyan",
            )
        )

        # STEP 1: Semakan Modul Asas (X01-X07, X09, X12)
        mod_tbl = Table(title="Langkah 1: Semakan Modul Teras & Pustaka Media", box=box.ROUNDED, header_style="bold yellow")
        mod_tbl.add_column("Modul / Komponen", style="bold white")
        mod_tbl.add_column("Status", style="cyan")
        for label, status in [
            ("X01_config", st_x01),
            ("X02_sqlite_ledger", st_x02),
            ("X03_b2_proxy_client", st_x03),
            ("X04_redis_sharded", st_x04),
            ("X05_vector_sharded", st_x05),
            ("X06_local_llm_engine", st_x06),
            ("X07_vision_ocr_analyzer", st_x07),
            ("X09_media_harvester", st_x09),
            ("X12_telegram_notifier", st_x12),
            ("MoviePy & Librosa", f"MoviePy v2={MOVIEPY_V2} | Librosa={LIBROSA_OK}"),
        ]:
            mod_tbl.add_row(label, status)
        console.print(mod_tbl)

        if not all([cfg, _x04, _x05, _x06, _x07]):
            raise RuntimeError("Sebahagian modul teras (X01/X04/X05/X06/X07) gagal diimport.")

        PS2RedisShardedManager = getattr(_x04, "PS2RedisShardedManager")
        PS2VectorShardedManager = getattr(_x05, "PS2VectorShardedManager")
        llm_engine = getattr(_x06, "llm_engine")
        clean_and_scrub_bm_story = getattr(_x06, "clean_and_scrub_bm_story")
        vision_analyzer = getattr(_x07, "vision_analyzer")

        reels_redis = PS2RedisShardedManager(namespace="ps2n_reels")
        reels_vector = PS2VectorShardedManager(namespace="ps2n_reels")
        state_store = TempOcrStateStore(json_path=OCR_SESSION_JSON, db_path=DEBUG_STATE_DB)

        # STEP 2: Pilih & Analisis Muzik dari assets/music/
        with console.status("[bold cyan]Langkah 2: Mengimbas 200+ fail muzik di assets/music/ & mengesan rentak...[/bold cyan]"):
            audio_clip, music_meta, total_music_files = select_and_analyze_music(
                reels_redis=reels_redis,
                target_duration=args.duration,
            )
        console.print(
            Panel(
                f"• Jumlah Koleksi Muzik Ditemui: [bold green]{total_music_files} fail[/bold green] di [cyan]{MUSIC_DIR}[/cyan]\n"
                f"• Trek Terpilih: [bold white]{music_meta['title']}[/bold white] ({music_meta['filename']})\n"
                f"• Artis / Genre: {music_meta['artist']} | {music_meta['genre']} | Durasi Asal: {music_meta['full_duration_sec']}s\n"
                f"• Analisis Librosa: [bold yellow]{music_meta['bpm']} BPM[/bold yellow] ({music_meta['beat_count']} ketukan) | {music_meta['vibe']}",
                title="Langkah 2: Keputusan Enjin Muzik & Rentak",
                border_style="green",
            )
        )

        # STEP 3: Tarik Video Pexels (1 Request Sahaja) + Tapis Redis + Muat Turun 3-5 Klip
        with console.status("[bold cyan]Langkah 3: Menarik calon video Pexels 9:16 (1 Request) & menapis guna Redis...[/bold cyan]"):
            topic_cat, pexels_query, clips, pex_stats = fetch_and_download_pexels_clips(
                reels_redis=reels_redis,
                desired_clips=args.clips,
                custom_query=args.query,
            )

        if len(clips) < MIN_CLIPS_PICK:
            raise RuntimeError(f"Hanya {len(clips)} klip Pexels berjaya dimuat turun (minimum {MIN_CLIPS_PICK} diperlukan). Stats: {pex_stats}")

        clip_tbl = Table(
            title=f"Langkah 3: {len(clips)} Klip Pexels Terpilih ({topic_cat}: '{pexels_query}' | {pex_stats['api_requests_used']} Request API)",
            box=box.ROUNDED,
            header_style="bold cyan",
        )
        clip_tbl.add_column("#", justify="center")
        clip_tbl.add_column("Pexels Video ID", style="bold white")
        clip_tbl.add_column("Resolusi", justify="center")
        clip_tbl.add_column("Durasi Asal", justify="right")
        clip_tbl.add_column("Saiz Fail", justify="right")
        for idx, c in enumerate(clips, 1):
            clip_tbl.add_row(str(idx), c["id"], f"{c['width']}x{c['height']}", f"{c['duration']}s", f"{c['size_mb']} MB")
        console.print(clip_tbl)

        # STEP 4: Snapshot Maksimum 10 Gambar & Proses OCR Sesi Demi Sesi (3-4 Gambar/Sesi)
        console.print("[bold cyan]Langkah 4: Mengekstrak 10 snapshot & memproses OCR berperingkat (3-4 gambar/sesi)...[/bold cyan]")
        ocr_insights = extract_and_analyze_snapshots_in_batches(
            clips=clips,
            topic_hint=f"{topic_cat} {pexels_query}",
            state_store=state_store,
            vision_analyzer=vision_analyzer,
            max_total_images=MAX_TOTAL_SNAPSHOTS,
            batch_size=MAX_IMAGES_PER_OCR_BATCH,
        )
        if not ocr_insights:
            raise RuntimeError("Tiada dapatan Vision/OCR berjaya dihasilkan daripada snapshot video.")

        ocr_tbl = Table(
            title=f"Ringkasan {len(ocr_insights)} Bingkai OCR (Disimpan ke {OCR_SESSION_JSON.name} & {DEBUG_STATE_DB.name})",
            box=box.ROUNDED,
            header_style="bold magenta",
        )
        ocr_tbl.add_column("Sesi", justify="center")
        ocr_tbl.add_column("Bingkai", justify="center")
        ocr_tbl.add_column("Klip ID")
        ocr_tbl.add_column("Saiz", justify="right")
        ocr_tbl.add_column("Objek & Teks OCR")
        for r in ocr_insights:
            objs = ", ".join(r["detected_objects"][:3])
            ocr_tbl.add_row(
                f"B{r['batch_no']}",
                f"#{r['frame_no']}",
                r["clip_id"],
                f"{r['compressed_kb']} KB",
                f"{objs} | OCR: {r['ocr_text'][:40]}",
            )
        console.print(ocr_tbl)

        # STEP 5: Potong & Cantum Video Menegak 1080x1920 (30-50 Saat) + Muzik
        with console.status(f"[bold cyan]Langkah 5: Menyunting & mencantum video 1080x1920 ({REEL_MIN_DURATION_SEC}-{REEL_MAX_DURATION_SEC}s) + muzik...[/bold cyan]"):
            final_video_path, final_video_dur = edit_and_stitch_reels_video(
                clips=clips,
                audio_clip=audio_clip,
                music_meta=music_meta,
                target_total_sec=args.duration,
            )
        final_mb = round(final_video_path.stat().st_size / (1024 * 1024), 2)
        console.print(
            Panel(
                f"• Fail Video Akhir: [bold green]{final_video_path}[/bold green]\n"
                f"• Resolusi & Durasi: [bold white]1080x1920 (9:16)[/bold white] | [bold yellow]{final_video_dur} saat[/bold yellow] (Saiz: {final_mb} MB)",
                title="Langkah 5: Video Reels Siap Dijana",
                border_style="green",
            )
        )

        # STEP 6: Penjanaan Kapsyen Persona AI Santai (Sedar Waktu, Hari, Mood & Watak)
        with console.status("[bold cyan]Langkah 6: Menjana kapsyen Persona AI santai & menyemak Vector...[/bold cyan]"):
            story_bm, full_caption, llm_meta = generate_smart_reels_caption(
                ocr_insights=ocr_insights,
                topic_category=topic_cat,
                pexels_query=pexels_query,
                music_meta=music_meta,
                cal_ctx=cal_ctx,
                llm_engine=llm_engine,
                clean_fn=clean_and_scrub_bm_story,
                reels_vector=reels_vector,
            )

        console.print(
            Panel(
                f"[bold yellow]KAPSYEN REELS ({len(story_bm)} aksara | Watak: {cal_ctx['character']['char_id']}):[/bold yellow]\n"
                f"[white]{full_caption}[/white]\n\n"
                f"[dim]Metrik LLM & Vector: {llm_meta}[/dim]",
                title="Langkah 6: Hasil Kapsyen Persona AI",
                border_style="green",
            )
        )

        # STEP 7: Ujian Penerbitan (FB Reels Dahulu -> Fallback ke FB Feed Jika Gagal)
        fb_result: Dict[str, Any] = {
            "publish_mode": "DRY_RUN",
            "fb_post_id": "DRY_RUN",
            "permalink": "",
            "feed_skipped": True,
        }
        if args.publish_fb:
            fb_result = publish_fb_reels_with_feed_fallback(
                video_path=final_video_path,
                caption=full_caption,
            )
            console.print(
                f"[bold green]🚀 BERJAYA TERBIT ({fb_result['publish_mode']})![/bold green] "
                f"ID: [bold white]{fb_result['fb_post_id']}[/bold white] | Link: {fb_result['permalink']}"
            )
        else:
            console.print("[yellow]ℹ️ Mod selamat (tanpa --publish-fb): Penerbitan FB dilangkau. Gunakan --publish-fb untuk uji pos sebenar.[/yellow]")

        report_payload = {
            "post_uid": f"exp11_{cal_ctx['now_epoch']}",
            "posted_at_iso": cal_ctx["now_iso"],
            "posted_epoch": cal_ctx["now_epoch"],
            "persona_ref": "admin",
            "pillar_id": f"{cal_ctx['character']['char_id']}:{topic_cat}",
            "game_title": f"Pexels Reels ({pexels_query})",
            "story_bm": story_bm,
            "story_chars": len(story_bm),
            "post_caption": full_caption,
            "shopee_product_code": "TRAFFIC_ONLY",
            "shopee_product_name": "Mod Trafik & Follower (Tiada Link Produk)",
            "shopee_url": "-",
            "comment_text": "(Tiada komen produk — fokus trafik organik Reels)",
            "fb_post_id": fb_result["fb_post_id"],
            "publish_detail": fb_result,
            "music_meta": {k: v for k, v in music_meta.items() if k != "beat_timestamps"},
            "pexels_stats": pex_stats,
            "video_output": str(final_video_path),
            "video_duration_sec": final_video_dur,
            "ocr_json_ref": str(OCR_SESSION_JSON),
            "ocr_db_ref": str(DEBUG_STATE_DB),
            "llm_metrics": llm_meta,
            "vector_shard": llm_meta.get("vector_shard", 0),
        }
        REPORT_OUTPUT_JSON.write_text(json.dumps(report_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        console.print(f"[bold green]✅ Ujian EXP11 selesai sepenuhnya! Laporan disimpan di: {REPORT_OUTPUT_JSON}[/bold green]")

        if args.notify_tg and _x12 is not None:
            tg_notifier = getattr(_x12, "tg_notifier")
            tg_notifier.notify_post_success(
                report_data=report_payload,
                platform_label=f"Facebook Reels Test ({fb_result['publish_mode']})",
                channel_alias="default",
            )
            console.print("[bold green]📨 Laporan ujian dihantar ke Telegram Channel melalui X12![/bold green]")

    except Exception as exc:
        console.print(
            Panel(
                f"[bold red]❌ RALAT DIKESAN SEMASA UJIAN EXP11:[/bold red] {exc}\n\n"
                f"[dim]{traceback.format_exc()}[/dim]",
                title="Diagnostik Ralat EXP11",
                border_style="red",
            )
        )
        sys.exit(1)


if __name__ == "__main__":
    main()