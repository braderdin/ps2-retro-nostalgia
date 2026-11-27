#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - PERSONA AI TEXT-ONLY FB AUTO-POSTER (X11)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/live-engine-01/X11_fb_text_persona_poster.py
#
# FUNGSI UTAMA:
# 1. Persona AI: Lelaki Malaysia berumur 35 tahun yang terpelajar, matang, dan
#    arif tentang dunia konsol retro PlayStation 2 (modchip Matrix vs Non-Matrix,
#    Mod Card FMCB / OpenTuna / Fortuna + Code Breaker, Pendrive / HDD OPL,
#    serta sejarah judul permainan PS2). Bahasakan diri "admin" atau "min".
# 2. Kesedaran Masa & Kalendar Lengkap (MYT / UTC+8):
#    Mengesan minit, jam, hari, minggu dalam bulan/tahun, bulan, tahun, suasana
#    waktu (Pagi / Tengah Hari / Petang / Malam), serta hari istimewa & perayaan
#    di Malaysia + tarikh sejarah PlayStation 2.
# 3. Integrasi Penuh Modul X01 - X06 & Pangkalan Data SQLite3:
#    - Membaca /data/ps2_shopee_products.db (10 produk Shopee sebenar)
#    - Membaca /data/ps2_games_catalog.db (katalog judul game PS2)
#    - Menyuap 10 posting terakhir daripada Redis Rolling History (X04) + SQLite
#    - Penapis Semantik Upstash Vector (X05):
#        * Ayat 100% / hampir sama (>= 0.95) dijarakkan minimum 60 hari (2 bulan)
#        * Ayat 75% - 80% sama (>= 0.75) dijarakkan minimum 5 hari
#    - Penggiliran adil produk & variasi komen merentas GitHub Actions (X04)
#    - Simpanan rekod ke SQLite Ledger (X02), Upstash Redis (X04), & Backblaze B2 (X03).
# 4. Format Hantaran & Komen Pertama:
#    - Post Utama (Text-Only): 500 - 650 aksara sahaja, gaya bercerita + soft-sell
#      halus, SIFAR emoji & SIFAR simbol pelik dalam keseluruhan kapsyen utama.
#    - Komen Pertama: Menggunakan emoji, CTA ajak klik link, kitaran 18 variasi
#      ayat komen berbeza, dan link produk Shopee yang sentiasa berbeza setiap post.
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
# IMPORT SEMUA MODUL TERAS DARI live-engine-01 (X01 - X06)
# ------------------------------------------------------------------------------
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for p in [CURRENT_DIR, PROJECT_ROOT]:
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

cfg: Any = importlib.import_module("X01_config")
_ledger_mod: Any = importlib.import_module("X02_sqlite_ledger")
_b2_mod: Any = importlib.import_module("X03_b2_proxy_client")
_redis_mod: Any = importlib.import_module("X04_redis_sharded")
_vector_mod: Any = importlib.import_module("X05_vector_sharded")
_llm_mod: Any = importlib.import_module("X06_local_llm_engine")

ledger_db: Any = getattr(_ledger_mod, "ledger_db")
b2_proxy: Any = getattr(_b2_mod, "b2_proxy")
redis_sharded: Any = getattr(_redis_mod, "redis_sharded")
vector_sharded: Any = getattr(_vector_mod, "vector_sharded")
llm_engine: Any = getattr(_llm_mod, "llm_engine")
clean_and_scrub_bm_story: Any = getattr(_llm_mod, "clean_and_scrub_bm_story")
validate_story_quality: Any = getattr(_llm_mod, "validate_story_quality")


# ==============================================================================
# [BAHAGIAN TUNING MANUAL X11 - PERSONA TEXT-ONLY & ANTI-DUPLIKASI]
# ==============================================================================
STORY_MIN_CHARS: int = int(os.getenv("PS2N_X11_MIN_CHARS", "500"))
STORY_MAX_CHARS: int = int(os.getenv("PS2N_X11_MAX_CHARS", "650"))
STORY_SOFT_TRIM: int = int(os.getenv("PS2N_X11_TRIM_CHARS", "645"))
STORY_MAX_TOKENS: int = int(os.getenv("PS2N_X11_MAX_TOKENS", "380"))
STORY_TEMPERATURE: float = float(os.getenv("PS2N_X11_TEMP", "0.74"))

# Ambang Jarak Masa Penapis Semantik Vector
EXACT_MATCH_THRESHOLD: float = 0.95       # >= 95% sama -> jarak minimum 60 hari (2 bulan)
EXACT_COOLDOWN_DAYS: int = 60
SIMILAR_MATCH_THRESHOLD: float = 0.75     # 75% - 80% sama -> jarak minimum 5 hari
SIMILAR_COOLDOWN_DAYS: int = 5

# Laluan Fail Data Tempatan
DATA_DIR: Path = Path(getattr(cfg, "DATA_DIR", PROJECT_ROOT / "data"))
TEMP_DIR: Path = Path(getattr(cfg, "TEMP_DIR", PROJECT_ROOT / "temp"))
SHOPEE_DB_PATH: Path = DATA_DIR / "ps2_shopee_products.db"
GAMES_DB_PATH: Path = DATA_DIR / "ps2_games_catalog.db"
TEXT_HISTORY_DB_PATH: Path = DATA_DIR / "ps2_text_posts_history.db"
LAST_OUTPUT_JSON: Path = TEMP_DIR / "x11_last_text_post.json"
# ==============================================================================

MYT = timezone(timedelta(hours=8))


# ==============================================================================
# 1. KESEDARAN MASA, KALENDAR & HARI ISTIMEWA / PERAYAAN MALAYSIA (MYT)
# ==============================================================================
FIXED_SPECIAL_DATES_MY: Dict[Tuple[int, int], str] = {
    (1, 1): "Sambutan Tahun Baru — permulaan tahun baharu, masa terbaik mengemas sudut hiburan rumah dan hidupkan semula konsol lama.",
    (2, 14): "Pertengahan Februari — musim cuti sekolah penggal awal dan suasana santai bersama keluarga di rumah.",
    (3, 4): "Tarikh Bersejarah PlayStation 2 (4 Mac 2000) — ulang tahun pelancaran rasmi konsol PS2 di Jepun yang mengubah sejarah dunia gaming.",
    (5, 1): "Hari Pekerja (1 Mei) — cuti umum untuk warga bekerja berehat di rumah sambil mengimbau hobi zaman muda.",
    (5, 16): "Hari Guru (16 Mei) — mengimbau kenangan zaman persekolahan tahun 2000-an, balik sekolah terus singgah kedai game atau buka PS2.",
    (8, 31): "Hari Kebangsaan / Merdeka (31 Ogos) — semangat kemerdekaan Malaysia, cuti umum penuh aktiviti santai bersama rakan dan keluarga.",
    (9, 16): "Hari Malaysia (16 September) — cuti umum meraikan perpaduan rakyat Malaysia dari Semenanjung hingga Sabah dan Sarawak.",
    (10, 26): "Tarikh Pelancaran PS2 Amerika Utara (26 Oktober 2000) — detik bermulanya fenomena global PlayStation 2.",
    (12, 25): "Cuti Umum Hari Krismas & Musim Cuti Sekolah Akhir Tahun — ramai balik kampung dan berkumpul dengan sepupu serta rakan lama.",
}

