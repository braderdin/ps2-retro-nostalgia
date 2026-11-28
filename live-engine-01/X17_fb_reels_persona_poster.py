#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - PERSONA AI #3: FACEBOOK REELS AUTO-POSTER (X17)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/live-engine-01/X17_fb_reels_persona_poster.py
#
# FUNGSI UTAMA:
# 1. Kawalan Jadual Pintar 2 Video Setiap 48 Jam (9:00 Pagi - 9:00 Malam MYT):
#    Menyemak kuota gelongsor Upstash Redis (`fb_reels_48h`, max_events=2,
#    window=172800s) + kebarangkalian rawak setiap jam SEBELUM memuatkan model
#    AI supaya masa runner GitHub Actions sifar pembaziran apabila slot penuh.
# 2. Penapis Anti-Ulangan Ketat 6 Bulan (180 Hari):
#    - Video Pexels ID & SHA-256 dikunci 180 hari (X14) + Muzik dikunci 60 hari (X15).
#    - Cincangan SHA-256 cerita dikunci 180 hari + Penapis Semantik Vector X05
#      (60 hari untuk persamaan >=95%, 5 hari untuk persamaan >=76%).
#    - Memori 5 ayat terakhir (`MEMORI 5 AYAT TERAKHIR`) dibekalkan terus kepada
#      AI supaya struktur pembuka dan jalan cerita sentiasa segar & tidak kaku.
# 3. Watak Dinamik Mengikut Hari, Waktu MYT, Suasana Malaysia & Vibe Muzik:
#    7 watak berbeza (Isnin hingga Ahad) + 5 slot masa MYT + kalendar Malaysia.
# 4. Strategi Komen Hibrid 70% / 30%:
#    - 70% Hantaran Umum/Santai: Komen soalan interaksi santai untuk menarik
#      follower baharu & melonjakkan algoritma Reels (tanpa pautan luar).
#    - 30% Hantaran Konsol/Retro/Gaming: Komen pertama bergilir 1 daripada 10
#      produk Shopee daripada `data/ps2_shopee_products.db`.
# 5. Penerbitan FB Reels Dahulu -> Langkau Feed Jika Berjaya -> Fallback FB Feed
#    Jika Reels Gagal + Arkib Video & Log ke Backblaze B2 (X03) + Laporan X12.
# ==============================================================================

import argparse
import hashlib
import hmac
import importlib
import inspect
import json
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# ------------------------------------------------------------------------------
# IMPORT MODUL TERAS (X01 - X07) & MODUL REELS (X14 - X16)
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
_x14_mod: Any = importlib.import_module("X14_pexels_video_fetcher")
_x15_mod: Any = importlib.import_module("X15_reels_audio_beat_engine")
_x16_mod: Any = importlib.import_module("X16_reels_video_editor_ocr")

b2_proxy: Any = getattr(_b2_mod, "b2_proxy")
PS2RedisShardedManager: Any = getattr(_redis_mod, "PS2RedisShardedManager")
PS2VectorShardedManager: Any = getattr(_vector_mod, "PS2VectorShardedManager")
llm_engine: Any = getattr(_llm_mod, "llm_engine")
clean_and_scrub_bm_story: Any = getattr(_llm_mod, "clean_and_scrub_bm_story")

fetch_and_prepare_pexels_clips: Any = getattr(_x14_mod, "fetch_and_prepare_pexels_clips")
mark_pexels_clips_used_6_months: Any = getattr(_x14_mod, "mark_pexels_clips_used_6_months")
prepare_reels_music_track: Any = getattr(_x15_mod, "prepare_reels_music_track")
mark_music_track_used: Any = getattr(_x15_mod, "mark_music_track_used")
extract_and_analyze_clips_ocr: Any = getattr(_x16_mod, "extract_and_analyze_clips_ocr")
render_vertical_reels_video: Any = getattr(_x16_mod, "render_vertical_reels_video")

# Ruang nama khusus Reels pada Upstash Redis & Upstash Vector
reels_redis = PS2RedisShardedManager(namespace="ps2n_reels")
reels_vector = PS2VectorShardedManager(namespace="ps2n_reels")

# ==============================================================================
# KONFIGURASI PRODUKSI X17
# ==============================================================================
MYT = timezone(timedelta(hours=8))

DATA_DIR: Path = Path(getattr(cfg, "DATA_DIR", PROJECT_ROOT / "data"))
TEMP_DIR: Path = Path(getattr(cfg, "TEMP_DIR", PROJECT_ROOT / "temp"))
WORK_DIR: Path = TEMP_DIR / "x17_reels_run"

REELS_HISTORY_DB_PATH: Path = DATA_DIR / "ps2_reels_posts_history.db"
SHOPEE_DB_PATH: Path = DATA_DIR / "ps2_shopee_products.db"
LAST_OUTPUT_JSON: Path = TEMP_DIR / "x17_last_reels_post.json"

# Kawalan Kuota 2 Reels Setiap 48 Jam (172,800 Saat)
REELS_QUOTA_KEY: str = "fb_reels_48h"
REELS_MAX_PER_48H: int = 2
REELS_WINDOW_SECONDS: int = 48 * 3600

# Kawalan Teks & Anti-Ulangan
CAPTION_MIN_CHARS: int = 330
CAPTION_MAX_CHARS: int = 560
CAPTION_SOFT_TRIM: int = 540
STORY_EXACT_COOLDOWN_DAYS: int = 180
STORY_SEMANTIC_EXACT_DAYS: int = 60
STORY_SEMANTIC_SIMILAR_DAYS: int = 5

# Had Saiz SQLite < 30 MB untuk GitHub
SQLITE_MAX_SAFE_BYTES: int = 26 * 1024 * 1024
SQLITE_MAX_ARCHIVE_SHARDS: int = 6

# 7 Watak Unik Mengikut Hari Seminggu (Dinamik & Tidak Kaku)
WEEKLY_REELS_CHARACTERS: List[Dict[str, str]] = [
    {
        "day_idx": "0",
        "char_id": "WATAK_ISNIN_BENGKEL",
        "label": "Abang Bengkel Konsol (Santai & Praktikal)",
        "persona_guide": (
            "Gaya abang 35 tahun yang suka belek perkakasan, susun kabel, dan menghargai "
            "teknologi klasik yang masih bertahan sampai hari ni. Bercerita terus terang macam sembang kedai kopi."
        ),
        "hook_hint": "Mulakan dengan pemerhatian visual menarik atau persoalan ringkas tentang perkakasan/suasana.",
    },
    {
        "day_idx": "1",
        "char_id": "WATAK_SELASA_NOSTALGIA",
        "label": "Member Kedai Game Era 2000-an",
        "persona_guide": (
            "Gaya kawan lama yang suka mengimbau zaman balik sekolah singgah kedai game, "
            "bunyi kipas konsol, dan perasaan seronok dapat main game kegemaran tanpa fikir masalah kerja."
        ),
        "hook_hint": "Mulakan dengan ayat yang terus menjentik memori zaman membesar era 2000-an.",
    },
    {
        "day_idx": "2",
        "char_id": "WATAK_RABU_TERAPI",
        "label": "Penglipur Lara Santai Tengah Minggu",
        "persona_guide": (
            "Nada tenang, relaks, dan menenangkan fikiran. Mengajak penonton berhenti sekejap "
            "dari kesibukan kerja sambil menikmati visual dan alunan muzik latar."
        ),
        "hook_hint": "Mulakan dengan sapaan mesra yang mengajak pembaca tarik nafas dan berehat seketika.",
    },
    {
        "day_idx": "3",
        "char_id": "WATAK_KHAMIS_KURATOR",
        "label": "Kurator Budaya & Visual Retro",
        "persona_guide": (
            "Menghargai estetika pencahayaan neon, skrin klasik, dan aura retro yang ada jiwa "
            "tersendiri berbanding hiburan moden yang terlalu laju."
        ),
        "hook_hint": "Mulakan dengan pujian santai terhadap suasana dan warna visual yang terpapar.",
    },
    {
        "day_idx": "4",
        "char_id": "WATAK_JUMAAT_MARATHON",
        "label": "Kaki Lepak Malam Minggu",
        "persona_guide": (
            "Aura ceria dan bersemangat menyambut malam minggu dan hujung minggu — waktu paling "
            "sesuai untuk lepak santai, pasang muzik, atau marathon game sampai puas."
        ),
        "hook_hint": "Mulakan dengan suasana bebas kerja menjelang hujung minggu.",
    },
    {
        "day_idx": "5",
        "char_id": "WATAK_SABTU_SETUP",
        "label": "Peminat Setup Bilik & Estetika",
        "persona_guide": (
            "Suka memerhati susun atur meja, cahaya lampu bilik, dan bagaimana ruang kecil "
            "boleh jadi tempat paling selesa untuk melepaskan lelah."
        ),
        "hook_hint": "Mulakan dengan komen santai tentang sudut ruang atau pencahayaan dalam klip.",
    },
    {
        "day_idx": "6",
        "char_id": "WATAK_AHAD_SANTAI",
        "label": "Abang Santai Hujung Minggu",
        "persona_guide": (
            "Nada hangat, mesra, dan bersahaja meluangkan masa berkualiti di rumah pada hari Ahad "
            "sebelum kembali sibuk pada hari Isnin."
        ),
        "hook_hint": "Mulakan dengan suasana damai hujung minggu di rumah.",
    },
]