# Anggaran jendela musim perayaan utama 2026 & hujung tahun untuk kesedaran konteks AI
SEASONAL_WINDOWS_2026: List[Tuple[Tuple[int, int], Tuple[int, int], str]] = [
    ((2, 15), (2, 25), "Musim Perayaan Tahun Baru Cina — suasana balik kampung dan berkumpul beramai-ramai di ruang tamu."),
    ((2, 19), (3, 19), "Bulan Ramadan — ramai gamer 30-an suka layan game PS2 santai sementara menunggu waktu berbuka atau selepas tarawih sebelum sahur."),
    ((3, 20), (3, 30), "Suasana Hari Raya Aidilfitri — meriah melayan sanak saudara dan sepupu bermain game multiplayer PS2 di kampung."),
    ((5, 25), (6, 2), "Musim Perayaan Hari Raya Aidiladha & Pesta Kaamatan / Hari Gawai — cuti panjang penuh kemeriahan di seluruh Malaysia."),
    ((11, 5), (11, 15), "Suasana Perayaan Deepavali — cahaya kemeriahan dan masa berkualiti bersama keluarga di rumah."),
    ((11, 20), (12, 31), "Musim Cuti Sekolah Hujung Tahun & Monsun Hujan — cuaca redup di luar sangat sesuai melayan game klasik PS2 di rumah."),
]


def get_rich_myt_calendar_context() -> Dict[str, Any]:
    """
    Membina konteks waktu lengkap (minit, jam, hari, minggu ke-berapa, bulan, tahun,
    suasana pagi/petang/malam, serta hari istimewa atau musim perayaan semasa).
    """
    now = datetime.now(tz=MYT)
    days_bm = ["Isnin", "Selasa", "Rabu", "Khamis", "Jumaat", "Sabtu", "Ahad"]
    months_bm = [
        "Januari", "Februari", "Mac", "April", "Mei", "Jun",
        "Julai", "Ogos", "September", "Oktober", "November", "Disember",
    ]

    day_name = days_bm[now.weekday()]
    month_name = months_bm[now.month - 1]
    week_of_month = ((now.day - 1) // 7) + 1
    iso_week = now.isocalendar()[1]
    is_weekend = now.weekday() >= 5 or (now.weekday() == 4 and now.hour >= 17)

    hour = now.hour
    minute = now.minute

    if 5 <= hour < 12:
        period = "Pagi"
        forbidden_times = "petang ni, malam ni, tengah malam"
        time_vibe = (
            "Suasana pagi yang segar sebelum memulakan tugasan harian; sesuai berkongsi info teknikal ringkas, "
            "fakta sejarah PS2, atau tip penjagaan konsol yang mencerdaskan minda."
        )
    elif 12 <= hour < 15:
        period = "Tengah Hari"
        forbidden_times = "pagi yang cerah, malam ni, tengah malam"
        time_vibe = "Waktu rehat tengah hari; sesuai untuk bacaan ringan dan trivia pantas dunia retro gaming."
    elif 15 <= hour < 19:
        period = "Petang"
        forbidden_times = "pagi yang cerah, selamat pagi, pagi ini, tengah malam"
        time_vibe = "Suasana petang selepas habis waktu kerja atau sekolah; mengimbau kenangan lepak petang bermain PS2."
    else:
        period = "Malam"
        forbidden_times = "pagi yang cerah, selamat pagi, pagi ini, tengah hari"
        time_vibe = (
            "Suasana malam yang tenang untuk berehat di rumah; sangat kena dengan cerita nostalgia mendalam, "
            "pengalaman menghabiskan misi game malam-malam, dan menghidupkan semula PS2 lama."
        )

    # Semak hari istimewa tepat atau musim perayaan
    special_occasion = FIXED_SPECIAL_DATES_MY.get((now.month, now.day), "")
    if not special_occasion:
        cur_md = (now.month, now.day)
        for start_md, end_md, desc in SEASONAL_WINDOWS_2026:
            if start_md <= cur_md <= end_md:
                special_occasion = desc
                break

    if not special_occasion:
        if is_weekend:
            special_occasion = "Hujung Minggu — masa santai di rumah bersama keluarga atau mengemas koleksi konsol retro."
        else:
            special_occasion = f"Hari Bekerja Biasa (Minggu ke-{week_of_month} bulan {month_name}) — perkongsian ilmu dan nostalgia retro untuk menghilangkan penat kerja."

    full_timestamp = (
        f"{day_name}, {now.day} {month_name} {now.year} | "
        f"Jam {hour:02d}:{minute:02d} ({now.strftime('%I:%M %p')} MYT) | "
        f"Minggu ke-{week_of_month} ({month_name}) / Minggu ke-{iso_week} Tahun {now.year}"
    )

    return {
        "now_iso": now.isoformat(),
        "now_epoch": int(now.timestamp()),
        "day_name": day_name,
        "day_num": now.day,
        "month_name": month_name,
        "year": now.year,
        "hour": hour,
        "minute": minute,
        "week_of_month": week_of_month,
        "iso_week": iso_week,
        "period": period,
        "forbidden_times": forbidden_times,
        "time_vibe": time_vibe,
        "special_occasion": special_occasion,
        "full_timestamp": full_timestamp,
    }


# ==============================================================================
# 2. TEMA TOPIK BERGILIR & 18 VARIASI AYAT KOMEN PERTAMA SHOPEE
# ==============================================================================
PERSONA_TOPIC_PILLARS: List[Dict[str, str]] = [
    {
        "pillar_id": "GAME_NOSTALGIA",
        "label": "Kisah & Trivia Judul Game PS2",
        "preferred_compat": "UNIVERSAL_STORAGE",
        "prompt_angle": (
            "Ceritakan keunikan mekanik permainan, jalan cerita, atau kenangan pemain Malaysia terhadap judul game PS2 "
            "yang dipilih dari katalog, dan selitkan betapa senangnya zaman sekarang bila semua game klasik itu boleh "
            "dimainkan terus dari pendrive atau hardisk tanpa risau cakera tercalar."
        ),
    },
    {
        "pillar_id": "MATRIX_VS_NON_MATRIX",
        "label": "Ilmu Teknikal Modchip Matrix vs PS2 Standard",
        "preferred_compat": "MATRIX_ONLY",
        "prompt_angle": (
            "Kongsikan ilmu teknikal ringkas tetapi mudah faham tentang perbezaan konsol PS2 yang mempunyai cip Matrix "
            "(keluar logo Matrix masa dihidupkan dan ada tetapan MASS pada boot mode) berbanding PS2 standard, "
            "supaya pemilik PS2 lama tahu cara paling tepat untuk main game melalui USB."
        ),
    },
    {
        "pillar_id": "MODCARD_FMCB_OPENTUNA",
        "label": "Penyelamat PS2 Lens Rosak: FMCB, OpenTuna & Code Breaker",
        "preferred_compat": "NON_MATRIX_OPL_SET",
        "prompt_angle": (
            "Terangkan secara bijak bagaimana konsol PS2 lama yang lens atau lasernya sudah lemah tidak perlu dibuang "
            "atau dibedah perkakasannya; cukup menggunakan Mod Card (FMCB untuk PS2 Fat atau OpenTuna/Fortuna untuk PS2 Slim) "
            "bersama pendrive untuk melancarkan sistem OPL dengan selamat tanpa membatalkan keaslian konsol."
        ),
    },
    {
        "pillar_id": "MEMORY_CARD_CHEATS",
        "label": "Nostalgia Memory Card 8MB & Code Breaker",
        "preferred_compat": "MOD_CARD_ONLY",
        "prompt_angle": (
            "Imbau kembali debaran menjaga fail simpanan dalam Memory Card 8MB dan keseronokan menggunakan Code Breaker "
            "pada era 2000-an, serta bagaimana kad memori sistem moden hari ini sudah siap terbina fungsi OPL dan Code Breaker "
            "untuk menghidupkan semula koleksi PS2 di rumah."
        ),
    },
    {
        "pillar_id": "HARDWARE_EMOTION_ENGINE",
        "label": "Kehebatan Perkakasan PS2 & Koleksi Retro",
        "preferred_compat": "UNIVERSAL_STORAGE",
        "prompt_angle": (
            "Ulas dari sudut pandang peminat retro berusia 35 tahun tentang mengapa konsol PlayStation 2 kekal menjadi "
            "konsol terlaris sepanjang zaman dengan seni bina Emotion Engine yang kukuh, serta bagaimana penggunaan storan USB "
            "memanjangkan jangka hayat konsol legenda ini."
        ),
    },
]

# 18 Variasi Komen Pertama (antara 10-20 variasi) lengkap dengan emoji & ajakan tekan link
COMMENT_VARIATIONS_18: List[str] = [
    "🎮 Rindu nak layan semula game klasik PS2 kat rumah tanpa pening kepala CD calar? Tekan link Shopee di bawah untuk tengok pilihan siap isi game:\n👉 {url}",
    "🕹️ Untuk geng yang masih simpan konsol PS2 lama dalam almari, jom hidupkan balik nostalgia zaman sekolah! Klik link ni untuk semak kesesuaian konsol korang:\n👉 {url}",
    "💾 Konsol PS2 kat rumah lens dah tak baca disk? Jangan simpan dalam stor dulu, tekan link kat bawah ni untuk tengok penyelesaian Plug & Play:\n👉 {url}",
    "🔥 Nak pilih sendiri senarai game PS2 kegemaran zaman dulu terus masuk pendrive atau hardisk? Boleh terus klik link Shopee kat sini geng:\n👉 {url}",
    "⚡ Senang kerja zaman sekarang, cucuk je terus boleh layan game klasik PS2 dengan lancar! Tekan pautan di bawah untuk tengok butiran penuh:\n👉 {url}",
    "🎯 Khas untuk peminat retro PS2 di Malaysia! Kalau nak tengok pakej pendrive, hardisk atau mod card yang padan dengan model PS2 korang, klik sini:\n👉 {url}",
    "📀 Dah penat tiup disk PS2 tapi masih sangkut kat skrin loading? Jom beralih ke sistem USB yang lebih stabil, tekan link di bawah sekarang:\n👉 {url}",
    "🏆 Ramai geng 30-an dah hidupkan balik PS2 lama masing-masing kat rumah! Nak tahu macam mana nak mula? Tekan link Shopee kat bawah ni:\n👉 {url}",
    "✨ Satu-satunya cara paling selamat untuk main game PS2 tanpa perlu ubah suai dalaman konsol. Klik pautan di bawah untuk semak pilihan kapasiti:\n👉 {url}",
    "🚀 Nak marathon game klasik PS2 hujung minggu ni dengan member atau anak-anak? Tekan link kat bawah untuk pilih koleksi game korang:\n👉 {url}",
    "🛡️ Pastikan korang pilih versi yang ngam dengan model PS2 Fat atau Slim kat rumah. Tekan link Shopee kat bawah untuk baca info lengkap:\n👉 {url}",
    "🎲 Dari game aksi sampai game bola legend zaman 2000-an, semua boleh susun ikut citarasa sendiri! Klik pautan di bawah untuk tengok katalog:\n👉 {url}",
    "💡 Tip mudah dari min: guna storan USB siap sistem OPL supaya laser PS2 tak terbeban. Tekan link di bawah untuk tengok set yang tersedia:\n👉 {url}",
    "🎉 Mengimbau kenangan zaman lepak kedai game dulu memang tak pernah jemu! Kalau nak rasa semula suasana tu kat rumah, klik link kat sini:\n👉 {url}",
    "🔧 Tak kira PS2 jenis Matrix atau standard, ada penyelesaian mudah untuk terus main dari USB. Tekan link Shopee di bawah untuk semak:\n👉 {url}",
    "📲 Boleh terus pilih tajuk game pilihan korang lepas buat pesanan! Tekan pautan rasmi di bawah untuk lihat senarai penuh:\n👉 {url}",
    "🌟 Hidupkan semula bunyi boot ikonik PS2 kat TV ruang tamu malam ni! Klik link di bawah untuk tengok kelengkapan PS2 yang siap sedia:\n👉 {url}",
    "🏁 Jangan biarkan konsol legenda PS2 tersadai berhabuk! Tekan link kat bawah ni untuk tengok pakej plug and play yang sesuai untuk korang:\n👉 {url}",
]


# ==============================================================================
# 3. PENGURUS DATA: KATALOG GAME PS2, PRODUK SHOPEE & MEMORI 10 POST TERAKHIR
# ==============================================================================
class PS2DataCoordinator:
    """
    Menyelaraskan bacaan & kemas kini daripada:
    - /data/ps2_shopee_products.db (10 produk Shopee sebenar)
    - /data/ps2_games_catalog.db (senarai judul game PS2)
    - /data/ps2_text_posts_history.db + Upstash Redis (X04) + Backblaze B2 (X03)
    """

    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        self._init_history_db()

    def _init_history_db(self) -> None:
        conn = sqlite3.connect(str(TEXT_HISTORY_DB_PATH))
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS text_post_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    posted_at_iso TEXT NOT NULL,
                    posted_epoch INTEGER NOT NULL,
                    run_env TEXT NOT NULL,
                    persona_ref TEXT NOT NULL,
                    pillar_id TEXT NOT NULL,
                    game_title TEXT NOT NULL,
                    story_bm TEXT NOT NULL,
                    post_caption TEXT NOT NULL,
                    story_hash TEXT NOT NULL,
                    shopee_product_code TEXT NOT NULL,
                    shopee_url TEXT NOT NULL,
                    comment_variant_idx INTEGER NOT NULL,
                    comment_text TEXT NOT NULL,
                    fb_post_id TEXT DEFAULT '',
                    fb_comment_id TEXT DEFAULT '',
                    vector_shard INTEGER DEFAULT 0
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tph_epoch ON text_post_history(posted_epoch DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tph_hash ON text_post_history(story_hash)")
            conn.commit()
        finally:
            conn.close()

    def get_recent_posts(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Mengambil sehingga 10 posting terakhir gabungan daripada Upstash Redis
        Rolling History (X04) dan SQLite tempatan supaya WSL & GitHub Actions segerak.
        """
        records: List[Dict[str, Any]] = []
        seen_hashes = set()

        # 1. Baca dari Upstash Redis Rolling History (X04: aliran 'text' & 'global')
        for stream_name in ("text", "global"):
            try:
                redis_items = redis_sharded.get_rolling_history(stream=stream_name, limit=limit)
                for obj in redis_items:
                    if isinstance(obj, dict):
                        h = obj.get("story_hash", "")
                        if h and h not in seen_hashes:
                            seen_hashes.add(h)
                            records.append(obj)
            except Exception:
                pass

        # 2. Gabung dengan rekod tempatan SQLite
        if TEXT_HISTORY_DB_PATH.exists():
            conn = sqlite3.connect(str(TEXT_HISTORY_DB_PATH))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    SELECT posted_at_iso, posted_epoch, persona_ref, pillar_id,
                           game_title, story_bm, story_hash, shopee_product_code,
                           shopee_url, comment_variant_idx
                    FROM text_post_history
                    ORDER BY posted_epoch DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                for r in rows:
                    d = dict(r)
                    h = d.get("story_hash", "")
                    if h and h not in seen_hashes:
                        seen_hashes.add(h)
                        records.append(d)
            finally:
                conn.close()

        records.sort(key=lambda x: int(x.get("posted_epoch", 0)), reverse=True)
        return records[:limit]

    def pick_game_from_catalog(self, recent_posts: List[Dict[str, Any]], forced_title: str = "") -> Dict[str, Any]:
        """Memilih tajuk game daripada data/ps2_games_catalog.db yang tidak bertindih dengan post terkini."""
        recent_games = {str(p.get("game_title", "")).strip().lower() for p in recent_posts[:6]}

        if not GAMES_DB_PATH.exists():
            return {
                "title": forced_title or "Grand Theft Auto: San Andreas",
                "genre": "Action-Adventure",
                "release_year": 2004,
                "developer": "Rockstar North",
                "trivia_hint": "Permainan dunia terbuka ikonik PS2 dengan kod cheat bertulis atas kertas buku latihan.",
            }

        conn = sqlite3.connect(str(GAMES_DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            if forced_title.strip():
                row = conn.execute(
                    "SELECT * FROM ps2_games WHERE LOWER(title) LIKE ? LIMIT 1",
                    (f"%{forced_title.strip().lower()}%",),
                ).fetchone()
                if row:
                    return dict(row)
                return {
                    "title": forced_title.strip(),
                    "genre": "Retro PS2",
                    "release_year": 2004,
                    "developer": "",
                    "trivia_hint": "Kenangan klasik permainan PlayStation 2 era 2000-an.",
                }

            rows = conn.execute(
                """
                SELECT * FROM ps2_games
                ORDER BY times_posted ASC, COALESCE(last_posted_at, '1970-01-01') ASC
                LIMIT 15
                """
            ).fetchall()

            candidates = [dict(r) for r in rows if str(r["title"]).strip().lower() not in recent_games]
            if not candidates:
                candidates = [dict(r) for r in rows]

            if candidates:
                return random.choice(candidates[:8])
        finally:
            conn.close()

        return {
            "title": "Winning Eleven 8",
            "genre": "Sports / Football",
            "release_year": 2004,
            "developer": "Konami",
            "trivia_hint": "Permainan bola sepak paling meriah bila berkumpul dengan kawan-kawan zaman dulu.",
        }

    def pick_rotating_shopee_product(
        self,
        preferred_compat: str,
        recent_posts: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Memilih 1 produk daripada data/ps2_shopee_products.db dengan syarat WAJIB:
        - Link produk mesti BERBEZA daripada post terakhir (dan sebolehnya 4 post terakhir).
        - Menggunakan penggiliran adil Upstash Redis (X04) + SQLite supaya semua
          10 link Shopee bergilir sekata walaupun dijalankan di GitHub Actions.
        """
        recent_codes = [str(p.get("shopee_product_code", "")) for p in recent_posts[:5] if p.get("shopee_product_code")]
        recent_urls = [str(p.get("shopee_url", "")) for p in recent_posts[:5] if p.get("shopee_url")]
        last_code = recent_codes[0] if recent_codes else ""
        last_url = recent_urls[0] if recent_urls else ""

        if not SHOPEE_DB_PATH.exists():
            raise FileNotFoundError(
                f"Fail katalog Shopee tidak ditemui: {SHOPEE_DB_PATH}. Sila jalankan exp08_import_shopee_links_to_db.py dahulu."
            )

        conn = sqlite3.connect(str(SHOPEE_DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            all_rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM shopee_products WHERE is_active = 1 ORDER BY times_promoted ASC, id ASC"
                ).fetchall()
            ]
        finally:
            conn.close()

        if not all_rows:
            raise RuntimeError("Tiada produk aktif dalam ps2_shopee_products.db.")

        # 1. Keluarkan produk yang digunakan pada post terakhir supaya link SENTIASA berbeza setiap post
        non_repeat_pool = [
            r for r in all_rows
            if r["product_code"] != last_code and r["shopee_url"] != last_url
        ]
        if not non_repeat_pool:
            non_repeat_pool = all_rows

        # 2. Cuba elak 4 produk terakhir jika masih ada pilihan lain
        fresh_pool = [
            r for r in non_repeat_pool
            if r["product_code"] not in recent_codes[:4] and r["shopee_url"] not in recent_urls[:4]
        ]
        working_pool = fresh_pool if fresh_pool else non_repeat_pool

        # 3. Padankan kategori keserasian cerita jika ada dalam working_pool
        compat_matched = [r for r in working_pool if r["compatibility"] == preferred_compat]
        final_pool = compat_matched if compat_matched else working_pool

        # 4. Gunakan Redis Least-Used Rotator (X04) untuk pilih yang paling jarang dipromosikan
        by_code = {r["product_code"]: r for r in final_pool}
        picked_code = redis_sharded.pick_least_used_item(
            pool_name="x11_shopee_products",
            candidate_ids=list(by_code.keys()),
            exclude_ids=recent_codes[:4],
        )
        if picked_code and picked_code in by_code:
            return by_code[picked_code]

        return random.choice(final_pool[:2]) if len(final_pool) >= 2 else final_pool[0]

    def build_rotating_first_comment(
        self,
        product: Dict[str, Any],
        recent_posts: List[Dict[str, Any]],
    ) -> Tuple[str, int]:
        """
        Membina ayat komen pertama menggunakan kitaran 18 variasi berbeza (dengan emoji
        dan ajakan tekan link) supaya ayat komen sentiasa segar dan tidak berulang.
        """
        used_indices = [
            int(p["comment_variant_idx"])
            for p in recent_posts
            if p.get("comment_variant_idx") is not None
        ]

        total_vars = len(COMMENT_VARIATIONS_18)
        candidate_ids = [f"var_{i:02d}" for i in range(total_vars)]
        exclude_ids = [f"var_{i:02d}" for i in used_indices[:10]]

        picked_var = redis_sharded.pick_least_used_item(
            pool_name="x11_comment_variants",
            candidate_ids=candidate_ids,
            exclude_ids=exclude_ids,
        )
        if picked_var and picked_var.startswith("var_"):
            next_idx = int(picked_var.split("_")[1])
        elif used_indices:
            next_idx = (used_indices[0] + 1) % total_vars
            for offset in range(total_vars):
                cand = (next_idx + offset) % total_vars
                if cand not in used_indices[:10]:
                    next_idx = cand
                    break
        else:
            next_idx = random.randint(0, total_vars - 1)

        template = COMMENT_VARIATIONS_18[next_idx]
        url = str(product["shopee_url"]).strip()
        compat = str(product.get("compatibility", ""))
        tier = str(product.get("storage_tier", "")).replace("_", " ")
        hint = str(product.get("game_count_hint", ""))

        # Tambah 1 baris nota keserasian ringkas supaya pembeli jelas & yakin klik link
        if compat == "MATRIX_ONLY":
            spec_note = f"📌 Khas PS2 Matrix ({tier} | {hint})"
        elif compat == "NON_MATRIX_OPL_SET":
            spec_note = f"📌 Set Lengkap Mod Card FMCB/OpenTuna + {tier} ({hint})"
        elif compat == "MOD_CARD_ONLY":
            spec_note = f"📌 Mod Card Sistem OPL + Code Breaker (PS2 Fat & Slim)"
        else:
            spec_note = f"📌 Pilihan Kapasiti ({hint}) — Boleh pilih game sendiri!"

        comment_body = f"{template.format(url=url)}\n\n{spec_note}"
        return comment_body, next_idx

    def record_published_post(
        self,
        record: Dict[str, Any],
        vec_story_id: str,
        b2_archive: bool = True,
    ) -> None:
        """
        Menyimpan rekod hantaran ke:
        1. SQLite tempatan (text_post_history, shopee_products, ps2_games)
        2. Upstash Vector (X05) dengan metadata timestamp untuk jarak 60 hari / 5 hari
        3. Upstash Redis (X04) untuk ingatan pantas 10 post terakhir + giliran produk
        4. Backblaze B2 (X03) sebagai arkib JSON kekal (pilihan selamat)
        """
        # 1. Simpan ke SQLite History
        conn = sqlite3.connect(str(TEXT_HISTORY_DB_PATH))
        try:
            conn.execute(
                """
                INSERT INTO text_post_history (
                    posted_at_iso, posted_epoch, run_env, persona_ref, pillar_id,
                    game_title, story_bm, post_caption, story_hash, shopee_product_code,
                    shopee_url, comment_variant_idx, comment_text, fb_post_id,
                    fb_comment_id, vector_shard
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["posted_at_iso"],
                    record["posted_epoch"],
                    record["run_env"],
                    record["persona_ref"],
                    record["pillar_id"],
                    record["game_title"],
                    record["story_bm"],
                    record["post_caption"],
                    record["story_hash"],
                    record["shopee_product_code"],
                    record["shopee_url"],
                    record["comment_variant_idx"],
                    record["comment_text"],
                    record["fb_post_id"],
                    record["fb_comment_id"],
                    record["vector_shard"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

        # Kemas kini kaunter promosi produk Shopee dalam ps2_shopee_products.db
        if SHOPEE_DB_PATH.exists():
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

        # Kemas kini kaunter game dalam ps2_games_catalog.db
        if GAMES_DB_PATH.exists():
            g_conn = sqlite3.connect(str(GAMES_DB_PATH))
            try:
                g_conn.execute(
                    """
                    UPDATE ps2_games
                    SET times_posted = times_posted + 1, last_posted_at = ?
                    WHERE LOWER(title) = ?
                    """,
                    (record["posted_at_iso"], record["game_title"].strip().lower()),
                )
                g_conn.commit()
            finally:
                g_conn.close()

        # 2. Simpan ke Upstash Vector (X05)
        try:
            vec_ok, vec_idx = vector_sharded.upsert_story_vector(
                post_uid=vec_story_id,
                game_title=record["game_title"],
                story_text=record["story_bm"],
                extra_meta={
                    "posted_at_iso": record["posted_at_iso"],
                    "posted_epoch": record["posted_epoch"],
                    "story_hash": record["story_hash"],
                    "game_title": record["game_title"],
                    "pillar_id": record["pillar_id"],
                    "shopee_product_code": record["shopee_product_code"],
                },
            )
            if not vec_ok:
                console.print(f"[yellow]⚠️ Amaran simpan Vector X05 pada Shard #{vec_idx:03d}.[/yellow]")
        except Exception as exc:
            console.print(f"[yellow]⚠️ Amaran simpan Vector X05: {exc}[/yellow]")

        # 3. Simpan ke Upstash Redis (X04: Metadata Post + Rolling History 10 Post + Rotasi)
        try:
            redis_sharded.save_published_post(
                post_uid=vec_story_id,
                post_meta={
                    "published_at": record["posted_epoch"],
                    "posted_at_iso": record["posted_at_iso"],
                    "fb_post_id": record["fb_post_id"],
                    "fb_comment_id": record["fb_comment_id"],
                    "game_title": record["game_title"],
                    "pillar_id": record["pillar_id"],
                    "shopee_product_code": record["shopee_product_code"],
                    "shopee_url": record["shopee_url"],
                },
                dedup_fingerprint=record["story_hash"],
                dedup_ttl_seconds=EXACT_COOLDOWN_DAYS * 86400,
            )

            redis_sharded.push_rolling_history(
                record={
                    "post_uid": vec_story_id,
                    "posted_at_iso": record["posted_at_iso"],
                    "posted_epoch": record["posted_epoch"],
                    "persona_ref": record["persona_ref"],
                    "pillar_id": record["pillar_id"],
                    "game_title": record["game_title"],
                    "story_bm": record["story_bm"],
                    "story_hash": record["story_hash"],
                    "shopee_product_code": record["shopee_product_code"],
                    "shopee_url": record["shopee_url"],
                    "comment_variant_idx": record["comment_variant_idx"],
                },
                stream="text",
                max_items=20,
                also_push_global=True,
            )

            redis_sharded.increment_rotation_score("x11_shopee_products", record["shopee_product_code"])
            redis_sharded.increment_rotation_score("x11_comment_variants", f"var_{record['comment_variant_idx']:02d}")
            redis_sharded.increment_rotation_score("x11_topic_pillars", record["pillar_id"])
        except Exception as exc:
            console.print(f"[yellow]⚠️ Amaran simpan Redis X04: {exc}[/yellow]")

        # 4. Arkib JSON ke Backblaze B2 melalui Cloudflare Worker Proxy (X03)
        if b2_archive and getattr(cfg, "CF_WORKER_B2_PROXY_STORAGE", ""):
            try:
                month_tag = datetime.now(tz=MYT).strftime("%Y%m")
                tmp_log = TEMP_DIR / f"x11_post_{record['posted_epoch']}.json"
                tmp_log.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

                sig = inspect.signature(b2_proxy.upload_file)
                params = list(sig.parameters.keys())
                remote_key = f"logs/text_posts/{month_tag}/{tmp_log.name}"
                if "local_path" in params and "remote_path" in params:
                    b2_proxy.upload_file(local_path=str(tmp_log), remote_path=remote_key)
                elif "file_path" in params and "remote_path" in params:
                    b2_proxy.upload_file(file_path=str(tmp_log), remote_path=remote_key)
                else:
                    b2_proxy.upload_file(str(tmp_log), remote_key)
                tmp_log.unlink(missing_ok=True)
            except Exception:
                pass


# ==============================================================================
# 4. PENAPIS ANTI-DUPLIKASI SEMANTIK (60 HARI EXACT / 5 HARI 75%-80% SIMILAR)
# ==============================================================================
def check_semantic_cooldown(
    candidate_story: str,
    game_title: str,
    recent_posts: List[Dict[str, Any]],
    now_epoch: int,
) -> Tuple[bool, str, int]:
    """
    Menguatkuasakan dasar penapisan semantik:
    1. Ayat 100% / hampir sama (skor >= 0.95 atau hash sama) -> jarak minimum 60 hari (2 bulan).
    2. Ayat 75% - 80% sama (skor >= 0.75) -> jarak minimum 5 hari.
    Memeriksa rekod tempatan/Redis dahulu, kemudian menyemak Upstash Vector (X05).
    Mengembalikan (lulus_unik, sebab_keputusan, indeks_shard_vector).
    """
    cand_norm = re.sub(r"\s+", " ", candidate_story.strip().lower())
    cand_hash = hashlib.sha256(cand_norm.encode("utf-8")).hexdigest()
    cand_tokens = set(re.findall(r"\b[a-z0-9]{3,}\b", cand_norm))

    # A. Semakan pantas terhadap sejarah terkini (60 hari & 5 hari)
    for prev in recent_posts:
        prev_story = re.sub(r"\s+", " ", str(prev.get("story_bm", "")).strip().lower())
        if not prev_story:
            continue
        prev_hash = str(prev.get("story_hash") or hashlib.sha256(prev_story.encode("utf-8")).hexdigest())
        prev_epoch = int(prev.get("posted_epoch", 0))
        age_days = max(0.0, (now_epoch - prev_epoch) / 86400.0) if prev_epoch > 0 else 0.0

        if cand_hash == prev_hash and age_days < EXACT_COOLDOWN_DAYS:
            return False, f"Ditolak: Ayat 100% sama pernah dipost {age_days:.1f} hari lalu (had 60 hari).", 0

        prev_tokens = set(re.findall(r"\b[a-z0-9]{3,}\b", prev_story))
        if cand_tokens and prev_tokens:
            jaccard = len(cand_tokens & prev_tokens) / max(1, len(cand_tokens | prev_tokens))
            if jaccard >= 0.75 and age_days < SIMILAR_COOLDOWN_DAYS:
                return False, f"Ditolak: Pertindihan kata {jaccard*100:.0f}% dalam tempoh {age_days:.1f} hari (had 5 hari).", 0

    # B. Semakan Semantik Upstash Vector (X05 dengan Cooldown 60 Hari / 5 Hari)
    try:
        is_dup, dup_match, vec_shard = vector_sharded.check_semantic_duplicate(
            game_title=game_title,
            candidate_story=candidate_story,
            similarity_threshold=SIMILAR_MATCH_THRESHOLD,
            exact_threshold=EXACT_MATCH_THRESHOLD,
            exact_cooldown_days=EXACT_COOLDOWN_DAYS,
            similar_cooldown_days=SIMILAR_COOLDOWN_DAYS,
            now_epoch=now_epoch,
        )
        if is_dup and isinstance(dup_match, dict):
            reason = dup_match.get("block_reason") or (
                f"Persamaan {float(dup_match.get('score', 0.0))*100:.1f}% dalam tempoh sekatan."
            )
            return False, f"Ditolak Vector (#{vec_shard}): {reason}", vec_shard

        return True, f"Lulus Semantik Vector (Shard #{vec_shard})", vec_shard
    except Exception as exc:
        return True, f"Lulus (Amaran Vector: {exc})", 0


# ==============================================================================
# 5. PENJANA PERSONA AI (LELAKI 35 TAHUN TERPELAJAR: ADMIN / MIN)
# ==============================================================================
def enforce_clean_text_only_rules(text: str, self_ref: str) -> str:
    """
    Memastikan teks bersih 100%:
    - Bahasakan diri 'admin' atau 'min' sahaja (tiada 'aku'/'saya').
    - Tiada kebocoran metadata masa atau tajuk pada baris pertama.
    - Tiada emoji atau simbol pelik.
    """
    text = re.sub(r"^(cikgu|tuan|puan|bos|masa siaran[^\n]*)[,\s:!]+", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\baku\b", self_ref, text, flags=re.IGNORECASE)
    text = re.sub(r"\bsaya\b", self_ref, text, flags=re.IGNORECASE)
    text = re.sub(rf"(^|[.!?]\s+){self_ref}\b", lambda m: f"{m.group(1)}{self_ref.capitalize()}", text)

    # Buang sebarang aksara bukan ASCII standard (tiada emoji / simbol pelik)
    text = re.sub(r"[^\x20-\x7E\n]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def validate_strict_text_only_post(
    story_bm: str,
    full_caption: str,
    cal_ctx: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """Menyemak had panjang 500-650 aksara, keselarasan waktu, dan larangan emoji/simbol."""
    ok_base, msg_base = validate_story_quality(story_bm, min_len=STORY_MIN_CHARS, max_len=STORY_MAX_CHARS)
    if not ok_base:
        return False, msg_base

    # Larangan emoji & simbol pelik pada keseluruhan kapsyen hantaran utama
    strict_ascii_re = re.compile(r"^[a-zA-Z0-9\s.,!?'\"\-\(\)/%:;&#]+$")
    if not strict_ascii_re.match(full_caption):
        return False, "Dikesan emoji atau simbol pelik dalam kapsyen utama."

    if "http://" in full_caption.lower() or "https://" in full_caption.lower() or "shopee" in full_caption.lower():
        return False, "Dikesan pautan atau sebutan Shopee terus dalam hantaran utama."

    # Semak supaya AI tidak menyebut sapaan waktu yang bercanggah dengan waktu MYT semasa
    if cal_ctx:
        low_story = story_bm.lower()
        period = cal_ctx.get("period", "")
        if period in ("Petang", "Malam") and re.search(r"\b(selamat\s+pagi|pagi\s+yang|pagi\s+ini|pagi\s+ni)\b", low_story):
            return False, f"Konteks waktu bercanggah: menyebut 'pagi' ketika Waktu {period}."
        if period == "Pagi" and re.search(r"\b(selamat\s+malam|malam\s+ini|malam\s+ni)\b", low_story):
            return False, "Konteks waktu bercanggah: menyebut 'malam ni' ketika Waktu Pagi."

    return True, "Lulus"


def generate_admin_text_only_post(
    cal_ctx: Dict[str, Any],
    pillar: Dict[str, str],
    game_rec: Dict[str, Any],
    product_rec: Dict[str, Any],
    recent_posts: List[Dict[str, Any]],
) -> Tuple[bool, Dict[str, Any]]:
    """
    Menjana hantaran Facebook tanpa gambar (500-650 aksara) dengan persona admin
    berusia 35 tahun yang terpelajar, berpengetahuan luas tentang PS2, serta
    menyediakan 10 posting terakhir kepada AI sebagai konteks anti-ulangan.
    """
    self_ref = random.choice(["admin", "min"])
    game_title = game_rec["title"]
    game_year = game_rec.get("release_year", 2004)
    game_genre = game_rec.get("genre", "Retro PS2")
    game_dev = game_rec.get("developer", "")
    game_hint = game_rec.get("trivia_hint", "")

    # Susun senarai 10 posting terakhir untuk dibaca AI sebelum menulis
    if recent_posts:
        recent_lines = []
        for idx, rp in enumerate(recent_posts[:10], 1):
            snippet = str(rp.get("story_bm", ""))[:135].replace("\n", " ")
            recent_lines.append(
                f"{idx}. [Game: {rp.get('game_title', '-')}] {snippet}..."
            )
        recent_10_block = "\n".join(recent_lines)
    else:
        recent_10_block = "Belum ada rekod hantaran terdahulu."

    system_prompt = (
        f"Kau ialah '{self_ref}', seorang lelaki Malaysia berumur 35 tahun yang terpelajar, matang, "
        "dan menjadi admin halaman Facebook 'PS2 Retro Nostalgia'. Kau sangat berpengetahuan tinggi tentang "
        "sejarah konsol PlayStation 2, seni bina perkakasan PS2 Fat dan Slim, perbezaan modchip Matrix berbanding "
        "PS2 standard, penggunaan Mod Card (FMCB, OpenTuna, Fortuna, Code Breaker), serta cara bermain game PS2 "
        "melalui pendrive dan hardisk OPL.\n\n"
        "GAYA PENCERITAAN & STRATEGI PROMOSI HALUS:\n"
        f"1. Bahasakan diri HANYA sebagai '{self_ref}' (DILARANG guna 'aku' atau 'saya') dan sapa pembaca sebagai 'korang' atau 'geng'.\n"
        "2. Tulis gaya perkongsian ilmu, pengalaman sebenar era 2000-an, atau info berguna yang membuatkan pembaca rasa dekat dan teruja.\n"
        "3. Selitkan secara halus dan cerdik (soft-sell) kelebihan menghidupkan semula konsol PS2 lama menggunakan "
        "pendrive, hardisk, atau mod card tanpa perlu risau lens rosak atau CD calar, supaya pembaca berminat menyemak ruangan komen.\n"
        f"4. Sekarang ialah WAKTU {cal_ctx['period'].upper()}. Sesuaikan nada dengan suasana {cal_ctx['period'].lower()} dan DILARANG menyebut waktu bercanggah ({cal_ctx['forbidden_times']}).\n\n"
        "KAWALAN KETAT:\n"
        "- Panjang cerita MESTI antara 510 hingga 625 aksara sahaja.\n"
        "- DILARANG menulis tajuk atau header pada baris pertama.\n"
        "- DILARANG meletakkan sebarang emoji, simbol pelik, harga RM, atau pautan URL dalam cerita utama.\n"
        "- DILARANG guna perkataan Indonesia (bisa, banget, nggak, ngak, bikin, gampang, cobain, cocok, yuk, kalian, anda).\n"
        "- Pastikan ayat berakhir dengan tanda noktah (.) yang lengkap."
    )

    user_prompt = (
        f"KONTEKS KALENDAR & MASA MALAYSIA (MYT):\n"
        f"- Waktu Sekarang: {cal_ctx['full_timestamp']} (WAKTU {cal_ctx['period'].upper()})\n"
        f"- Panduan Suasana Waktu: {cal_ctx['time_vibe']}\n"
        f"- Konteks Hari / Perayaan: {cal_ctx['special_occasion']}\n\n"
        f"FOKUS TOPIK HARI INI ({pillar['label']}):\n"
        f"- Arahan Sudut Cerita: {pillar['prompt_angle']}\n"
        f"- Rujukan Judul Game PS2: {game_title} ({game_year}, {game_genre}"
        f"{f', {game_dev}' if game_dev else ''}) - {game_hint}\n"
        f"- Konteks Produk Komen Pertama (untuk selitan promosi halus sahaja, JANGAN sebut link/harga): "
        f"{product_rec['product_name']} ({product_rec['game_count_hint']})\n\n"
        f"10 POSTING TERAKHIR DIJANA AI (WAJIB ELAK PEMBUKA AYAT & SUDUT CERITA YANG SAMA):\n"
        f"{recent_10_block}\n\n"
        f"Tulis 1 hingga 2 perenggan cerita daripada sudut pandang '{self_ref}' (lelaki 35 tahun terpelajar) "
        f"pada suasana {cal_ctx['period'].lower()} ini sepanjang 510 hingga 625 aksara tanpa sebarang emoji:"
    )

    last_meta: Dict[str, Any] = {}
    for attempt in range(1, 5):
        adj_temp = min(0.92, round(STORY_TEMPERATURE + ((attempt - 1) * 0.04), 2))
        inf = llm_engine.generate_chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=adj_temp,
            max_tokens=STORY_MAX_TOKENS,
        )

        cleaned = clean_and_scrub_bm_story(inf["raw_text"], max_chars=STORY_SOFT_TRIM)
        cleaned = enforce_clean_text_only_rules(cleaned, self_ref=self_ref)

        slug_tag = re.sub(r"[^a-zA-Z0-9]", "", game_title)[:22]
        hashtags = f"#PS2RetroNostalgia #PS2Malaysia #RetroGaming #{slug_tag} #Nostalgia2000an"
        # Kekalkan kapsyen utama tanpa emoji & tanpa simbol pelik
        post_caption = f"{cleaned}\n\n{hashtags}"

        ok_format, fmt_msg = validate_strict_text_only_post(cleaned, post_caption, cal_ctx=cal_ctx)
        if not ok_format:
            last_meta = {
                "attempt": attempt,
                "elapsed_sec": inf["elapsed_sec"],
                "tok_per_sec": inf["tok_per_sec"],
                "story_chars": len(cleaned),
                "status": fmt_msg,
            }
            continue

        ok_sem, sem_msg, vec_shard = check_semantic_cooldown(
            candidate_story=cleaned,
            game_title=game_title,
            recent_posts=recent_posts,
            now_epoch=cal_ctx["now_epoch"],
        )
        last_meta = {
            "attempt": attempt,
            "elapsed_sec": inf["elapsed_sec"],
            "completion_tokens": inf["completion_tokens"],
            "tok_per_sec": inf["tok_per_sec"],
            "story_chars": len(cleaned),
            "status": sem_msg,
            "vector_shard": vec_shard,
        }

        if ok_sem:
            story_hash = hashlib.sha256(cleaned.lower().encode("utf-8")).hexdigest()
            return True, {
                "persona_ref": self_ref,
                "story_bm": cleaned,
                "story_chars": len(cleaned),
                "post_caption": post_caption,
                "caption_chars": len(post_caption),
                "story_hash": story_hash,
                "vector_shard": vec_shard,
                "metrics": last_meta,
            }

    return False, {"error": "Gagal menjana cerita yang melepasi semakan panjang/semantik.", "metrics": last_meta}


# ==============================================================================
# 6. PENERBIT FACEBOOK GRAPH API v26.0 (STATUS TEKS SAHAJA + KOMEN PERTAMA)
# ==============================================================================
class FacebookTextOnlyPublisher:
    """
    Menerbitkan hantaran status teks sahaja ke /{page_id}/feed melalui Meta Graph API
    v26.0 dan terus menghantar komen pertama dengan pautan produk Shopee.
    """

    def __init__(self) -> None:
        self.app_secret: str = getattr(cfg, "PS2N_FB_APP_SECRET", "") or os.getenv("PS2N_FB_APP_SECRET", "")
        self.page_id: str = getattr(cfg, "PS2N_FB_PAGE_ID", "") or os.getenv("PS2N_FB_PAGE_ID", "")
        self.page_token: str = getattr(cfg, "PS2N_FB_PAGE_TOKEN", "") or os.getenv("PS2N_FB_PAGE_TOKEN", "")
        self.graph_ver: str = (
            getattr(cfg, "PS2N_FB_GRAPH_VERSION", "")
            or os.getenv("PS2N_FB_GRAPH_VERSION", "")
            or "v26.0"
        )
        self.base_url: str = f"https://graph.facebook.com/{self.graph_ver}"

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

    def publish_text_post_and_comment(self, post_caption: str, comment_text: str) -> Dict[str, str]:
        feed_url = f"{self.base_url}/{self.page_id}/feed"
        payload = self._auth_params()
        payload["message"] = post_caption

        resp = requests.post(feed_url, data=payload, timeout=35)
        data = resp.json()
        if resp.status_code != 200 or "error" in data:
            raise RuntimeError(f"Gagal menerbitkan status teks FB: {json.dumps(data)}")

        post_id = str(data.get("id", ""))
        if not post_id:
            raise RuntimeError("Meta Graph API tidak memulangkan post_id.")

        # Tunggu sebentar supaya FB selesai mengindeks hantaran sebelum komen pertama dihantar
        time.sleep(2.5)
        comment_url = f"{self.base_url}/{post_id}/comments"
        c_payload = self._auth_params()
        c_payload["message"] = comment_text

        c_resp = requests.post(comment_url, data=c_payload, timeout=30)
        c_data = c_resp.json()
        if c_resp.status_code != 200 or "error" in c_data:
            raise RuntimeError(f"Status berjaya ({post_id}) tetapi gagal hantar komen: {json.dumps(c_data)}")

        return {"post_id": post_id, "comment_id": str(c_data.get("id", ""))}


def main() -> None:
    parser = argparse.ArgumentParser(description="X11: Persona AI Text-Only Facebook Auto-Poster")
    parser.add_argument("--dry-run", action="store_true", help="Uji penjanaan tanpa menerbitkan ke Facebook sebenar.")
    parser.add_argument("--game", type=str, default="", help="Paksa tajuk game tertentu dari katalog PS2.")
    args = parser.parse_args()

    is_ci = os.getenv("GITHUB_ACTIONS", "").lower() == "true"
    run_env = "GITHUB_ACTIONS" if is_ci else "LOCAL_WSL"

    # Kunci pelaksanaan supaya larian cron dan manual tidak bertindih
    lock_token: Optional[str] = None
    if not args.dry_run:
        lock_token = redis_sharded.acquire_lock("x11_fb_text_post", ttl_seconds=360)
        if not lock_token:
            console.print("[bold yellow]⚠️ Proses X11 lain sedang berjalan. Menghentikan larian ini bagi mengelak post berganda.[/bold yellow]")
            return

    try:
        cal_ctx = get_rich_myt_calendar_context()
        coordinator = PS2DataCoordinator()
        recent_posts = coordinator.get_recent_posts(limit=10)

        console.print(
            Panel.fit(
                "[bold cyan]PS2-RETRO-NOSTALGIA :: PERSONA AI TEXT-ONLY FB PUBLISHER (X11)[/bold cyan]\n"
                f"[dim]Persekitaran: {run_env} | {cal_ctx['full_timestamp']}\n"
                f"Konteks Hari: {cal_ctx['special_occasion']}[/dim]",
                border_style="cyan",
            )
        )

        # 1. Pilih Pilar Topik (elak mengulang pilar post terakhir & imbangkan menerusi Redis)
        last_pillar = recent_posts[0].get("pillar_id", "") if recent_posts else ""
        avail_pillars = [p for p in PERSONA_TOPIC_PILLARS if p["pillar_id"] != last_pillar] or PERSONA_TOPIC_PILLARS
        pillar_by_id = {p["pillar_id"]: p for p in avail_pillars}
        picked_pillar_id = redis_sharded.pick_least_used_item(
            pool_name="x11_topic_pillars",
            candidate_ids=list(pillar_by_id.keys()),
            exclude_ids=[last_pillar] if last_pillar else [],
        )
        chosen_pillar = pillar_by_id.get(picked_pillar_id or "", random.choice(avail_pillars))

        # 2. Pilih Judul Game dari ps2_games_catalog.db & Produk Shopee berbeza dari ps2_shopee_products.db
        game_rec = coordinator.pick_game_from_catalog(recent_posts=recent_posts, forced_title=args.game)
        product_rec = coordinator.pick_rotating_shopee_product(
            preferred_compat=chosen_pillar["preferred_compat"],
            recent_posts=recent_posts,
        )
        comment_text, comment_var_idx = coordinator.build_rotating_first_comment(
            product=product_rec,
            recent_posts=recent_posts,
        )

        plan_tbl = Table(title="1. Pelan Kandungan & Giliran Produk Shopee", box=box.ROUNDED, header_style="bold yellow")
        plan_tbl.add_column("Komponen", style="bold white")
        plan_tbl.add_column("Pilihan Sesi Ini", style="cyan")
        plan_tbl.add_row("Pilar Topik", f"{chosen_pillar['label']} ({chosen_pillar['pillar_id']})")
        plan_tbl.add_row("Rujukan Game PS2", f"{game_rec['title']} ({game_rec.get('release_year', 2004)})")
        plan_tbl.add_row("Produk Shopee (Bergilir)", f"{product_rec['product_code']} | {product_rec['compatibility']} | {product_rec['storage_tier']}")
        plan_tbl.add_row("Pautan Shopee Komen", product_rec["shopee_url"])
        plan_tbl.add_row("Variasi Komen Ke-", f"#{comment_var_idx + 1} daripada {len(COMMENT_VARIATIONS_18)} templat")
        plan_tbl.add_row("Memori 10 Post Terakhir", f"{len(recent_posts)} rekod dimuatkan ke AI")
        console.print(plan_tbl)

        # 3. Jana Cerita Persona Admin (35 tahun terpelajar, 500-650 aksara, 0 emoji)
        with console.status("[bold cyan]Menjana penceritaan Persona Admin (35 tahun) dengan semakan Vector...[/bold cyan]"):
            ok_gen, gen_pkg = generate_admin_text_only_post(
                cal_ctx=cal_ctx,
                pillar=chosen_pillar,
                game_rec=game_rec,
                product_rec=product_rec,
                recent_posts=recent_posts,
            )
        llm_engine.unload()

        if not ok_gen:
            console.print(f"[bold red]❌ Gagal menjana hantaran sah: {gen_pkg}[/bold red]")
            sys.exit(1)

        bm = gen_pkg["metrics"]
        console.print(
            Panel(
                f"[bold yellow]KAPSYEN HANTARAN UTAMA FB (Tanpa Gambar | Persona: {gen_pkg['persona_ref']} | "
                f"Cerita: {gen_pkg['story_chars']} aksara | Sifar Emoji):[/bold yellow]\n"
                f"[white]{gen_pkg['post_caption']}[/white]\n\n"
                f"[bold green]KOMEN PERTAMA AUTOMATIK (Variasi #{comment_var_idx + 1} | {product_rec['product_code']}):[/bold green]\n"
                f"[cyan]{comment_text}[/cyan]\n\n"
                f"[dim]Metrik LLM: {bm['elapsed_sec']}s ({bm['tok_per_sec']} tok/s, cubaan #{bm['attempt']}) | {bm['status']}[/dim]",
                title="2. Pratonton Hantaran & Komen Pertama",
                border_style="green",
            )
        )

        # 4. Terbit ke Facebook Page & Hantar Komen Pertama
        fb_res = {"post_id": "DRY_RUN", "comment_id": "DRY_RUN"}
        if not args.dry_run:
            with console.status("[bold cyan]Menerbitkan status teks ke Facebook Page & menghantar komen pertama...[/bold cyan]"):
                publisher = FacebookTextOnlyPublisher()
                fb_res = publisher.publish_text_post_and_comment(
                    post_caption=gen_pkg["post_caption"],
                    comment_text=comment_text,
                )
            console.print(
                f"[bold green]🚀 BERJAYA TERBIT KE FACEBOOK![/bold green] "
                f"Post ID: [bold white]{fb_res['post_id']}[/bold white] | "
                f"Comment ID: [bold white]{fb_res['comment_id']}[/bold white]"
            )
        else:
            console.print("[yellow]ℹ️ Mod --dry-run aktif: melangkau penerbitan sebenar ke Facebook.[/yellow]")

        # 5. Simpan Rekod ke SQLite, Upstash Vector, Upstash Redis & Backblaze B2
        final_record = {
            "posted_at_iso": cal_ctx["now_iso"],
            "posted_epoch": cal_ctx["now_epoch"],
            "run_env": run_env,
            "persona_ref": gen_pkg["persona_ref"],
            "pillar_id": chosen_pillar["pillar_id"],
            "game_title": game_rec["title"],
            "story_bm": gen_pkg["story_bm"],
            "story_chars": gen_pkg["story_chars"],
            "post_caption": gen_pkg["post_caption"],
            "story_hash": gen_pkg["story_hash"],
            "shopee_product_code": product_rec["product_code"],
            "shopee_product_name": product_rec["product_name"],
            "shopee_url": product_rec["shopee_url"],
            "comment_variant_idx": comment_var_idx,
            "comment_text": comment_text,
            "fb_post_id": fb_res["post_id"],
            "fb_comment_id": fb_res["comment_id"],
            "vector_shard": gen_pkg["vector_shard"],
            "llm_metrics": bm,
        }

        if not args.dry_run:
            vec_id = f"ps2n_x11_{cal_ctx['now_epoch']}_{gen_pkg['story_hash'][:10]}"
            coordinator.record_published_post(record=final_record, vec_story_id=vec_id, b2_archive=True)
            console.print("[bold green]✅ Rekod disimpan ke SQLite, Upstash Vector (X05), Upstash Redis (X04), dan Backblaze B2 (X03).[/bold green]")

        LAST_OUTPUT_JSON.write_text(json.dumps(final_record, indent=2, ensure_ascii=False), encoding="utf-8")
        console.print(f"[dim]Laporan JSON disimpan di: {LAST_OUTPUT_JSON}[/dim]")
    finally:
        if lock_token:
            redis_sharded.release_lock("x11_fb_text_post", lock_token)


if __name__ == "__main__":
    main()