# 20 Variasi Komen Interaksi Murni (70% Mod Trafik & Follower Baru — Tiada Link)
ORGANIC_INTERACTION_COMMENTS_20: List[str] = [
    "💬 Korang kalau dapat masa lapang macam ni, lagi suka layan suasana tenang sorang-sorang ke lepak ramai-ramai dengan member?",
    "🎮 Jujur cakap geng, antara game era 2000-an dengan game moden sekarang, mana satu yang paling melekat dalam ingatan korang?",
    "🌙 Cuaca dan suasana macam ni memang ngam kalau pasang muzik perlahan sambil santai. Korang tengah buat apa tu?",
    "🕹️ Siapa kat sini masih simpan lagi konsol atau aksesori lama kat rumah? Cuba komen sikit model apa yang masih hidup!",
    "✨ Kadang-kadang benda paling ringkas macam ni pun dah cukup buat fikiran rasa lapang lepas seharian penat kerja. Setuju tak geng?",
    "🎧 Korang jenis main game atau buat kerja sambil pasang muzik latar, ke lagi suka suasana sunyi sepi?",
    "📺 Cuba kongsi satu tajuk game klasik yang kalau korang dengar muzik dia je terus teringat zaman sekolah dulu!",
    "🔥 Malam-malam macam ni memang syok kalau dapat lepak depan skrin tanpa gangguan. Korang team tidur awal ke team berjaga malam?",
    "💾 Ingat lagi tak perasaan bila save data lama tiba-tiba hilang masa tengah syok main? Cuba cerita pengalaman paling epik korang kat bawah!",
    "🌿 Visual tenang macam ni memang terapi mata lepas menghadap skrin kerja sepanjang hari. Korang dari negeri mana yang tengah tengok Reels ni?",
    "⚡ Kalau diberi peluang ulang balik satu hari zaman remaja dulu kat kedai game, korang nak main game apa dulu dengan kawan-kawan?",
    "🎯 Korang lebih minat setup meja yang penuh lampu neon warna-warni ke gaya ringkas dan kemas?",
    "🏆 Antara game lumba kereta, bola sepak klasik, dengan game pengembaraan dunia terbuka, mana satu pilihan nombor satu korang?",
    "☕ Waktu-waktu macam ni paling sedap layan kopi panas sambil tengok visual santai. Korang dah rehat ke belum hari ni?",
    "👾 Ramai geng 30-an sekarang mula kumpul balik barang retro zaman membesar dulu. Korang pun ada hobi kumpul barang klasik ke?",
    "🎶 Rentak muzik macam ni memang ngam dengan suasana santai. Korang rasa lebih masuk mood pagi ke mood malam?",
    "🚀 Kalau korang kena pilih SATU konsol sepanjang zaman untuk dibawa ke pulau terpencil, konsol apa yang korang pilih?",
    "💡 Tip santai dari min: sekali-sekala bagi masa kat diri sendiri untuk relaks dan layan hobi lama supaya stres kerja hilang. Betul tak?",
    "🌟 Terima kasih yang sudi singgah dan tengok sampai habis! Jangan lupa tekan follow supaya tak terlepas video santai & nostalgia seterusnya.",
    "🎲 Korang lagi suka main mode cerita seorang diri ke mode berdua yang selalu buat satu bilik riuh rendah?",
]

# 14 Variasi Komen Shopee Bersasar (30% Mod Promo — Hanya Bila Visual Berkait Konsol/Retro/Gaming)
REELS_SHOPEE_COMMENTS_14: List[str] = [
    "🎮 Rindu nak layan semula koleksi klasik PS2 kat rumah tanpa risau CD calar atau lens rosak? Tekan link Shopee di bawah untuk tengok set siap game:\n👉 {url}",
    "🕹️ Ramai geng 30-an dah hidupkan balik konsol PS2 lama kat rumah guna pendrive & hardisk OPL! Klik link di bawah untuk pilih pakej yang ngam:\n👉 {url}",
    "💾 Konsol PS2 kat rumah lens dah lemah tak baca disk? Jangan simpan dalam stor dulu, tekan link Shopee di bawah untuk set Plug & Play:\n👉 {url}",
    "🔥 Nak pilih sendiri senarai game legend PS2 siap masuk pendrive atau hardisk? Boleh terus klik pautan rasmi di bawah ni geng:\n👉 {url}",
    "⚡ Senang kerja sekarang, cucuk terus boleh main game klasik PS2 dengan lancar! Tekan link di bawah untuk semak pilihan kapasiti:\n👉 {url}",
    "🎯 Khas untuk peminat retro PS2 di Malaysia! Semak sama ada PS2 korang jenis Matrix atau standard dan pilih set yang sesuai kat sini:\n👉 {url}",
    "📀 Dah penat tiup disk PS2 tapi masih sangkut kat skrin loading? Jom beralih ke sistem OPL USB yang lebih pantas, tekan link di bawah:\n👉 {url}",
    "✨ Cara paling selamat untuk main ratusan game PS2 tanpa perlu ubah suai dalaman konsol. Klik pautan Shopee di bawah sekarang:\n👉 {url}",
    "🚀 Nak marathon game klasik PS2 hujung minggu ni dengan member atau anak-anak? Tekan link kat bawah untuk susun senarai game korang:\n👉 {url}",
    "🛡️ Pastikan korang pilih versi yang tepat untuk model PS2 Fat atau PS2 Slim di rumah. Tekan link di bawah untuk baca maklumat penuh:\n👉 {url}",
    "🎲 Dari game aksi, lumba kereta sampai bola sepak legend era 2000-an, semua boleh pilih ikut citarasa sendiri! Klik link kat sini:\n👉 {url}",
    "🔧 Tak kira konsol PS2 Matrix mahupun Non-Matrix, ada jalan mudah untuk terus main dari USB. Klik pautan di bawah untuk semak:\n👉 {url}",
    "🌟 Hidupkan semula bunyi skrin boot ikonik PS2 kat TV ruang tamu malam ni! Klik link di bawah untuk tengok kelengkapan siap sedia:\n👉 {url}",
    "👾 Nak bagi anak-anak rasa pengalaman game zaman kita membesar dulu? Set PS2 plug and play ni memang senang nak guna, tekan link bawah:\n👉 {url}",
]


# ==============================================================================
# 1. KONTEKS KALENDAR MALAYSIA (MYT), WATAK HARIAN & MOOD DINAMIK
# ==============================================================================
def get_malaysia_calendar_note(now: datetime) -> str:
    m, d, wd = now.month, now.day, now.weekday()
    notes: List[str] = []

    fixed = {
        (1, 1): "Sambutan Tahun Baru di Malaysia",
        (2, 1): "Hari Wilayah Persekutuan",
        (3, 4): "Ulang Tahun Pelancaran PlayStation 2 (4 Mac 2000)",
        (5, 1): "Hari Pekerja — cuti rehat santai di rumah",
        (8, 31): "Hari Kebangsaan (Merdeka)",
        (9, 16): "Hari Malaysia",
        (12, 25): "Hari Krismas & musim cuti hujung tahun",
    }
    if (m, d) in fixed:
        notes.append(fixed[(m, d)])

    if m in (11, 12):
        notes.append("Musim cuti sekolah hujung tahun & cuaca hujan monsun di Malaysia")
    elif m in (5, 6) and d <= 15:
        notes.append("Musim cuti pertengahan tahun sekolah")

    if wd == 4 and now.hour >= 17:
        notes.append("Malam minggu (Jumaat malam) — waktu paling santai lepaskan penat seminggu")
    elif wd in (5, 6):
        notes.append("Hujung minggu (Sabtu/Ahad) — masa lapang santai di rumah")

    return " | ".join(notes) if notes else "Hari biasa — suasana santai komuniti Malaysia"


def get_dynamic_myt_context() -> Dict[str, Any]:
    now = datetime.now(tz=MYT)
    days_bm = ["Isnin", "Selasa", "Rabu", "Khamis", "Jumaat", "Sabtu", "Ahad"]
    months_bm = [
        "Januari", "Februari", "Mac", "April", "Mei", "Jun",
        "Julai", "Ogos", "September", "Oktober", "November", "Disember",
    ]
    wd = now.weekday()
    hour = now.hour
    character = WEEKLY_REELS_CHARACTERS[wd % len(WEEKLY_REELS_CHARACTERS)]

    if 5 <= hour < 12:
        period = "Pagi"
        mood = "Segar, tenang & membangkitkan semangat pagi"
        allowed_cues = "pagi ni, awal pagi ni, hari ni"
        forbidden_cues = "tengah hari ni, petang ni, petang tadi, malam ni, malam tadi, tengah malam"
        forbidden_re = r"\b(tengah hari ni|petang ni|petang tadi|malam ni|malam tadi|tengah malam)\b"
    elif 12 <= hour < 15:
        period = "Tengah Hari"
        mood = "Santai waktu rehat tengah hari"
        allowed_cues = "tengah hari ni, waktu rehat ni, hari ni"
        forbidden_cues = "pagi ni, pagi tadi, selamat pagi, petang ni, malam ni, tengah malam"
        forbidden_re = r"\b(pagi ni|pagi ini|pagi tadi|selamat pagi|petang ni|malam ni|tengah malam)\b"
    elif 15 <= hour < 19:
        period = "Petang"
        mood = "Relaks lepas habis waktu kerja atau sekolah"
        allowed_cues = "petang ni, lewat petang ni, hari ni"
        forbidden_cues = "pagi ni, pagi tadi, selamat pagi, tengah hari ni, malam ni, tengah malam"
        forbidden_re = r"\b(pagi ni|pagi ini|pagi tadi|selamat pagi|tengah hari ni|malam ni|tengah malam)\b"
    elif 19 <= hour <= 23:
        period = "Malam"
        mood = "Nostalgia malam & lepak santai"
        allowed_cues = "malam ni, malam-malam macam ni, waktu malam ni"
        forbidden_cues = "pagi ni, pagi ini, pagi tadi, selamat pagi, tengah hari ni, petang ni, tengah malam ni"
        forbidden_re = r"\b(pagi ni|pagi ini|pagi tadi|selamat pagi|tengah hari ni|petang ni|tengah malam ni)\b"
    else:
        period = "Tengah Malam"
        mood = "Sunyi, mendalam & layan perasaan lewat malam"
        allowed_cues = "lewat malam ni, tengah malam ni"
        forbidden_cues = "pagi yang cerah, selamat pagi, tengah hari ni, petang ni, petang tadi"
        forbidden_re = r"\b(pagi yang cerah|selamat pagi|tengah hari ni|petang ni|petang tadi)\b"

    ts_display = (
        f"{days_bm[wd]}, {now.day:02d} {months_bm[now.month - 1]} {now.year} | "
        f"{now.strftime('%I:%M %p')} MYT ({period})"
    )
    return {
        "now_iso": now.isoformat(),
        "now_epoch": int(now.timestamp()),
        "hour": hour,
        "day_name": days_bm[wd],
        "period": period,
        "mood": mood,
        "allowed_cues": allowed_cues,
        "forbidden_cues": forbidden_cues,
        "forbidden_regex": forbidden_re,
        "special_note": get_malaysia_calendar_note(now),
        "timestamp_display": ts_display,
        "character": character,
    }


# ==============================================================================
# 2. PENYELARAS REKOD SQLITE (< 30 MB), MEMORI 5 AYAT & KOMEN HIBRID 70/30
# ==============================================================================
class ReelsHistoryCoordinator:
    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        self._init_db(REELS_HISTORY_DB_PATH)
        self.enforce_sqlite_size_guard()

    def _init_db(self, db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reels_post_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_uid TEXT NOT NULL UNIQUE,
                    posted_at_iso TEXT NOT NULL,
                    posted_epoch INTEGER NOT NULL,
                    run_env TEXT NOT NULL,
                    publish_mode TEXT NOT NULL,
                    persona_ref TEXT NOT NULL,
                    character_id TEXT NOT NULL,
                    topic_category TEXT NOT NULL,
                    pexels_query TEXT NOT NULL,
                    pexels_ids_json TEXT NOT NULL,
                    video_sha256_json TEXT NOT NULL,
                    music_filename TEXT NOT NULL,
                    music_bpm REAL DEFAULT 0.0,
                    video_duration_sec REAL DEFAULT 0.0,
                    story_bm TEXT NOT NULL,
                    post_caption TEXT NOT NULL,
                    story_hash TEXT NOT NULL,
                    comment_mode TEXT NOT NULL,
                    shopee_product_code TEXT DEFAULT 'TRAFFIC_ONLY',
                    shopee_url TEXT DEFAULT '-',
                    comment_variant_idx INTEGER DEFAULT 0,
                    comment_text TEXT NOT NULL,
                    fb_post_id TEXT DEFAULT '',
                    fb_comment_id TEXT DEFAULT '',
                    vector_shard INTEGER DEFAULT 0
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_reels_epoch ON reels_post_history(posted_epoch DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_reels_hash ON reels_post_history(story_hash)")
            conn.commit()
        finally:
            conn.close()

    def enforce_sqlite_size_guard(self) -> None:
        if not REELS_HISTORY_DB_PATH.exists():
            return
        if REELS_HISTORY_DB_PATH.stat().st_size < SQLITE_MAX_SAFE_BYTES:
            return

        conn = sqlite3.connect(str(REELS_HISTORY_DB_PATH))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            conn.commit()
        finally:
            conn.close()

        if REELS_HISTORY_DB_PATH.stat().st_size < SQLITE_MAX_SAFE_BYTES:
            return

        existing = sorted(DATA_DIR.glob("ps2_reels_posts_history_part*.db"))
        shard_path = DATA_DIR / f"ps2_reels_posts_history_part{len(existing) + 1:03d}.db"
        self._init_db(shard_path)

        src = sqlite3.connect(str(REELS_HISTORY_DB_PATH))
        dst = sqlite3.connect(str(shard_path))
        try:
            total = int(src.execute("SELECT COUNT(*) FROM reels_post_history").fetchone()[0])
            move_n = max(1, total // 2)
            rows = src.execute("SELECT * FROM reels_post_history ORDER BY posted_epoch ASC LIMIT ?", (move_n,)).fetchall()
            if rows:
                cols = [d[1] for d in src.execute("PRAGMA table_info(reels_post_history)").fetchall()]
                non_id = [c for c in cols if c != "id"]
                id_i = cols.index("id")
                c_idx = [cols.index(c) for c in non_id]
                sql_ins = f"INSERT OR IGNORE INTO reels_post_history ({', '.join(non_id)}) VALUES ({', '.join(['?'] * len(non_id))})"
                del_ids = []
                for r in rows:
                    del_ids.append((int(r[id_i]),))
                    dst.execute(sql_ins, [r[i] for i in c_idx])
                dst.commit()
                src.executemany("DELETE FROM reels_post_history WHERE id = ?", del_ids)
                src.commit()
                src.execute("VACUUM")
                src.commit()
        finally:
            src.close()
            dst.close()

        all_shards = sorted(DATA_DIR.glob("ps2_reels_posts_history_part*.db"))
        while len(all_shards) > SQLITE_MAX_ARCHIVE_SHARDS:
            all_shards.pop(0).unlink(missing_ok=True)

    def get_recent_reels_posts(self, limit: int = 10) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        seen = set()

        try:
            for item in reels_redis.get_rolling_history(stream="reels", limit=limit):
                if isinstance(item, dict):
                    h = item.get("story_hash", "")
                    if h and h not in seen:
                        seen.add(h)
                        records.append(item)
        except Exception:
            pass

        if REELS_HISTORY_DB_PATH.exists():
            conn = sqlite3.connect(str(REELS_HISTORY_DB_PATH))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    SELECT post_uid, posted_at_iso, posted_epoch, character_id,
                           topic_category, pexels_query, story_bm, story_hash,
                           comment_mode, shopee_product_code, comment_variant_idx
                    FROM reels_post_history
                    ORDER BY posted_epoch DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                for r in rows:
                    d = dict(r)
                    h = d.get("story_hash", "")
                    if h and h not in seen:
                        seen.add(h)
                        records.append(d)
            finally:
                conn.close()

        records.sort(key=lambda x: int(x.get("posted_epoch", 0)), reverse=True)
        return records[:limit]

    def decide_and_build_hybrid_comment(
        self,
        is_gaming_eligible: bool,
        topic_hint: str,
        recent_posts: List[Dict[str, Any]],
        force_promo_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Strategi Komen Hibrid 70% / 30%:
        - 70% Mod Interaksi Follower (soalan santai tanpa link).
        - 30% Mod Promo Shopee Bersasar (hanya jika visual/topik berkait konsol/retro/gaming).
        """
        recent_promo_count = sum(
            1 for p in recent_posts[:6] if str(p.get("comment_mode", "")) == "SHOPEE_PROMO_30"
        )

        if force_promo_mode == "shopee":
            use_shopee = True
        elif force_promo_mode == "organic":
            use_shopee = False
        else:
            # Kekalkan nisbah sekitar 30% promo (maksimum 2 daripada 6 post terakhir) & hanya untuk topik gaming/retro
            use_shopee = bool(is_gaming_eligible and recent_promo_count < 2 and SHOPEE_DB_PATH.exists())

        if not use_shopee:
            used_org = [
                int(p["comment_variant_idx"])
                for p in recent_posts
                if p.get("comment_mode") == "ORGANIC_FOLLOWER_70" and p.get("comment_variant_idx") is not None
            ]
            total_org = len(ORGANIC_INTERACTION_COMMENTS_20)
            cands = [f"org_{i:02d}" for i in range(total_org)]
            excl = [f"org_{i:02d}" for i in used_org[:12]]
            picked = reels_redis.pick_least_used_item("reels_organic_comments", cands, exclude_ids=excl)
            idx = int(picked.split("_")[1]) if (picked and picked.startswith("org_")) else random.randrange(total_org)

            return {
                "comment_mode": "ORGANIC_FOLLOWER_70",
                "shopee_product_code": "TRAFFIC_ONLY",
                "shopee_product_name": "Mod Interaksi Follower (Tiada Link Produk)",
                "shopee_url": "-",
                "comment_variant_idx": idx,
                "comment_text": ORGANIC_INTERACTION_COMMENTS_20[idx],
            }

        # Mod 30% Promo Shopee Bersasar
        low = topic_hint.lower()
        if "matrix" in low:
            target_compat = "MATRIX_ONLY"
        elif any(k in low for k in ("fmcb", "opentuna", "memory card", "fortuna")):
            target_compat = "MOD_CARD_ONLY"
        elif any(k in low for k in ("console", "ps2", "playstation", "crt", "controller", "opl")):
            target_compat = "NON_MATRIX_OPL_SET"
        else:
            target_compat = "UNIVERSAL_STORAGE"

        conn = sqlite3.connect(str(SHOPEE_DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            all_prods = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM shopee_products WHERE is_active = 1 ORDER BY times_promoted ASC, id ASC"
                ).fetchall()
            ]
        finally:
            conn.close()

        recent_codes = [
            str(p.get("shopee_product_code", ""))
            for p in recent_posts[:5]
            if p.get("shopee_product_code") and p.get("shopee_product_code") != "TRAFFIC_ONLY"
        ]
        fresh_prods = [r for r in all_prods if r["product_code"] not in recent_codes] or all_prods
        compat_prods = [r for r in fresh_prods if r["compatibility"] == target_compat] or fresh_prods
        by_code = {r["product_code"]: r for r in compat_prods}

        picked_code = reels_redis.pick_least_used_item(
            "reels_shopee_products",
            list(by_code.keys()),
            exclude_ids=recent_codes,
        )
        prod = by_code.get(picked_code or "", compat_prods[0])

        used_shp = [
            int(p["comment_variant_idx"])
            for p in recent_posts
            if p.get("comment_mode") == "SHOPEE_PROMO_30" and p.get("comment_variant_idx") is not None
        ]
        total_shp = len(REELS_SHOPEE_COMMENTS_14)
        s_cands = [f"shp_{i:02d}" for i in range(total_shp)]
        s_excl = [f"shp_{i:02d}" for i in used_shp[:8]]
        picked_s = reels_redis.pick_least_used_item("reels_shopee_comments", s_cands, exclude_ids=s_excl)
        s_idx = int(picked_s.split("_")[1]) if (picked_s and picked_s.startswith("shp_")) else random.randrange(total_shp)

        url = str(prod["shopee_url"]).strip()
        tier = str(prod.get("storage_tier", "")).replace("_", " ")
        hint = str(prod.get("game_count_hint", ""))
        compat = str(prod.get("compatibility", ""))

        if compat == "MATRIX_ONLY":
            spec = f"📌 Khas PS2 Matrix ({tier} | {hint})"
        elif compat == "NON_MATRIX_OPL_SET":
            spec = f"📌 Set Lengkap Mod Card FMCB/OpenTuna + {tier} ({hint})"
        elif compat == "MOD_CARD_ONLY":
            spec = "📌 Mod Card Sistem OPL + Code Breaker (PS2 Fat & Slim)"
        else:
            spec = f"📌 Pilihan Kapasiti ({hint}) — Boleh pilih game sendiri!"

        comment_body = f"{REELS_SHOPEE_COMMENTS_14[s_idx].format(url=url)}\n\n{spec}"
        return {
            "comment_mode": "SHOPEE_PROMO_30",
            "shopee_product_code": prod["product_code"],
            "shopee_product_name": prod["product_name"],
            "shopee_url": url,
            "comment_variant_idx": s_idx,
            "comment_text": comment_body,
        }

    def archive_video_to_b2(self, video_path: Path, post_uid: str) -> List[Dict[str, Any]]:
        proxy_url = (getattr(cfg, "CF_WORKER_B2_PROXY_STORAGE", "") or os.getenv("CF_WORKER_B2_PROXY_STORAGE", "")).strip()
        proxy_secret = (getattr(cfg, "PS2N_PROXY_SECRET", "") or os.getenv("PS2N_PROXY_SECRET", "")).strip()
        if not proxy_url or not proxy_secret or not video_path.exists():
            return []

        month_tag = datetime.now(tz=MYT).strftime("%Y%m")
        remote_key = f"reels_videos/{month_tag}/{post_uid}.mp4"
        sig = inspect.signature(b2_proxy.upload_file)
        params = list(sig.parameters.keys())
        try:
            if "local_path" in params and "remote_path" in params:
                res = b2_proxy.upload_file(local_path=video_path, remote_path=remote_key)
            elif "file_path" in params and "remote_path" in params:
                res = b2_proxy.upload_file(file_path=video_path, remote_path=remote_key)
            else:
                res = b2_proxy.upload_file(video_path, remote_key)
            return [{"file": video_path.name, "remote_key": remote_key, "result": res}]
        except Exception as exc:
            console.print(f"[yellow]⚠️ Amaran muat naik video Reels ke B2: {exc}[/yellow]")
            return []

    def save_all_records(
        self,
        record: Dict[str, Any],
        clips: List[Dict[str, Any]],
        music_info: Dict[str, Any],
        b2_records: List[Dict[str, Any]],
    ) -> None:
        post_uid = record["post_uid"]
        pexels_ids = [str(c["id"]) for c in clips]
        video_shas = [str(c["sha256"]) for c in clips if c.get("sha256")]

        # 1. SQLite Tempatan (< 30 MB)
        conn = sqlite3.connect(str(REELS_HISTORY_DB_PATH))
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO reels_post_history (
                    post_uid, posted_at_iso, posted_epoch, run_env, publish_mode,
                    persona_ref, character_id, topic_category, pexels_query,
                    pexels_ids_json, video_sha256_json, music_filename, music_bpm,
                    video_duration_sec, story_bm, post_caption, story_hash,
                    comment_mode, shopee_product_code, shopee_url,
                    comment_variant_idx, comment_text, fb_post_id, fb_comment_id, vector_shard
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    post_uid,
                    record["posted_at_iso"],
                    record["posted_epoch"],
                    record["run_env"],
                    record["publish_mode"],
                    record["persona_ref"],
                    record["character_id"],
                    record["topic_category"],
                    record["pexels_query"],
                    json.dumps(pexels_ids),
                    json.dumps(video_shas),
                    music_info["filename"],
                    float(music_info.get("bpm", 0.0)),
                    float(record["video_duration_sec"]),
                    record["story_bm"],
                    record["post_caption"],
                    record["story_hash"],
                    record["comment_mode"],
                    record["shopee_product_code"],
                    record["shopee_url"],
                    int(record["comment_variant_idx"]),
                    record["comment_text"],
                    record["fb_post_id"],
                    record["fb_comment_id"],
                    int(record["vector_shard"]),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        self.enforce_sqlite_size_guard()

        # Kemas kini kaunter Shopee jika mod promo 30% digunakan
        if record["comment_mode"] == "SHOPEE_PROMO_30" and SHOPEE_DB_PATH.exists():
            s_conn = sqlite3.connect(str(SHOPEE_DB_PATH))
            try:
                s_conn.execute(
                    """
                    UPDATE shopee_products
                    SET times_promoted = times_promoted + 1, last_promoted_at = ?
                    WHERE product_code = ?
                    """,
                    (record["posted_at_iso"], record["shopee_product_code"]),
                )
                s_conn.commit()
            finally:
                s_conn.close()

        # 2. Kunci Pexels IDs & SHA-256 (180 Hari) + Muzik (60 Hari)
        mark_pexels_clips_used_6_months(
            clips=clips,
            category=record["topic_category"],
            query_str=record["pexels_query"],
            post_uid=post_uid,
        )
        mark_music_track_used(music_info=music_info, post_uid=post_uid)

        # 3. Simpan ke Upstash Redis (`ps2n_reels`) + Kuota Gelongsor 48 Jam
        reels_redis.save_published_post(
            post_uid=post_uid,
            post_meta={
                "source": "reels",
                "published_at": record["posted_epoch"],
                "posted_at_iso": record["posted_at_iso"],
                "publish_mode": record["publish_mode"],
                "fb_post_id": record["fb_post_id"],
                "fb_comment_id": record["fb_comment_id"],
                "topic_category": record["topic_category"],
                "pexels_ids": pexels_ids,
                "music_filename": music_info["filename"],
                "comment_mode": record["comment_mode"],
                "shopee_product_code": record["shopee_product_code"],
                "b2_media": b2_records,
            },
            dedup_fingerprint=f"reels_story:{record['story_hash']}",
            dedup_ttl_seconds=STORY_EXACT_COOLDOWN_DAYS * 86400,
        )
        reels_redis.record_quota_event(
            quota_name=REELS_QUOTA_KEY,
            event_id=post_uid,
            window_seconds=REELS_WINDOW_SECONDS,
        )
        reels_redis.push_rolling_history(
            record={
                "post_uid": post_uid,
                "posted_at_iso": record["posted_at_iso"],
                "posted_epoch": record["posted_epoch"],
                "character_id": record["character_id"],
                "topic_category": record["topic_category"],
                "pexels_query": record["pexels_query"],
                "story_bm": record["story_bm"],
                "story_hash": record["story_hash"],
                "comment_mode": record["comment_mode"],
                "shopee_product_code": record["shopee_product_code"],
                "comment_variant_idx": record["comment_variant_idx"],
            },
            stream="reels",
            max_items=20,
            also_push_global=True,
        )

        if record["comment_mode"] == "ORGANIC_FOLLOWER_70":
            reels_redis.increment_rotation_score("reels_organic_comments", f"org_{record['comment_variant_idx']:02d}")
        else:
            reels_redis.increment_rotation_score("reels_shopee_products", record["shopee_product_code"])
            reels_redis.increment_rotation_score("reels_shopee_comments", f"shp_{record['comment_variant_idx']:02d}")

        # 4. Simpan Vektor Semantik Cerita & Kategori Visual (`ps2n_reels`)
        try:
            reels_vector.upsert_story_vector(
                post_uid=post_uid,
                game_title=f"reels:{record['topic_category']}:{record['pexels_query']}",
                story_text=record["story_bm"],
                extra_meta={
                    "source": "reels",
                    "character_id": record["character_id"],
                    "topic_category": record["topic_category"],
                    "posted_epoch": record["posted_epoch"],
                    "story_hash": record["story_hash"],
                },
            )
            reels_vector.upsert_media_vector(
                media_uid=post_uid,
                category="cat_reels_clip",
                visual_description=f"{record['topic_category']} | {record['pexels_query']}",
                extra_meta={"pexels_ids": pexels_ids, "music": music_info["filename"]},
            )
        except Exception as exc:
            console.print(f"[yellow]⚠️ Amaran simpan Vector Reels: {exc}[/yellow]")


# ==============================================================================
# 3. PENJANA KAPSYEN PERSONA AI DINAMIK (MEMORI 5 AYAT TERAKHIR + WATAK HARIAN)
# ==============================================================================
def generate_dynamic_reels_story(
    cinematic_brief: str,
    topic_category: str,
    pexels_query: str,
    music_info: Dict[str, Any],
    cal_ctx: Dict[str, Any],
    recent_posts: List[Dict[str, Any]],
) -> Tuple[bool, Dict[str, Any]]:
    self_ref = random.choice(["admin", "min"])
    char_obj = cal_ctx["character"]

    # Bina Memori 5 Ayat Terakhir yang telah dijana sebelum ini
    last_5_lines: List[str] = []
    for idx, prev in enumerate(recent_posts[:5], 1):
        prev_txt = re.sub(r"\s+", " ", str(prev.get("story_bm", "")).strip())
        if prev_txt:
            last_5_lines.append(f"{idx}. [{prev.get('character_id', '-')}] {prev_txt[:190]}...")
    memory_5_block = (
        "\n".join(last_5_lines)
        if last_5_lines
        else "Tiada rekod terdahulu — ini siaran pertama."
    )

    system_prompt = (
        f"Kau ialah '{self_ref}', lelaki Malaysia berusia 35 tahun yang mengendalikan halaman "
        "Facebook 'PS2 Retro Nostalgia'.\n\n"
        f"WATAK & KARAKTER HARI INI ({cal_ctx['day_name']} — {char_obj['label']}):\n"
        f"{char_obj['persona_guide']}\n"
        f"PANDUAN PEMBUKA: {char_obj['hook_hint']}\n\n"
        "PERATURAN WAJIB PENULISAN REELS:\n"
        "1. Tulis 1 perenggan cerita pendek yang hidup, santai, dan semula jadi dalam Bahasa Melayu Malaysia "
        "harian (macam sembang kedai kopi, BUKAN terjemahan kaku).\n"
        "2. Gabungkan jalan cerita visual daripada babak video + suasana muzik latar secara bersahaja.\n"
        f"3. Bahasakan diri sebagai '{self_ref}' (DILARANG guna 'aku' atau 'saya') dan sapa pembaca sebagai 'korang' atau 'geng'.\n"
        f"4. KESEDARAN MASA MALAYSIA: {cal_ctx['timestamp_display']} | Mood: {cal_ctx['mood']} | Suasana: {cal_ctx['special_note']}.\n"
        f"   Jika sebut waktu, HANYA guna '{cal_ctx['allowed_cues']}' dan HARAM sebut '{cal_ctx['forbidden_cues']}'.\n"
        "5. Rujuk MEMORI 5 AYAT TERAKHIR di bawah — DILARANG meniru ayat pembuka, struktur ayat, atau sudut cerita yang sama!\n"
        "6. Panjang teks antara 350 hingga 520 aksara, SIFAR emoji, tanpa tajuk di baris atas, tanpa link URL atau harga RM, "
        "dan tutup dengan tanda noktah (.) yang lengkap."
    )

    user_prompt = (
        f"KATEGORI VISUAL ({topic_category}): {pexels_query}\n"
        f"MUZIK LATAR: '{music_info['title']}' — {music_info['vibe_bm']}\n\n"
        f"URUTAN BABAK VISUAL VIDEO (HASIL OCR):\n{cinematic_brief}\n\n"
        f"MEMORI 5 AYAT TERAKHIR (WAJIB BERBEZA GAYA & PEMBUKA):\n{memory_5_block}\n\n"
        f"Tulis kapsyen Reels santai daripada watak '{self_ref}' (350 - 520 aksara, tanpa emoji):"
    )

    last_diag: Dict[str, Any] = {}
    for attempt in range(1, 5):
        adj_temp = min(0.86, round(0.70 + ((attempt - 1) * 0.04), 2))
        inf = llm_engine.generate_chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=adj_temp,
            max_tokens=380,
        )

        cleaned = clean_and_scrub_bm_story(inf["raw_text"], max_chars=CAPTION_SOFT_TRIM)
        cleaned = re.sub(r"\b(aku|saya)\b", self_ref, cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(rf"(^|[.!?]\s+){self_ref}\b", lambda m: f"{m.group(1)}{self_ref.capitalize()}", cleaned)
        cleaned = re.sub(r"[^\x20-\x7E\n]", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        if len(cleaned) < CAPTION_MIN_CHARS or len(cleaned) > CAPTION_MAX_CHARS:
            last_diag = {"attempt": attempt, "status": f"Panjang luar julat ({len(cleaned)} aksara)"}
            continue

        low = cleaned.lower()
        if "http://" in low or "https://" in low or re.search(r"\brm\s?\d+", low):
            last_diag = {"attempt": attempt, "status": "Dikesan URL atau harga RM"}
            continue

        if re.search(cal_ctx["forbidden_regex"], low):
            last_diag = {"attempt": attempt, "status": f"Percanggahan waktu dengan slot {cal_ctx['period']}"}
            continue

        # Semak pembuka 4 perkataan & pertindihan kata terhadap 10 post lepas
        cand_hash = hashlib.sha256(low.encode("utf-8")).hexdigest()
        cand_words = re.findall(r"\b[a-z0-9]{3,}\b", low)
        cand_open_4 = cand_words[:4]
        cand_set = set(cand_words)

        is_clone = False
        for prev in recent_posts[:10]:
            p_low = str(prev.get("story_bm", "")).lower()
            p_hash = str(prev.get("story_hash") or hashlib.sha256(p_low.encode("utf-8")).hexdigest())
            if cand_hash == p_hash:
                is_clone = True
                last_diag = {"attempt": attempt, "status": "Ayat 100% sama dengan rekod lepas"}
                break

            p_words = re.findall(r"\b[a-z0-9]{3,}\b", p_low)
            if len(cand_open_4) >= 4 and cand_open_4 == p_words[:4]:
                is_clone = True
                last_diag = {"attempt": attempt, "status": "4 perkataan pembuka sama dengan rekod lepas"}
                break

            p_set = set(p_words)
            if cand_set and p_set:
                jaccard = len(cand_set & p_set) / max(1, len(cand_set | p_set))
                if jaccard >= 0.72:
                    is_clone = True
                    last_diag = {"attempt": attempt, "status": f"Pertindihan kata terlalu tinggi ({jaccard*100:.0f}%)"}
                    break

        if is_clone:
            continue

        # Semak Redis & Upstash Vector Semantik
        dup_r, _ = reels_redis.is_duplicate_fingerprint(f"reels_story:{cand_hash}")
        if dup_r:
            last_diag = {"attempt": attempt, "status": "Hash cerita dikunci dalam Redis (180 hari)"}
            continue

        is_dup_vec, dup_match, vec_shard = reels_vector.check_semantic_duplicate(
            game_title=f"reels:{topic_category}:{pexels_query}",
            candidate_story=cleaned,
            similarity_threshold=0.76,
            exact_threshold=0.95,
            exact_cooldown_days=STORY_SEMANTIC_EXACT_DAYS,
            similar_cooldown_days=STORY_SEMANTIC_SIMILAR_DAYS,
            now_epoch=cal_ctx["now_epoch"],
        )
        if is_dup_vec:
            last_diag = {"attempt": attempt, "status": f"Ditolak Vector (#{vec_shard}): {dup_match}"}
            continue

        hashtags = (
            "#PS2RetroNostalgia #PS2Malaysia #ReelsMalaysia #RetroGaming #GamingMalaysia #Nostalgia2000an"
            if "PS2" in topic_category or "RETRO" in topic_category or "GAMING" in topic_category
            else "#PS2RetroNostalgia #ReelsMalaysia #RetroVibes #GamingMalaysia #SantaiMalam #Nostalgia"
        )
        return True, {
            "persona_ref": self_ref,
            "story_bm": cleaned,
            "story_chars": len(cleaned),
            "post_caption": f"{cleaned}\n\n{hashtags}",
            "story_hash": cand_hash,
            "vector_shard": vec_shard,
            "llm_metrics": {
                "attempt": attempt,
                "temperature_used": adj_temp,
                "elapsed_sec": inf["elapsed_sec"],
                "completion_tokens": inf["completion_tokens"],
                "tok_per_sec": inf["tok_per_sec"],
                "story_chars": len(cleaned),
                "status": "Lulus Unik (Memori + Redis + Vector)",
            },
        }

    return False, {"error": "Gagal menjana kapsyen unik selepas 4 percubaan.", "diag": last_diag}


# ==============================================================================
# 4. PENERBIT FB REELS DAHULU -> LANGKAU FEED JIKA BERJAYA -> FALLBACK FEED
# ==============================================================================
class FacebookReelsPublisher:
    def __init__(self) -> None:
        self.app_secret = (getattr(cfg, "PS2N_FB_APP_SECRET", "") or os.getenv("PS2N_FB_APP_SECRET", "")).strip()
        self.page_id = (getattr(cfg, "PS2N_FB_PAGE_ID", "") or os.getenv("PS2N_FB_PAGE_ID", "")).strip()
        self.page_token = (getattr(cfg, "PS2N_FB_PAGE_TOKEN", "") or os.getenv("PS2N_FB_PAGE_TOKEN", "")).strip()
        self.graph_ver = (getattr(cfg, "PS2N_FB_GRAPH_VERSION", "") or os.getenv("PS2N_FB_GRAPH_VERSION", "") or "v26.0").strip()

        if not self.page_id or not self.page_token:
            raise RuntimeError("Kredensial PS2N_FB_PAGE_ID atau PS2N_FB_PAGE_TOKEN tiada.")

    def _auth_params(self) -> Dict[str, str]:
        params: Dict[str, str] = {"access_token": self.page_token}
        if self.app_secret:
            params["appsecret_proof"] = hmac.new(
                self.app_secret.encode("utf-8"),
                self.page_token.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        return params

    def publish_reel_or_feed_with_comment(
        self,
        video_path: Path,
        caption: str,
        comment_text: str,
    ) -> Dict[str, str]:
        file_size = video_path.stat().st_size
        reels_url = f"https://graph.facebook.com/{self.graph_ver}/{self.page_id}/video_reels"
        publish_mode = "FB_REELS"
        fb_post_id = ""
        reels_err = ""

        # Laluan 1: Facebook Reels 3-Fasa
        try:
            p_start = self._auth_params()
            p_start["upload_phase"] = "start"
            r_start = requests.post(reels_url, data=p_start, timeout=30)
            j_start = r_start.json()
            if r_start.status_code != 200 or "video_id" not in j_start:
                raise RuntimeError(f"Fasa Start Reels gagal: {json.dumps(j_start)}")

            video_id = str(j_start["video_id"])
            upload_url = str(j_start["upload_url"])

            with open(video_path, "rb") as vf:
                video_bytes = vf.read()

            r_up = requests.post(
                upload_url,
                headers={
                    "Authorization": f"OAuth {self.page_token}",
                    "offset": "0",
                    "file_size": str(file_size),
                    "Content-Type": "application/octet-stream",
                },
                data=video_bytes,
                timeout=180,
            )
            if r_up.status_code != 200:
                raise RuntimeError(f"Fasa Rupload gagal (HTTP {r_up.status_code}): {r_up.text[:140]}")

            p_fin = self._auth_params()
            p_fin.update(
                {
                    "upload_phase": "finish",
                    "video_id": video_id,
                    "video_state": "PUBLISHED",
                    "description": caption,
                }
            )
            r_fin = requests.post(reels_url, data=p_fin, timeout=45)
            j_fin = r_fin.json()
            if r_fin.status_code != 200 or not j_fin.get("success"):
                raise RuntimeError(f"Fasa Finish Reels gagal: {json.dumps(j_fin)}")

            fb_post_id = video_id
        except Exception as exc:
            reels_err = str(exc)
            console.print(f"[yellow]⚠️ FB Reels tidak berjaya ({reels_err}). Beralih ke FB Feed Video...[/yellow]")
            publish_mode = "FB_FEED_FALLBACK"

            feed_url = f"https://graph-video.facebook.com/{self.graph_ver}/{self.page_id}/videos"
            f_data = self._auth_params()
            f_data["description"] = caption
            with open(video_path, "rb") as vf:
                r_feed = requests.post(
                    feed_url,
                    data=f_data,
                    files={"source": (video_path.name, vf, "video/mp4")},
                    timeout=180,
                )
            j_feed = r_feed.json()
            if r_feed.status_code not in (200, 201) or "id" not in j_feed:
                raise RuntimeError(
                    f"Gagal kedua-dua FB Reels ({reels_err}) dan FB Feed ({json.dumps(j_feed)})."
                )
            fb_post_id = str(j_feed["id"])

        # Hantar komen pertama (Interaksi Follower 70% atau Shopee 30%)
        time.sleep(3.0)
        comment_url = f"https://graph.facebook.com/{self.graph_ver}/{fb_post_id}/comments"
        c_data = self._auth_params()
        c_data["message"] = comment_text
        r_com = requests.post(comment_url, data=c_data, timeout=30)
        j_com = r_com.json()
        fb_comment_id = str(j_com.get("id", "")) if (r_com.status_code == 200 and "id" in j_com) else ""

        return {
            "publish_mode": publish_mode,
            "fb_post_id": fb_post_id,
            "fb_comment_id": fb_comment_id,
            "reels_fallback_reason": reels_err,
        }


# ==============================================================================
# 5. PINTU KUOTA 2 VIDEO / 48 JAM PADA JAM RAWAK (9 PAGI - 9 MALAM MYT)
# ==============================================================================
def should_execute_reels_slot(force_run: bool = False) -> Tuple[bool, str]:
    """
    Menyemak kuota gelongsor 48 jam (`fb_reels_48h`, max 2 video) dan kebarangkalian
    rawak harian antara jam 9:00 Pagi hingga 9:00 Malam MYT sebelum memuatkan GGUF.
    """
    if force_run:
        return True, "Mod --force aktif (melepasi pintu rawak & kuota)."

    allowed, used_in_48h, wait_sec = reels_redis.check_sliding_quota(
        quota_name=REELS_QUOTA_KEY,
        max_events=REELS_MAX_PER_48H,
        window_seconds=REELS_WINDOW_SECONDS,
    )
    if not allowed:
        hrs_left = round(wait_sec / 3600.0, 1)
        return False, f"Kuota 48 jam sudah penuh ({used_in_48h}/{REELS_MAX_PER_48H} Reels disiarkan). Tunggu ~{hrs_left} jam lagi."

    now = datetime.now(tz=MYT)
    remaining_needed = REELS_MAX_PER_48H - used_in_48h

    # Terdapat 13 slot jam sehari (9:20 AM .. 9:20 PM MYT). Anggaran baki slot dalam 48 jam:
    hours_left_today = max(1, 22 - max(9, now.hour))
    estimated_remaining_slots = hours_left_today + 13 if (now.timetuple().tm_yday % 2 == 0) else hours_left_today

    # Kira kebarangkalian rawak supaya tepat ~2 video disiarkan dalam tetingkap 48 jam pada jam berbeza-beza
    prob = min(1.0, max(0.14, remaining_needed / max(1, estimated_remaining_slots)))
    roll = random.random()
    if roll <= prob:
        return True, f"Slot rawak terpilih (Roll {roll:.2f} <= {prob:.2f} | Kuota 48j: {used_in_48h}/{REELS_MAX_PER_48H})."
    return False, f"Slot jam ini dilangkau secara rawak (Roll {roll:.2f} > {prob:.2f} | Kuota 48j: {used_in_48h}/{REELS_MAX_PER_48H})."


def main() -> None:
    parser = argparse.ArgumentParser(description="X17: Persona AI #3 Facebook Reels Auto-Poster")
    parser.add_argument("--dry-run", action="store_true", help="Jana video & kapsyen tanpa pos sebenar ke Facebook.")
    parser.add_argument("--force", action="store_true", help="Paksa larian tanpa melalui pintu kebarangkalian rawak.")
    parser.add_argument("--query", type=str, default="", help="Kata kunci carian Pexels khusus (pilihan).")
    parser.add_argument("--clips", type=int, default=4, choices=[3, 4, 5], help="Bilangan klip Pexels (3-5).")
    parser.add_argument("--promo-mode", type=str, default="auto", choices=["auto", "organic", "shopee"], help="Mod komen pertama.")
    args = parser.parse_args()

    is_ci = os.getenv("GITHUB_ACTIONS", "").lower() == "true"
    run_env = "GITHUB_ACTIONS" if is_ci else "LOCAL_WSL"
    cal_ctx = get_dynamic_myt_context()

    # Pintu Kuota Awal (Jimat sumber GitHub Actions jika kuota 2 Reels/48j penuh atau bukan giliran jam rawak)
    should_run, gate_msg = should_execute_reels_slot(force_run=(args.force or args.dry_run))
    if not should_run:
        console.print(Panel(f"[bold yellow]⏭️ LANGKAU SESI REELS:[/bold yellow] {gate_msg}", border_style="yellow"))
        skip_payload = {
            "status": "SKIPPED_QUOTA_OR_RANDOM_SLOT",
            "reason": gate_msg,
            "posted_at_iso": cal_ctx["now_iso"],
            "fb_post_id": "SKIPPED",
        }
        LAST_OUTPUT_JSON.write_text(json.dumps(skip_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return

    lock_token: Optional[str] = None
    if not args.dry_run:
        lock_token = reels_redis.acquire_lock("x17_fb_reels_post", ttl_seconds=900)
        if not lock_token:
            console.print("[yellow]⚠️ Proses X17 lain sedang aktif. Menghentikan larian bagi mengelak pertindihan.[/yellow]")
            return

    try:
        coordinator = ReelsHistoryCoordinator()
        recent_posts = coordinator.get_recent_reels_posts(limit=10)
        char_obj = cal_ctx["character"]

        console.print(
            Panel.fit(
                "[bold cyan]PS2-RETRO-NOSTALGIA :: PERSONA AI #3 FACEBOOK REELS (X17)[/bold cyan]\n"
                f"[dim]Persekitaran: {run_env} | {cal_ctx['timestamp_display']}\n"
                f"Watak Hari Ini: {char_obj['label']} ({char_obj['char_id']}) | {gate_msg}[/dim]",
                border_style="cyan",
            )
        )

        # 1. Pilih & Analisis Muzik dari assets/music/ (X15)
        target_dur = random.randint(34, 46)
        with console.status("[bold cyan]1. Memilih lagu segar dari assets/music/ & menganalisis rentak Librosa (X15)...[/bold cyan]"):
            audio_clip, music_info = prepare_reels_music_track(target_duration_sec=target_dur)

        # 2. Tarik 3-5 Klip Pexels 9:16 (1 Request) & Tapis 180 Hari (X14)
        with console.status("[bold cyan]2. Menarik 3-5 klip Pexels 9:16 (1 Request) & menapis kunci 180 hari (X14)...[/bold cyan]"):
            pex_res = fetch_and_prepare_pexels_clips(
                work_dir=WORK_DIR,
                desired_count=args.clips,
                custom_query=args.query,
            )
        clips = pex_res["clips"]
        if len(clips) < 3:
            if audio_clip:
                audio_clip.close()
            raise RuntimeError(f"Hanya {len(clips)} klip Pexels berjaya disediakan (minimum 3 diperlukan). Stats: {pex_res['stats']}")

        # 3. Ekstrak 3 Snapshot/Video & Proses OCR Berkelompok 3-4 Gambar/Sesi (X16)
        console.print(
            f"[bold cyan]3. Mengekstrak 3 snapshot/video ({len(clips) * 3} gambar) & memproses OCR berperingkat (X16)...[/bold cyan]"
        )
        ocr_pkg = extract_and_analyze_clips_ocr(
            clips=clips,
            work_dir=WORK_DIR,
            topic_hint=f"{pex_res['category']} {pex_res['query']}",
            json_ref_path=TEMP_DIR / "x17_ocr_sessions.json",
            db_ref_path=TEMP_DIR / "x17_reels_ocr_state.db",
        )
        clean_clips = ocr_pkg["clean_clips"]

        # 4. Sunting & Cantum Video Menegak 1080x1920 (30-50 Saat) + Muzik (X16)
        out_mp4 = WORK_DIR / f"ps2n_reels_{cal_ctx['now_epoch']}.mp4"
        with console.status("[bold cyan]4. Menyunting & mencantum video Reels 1080x1920 (30-50s) + muzik latar (X16)...[/bold cyan]"):
            final_video_path, final_video_dur = render_vertical_reels_video(
                clips=clean_clips,
                audio_clip=audio_clip,
                music_info=music_info,
                output_mp4_path=out_mp4,
                target_duration_sec=target_dur,
            )

        # 5. Penjanaan Kapsyen Persona AI Dinamik (Memori 5 Ayat Terakhir + Watak Harian)
        with console.status("[bold cyan]5. Menjana kapsyen santai Persona AI (Memori 5 ayat + Redis + Vector)...[/bold cyan]"):
            ok_gen, gen_pkg = generate_dynamic_reels_story(
                cinematic_brief=ocr_pkg["cinematic_brief"],
                topic_category=pex_res["category"],
                pexels_query=pex_res["query"],
                music_info=music_info,
                cal_ctx=cal_ctx,
                recent_posts=recent_posts,
            )
        llm_engine.unload()

        if not ok_gen:
            raise RuntimeError(f"Gagal menjana kapsyen Reels sah: {gen_pkg}")

        # 6. Tentukan Komen Pertama Hibrid (70% Interaksi Follower / 30% Link Shopee Bersasar)
        is_gaming_eligible = bool(pex_res["is_gaming_promo_eligible"] or ocr_pkg["visual_has_gaming_elements"])
        force_mode = None if args.promo_mode == "auto" else args.promo_mode
        comment_pkg = coordinator.decide_and_build_hybrid_comment(
            is_gaming_eligible=is_gaming_eligible,
            topic_hint=f"{pex_res['category']} {pex_res['query']} {ocr_pkg['cinematic_brief']}",
            recent_posts=recent_posts,
            force_promo_mode=force_mode,
        )

        summary_tbl = Table(title="Ringkasan Produksi Reels X17", box=box.ROUNDED, header_style="bold yellow")
        summary_tbl.add_column("Komponen", style="bold white")
        summary_tbl.add_column("Butiran", style="cyan")
        summary_tbl.add_row("Kategori & Pexels", f"{pex_res['category']} ('{pex_res['query']}') | {len(clean_clips)} klip")
        summary_tbl.add_row("Muzik & Tempo", f"{music_info['title']} ({music_info['bpm']} BPM)")
        summary_tbl.add_row("Video Reels Akhir", f"1080x1920 | {final_video_dur}s | {round(final_video_path.stat().st_size / (1024*1024), 2)} MB")
        summary_tbl.add_row("Watak & Persona", f"{char_obj['char_id']} ({gen_pkg['persona_ref']})")
        summary_tbl.add_row("Strategi Komen", f"{comment_pkg['comment_mode']} ({comment_pkg['shopee_product_code']})")
        console.print(summary_tbl)

        console.print(
            Panel(
                f"[bold yellow]KAPSYEN UTAMA ({gen_pkg['story_chars']} aksara | Sifar Emoji):[/bold yellow]\n"
                f"[white]{gen_pkg['post_caption']}[/white]\n\n"
                f"[bold green]KOMEN PERTAMA ({comment_pkg['comment_mode']}):[/bold green]\n"
                f"[cyan]{comment_pkg['comment_text']}[/cyan]",
                title="Pratonton Siaran Reels",
                border_style="green",
            )
        )

        # 7. Terbitkan FB Reels Dahulu -> Fallback FB Feed Jika Gagal
        fb_res = {"publish_mode": "DRY_RUN", "fb_post_id": "DRY_RUN", "fb_comment_id": "DRY_RUN", "reels_fallback_reason": ""}
        if not args.dry_run:
            with console.status("[bold cyan]6. Menerbitkan video ke Facebook Reels (Fallback Feed jika perlu)...[/bold cyan]"):
                publisher = FacebookReelsPublisher()
                fb_res = publisher.publish_reel_or_feed_with_comment(
                    video_path=final_video_path,
                    caption=gen_pkg["post_caption"],
                    comment_text=comment_pkg["comment_text"],
                )
            console.print(
                f"[bold green]🚀 BERJAYA TERBIT ({fb_res['publish_mode']})![/bold green] "
                f"ID: [bold white]{fb_res['fb_post_id']}[/bold white]"
            )

        post_uid = f"x17_{cal_ctx['now_epoch']}_{gen_pkg['story_hash'][:10]}"
        b2_records: List[Dict[str, Any]] = []
        if not args.dry_run:
            b2_records = coordinator.archive_video_to_b2(final_video_path, post_uid=post_uid)

        final_record = {
            "status": "PUBLISHED" if not args.dry_run else "DRY_RUN",
            "post_uid": post_uid,
            "posted_at_iso": cal_ctx["now_iso"],
            "posted_epoch": cal_ctx["now_epoch"],
            "run_env": run_env,
            "publish_mode": fb_res["publish_mode"],
            "persona_ref": gen_pkg["persona_ref"],
            "character_id": char_obj["char_id"],
            "pillar_id": f"{char_obj['char_id']}:{pex_res['category']}",
            "topic_category": pex_res["category"],
            "pexels_query": pex_res["query"],
            "game_title": f"Reels ({pex_res['category']}: {pex_res['query']})",
            "music_filename": music_info["filename"],
            "music_title": music_info["title"],
            "music_bpm": music_info["bpm"],
            "video_duration_sec": final_video_dur,
            "story_bm": gen_pkg["story_bm"],
            "story_chars": gen_pkg["story_chars"],
            "post_caption": gen_pkg["post_caption"],
            "story_hash": gen_pkg["story_hash"],
            "comment_mode": comment_pkg["comment_mode"],
            "shopee_product_code": comment_pkg["shopee_product_code"],
            "shopee_product_name": comment_pkg["shopee_product_name"],
            "shopee_url": comment_pkg["shopee_url"],
            "comment_variant_idx": comment_pkg["comment_variant_idx"],
            "comment_text": comment_pkg["comment_text"],
            "fb_post_id": fb_res["fb_post_id"],
            "fb_comment_id": fb_res["fb_comment_id"],
            "vector_shard": gen_pkg["vector_shard"],
            "llm_metrics": gen_pkg["llm_metrics"],
            "b2_uploaded_media": b2_records,
        }

        if not args.dry_run:
            coordinator.save_all_records(
                record=final_record,
                clips=clean_clips,
                music_info=music_info,
                b2_records=b2_records,
            )
            console.print("[bold green]✅ Rekod disimpan ke SQLite (<30MB), Redis 180-hari, Vector, & B2 Proxy.[/bold green]")

        LAST_OUTPUT_JSON.write_text(json.dumps(final_record, indent=2, ensure_ascii=False), encoding="utf-8")
        console.print(f"[dim]Laporan JSON disimpan di: {LAST_OUTPUT_JSON}[/dim]")
    finally:
        if lock_token:
            reels_redis.release_lock("x17_fb_reels_post", lock_token)


if __name__ == "__main__":
    main()