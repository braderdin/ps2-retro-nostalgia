#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - REDDIT PS2 + VISION OCR + FB AUTO-POSTER (X13)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/live-engine-01/X13_reddit_ps2_story_poster.py
#
# FUNGSI UTAMA (EDISI NAIK TARAF):
# 1. Penarik Idea & Gambar Komuniti Reddit PS2 (r/ps2, r/ps2homebrew,
#    r/PlayStation2) melalui 4 lapisan enjin (Arctic Shift -> PullPush ->
#    Direct JSON -> Atom RSS XML) + 5 komen teratas komuniti.
# 2. Penapis Anti-Ulangan Ketat 60 Hari (Gambar SHA-256, Tajuk/ID Reddit, & Ayat):
#    - Tiada gambar sama (SHA-256) atau ayat 100% sama dalam tempoh 60 hari.
#    - Selepas 60 hari tamat, topik/gambar dibenarkan semula secara automatik.
# 3. Otak Memori SQLite3 Pintar (< 30 MB Auto-Split & Auto-Prune):
#    - Membaca katalog game (.db), produk Shopee (.db), sejarah X11 & X13 (.db).
#    - Memantau saiz fail .db; jika mencecah 26 MB (bawah had 30 MB GitHub),
#      rekod lama dipecahkan automatik ke fail *_partNNN.db dan di-VACUUM.
# 4. Persona AI Santai (500 - 750 Aksara | Guardrail Ringan | 3 Persona 2-Hari):
#    - Menggabungkan hasil bacaan Vision/OCR (< 30 KB, X07) + diskusi Reddit
#      dalam Bahasa Melayu Malaysia santai tanpa emoji pada kapsyen utama.
# 5. Komen Pertama Automatik (20 Pusingan Variasi Hidup) + 10 Link Shopee
#    bergilir + arkib media/log ke Backblaze B2 (X03).
# ==============================================================================

import argparse
import hashlib
import hmac
import html
import importlib
import inspect
import json
import os
import random
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx
import requests
from PIL import Image
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# ------------------------------------------------------------------------------
# IMPORT MODUL TERAS DARI live-engine-01 (X01 - X07)
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
_vision_mod: Any = importlib.import_module("X07_vision_ocr_analyzer")

ledger_db: Any = getattr(_ledger_mod, "ledger_db")
b2_proxy: Any = getattr(_b2_mod, "b2_proxy")
compute_file_sha256: Any = getattr(_b2_mod, "compute_file_sha256")
PS2RedisShardedManager: Any = getattr(_redis_mod, "PS2RedisShardedManager")
PS2VectorShardedManager: Any = getattr(_vector_mod, "PS2VectorShardedManager")
llm_engine: Any = getattr(_llm_mod, "llm_engine")
clean_and_scrub_bm_story: Any = getattr(_llm_mod, "clean_and_scrub_bm_story")
vision_analyzer: Any = getattr(_vision_mod, "vision_analyzer")

# Instans Redis & Vector khusus berawalan 'reddit' (ps2n_reddit:...)
reddit_redis = PS2RedisShardedManager(namespace="ps2n_reddit")
reddit_vector = PS2VectorShardedManager(namespace="ps2n_reddit")


# ==============================================================================
# [BAHAGIAN TUNING MANUAL X13 - SANTAI, 500-750 AKSARA & KAWALAN DB < 30MB]
# ==============================================================================
STORY_MIN_CHARS: int = int(os.getenv("PS2N_X13_MIN_CHARS", "500"))
STORY_MAX_CHARS: int = int(os.getenv("PS2N_X13_MAX_CHARS", "750"))
STORY_SOFT_TRIM: int = int(os.getenv("PS2N_X13_TRIM_CHARS", "740"))
STORY_MAX_TOKENS: int = int(os.getenv("PS2N_X13_MAX_TOKENS", "480"))
STORY_TEMPERATURE: float = float(os.getenv("PS2N_X13_TEMP", "0.68"))

# Tempoh Sekatan Ulangan (60 Hari untuk 100% Sama | 5 Hari untuk Hampir Sama)
EXACT_MATCH_THRESHOLD: float = 0.95
EXACT_COOLDOWN_DAYS: int = 60
SIMILAR_MATCH_THRESHOLD: float = 0.78
SIMILAR_COOLDOWN_DAYS: int = 5
IMAGE_SHA256_COOLDOWN_DAYS: int = 60
REDDIT_POST_COOLDOWN_DAYS: int = 60

# Had Saiz Fail SQLite3 untuk GitHub (< 30 MB per fail; mula split pada 26 MB)
SQLITE_MAX_SAFE_BYTES: int = int(os.getenv("PS2N_SQLITE_MAX_BYTES", str(26 * 1024 * 1024)))
SQLITE_MAX_ARCHIVE_SHARDS: int = 6
SQLITE_PRUNE_OLDER_THAN_DAYS: int = 150

# Laluan Data & Temp
DATA_DIR: Path = Path(getattr(cfg, "DATA_DIR", PROJECT_ROOT / "data"))
TEMP_DIR: Path = Path(getattr(cfg, "TEMP_DIR", PROJECT_ROOT / "temp"))
WORK_MEDIA_DIR: Path = TEMP_DIR / "x13_reddit_media"
SHOPEE_DB_PATH: Path = DATA_DIR / "ps2_shopee_products.db"
GAMES_DB_PATH: Path = DATA_DIR / "ps2_games_catalog.db"
TEXT_HISTORY_DB_PATH: Path = DATA_DIR / "ps2_text_posts_history.db"
REDDIT_HISTORY_DB_PATH: Path = DATA_DIR / "ps2_reddit_posts_history.db"
LAST_OUTPUT_JSON: Path = TEMP_DIR / "x13_last_reddit_post.json"
# ==============================================================================

MYT = timezone(timedelta(hours=8))

CORE_PS2_SUBS: List[str] = ["ps2", "ps2homebrew", "PlayStation2"]

LOW_STORY_TITLE_PATTERNS = (
    r"\bhow much is this worth\b",
    r"\bprice check\b",
    r"\bis this fake\b",
    r"\bauthenticity check\b",
    r"\bfor sale\b",
    r"\bthrift haul\b",
)

REDDIT_APP_HEADERS: Dict[str, str] = {
    "User-Agent": "linux:ps2-retro-nostalgia-curator:v1.1.0 (by /u/ps2retronostalgia)",
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

# 3 Kitaran Persona Utama (Bergilir Setiap 2 Hari Secara Automatik)
THREE_PERSONA_CYCLES: List[Dict[str, str]] = [
    {
        "persona_id": "P_KAKI_LEPAK",
        "title": "Abang Kedai Game (Santai & Mengimbau)",
        "style_note": (
            "Bercerita macam member lama kat kedai game era 2000-an. Santai, ramah, ada unsur nostalgia "
            "zaman sekolah, berebut kontroler dengan kawan, dan seronok kongsi cerita tanpa bahasa skema."
        ),
        "one_shot_anchor": (
            "Contoh nada santai: Masa zaman sekolah dulu, kalau dapat lepak depan TV lepas habis kelas memang tak ingat "
            "benda lain dah. Lagi-lagi bila jumpa game legend yang buat satu bilik riuh rendah."
        ),
    },
    {
        "persona_id": "P_DOKTOR_KONSOL",
        "title": "Pakar Perkakasan & Modding PS2",
        "style_note": (
            "Gaya abang 35 tahun yang mahir selok-belok konsol PS2 Fat dan Slim, sistem OPL, modchip Matrix, "
            "serta kad memori FMCB/OpenTuna. Terangkan fakta teknikal dengan ayat mudah orang biasa faham."
        ),
        "one_shot_anchor": (
            "Contoh nada santai: Ramai yang ingat bila laser PS2 dah mula lemah tak boleh baca CD, konsol lama tu terus "
            "kena simpan dalam stor. Padahal kalau guna sistem OPL terus dari pendrive, kerja lagi cepat dan selamat."
        ),
    },
    {
        "persona_id": "P_KURATOR_RETRO",
        "title": "Kurator Koleksi & Sejarah Klasik",
        "style_note": (
            "Menghargai nilai sentimental kover game asal, ketahanan perkakasan Sony era 2000-an, dan kisah "
            "di sebalik tabir pembangunan game klasik dengan penceritaan yang hidup dan menyentuh emosi."
        ),
        "one_shot_anchor": (
            "Contoh nada santai: Tengok balik kover kotak biru PlayStation 2 zaman dulu pun dah cukup buat ingatan "
            "melayang ke tahun 2004. Setiap judul masa tu ada identiti tersendiri yang susah nak jumpa sekarang."
        ),
    },
]

# 4 Gaya Mood Mengikut Waktu & Jenis Topik
REDDIT_PERSONA_MOODS: List[Dict[str, str]] = [
    {
        "mood_id": "TECH_INSIDER",
        "label": "Kupasan Perkakasan & Modding PS2",
        "preferred_slots": "Pagi,Tengah Hari",
        "instruction": (
            "Fokus pada bagaimana komuniti PS2 hari ini memanfaatkan OPL, sambungan rangkaian, kad memori mod, "
            "atau cip Matrix untuk menghidupkan semula konsol lama tanpa perlu bergantung pada cakera optik."
        ),
    },
    {
        "mood_id": "STORYTELLER_NOSTALGIA",
        "label": "Penceritaan Nostalgia Era 2000-an",
        "preferred_slots": "Petang,Malam",
        "instruction": (
            "Kaitkan suasana dalam gambar dan perbualan komuniti dengan kenangan sebenar pemain PS2 di Malaysia "
            "seperti menjaga save data 8MB atau melayan game klasik bersama kawan dan adik-beradik."
        ),
    },
    {
        "mood_id": "DEBATE_HOST",
        "label": "Pengacara Topik & Perdebatan Komuniti",
        "preferred_slots": "Tengah Hari,Malam",
        "instruction": (
            "Angkat persoalan atau perbandingan game yang sedang hangat dibincangkan peminat PS2 dengan gaya "
            "sembang santai supaya pembaca teruja untuk berkongsi pilihan mereka di ruangan komen."
        ),
    },
    {
        "mood_id": "COLLECTOR_SHOWCASE",
        "label": "Ulasan Koleksi & Setup Retro Klasik",
        "preferred_slots": "Pagi,Petang",
        "instruction": (
            "Ulas butiran menarik yang kelihatan pada kover game, konsol, kabel, atau susun atur retro dalam gambar "
            "sambil menyelitkan kemudahan memainkan semula koleksi tersebut menggunakan storan USB moden."
        ),
    },
]

# 20 Pusingan Variasi Komen Pertama Shopee (Hidup, Pelbagai Nada & Sedia Klik)
REDDIT_COMMENT_VARIATIONS_20: List[str] = [
    "🎮 Rindu nak layan semula koleksi klasik PS2 kat rumah tanpa risau CD calar atau lens rosak? Tekan link Shopee di bawah untuk pilih game kegemaran korang:\n👉 {url}",
    "🕹️ Ramai geng 30-an dah hidupkan balik konsol PS2 lama kat rumah guna cara moden ni! Klik link di bawah untuk semak pakej yang ngam dengan PS2 korang:\n👉 {url}",
    "💾 Konsol PS2 kat rumah lens dah lemah tak baca disk? Jangan simpan dalam stor dulu, tekan link Shopee di bawah untuk penyelesaian Plug & Play:\n👉 {url}",
    "🔥 Nak pilih sendiri senarai game legend PS2 siap masuk pendrive atau hardisk? Boleh terus klik pautan rasmi di bawah ni geng:\n👉 {url}",
    "⚡ Senang kerja sekarang, cucuk terus boleh main game klasik PS2 dengan lancar! Tekan link di bawah untuk tengok pilihan kapasiti:\n👉 {url}",
    "🎯 Khas untuk peminat retro PS2 di Malaysia! Semak sama ada PS2 korang jenis Matrix atau standard dan pilih set yang sesuai kat sini:\n👉 {url}",
    "📀 Dah penat tiup disk PS2 tapi masih sangkut kat skrin loading? Jom beralih ke sistem OPL USB yang lebih pantas, tekan link di bawah:\n👉 {url}",
    "✨ Cara paling selamat untuk main ratusan game PS2 tanpa perlu ubah suai dalaman konsol. Klik pautan Shopee di bawah sekarang:\n👉 {url}",
    "🚀 Nak marathon game klasik PS2 hujung minggu ni dengan member atau anak-anak? Tekan link kat bawah untuk susun senarai game korang:\n👉 {url}",
    "🛡️ Pastikan korang pilih versi yang tepat untuk model PS2 Fat atau PS2 Slim di rumah. Tekan link di bawah untuk baca maklumat penuh:\n👉 {url}",
    "🎲 Dari game aksi, lumba kereta sampai bola sepak legend era 2000-an, semua boleh pilih ikut citarasa sendiri! Klik link kat sini:\n👉 {url}",
    "💡 Tip ringkas dari min: guna storan USB siap sistem OPL supaya laser PS2 tak terbeban. Tekan link Shopee di bawah untuk tengok set tersedia:\n👉 {url}",
    "🔧 Tak kira konsol PS2 Matrix mahupun Non-Matrix, ada jalan mudah untuk terus main dari USB. Klik pautan di bawah untuk semak:\n👉 {url}",
    "📲 Lepas buat pesanan boleh terus hantar senarai game pilihan korang di chat! Tekan link Shopee di bawah untuk lihat butiran:\n👉 {url}",
    "🌟 Hidupkan semula bunyi skrin boot ikonik PS2 kat TV ruang tamu malam ni! Klik link di bawah untuk tengok kelengkapan siap sedia:\n👉 {url}",
    "🏁 Jangan biarkan konsol legenda PS2 tersadai berhabuk! Tekan link kat bawah ni untuk tengok pakej plug and play pilihan ramai:\n👉 {url}",
    "📺 Kalau ada TV lama atau monitor kat rumah, terus sambung PS2 guna set siap game ni memang puas hati. Tengok pilihan penuh kat link bawah:\n👉 {url}",
    "🧩 Susah nak cari CD game lama yang elok sekarang? Simpan je dalam pendrive atau hardisk siap susun ikut tajuk. Klik sini untuk semak:\n👉 {url}",
    "👾 Nak bagi anak-anak rasa pengalaman game zaman kita membesar dulu? Set PS2 plug and play ni memang senang nak guna, tekan link bawah:\n👉 {url}",
    "🏆 Korang team bola sepak klasik, game lumba kereta, atau game aksi pengembaraan? Semua tajuk popular boleh pilih sendiri kat pautan ni:\n👉 {url}",
]


# ==============================================================================
# 1. KONTEKS MASA MALAYSIA (MYT) & KITARAN PERSONA 2-HARI
# ==============================================================================
def get_malaysia_special_occasion(now: datetime) -> str:
    """Mengesan hari besar, perayaan, cuti sekolah, & tarikh bersejarah PS2 di Malaysia."""
    m, d, wd = now.month, now.day, now.weekday()
    notes: List[str] = []

    fixed_events = {
        (1, 1): "Sambutan Tahun Baru — permulaan tahun baharu dengan semangat retro.",
        (2, 1): "Hari Wilayah Persekutuan — cuti santai sesuai mengemas koleksi konsol.",
        (3, 4): "HARI BERSEJARAH PS2 (4 Mac 2000) — ulang tahun pelancaran rasmi PlayStation 2 di dunia!",
        (5, 1): "Hari Pekerja — masa terbaik berehat di rumah sambil melayan game klasik.",
        (8, 31): "Hari Kebangsaan (Merdeka) — semangat patriotik & kenangan membesar di Malaysia.",
        (9, 16): "Hari Malaysia — meraikan kebersamaan peminat retro dari Semenanjung hingga Sabah & Sarawak.",
        (12, 25): "Hari Krismas & musim percutian hujung tahun di Malaysia.",
    }
    if (m, d) in fixed_events:
        notes.append(fixed_events[(m, d)])

    # Tetingkap musim perayaan & cuti sekolah Malaysia
    if m == 2 and 10 <= d <= 25:
        notes.append("Suasana musim perayaan Tahun Baru Cina & cuti panjang.")
    elif m in (3, 4) and d <= 20:
        notes.append("Suasana musim Ramadan / Syawal — ramai berkumpul bersama keluarga dan sepupu di kampung.")
    elif m == 10 and d >= 20 or (m == 11 and d <= 12):
        notes.append("Suasana musim perayaan Deepavali & cuti perayaan.")

    if m in (11, 12):
        notes.append("Musim cuti sekolah hujung tahun & musim hujan monsun di Malaysia — paling ngam lepak dalam rumah layan PS2.")
    elif m in (5, 6) and d <= 15:
        notes.append("Musim cuti pertengahan tahun sekolah di Malaysia.")

    if wd == 4 and now.hour >= 18:
        notes.append("Malam minggu (Jumaat malam) — waktu paling ramai geng PS2 buat marathon game sampai lewat.")
    elif wd in (5, 6):
        notes.append("Hujung minggu (Sabtu/Ahad) — masa lapang santai tanpa fikir kerja atau sekolah.")

    return " | ".join(notes) if notes else "Hari biasa — suasana santai komuniti retro gaming Malaysia."


def get_myt_calendar_context() -> Dict[str, Any]:
    now = datetime.now(tz=MYT)
    days_bm = ["Isnin", "Selasa", "Rabu", "Khamis", "Jumaat", "Sabtu", "Ahad"]
    months_bm = [
        "Januari", "Februari", "Mac", "April", "Mei", "Jun",
        "Julai", "Ogos", "September", "Oktober", "November", "Disember",
    ]

    day_name = days_bm[now.weekday()]
    month_name = months_bm[now.month - 1]
    week_of_month = ((now.day - 1) // 7) + 1
    hour = now.hour

    # Kitaran 3 Persona AI berbeza setiap 2 hari (48 jam)
    day_of_year = now.timetuple().tm_yday
    persona_cycle = THREE_PERSONA_CYCLES[(day_of_year // 2) % len(THREE_PERSONA_CYCLES)]

    if 5 <= hour < 12:
        period = "Pagi"
        allowed_time_cues = "pagi ni, awal pagi ni, pagi-pagi ni, hari ni"
        forbidden_times = "tengah hari ni, petang ni, petang tadi, malam ni, malam tadi, tengah malam"
        forbidden_regex = r"(tengah hari ni|petang ni|petang tadi|malam ni|malam tadi|tengah malam)"
    elif 12 <= hour < 15:
        period = "Tengah Hari"
        allowed_time_cues = "tengah hari ni, waktu rehat ni, hari ni"
        forbidden_times = "pagi ni, pagi tadi, selamat pagi, petang ni, malam ni, tengah malam"
        forbidden_regex = r"(pagi ni|pagi ini|pagi tadi|selamat pagi|petang ni|malam ni|tengah malam)"
    elif 15 <= hour < 19:
        period = "Petang"
        allowed_time_cues = "petang ni, lepas kerja petang ni, hari ni"
        forbidden_times = "pagi ni, pagi tadi, selamat pagi, tengah hari ni, malam ni, tengah malam"
        forbidden_regex = r"(pagi ni|pagi ini|pagi tadi|selamat pagi|tengah hari ni|malam ni|tengah malam)"
    elif 19 <= hour <= 23:
        period = "Malam"
        allowed_time_cues = "malam ni, malam-malam macam ni, waktu malam ni"
        forbidden_times = "pagi ni, pagi ini, pagi tadi, selamat pagi, pagi yang cerah, tengah hari ni, petang ni, tengah malam ni"
        forbidden_regex = r"(pagi ni|pagi ini|pagi tadi|selamat pagi|pagi yang cerah|tengah hari ni|petang ni|tengah malam ni)"
    else:
        period = "Tengah Malam"
        allowed_time_cues = "lewat malam ni, tengah malam ni, awal pagi ni"
        forbidden_times = "pagi yang cerah, selamat pagi, tengah hari ni, petang ni, petang tadi"
        forbidden_regex = r"(pagi yang cerah|selamat pagi|tengah hari ni|petang ni|petang tadi)"

    special_occasion = get_malaysia_special_occasion(now)
    full_ts = (
        f"{day_name}, {now.day:02d} {month_name} {now.year} | "
        f"{now.strftime('%I:%M %p')} MYT (Waktu {period} | Minggu ke-{week_of_month})"
    )
    return {
        "now_iso": now.isoformat(),
        "now_epoch": int(now.timestamp()),
        "day_name": day_name,
        "month_name": month_name,
        "year": now.year,
        "period": period,
        "allowed_time_cues": allowed_time_cues,
        "forbidden_times": forbidden_times,
        "forbidden_regex": forbidden_regex,
        "special_occasion": special_occasion,
        "full_timestamp": full_ts,
        "persona_cycle": persona_cycle,
    }

# ==============================================================================
# 2. OTAK MEMORI SQLITE3 PINTAR (< 30 MB AUTO-SPLIT & 60-HARI ANTI-DUPLIKASI)
# ==============================================================================
class RedditPostCoordinator:
    """
    Menguruskan:
    1. Pangkalan data SQLite3 tempatan (`data/ps2_reddit_posts_history.db`) dengan
       pengawal saiz pintar (< 30 MB per fail): jika saiz fail mencecah 26 MB,
       50% rekod tertua dipecahkan automatik ke `ps2_reddit_posts_history_partNNN.db`
       dan fail utama di-VACUUM supaya sentiasa ringan untuk git push.
    2. Penapisan 100% anti-ulangan dalam tempoh 60 hari bagi:
       - Cincangan SHA-256 setiap gambar (`image_sha256_list`)
       - ID pos Reddit (`reddit_post_id`) & cincangan tajuk asal (`title_sha256`)
       - Cincangan & vektor ayat cerita Bahasa Melayu (`story_hash`)
    3. Pembacaan otak memori daripada `.db` sedia ada (`ps2_games_catalog.db`,
       `ps2_shopee_products.db`, `ps2_text_posts_history.db`, dan sejarah Reddit).
    """

    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        WORK_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        self._init_db(REDDIT_HISTORY_DB_PATH)
        self.enforce_sqlite_size_guard()

    def _init_db(self, db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reddit_post_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_uid TEXT NOT NULL UNIQUE,
                    reddit_post_id TEXT NOT NULL,
                    title_sha256 TEXT DEFAULT '',
                    subreddit TEXT NOT NULL,
                    reddit_title TEXT NOT NULL,
                    reddit_permalink TEXT DEFAULT '',
                    posted_at_iso TEXT NOT NULL,
                    posted_epoch INTEGER NOT NULL,
                    run_env TEXT NOT NULL,
                    persona_ref TEXT NOT NULL,
                    persona_cycle_id TEXT DEFAULT '',
                    mood_id TEXT NOT NULL,
                    image_source_type TEXT NOT NULL,
                    image_count INTEGER NOT NULL,
                    image_sha256_list TEXT DEFAULT '[]',
                    ocr_summary TEXT DEFAULT '',
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
            # Naik taraf lajur jika fail .db versi lama sudah wujud
            existing_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(reddit_post_history)").fetchall()
            }
            for col_name, col_def in (
                ("title_sha256", "TEXT DEFAULT ''"),
                ("persona_cycle_id", "TEXT DEFAULT ''"),
                ("image_sha256_list", "TEXT DEFAULT '[]'"),
            ):
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE reddit_post_history ADD COLUMN {col_name} {col_def}")

            conn.execute("CREATE INDEX IF NOT EXISTS idx_rph_epoch ON reddit_post_history(posted_epoch DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rph_rid ON reddit_post_history(reddit_post_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rph_story_hash ON reddit_post_history(story_hash)")
            conn.commit()
        finally:
            conn.close()

    def _list_all_history_dbs(self) -> List[Path]:
        """Memulangkan fail DB aktif diikuti fail pecahan arkib terkini (jika ada)."""
        dbs: List[Path] = []
        if REDDIT_HISTORY_DB_PATH.exists():
            dbs.append(REDDIT_HISTORY_DB_PATH)
        shards = sorted(DATA_DIR.glob("ps2_reddit_posts_history_part*.db"), reverse=True)
        dbs.extend(shards)
        return dbs

    def enforce_sqlite_size_guard(self) -> Dict[str, Any]:
        """
        Memastikan setiap fail .db di folder data/ kekal di bawah 30 MB untuk GitHub.
        Jika fail utama mencecah 26 MB:
          1. Jalankan checkpoint & VACUUM.
          2. Jika masih >= 26 MB, pindahkan 50% rekod tertua ke fail pecahan baharu
             `ps2_reddit_posts_history_partNNN.db` dan padam dari fail utama.
          3. Padam rekod terlalu tua (> 150 hari) atau lebihan shard lama jika perlu.
        """
        if not REDDIT_HISTORY_DB_PATH.exists():
            return {"status": "NEW_DB", "size_mb": 0.0}

        cur_size = REDDIT_HISTORY_DB_PATH.stat().st_size
        if cur_size < SQLITE_MAX_SAFE_BYTES:
            return {"status": "OK_UNDER_30MB", "size_mb": round(cur_size / (1024 * 1024), 2)}

        conn = sqlite3.connect(str(REDDIT_HISTORY_DB_PATH))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            conn.commit()
        finally:
            conn.close()

        cur_size = REDDIT_HISTORY_DB_PATH.stat().st_size
        if cur_size < SQLITE_MAX_SAFE_BYTES:
            return {"status": "VACUUMED_OK", "size_mb": round(cur_size / (1024 * 1024), 2)}

        # Split 50% rekod tertua ke fail pecahan arkib baharu
        existing_shards = sorted(DATA_DIR.glob("ps2_reddit_posts_history_part*.db"))
        next_idx = len(existing_shards) + 1
        shard_path = DATA_DIR / f"ps2_reddit_posts_history_part{next_idx:03d}.db"
        self._init_db(shard_path)

        src_conn = sqlite3.connect(str(REDDIT_HISTORY_DB_PATH))
        dst_conn = sqlite3.connect(str(shard_path))
        try:
            total_rows = int(src_conn.execute("SELECT COUNT(*) FROM reddit_post_history").fetchone()[0])
            move_count = max(1, total_rows // 2)
            old_rows = src_conn.execute(
                "SELECT * FROM reddit_post_history ORDER BY posted_epoch ASC LIMIT ?",
                (move_count,),
            ).fetchall()

            if old_rows:
                cols = [d[1] for d in src_conn.execute("PRAGMA table_info(reddit_post_history)").fetchall()]
                non_id_cols = [c for c in cols if c != "id"]
                id_idx = cols.index("id")
                col_indices = [cols.index(c) for c in non_id_cols]
                placeholders = ", ".join(["?"] * len(non_id_cols))
                col_names_sql = ", ".join(non_id_cols)

                ids_to_delete: List[int] = []
                for r in old_rows:
                    ids_to_delete.append(int(r[id_idx]))
                    vals = [r[i] for i in col_indices]
                    dst_conn.execute(
                        f"INSERT OR IGNORE INTO reddit_post_history ({col_names_sql}) VALUES ({placeholders})",
                        vals,
                    )
                dst_conn.commit()

                src_conn.executemany("DELETE FROM reddit_post_history WHERE id = ?", [(i,) for i in ids_to_delete])
                src_conn.commit()
                src_conn.execute("VACUUM")
                src_conn.commit()
        finally:
            src_conn.close()
            dst_conn.close()

        # Padam rekod terlalu tua (> 150 hari) atau lebihan fail pecahan lama supaya storan GitHub kekal kecil
        all_shards = sorted(DATA_DIR.glob("ps2_reddit_posts_history_part*.db"))
        while len(all_shards) > SQLITE_MAX_ARCHIVE_SHARDS:
            oldest_shard = all_shards.pop(0)
            oldest_shard.unlink(missing_ok=True)

        new_size = REDDIT_HISTORY_DB_PATH.stat().st_size
        return {
            "status": "SPLIT_COMPLETED",
            "new_active_size_mb": round(new_size / (1024 * 1024), 2),
            "created_shard": shard_path.name,
        }

    def is_reddit_post_used_within_cooldown(
        self,
        reddit_post_id: str,
        reddit_title: str,
        now_epoch: int,
    ) -> Tuple[bool, str]:
        """Menyemak sama ada ID pos Reddit atau tajuk asal 100% sama dalam tempoh 60 hari."""
        cutoff_epoch = now_epoch - (REDDIT_POST_COOLDOWN_DAYS * 86400)
        norm_title = re.sub(r"\s+", " ", (reddit_title or "").strip().lower())
        title_hash = hashlib.sha256(norm_title.encode("utf-8")).hexdigest() if norm_title else ""

        # 1. Semak Redis (`ps2n_reddit`)
        if reddit_post_id:
            dup_id, _ = reddit_redis.is_duplicate_fingerprint(f"reddit_id:{reddit_post_id}")
            if dup_id:
                return True, f"ID Reddit '{reddit_post_id}' sudah digunakan (< 60 hari di Redis)."
        if title_hash:
            dup_t, _ = reddit_redis.is_duplicate_fingerprint(f"title_sha256:{title_hash}")
            if dup_t:
                return True, "Tajuk pos Reddit 100% sama sudah digunakan (< 60 hari di Redis)."

        # 2. Semak SQLite (Fail Aktif + Pecahan Arkib)
        for db_p in self._list_all_history_dbs()[:3]:
            conn = sqlite3.connect(str(db_p))
            try:
                row = conn.execute(
                    """
                    SELECT posted_epoch FROM reddit_post_history
                    WHERE (reddit_post_id = ? OR (title_sha256 != '' AND title_sha256 = ?))
                      AND posted_epoch > ?
                    LIMIT 1
                    """,
                    (str(reddit_post_id), title_hash, cutoff_epoch),
                ).fetchone()
                if row:
                    age_days = round((now_epoch - int(row[0])) / 86400.0, 1)
                    return True, f"Pos/Tajuk Reddit sama pernah disiarkan {age_days} hari lalu (had 60 hari)."
            finally:
                conn.close()

        return False, "OK"

    def are_images_used_within_60_days(
        self,
        image_paths: List[Path],
        now_epoch: int,
    ) -> Tuple[bool, List[str], str]:
        """
        Mengira SHA-256 setiap fail gambar dan memastikan tiada satu pun gambar
        yang 100% sama pernah dipost dalam tempoh 60 hari (dibenarkan selepas 60 hari).
        """
        cutoff_epoch = now_epoch - (IMAGE_SHA256_COOLDOWN_DAYS * 86400)
        hashes: List[str] = []

        for p in image_paths:
            if not p.exists():
                continue
            sha = compute_file_sha256(p)
            hashes.append(sha)

            # 1. Semak Redis (kunci SHA-256 gambar bertempoh 60 hari)
            is_dup_r, _ = reddit_redis.is_duplicate_fingerprint(f"img_sha256:{sha}")
            if is_dup_r:
                return True, hashes, f"Gambar {p.name} (SHA256:{sha[:12]}...) sudah dipost dalam 60 hari (Redis)."

        if not hashes:
            return False, [], "Tiada hash gambar"

        # 2. Semak SQLite (Fail Aktif + Pecahan Arkib) dalam julat 60 hari
        for db_p in self._list_all_history_dbs()[:3]:
            conn = sqlite3.connect(str(db_p))
            try:
                rows = conn.execute(
                    """
                    SELECT posted_epoch, image_sha256_list
                    FROM reddit_post_history
                    WHERE posted_epoch > ? AND image_sha256_list != '[]'
                    """,
                    (cutoff_epoch,),
                ).fetchall()
                for posted_ep, raw_json in rows:
                    try:
                        past_hashes = set(json.loads(raw_json or "[]"))
                        overlap = past_hashes.intersection(hashes)
                        if overlap:
                            age_days = round((now_epoch - int(posted_ep)) / 86400.0, 1)
                            sample_h = next(iter(overlap))
                            return (
                                True,
                                hashes,
                                f"Gambar sama (SHA256:{sample_h[:12]}...) pernah dipost {age_days} hari lalu (had 60 hari).",
                            )
                    except Exception:
                        continue
            finally:
                conn.close()

        return False, hashes, "Lulus (Semua gambar unik dalam 60 hari)"

    def fetch_brain_context_from_databases(
        self,
        topic_text: str,
        recent_posts: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Membaca data daripada fail-fail .db dalam folder data/ sebagai otak/memori AI:
        - `ps2_games_catalog.db`: Cari maklumat trivia game jika tajuk game disebut.
        - `ps2_text_posts_history.db`: Semak topik terbaharu daripada X11 supaya tidak bertindih.
        - `ps2_reddit_posts_history.db`: Ringkasan topik & pembuka ayat terdahulu.
        """
        low_topic = topic_text.lower()
        matched_game_fact = ""

        # 1. Baca ps2_games_catalog.db
        if GAMES_DB_PATH.exists():
            conn = sqlite3.connect(str(GAMES_DB_PATH))
            conn.row_factory = sqlite3.Row
            try:
                for row in conn.execute("SELECT * FROM ps2_games").fetchall():
                    g = dict(row)
                    g_title = str(g.get("title") or g.get("game_title") or "").strip()
                    if g_title and len(g_title) >= 4 and g_title.lower() in low_topic:
                        yr = g.get("release_year") or g.get("year") or ""
                        genre = g.get("genre") or ""
                        trivia = g.get("trivia") or g.get("description") or ""
                        matched_game_fact = f"{g_title} ({yr}, {genre}): {str(trivia)[:160]}"
                        break
            except Exception:
                pass
            finally:
                conn.close()

        # 2. Baca sejarah X11 (ps2_text_posts_history.db) jika wujud
        x11_recent_topics: List[str] = []
        if TEXT_HISTORY_DB_PATH.exists():
            conn = sqlite3.connect(str(TEXT_HISTORY_DB_PATH))
            try:
                rows = conn.execute(
                    "SELECT game_title, pillar_id FROM text_post_history ORDER BY posted_epoch DESC LIMIT 4"
                ).fetchall()
                for gt, pid in rows:
                    x11_recent_topics.append(f"{gt} ({pid})")
            except Exception:
                pass
            finally:
                conn.close()

        # 3. Ringkasan 10 topik Reddit terdahulu (tanpa menyalin ayat penuh untuk elak prompt priming)
        recent_topic_lines = [
            f"{i}. [r/{rp.get('subreddit', 'ps2')} | {rp.get('mood_id', '-')}] {str(rp.get('reddit_title', '-'))[:72]}"
            for i, rp in enumerate(recent_posts[:10], 1)
        ]

        return {
            "matched_game_fact": matched_game_fact,
            "x11_recent_topics": x11_recent_topics,
            "recent_topic_summary": "\n".join(recent_topic_lines) if recent_topic_lines else "Tiada rekod terdahulu.",
        }

    def get_recent_reddit_posts(self, limit: int = 12) -> List[Dict[str, Any]]:
        """Mengambil rekod terkini daripada Redis (`ps2n_reddit`) & SQLite."""
        records: List[Dict[str, Any]] = []
        seen_hashes = set()

        try:
            r_items = reddit_redis.get_rolling_history(stream="reddit", limit=limit)
            for obj in r_items:
                if isinstance(obj, dict):
                    h = obj.get("story_hash", "")
                    if h and h not in seen_hashes:
                        seen_hashes.add(h)
                        records.append(obj)
        except Exception:
            pass

        if REDDIT_HISTORY_DB_PATH.exists():
            conn = sqlite3.connect(str(REDDIT_HISTORY_DB_PATH))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    SELECT post_uid, reddit_post_id, subreddit, reddit_title,
                           posted_at_iso, posted_epoch, persona_ref, persona_cycle_id,
                           mood_id, story_bm, story_hash, shopee_product_code,
                           shopee_url, comment_variant_idx
                    FROM reddit_post_history
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

    def pick_rotating_shopee_product(
        self,
        topic_text: str,
        recent_posts: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Memilih 1 produk Shopee berbeza setiap post daripada data/ps2_shopee_products.db."""
        low = topic_text.lower()
        if "matrix" in low or "modchip" in low:
            target_compat = "MATRIX_ONLY"
        elif any(k in low for k in ("fmcb", "opentuna", "fortuna", "code breaker", "memory card")):
            target_compat = "MOD_CARD_ONLY"
        elif any(k in low for k in ("opl", "laser", "disc read", "lens", "fat", "slim", "scph", "hdd", "usb", "xfer")):
            target_compat = "NON_MATRIX_OPL_SET"
        else:
            target_compat = "UNIVERSAL_STORAGE"

        if not SHOPEE_DB_PATH.exists():
            raise FileNotFoundError(f"Fail katalog Shopee tidak ditemui: {SHOPEE_DB_PATH}")

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

        recent_codes = [str(p.get("shopee_product_code", "")) for p in recent_posts[:5] if p.get("shopee_product_code")]
        last_code = recent_codes[0] if recent_codes else ""

        non_repeat = [r for r in all_rows if r["product_code"] != last_code] or all_rows
        fresh_pool = [r for r in non_repeat if r["product_code"] not in recent_codes] or non_repeat
        compat_pool = [r for r in fresh_pool if r["compatibility"] == target_compat] or fresh_pool

        by_code = {r["product_code"]: r for r in compat_pool}
        picked_code = reddit_redis.pick_least_used_item(
            pool_name="reddit_shopee_products",
            candidate_ids=list(by_code.keys()),
            exclude_ids=recent_codes,
        )
        if picked_code and picked_code in by_code:
            return by_code[picked_code]
        return random.choice(compat_pool[:2]) if len(compat_pool) >= 2 else compat_pool[0]

    def build_rotating_first_comment(
        self,
        product: Dict[str, Any],
        recent_posts: List[Dict[str, Any]],
    ) -> Tuple[str, int]:
        used_indices = [
            int(p["comment_variant_idx"])
            for p in recent_posts
            if p.get("comment_variant_idx") is not None
        ]
        total_vars = len(REDDIT_COMMENT_VARIATIONS_20)
        candidate_ids = [f"rvar_{i:02d}" for i in range(total_vars)]
        exclude_ids = [f"rvar_{i:02d}" for i in used_indices[:12]]

        picked = reddit_redis.pick_least_used_item(
            pool_name="reddit_comment_variants",
            candidate_ids=candidate_ids,
            exclude_ids=exclude_ids,
        )
        if picked and picked.startswith("rvar_"):
            next_idx = int(picked.split("_")[1])
        else:
            avail = [i for i in range(total_vars) if i not in used_indices[:10]] or list(range(total_vars))
            next_idx = random.choice(avail)

        template = REDDIT_COMMENT_VARIATIONS_20[next_idx]
        url = str(product["shopee_url"]).strip()
        compat = str(product.get("compatibility", ""))
        tier = str(product.get("storage_tier", "")).replace("_", " ")
        hint = str(product.get("game_count_hint", ""))

        if compat == "MATRIX_ONLY":
            spec_note = f"📌 Khas PS2 Matrix ({tier} | {hint})"
        elif compat == "NON_MATRIX_OPL_SET":
            spec_note = f"📌 Set Lengkap Mod Card FMCB/OpenTuna + {tier} ({hint})"
        elif compat == "MOD_CARD_ONLY":
            spec_note = "📌 Mod Card Sistem OPL + Code Breaker (PS2 Fat & Slim)"
        else:
            spec_note = f"📌 Pilihan Kapasiti ({hint}) — Boleh pilih game sendiri!"

        return f"{template.format(url=url)}\n\n{spec_note}", next_idx

    def upload_media_to_b2_proxy(self, local_paths: List[Path], post_uid: str) -> List[Dict[str, Any]]:
        proxy_url = (
            getattr(cfg, "CF_WORKER_B2_PROXY_STORAGE", "")
            or os.getenv("CF_WORKER_B2_PROXY_STORAGE", "")
        ).strip()
        proxy_secret = (
            getattr(cfg, "PS2N_PROXY_SECRET", "")
            or os.getenv("PS2N_PROXY_SECRET", "")
        ).strip()

        if not proxy_url or not proxy_secret:
            return []

        month_tag = datetime.now(tz=MYT).strftime("%Y%m")
        uploaded: List[Dict[str, Any]] = []
        sig = inspect.signature(b2_proxy.upload_file)
        params = list(sig.parameters.keys())

        for idx, p in enumerate(local_paths, 1):
            if not p.exists():
                continue
            remote_key = f"reddit_media/{month_tag}/{post_uid}_{idx:02d}{p.suffix.lower()}"
            try:
                if "local_path" in params and "remote_path" in params:
                    res = b2_proxy.upload_file(local_path=p, remote_path=remote_key)
                elif "file_path" in params and "remote_path" in params:
                    res = b2_proxy.upload_file(file_path=p, remote_path=remote_key)
                else:
                    res = b2_proxy.upload_file(p, remote_key)
                uploaded.append({"file": p.name, "remote_key": remote_key, "result": res})
            except Exception as exc:
                console.print(f"[yellow]⚠️ Amaran muat naik B2 ({p.name}): {exc}[/yellow]")

        return uploaded

    def record_published_reddit_post(
        self,
        record: Dict[str, Any],
        post_uid: str,
        b2_uploaded_media: List[Dict[str, Any]],
    ) -> None:
        """Menyimpan rekod ke SQLite (< 30MB), Upstash Vector (`ps2n_reddit`), Redis (`ps2n_reddit`), & B2."""
        norm_title = re.sub(r"\s+", " ", record["reddit_title"].strip().lower())
        title_sha256 = hashlib.sha256(norm_title.encode("utf-8")).hexdigest()
        img_hashes: List[str] = record.get("image_sha256_list", [])

        # 1. SQLite Tempatan
        conn = sqlite3.connect(str(REDDIT_HISTORY_DB_PATH))
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO reddit_post_history (
                    post_uid, reddit_post_id, title_sha256, subreddit, reddit_title,
                    reddit_permalink, posted_at_iso, posted_epoch, run_env,
                    persona_ref, persona_cycle_id, mood_id, image_source_type,
                    image_count, image_sha256_list, ocr_summary, story_bm,
                    post_caption, story_hash, shopee_product_code, shopee_url,
                    comment_variant_idx, comment_text, fb_post_id, fb_comment_id, vector_shard
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    post_uid,
                    record["reddit_post_id"],
                    title_sha256,
                    record["subreddit"],
                    record["reddit_title"],
                    record["reddit_permalink"],
                    record["posted_at_iso"],
                    record["posted_epoch"],
                    record["run_env"],
                    record["persona_ref"],
                    record.get("persona_cycle_id", ""),
                    record["mood_id"],
                    record["image_source_type"],
                    record["image_count"],
                    json.dumps(img_hashes),
                    record["ocr_summary"],
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

        # Semak & pecahkan .db jika menghampiri 30 MB
        self.enforce_sqlite_size_guard()

        # Kemas kini kaunter produk Shopee
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

        # 2. Upstash Vector berawalan 'reddit' (`ps2n_reddit:reddit_...`)
        try:
            reddit_vector.upsert_story_vector(
                post_uid=f"reddit_{post_uid}",
                game_title=f"reddit:{record['subreddit']}:{record['reddit_title'][:60]}",
                story_text=record["story_bm"],
                extra_meta={
                    "source": "reddit",
                    "reddit_post_id": record["reddit_post_id"],
                    "subreddit": record["subreddit"],
                    "mood_id": record["mood_id"],
                    "posted_at_iso": record["posted_at_iso"],
                    "posted_epoch": record["posted_epoch"],
                    "story_hash": record["story_hash"],
                    "shopee_product_code": record["shopee_product_code"],
                },
            )
        except Exception as exc:
            console.print(f"[yellow]⚠️ Amaran simpan Vector reddit: {exc}[/yellow]")

        # 3. Upstash Redis berawalan 'reddit' (TTL 60 hari supaya dibenarkan semula selepas 60 hari)
        try:
            cooldown_60d_sec = EXACT_COOLDOWN_DAYS * 86400
            reddit_redis.save_published_post(
                post_uid=f"reddit_{post_uid}",
                post_meta={
                    "source": "reddit",
                    "published_at": record["posted_epoch"],
                    "posted_at_iso": record["posted_at_iso"],
                    "reddit_post_id": record["reddit_post_id"],
                    "subreddit": record["subreddit"],
                    "fb_post_id": record["fb_post_id"],
                    "fb_comment_id": record["fb_comment_id"],
                    "mood_id": record["mood_id"],
                    "shopee_product_code": record["shopee_product_code"],
                    "shopee_url": record["shopee_url"],
                    "image_sha256_list": img_hashes,
                    "b2_media": b2_uploaded_media,
                },
                dedup_fingerprint=f"reddit_story:{record['story_hash']}",
                dedup_ttl_seconds=cooldown_60d_sec,
            )

            reddit_redis.mark_dedup_fingerprint(
                f"reddit_id:{record['reddit_post_id']}",
                post_uid=f"reddit_{post_uid}",
                ttl_seconds=cooldown_60d_sec,
            )
            reddit_redis.mark_dedup_fingerprint(
                f"title_sha256:{title_sha256}",
                post_uid=f"reddit_{post_uid}",
                ttl_seconds=cooldown_60d_sec,
            )
            for sha in img_hashes:
                reddit_redis.mark_dedup_fingerprint(
                    f"img_sha256:{sha}",
                    post_uid=f"reddit_{post_uid}",
                    ttl_seconds=cooldown_60d_sec,
                )

            reddit_redis.push_rolling_history(
                record={
                    "post_uid": post_uid,
                    "reddit_post_id": record["reddit_post_id"],
                    "subreddit": record["subreddit"],
                    "reddit_title": record["reddit_title"],
                    "posted_at_iso": record["posted_at_iso"],
                    "posted_epoch": record["posted_epoch"],
                    "persona_ref": record["persona_ref"],
                    "persona_cycle_id": record.get("persona_cycle_id", ""),
                    "mood_id": record["mood_id"],
                    "story_bm": record["story_bm"],
                    "story_hash": record["story_hash"],
                    "shopee_product_code": record["shopee_product_code"],
                    "shopee_url": record["shopee_url"],
                    "comment_variant_idx": record["comment_variant_idx"],
                },
                stream="reddit",
                max_items=20,
                also_push_global=True,
            )

            reddit_redis.increment_rotation_score("reddit_shopee_products", record["shopee_product_code"])
            reddit_redis.increment_rotation_score("reddit_comment_variants", f"rvar_{record['comment_variant_idx']:02d}")
            reddit_redis.increment_rotation_score("reddit_moods", record["mood_id"])
        except Exception as exc:
            console.print(f"[yellow]⚠️ Amaran simpan Redis reddit: {exc}[/yellow]")

        # 4. Arkib JSON ke Backblaze B2 via Cloudflare Proxy
        if getattr(cfg, "CF_WORKER_B2_PROXY_STORAGE", "") and getattr(cfg, "PS2N_PROXY_SECRET", ""):
            try:
                month_tag = datetime.now(tz=MYT).strftime("%Y%m")
                tmp_log = TEMP_DIR / f"x13_log_{record['posted_epoch']}.json"
                tmp_log.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
                sig = inspect.signature(b2_proxy.upload_file)
                params = list(sig.parameters.keys())
                remote_key = f"logs/reddit_posts/{month_tag}/{tmp_log.name}"
                if "local_path" in params and "remote_path" in params:
                    b2_proxy.upload_file(local_path=tmp_log, remote_path=remote_key)
                elif "file_path" in params and "remote_path" in params:
                    b2_proxy.upload_file(file_path=tmp_log, remote_path=remote_key)
                else:
                    b2_proxy.upload_file(tmp_log, remote_key)
                tmp_log.unlink(missing_ok=True)
            except Exception:
                pass


# ==============================================================================
# 3. ENJIN PENARIKAN REDDIT 4-LAPISAN & EKSTRAKSI KOMEN / GAMBAR
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
    return re.sub(r"\s+", " ", text).strip()[:max_chars]


def extract_reddit_image_candidates(post_data: Dict[str, Any], max_images: int = 4) -> List[Dict[str, Any]]:
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
    return round(eng_pts + img_pts + hook_pts + body_pts, 1)


def normalize_raw_submission(p: Dict[str, Any], engine_name: str) -> Optional[Dict[str, Any]]:
    if not isinstance(p, dict):
        return None
    title = str(p.get("title") or "").strip()
    author = str(p.get("author") or "").strip()
    if not title or p.get("over_18") or p.get("stickied") or author in ("[deleted]", "AutoModerator"):
        return None

    low_t = title.lower()
    if any(re.search(pat, low_t) for pat in LOW_STORY_TITLE_PATTERNS):
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


def harvest_reddit_ps2_candidates() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    merged_by_id: Dict[str, Dict[str, Any]] = {}
    stats_list: List[Dict[str, Any]] = []

    t0 = time.perf_counter()
    e1_posts: List[Dict[str, Any]] = []
    e1_code = 0
    for sub in CORE_PS2_SUBS:
        url = f"https://arctic-shift.photon-reddit.com/api/posts/search?subreddit={sub}&limit=25&sort=desc"
        code, body, _ = _fetch_url_text(url, REDDIT_APP_HEADERS, timeout=16.0)
        e1_code = code
        if code == 200 and body:
            try:
                for it in json.loads(body).get("data", []):
                    norm = normalize_raw_submission(it, "E1_ARCTIC_SHIFT")
                    if norm:
                        e1_posts.append(norm)
            except Exception:
                pass
        time.sleep(0.35)
    stats_list.append({
        "engine": "1. Arctic Shift Mirror API",
        "http": e1_code,
        "ms": round((time.perf_counter() - t0) * 1000, 1),
        "count": len(e1_posts),
        "status": "OK" if e1_posts else "GAGAL",
    })
    for p in e1_posts:
        merged_by_id[p["post_id"]] = p

    t1 = time.perf_counter()
    e2_posts: List[Dict[str, Any]] = []
    e2_code = 0
    for sub in ("ps2", "ps2homebrew"):
        url = f"https://api.pullpush.io/reddit/search/submission/?subreddit={sub}&size=25&sort=desc&sort_type=created_utc"
        code, body, _ = _fetch_url_text(url, REDDIT_APP_HEADERS, timeout=16.0)
        e2_code = code
        if code == 200 and body:
            try:
                for it in json.loads(body).get("data", []):
                    norm = normalize_raw_submission(it, "E2_PULLPUSH_API")
                    if norm:
                        e2_posts.append(norm)
            except Exception:
                pass
        time.sleep(0.35)
    stats_list.append({
        "engine": "2. PullPush Mirror API",
        "http": e2_code,
        "ms": round((time.perf_counter() - t1) * 1000, 1),
        "count": len(e2_posts),
        "status": "OK" if e2_posts else "GAGAL",
    })
    for p in e2_posts:
        if p["post_id"] not in merged_by_id:
            merged_by_id[p["post_id"]] = p

    if len(merged_by_id) < 10:
        multi = "+".join(CORE_PS2_SUBS)
        for tag, url in (
            ("E3_WWW_JSON", f"https://www.reddit.com/r/{multi}/hot.json?limit=25&raw_json=1"),
            ("E3_OLD_JSON", f"https://old.reddit.com/r/{multi}/hot.json?limit=25&raw_json=1"),
        ):
            t2 = time.perf_counter()
            code, body, _ = _fetch_url_text(url, REDDIT_APP_HEADERS, timeout=12.0)
            added = 0
            if code == 200 and body:
                try:
                    for ch in json.loads(body).get("data", {}).get("children", []):
                        norm = normalize_raw_submission(ch.get("data", {}), tag)
                        if norm and norm["post_id"] not in merged_by_id:
                            merged_by_id[norm["post_id"]] = norm
                            added += 1
                except Exception:
                    pass
            stats_list.append({
                "engine": f"3. {tag}",
                "http": code,
                "ms": round((time.perf_counter() - t2) * 1000, 1),
                "count": added,
                "status": "OK" if added else "FALLBACK",
            })
            if added > 0:
                break

    if len(merged_by_id) < 5:
        multi = "+".join(CORE_PS2_SUBS)
        t3 = time.perf_counter()
        rss_url = f"https://www.reddit.com/r/{multi}/hot.rss?limit=25"
        code_r, xml_b, _ = _fetch_url_text(rss_url, REDDIT_APP_HEADERS, timeout=12.0)
        rss_added = 0
        if code_r == 200 and xml_b:
            try:
                root = ET.fromstring(xml_b.encode("utf-8", errors="ignore"))
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
                    slots = [{"urls": [html.unescape(u).replace("&amp;", "&")]} for u in dict.fromkeys(img_urls)][:4]
                    pid = id_el.text.split("_")[-1] if id_el is not None and id_el.text else f"rss_{idx}"
                    if pid not in merged_by_id:
                        cand = {
                            "post_id": pid,
                            "subreddit": cat_el.attrib.get("term", "ps2") if cat_el is not None else "ps2",
                            "title": title,
                            "cleaned_text": clean_reddit_body(raw_html),
                            "image_slots": slots,
                            "image_count": len(slots),
                            "score": max(200 - idx * 8, 25),
                            "num_comments": 10,
                            "author": "reddit_user",
                            "permalink": l_el.attrib.get("href", "") if l_el is not None else "",
                            "source_engine": "E4_MULTI_RSS",
                        }
                        cand["curation_score"] = score_ps2_candidate(cand)
                        merged_by_id[pid] = cand
                        rss_added += 1
            except Exception:
                pass
        stats_list.append({
            "engine": "4. Multireddit Atom RSS",
            "http": code_r,
            "ms": round((time.perf_counter() - t3) * 1000, 1),
            "count": rss_added,
            "status": "OK" if rss_added else "FALLBACK",
        })

    return list(merged_by_id.values()), stats_list


def fetch_top_reddit_comments(subreddit: str, post_id: str, max_comments: int = 5) -> List[str]:
    if not post_id or post_id.startswith("rss_"):
        return []

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
                return out_as
        except Exception:
            pass

    for host in ("www.reddit.com", "old.reddit.com"):
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
                        return out
            except Exception:
                pass
    return []


# ==============================================================================
# 4. MUAT TURUN 1-4 GAMBAR ANTI-403 & SANDARAN UNSPLASH
# ==============================================================================
def _download_single_image_resilient(candidate_urls: List[str]) -> Optional[bytes]:
    for raw_u in candidate_urls:
        clean_u = html.unescape(raw_u).strip().replace("&amp;", "&")
        if not clean_u.startswith(("http://", "https://")):
            continue

        try:
            with httpx.Client(headers=IMAGE_FETCH_HEADERS, follow_redirects=True, timeout=18.0) as client:
                r = client.get(clean_u)
                if r.status_code == 200 and len(r.content) > 2048:
                    return r.content
        except Exception:
            pass

        try:
            h2 = dict(IMAGE_FETCH_HEADERS)
            h2["Referer"] = "https://www.reddit.com/"
            r2 = requests.get(clean_u, headers=h2, timeout=18.0, allow_redirects=True)
            if r2.status_code == 200 and len(r2.content) > 2048:
                return r2.content
        except Exception:
            pass

        try:
            proxy_u = f"https://wsrv.nl/?url={quote(clean_u, safe='')}&w=1280&output=jpg"
            with httpx.Client(headers=IMAGE_FETCH_HEADERS, follow_redirects=True, timeout=22.0) as client:
                r3 = client.get(proxy_u)
                if r3.status_code == 200 and len(r3.content) > 2048:
                    return r3.content
        except Exception:
            pass

    return None


def download_reddit_images(image_slots: List[Dict[str, Any]], post_id: str, max_images: int = 4) -> List[Path]:
    saved_paths: List[Path] = []
    for idx, slot in enumerate(image_slots[:max_images], 1):
        img_bytes = _download_single_image_resilient(slot.get("urls") or [])
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
    api_key = getattr(cfg, "PS2N_UNSPLASH_ACCESS_KEY", "") or os.getenv("PS2N_UNSPLASH_ACCESS_KEY", "")
    if not api_key:
        local_pool = PROJECT_ROOT / "temp" / "test_media_pool_50" / "raw"
        return sorted(local_pool.glob("*.jpg"))[:count] if local_pool.exists() else []

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
            params={"query": query, "per_page": 10, "orientation": "landscape"},
            timeout=20,
        )
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            random.shuffle(results)
            for idx, item in enumerate(results[: count + 4], 1):
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
# 5. PENAPIS SEMANTIK VECTOR & OLAHAN PERSONA AI SANTAI (500 - 750 AKSARA)
# ==============================================================================
def check_reddit_semantic_cooldown(
    candidate_story: str,
    routing_topic: str,
    recent_posts: List[Dict[str, Any]],
    now_epoch: int,
) -> Tuple[bool, str, int]:
    """
    Menyemak jarak ulangan ayat:
    - Ayat 100% / >= 95% sama -> disekat selama 60 hari (dibenarkan selepas 60 hari)
    - Ayat 78% sama -> disekat selama 5 hari
    """
    cand_norm = re.sub(r"\s+", " ", candidate_story.strip().lower())
    cand_hash = hashlib.sha256(cand_norm.encode("utf-8")).hexdigest()
    cand_tokens = set(re.findall(r"\b[a-z0-9]{3,}\b", cand_norm))

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
            if jaccard >= 0.78 and age_days < SIMILAR_COOLDOWN_DAYS:
                return False, f"Ditolak: Pertindihan kata {jaccard*100:.0f}% dalam tempoh {age_days:.1f} hari (had 5 hari).", 0

    try:
        is_dup, dup_match, vec_shard = reddit_vector.check_semantic_duplicate(
            game_title=f"reddit:{routing_topic[:60]}",
            candidate_story=candidate_story,
            similarity_threshold=SIMILAR_MATCH_THRESHOLD,
            exact_threshold=EXACT_MATCH_THRESHOLD,
            exact_cooldown_days=EXACT_COOLDOWN_DAYS,
            similar_cooldown_days=SIMILAR_COOLDOWN_DAYS,
            now_epoch=now_epoch,
        )
        if is_dup and isinstance(dup_match, dict):
            reason = dup_match.get("block_reason") or "Persamaan tinggi dalam tempoh sekatan."
            return False, f"Ditolak Vector Reddit (#{vec_shard}): {reason}", vec_shard
        return True, f"Lulus Semantik Vector Reddit (Shard #{vec_shard})", vec_shard
    except Exception as exc:
        return True, f"Lulus (Amaran Vector: {exc})", 0


def polish_malay_story_phrasing(text: str, self_ref: str, cal_ctx: Dict[str, Any]) -> str:
    """Membersihkan frasa terjemahan janggal sambil mengekalkan nada santai semula jadi."""
    text = re.sub(r"^(cikgu|tuan|puan|bos)[,\s:!]+", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\baku\b", self_ref, text, flags=re.IGNORECASE)
    text = re.sub(r"\bsaya\b", self_ref, text, flags=re.IGNORECASE)
    text = re.sub(rf"(^|[.!?]\s+){self_ref}\b", lambda m: f"{m.group(1)}{self_ref.capitalize()}", text)

    awkward_fixes = {
        r"\bborak dan pelik\b": "renyah dan susah payah",
        r"\blambat mendongak\b": "mengambil masa agak lama",
        r"\bperangai pemilihan\b": "saingan undian",
        r"\bmelakar diri\b": "muncul",
        r"\btengam\b": "tengok",
        r"\binternet lautan\b": "internet laju",
    }
    for pat, rep in awkward_fixes.items():
        text = re.sub(pat, rep, text, flags=re.IGNORECASE)

    if cal_ctx.get("period") != "Tengah Malam":
        text = re.sub(r"\btengah malam ni\b", "malam ni", text, flags=re.IGNORECASE)

    text = re.sub(r"[^\x20-\x7E\n]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def validate_relaxed_story_quality(
    text: str,
    min_len: int,
    max_len: int,
    cal_ctx: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """
    Guardrail ringan supaya AI bebas bercerita dengan santai (500 - 750 aksara):
    - Membenarkan tanda baca dan gaya sembang harian tanpa sekatan ketat.
    - Hanya menyekat teks terlalu pendek/panjang, spam pengulangan melampau,
      atau kebocoran pautan URL/harga RM di dalam kapsyen utama.
    """
    if not text or len(text) < min_len:
        return False, f"Teks terlalu pendek ({len(text)}/{min_len} aksara)."
    if len(text) > max_len:
        return False, f"Teks melebihi had ({len(text)}/{max_len} aksara)."

    low = text.lower()
    if "http://" in low or "https://" in low or "shopee.com" in low:
        return False, "Dikesan pautan URL dalam kapsyen utama."
    if re.search(r"\brm\s?\d+", low):
        return False, "Dikesan tanda harga RM dalam kapsyen utama."

    words = re.findall(r"\b[a-z]{4,}\b", low)
    freq: Dict[str, int] = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
        if freq[w] > 12:
            return False, f"Glitch pengulangan melampau pada perkataan '{w}'."

    if cal_ctx and cal_ctx.get("forbidden_regex"):
        bad_time = re.search(cal_ctx["forbidden_regex"], low)
        if bad_time:
            return False, f"Ditolak: Perkataan waktu bercanggah dengan waktu {cal_ctx.get('period')} MYT ('{bad_time.group(0)}')."

    return True, "Lulus Santai"


def generate_reddit_persona_post(
    reddit_post: Dict[str, Any],
    top_comments: List[str],
    vision_items: List[Dict[str, Any]],
    image_source_type: str,
    chosen_mood: Dict[str, str],
    product_rec: Dict[str, Any],
    recent_posts: List[Dict[str, Any]],
    brain_ctx: Dict[str, Any],
    cal_ctx: Dict[str, Any],
) -> Tuple[bool, Dict[str, Any]]:
    self_ref = random.choice(["admin", "min"])
    persona_cycle = cal_ctx["persona_cycle"]

    ocr_notes: List[str] = []
    for idx, v in enumerate(vision_items, 1):
        objs = ", ".join(v.get("detected_objects", [])) or "perkakasan PS2"
        ocr_t = str(v.get("ocr_text", "None")).strip()
        angle = str(v.get("retro_nostalgia_angle", "")).strip()
        ocr_notes.append(f"- Visual {idx}: {angle} (Objek: {objs} | OCR: {ocr_t})")
    vision_summary = "\n".join(ocr_notes) if ocr_notes else "- Visual perkakasan klasik PlayStation 2."

    comments_block = (
        "\n".join(f"- {c}" for c in top_comments[:4])
        if top_comments
        else "- Ramai peminat PS2 berkongsi pengalaman mengimbau era 2000-an."
    )

    game_fact_line = (
        f"\nFAKTA KATALOG GAME (.DB): {brain_ctx['matched_game_fact']}"
        if brain_ctx.get("matched_game_fact")
        else ""
    )

    system_prompt = (
        f"Kau ialah '{self_ref}', seorang lelaki Malaysia berumur 35 tahun yang santai, berilmu, "
        "dan mengendalikan halaman Facebook 'PS2 Retro Nostalgia'.\n\n"
        f"WATAK KITARAN 2-HARI ({persona_cycle['title']}):\n"
        f"{persona_cycle['style_note']}\n"
        f"{persona_cycle['one_shot_anchor']}\n\n"
        f"MOOD SESI INI ({chosen_mood['label']}):\n"
        f"{chosen_mood['instruction']}\n\n"
        "PANDUAN BERCERITA SANTAI (500 - 750 AKSARA):\n"
        "1. Gabungkan intipati perbincangan komuniti luar negara dengan apa yang dilihat pada GAMBAR & TEKS OCR.\n"
        "2. Tulis macam abang-abang bersembang santai di kedai kopi dalam Bahasa Melayu Malaysia yang natural "
        "(JANGAN terjemah ayat Inggeris perkataan demi perkataan).\n"
        f"3. Bahasakan diri sebagai '{self_ref}' (elakkan 'aku' atau 'saya') dan sapa pembaca sebagai 'korang' atau 'geng'.\n"
        f"4. SEKARANG IALAH WAKTU {cal_ctx['period'].upper()} DI MALAYSIA ({cal_ctx['full_timestamp']}). "
        f"Jika mahu sebut waktu, HANYA guna ungkapan sesuai seperti '{cal_ctx['allowed_time_cues']}'. "
        f"HARAM sebut waktu bercanggah ({cal_ctx['forbidden_times']}).\n"
        f"5. KONTEKS SUASANA & PERAYAAN MALAYSIA: {cal_ctx['special_occasion']}\n"
        "5. Selitkan secara bersahaja (soft-sell) kelebihan menghidupkan semula PS2 lama guna pendrive/hardisk atau mod card "
        "supaya pembaca tertarik menyemak ruangan komen.\n"
        "6. Panjang cerita antara 520 hingga 720 aksara, tanpa emoji, tanpa tajuk di baris pertama, tanpa harga RM atau link, "
        "dan pastikan ayat ditutup dengan noktah (.) yang lengkap."
    )

    user_prompt = (
        f"MASA SIARAN SEBENAR MALAYSIA: {cal_ctx['full_timestamp']}\n"
        f"SUASANA & KALENDAR MALAYSIA: {cal_ctx['special_occasion']}\n"
        f"SUMBER IDEA KOMUNITI (r/{reddit_post['subreddit']}):\n"
        f"- Tajuk Asal: {reddit_post['title']}\n"
        f"- Huraian Asal: {reddit_post['cleaned_text'][:650] or 'Perkongsian visual setup & koleksi PS2.'}\n"
        f"- Reaksi Komen Komuniti:\n{comments_block}{game_fact_line}\n\n"
        f"DAPATAN VISION & OCR DARIPADA GAMBAR LAMPIRAN ({image_source_type}):\n"
        f"{vision_summary}\n\n"
        f"KONTEKS PRODUK KOMEN PERTAMA (selitan halus sahaja): {product_rec['product_name']}\n\n"
        f"REKOD TOPIK LEPAS (JANGAN ULANG SUDUT & PEMBUKA YANG SAMA):\n"
        f"{brain_ctx['recent_topic_summary']}\n\n"
        f"Tulis cerita santai daripada sudut pandang '{self_ref}' sepanjang 520 hingga 720 aksara tanpa emoji:"
    )

    last_meta: Dict[str, Any] = {}
    for attempt in range(1, 5):
        adj_temp = min(0.86, round(STORY_TEMPERATURE + ((attempt - 1) * 0.04), 2))
        inf = llm_engine.generate_chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=adj_temp,
            max_tokens=STORY_MAX_TOKENS,
        )

        cleaned = clean_and_scrub_bm_story(inf["raw_text"], max_chars=STORY_SOFT_TRIM)
        cleaned = polish_malay_story_phrasing(cleaned, self_ref=self_ref, cal_ctx=cal_ctx)

        sub_tag = re.sub(r"[^a-zA-Z0-9]", "", reddit_post["subreddit"]).upper()
        hashtags = f"#PS2RetroNostalgia #PS2Malaysia #RetroGaming #PlayStation2 #{sub_tag} #Nostalgia2000an"
        post_caption = f"{cleaned}\n\n{hashtags}"

        # Elakkan pembuka 5 perkataan pertama sebiji sama dengan post lepas
        cand_open_5 = re.findall(r"\b[a-z0-9]{3,}\b", cleaned.lower())[:5]
        opening_cloned = False
        for prev in recent_posts[:10]:
            prev_open_5 = re.findall(r"\b[a-z0-9]{3,}\b", str(prev.get("story_bm", "")).lower())[:5]
            if len(cand_open_5) >= 4 and cand_open_5[:4] == prev_open_5[:4]:
                opening_cloned = True
                break
        if opening_cloned:
            last_meta = {
                "attempt": attempt,
                "elapsed_sec": inf["elapsed_sec"],
                "tok_per_sec": inf["tok_per_sec"],
                "story_chars": len(cleaned),
                "status": "Ditolak: 4 perkataan pembuka sama dengan post terdahulu.",
            }
            continue

        ok_q, q_msg = validate_relaxed_story_quality(cleaned, min_len=STORY_MIN_CHARS, max_len=STORY_MAX_CHARS, cal_ctx=cal_ctx)
        if not ok_q:
            last_meta = {
                "attempt": attempt,
                "elapsed_sec": inf["elapsed_sec"],
                "tok_per_sec": inf["tok_per_sec"],
                "story_chars": len(cleaned),
                "status": q_msg,
            }
            continue

        ok_sem, sem_msg, vec_shard = check_reddit_semantic_cooldown(
            candidate_story=cleaned,
            routing_topic=reddit_post["title"],
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
                "story_hash": story_hash,
                "vector_shard": vec_shard,
                "ocr_summary": vision_summary,
                "metrics": last_meta,
            }

    return False, {"error": "Gagal menjana olahan cerita sah selepas 4 percubaan.", "metrics": last_meta}


# ==============================================================================
# 6. PENERBIT FACEBOOK GRAPH API v26.0 (1 FOTO / MULTI-FOTO + KOMEN PERTAMA)
# ==============================================================================
class FacebookRedditPhotoPublisher:
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

    def _upload_single_photo(self, img_path: Path, published: bool, caption: str = "") -> Dict[str, Any]:
        url = f"{self.base_url}/{self.page_id}/photos"
        data = self._auth_params()
        data["published"] = "true" if published else "false"
        if published and caption:
            data["caption"] = caption

        with open(img_path, "rb") as f:
            resp = requests.post(
                url,
                data=data,
                files={"source": (img_path.name, f, "image/jpeg")},
                timeout=45,
            )
        payload = resp.json()
        if resp.status_code != 200 or "error" in payload:
            raise RuntimeError(f"Gagal muat naik foto {img_path.name}: {json.dumps(payload)}")
        return payload

    def publish_photos_and_comment(
        self,
        image_paths: List[Path],
        post_caption: str,
        comment_text: str,
    ) -> Dict[str, str]:
        if len(image_paths) == 1:
            res = self._upload_single_photo(image_paths[0], published=True, caption=post_caption)
            post_id = str(res.get("post_id") or res.get("id") or "")
        else:
            attached_media: List[Dict[str, str]] = []
            for p in image_paths[:4]:
                up_res = self._upload_single_photo(p, published=False)
                media_id = str(up_res.get("id", ""))
                if media_id:
                    attached_media.append({"media_fbid": media_id})

            if not attached_media:
                raise RuntimeError("Tiada media_fbid berjaya dimuat naik untuk album multi-foto.")

            feed_url = f"{self.base_url}/{self.page_id}/feed"
            feed_payload: Dict[str, Any] = self._auth_params()
            feed_payload["message"] = post_caption
            feed_payload["attached_media"] = attached_media

            feed_resp = requests.post(feed_url, json=feed_payload, timeout=45)
            feed_data = feed_resp.json()
            if feed_resp.status_code != 200 or "error" in feed_data:
                raise RuntimeError(f"Gagal terbit multi-foto feed: {json.dumps(feed_data)}")
            post_id = str(feed_data.get("id", ""))

        if not post_id:
            raise RuntimeError("Meta Graph API tidak memulangkan post_id.")

        time.sleep(2.5)
        comment_url = f"{self.base_url}/{post_id}/comments"
        c_payload = self._auth_params()
        c_payload["message"] = comment_text

        c_resp = requests.post(comment_url, data=c_payload, timeout=30)
        c_data = c_resp.json()
        if c_resp.status_code != 200 or "error" in c_data:
            raise RuntimeError(f"Hantaran berjaya ({post_id}) tetapi gagal hantar komen: {json.dumps(c_data)}")

        return {"post_id": post_id, "comment_id": str(c_data.get("id", ""))}


def main() -> None:
    parser = argparse.ArgumentParser(description="X13: Reddit PS2 + Vision OCR + FB Auto-Poster")
    parser.add_argument("--dry-run", action="store_true", help="Uji penjanaan tanpa post ke Facebook sebenar.")
    parser.add_argument("--force-unsplash", action="store_true", help="Gunakan sandaran Unsplash untuk media.")
    args = parser.parse_args()

    is_ci = os.getenv("GITHUB_ACTIONS", "").lower() == "true"
    run_env = "GITHUB_ACTIONS" if is_ci else "LOCAL_WSL"

    lock_token: Optional[str] = None
    if not args.dry_run:
        lock_token = reddit_redis.acquire_lock("x13_reddit_fb_post", ttl_seconds=480)
        if not lock_token:
            console.print("[bold yellow]⚠️ Proses X13 lain sedang berjalan. Menghentikan larian ini bagi mengelak post berganda.[/bold yellow]")
            return

    try:
        cal_ctx = get_myt_calendar_context()
        persona_cycle = cal_ctx["persona_cycle"]
        coordinator = RedditPostCoordinator()
        recent_posts = coordinator.get_recent_reddit_posts(limit=12)

        console.print(
            Panel.fit(
                "[bold cyan]PS2-RETRO-NOSTALGIA :: REDDIT PS2 + VISION OCR + FB PUBLISHER (X13)[/bold cyan]\n"
                f"[dim]Persekitaran: {run_env} | {cal_ctx['full_timestamp']}\n"
                f"Watak 2-Hari: {persona_cycle['title']} ({persona_cycle['persona_id']}) | Memori Reddit: {len(recent_posts)} rekod[/dim]",
                border_style="cyan",
            )
        )

        # 1. Pilih Mood Persona AI secara bergilir mengikut waktu & sejarah
        last_mood = recent_posts[0].get("mood_id", "") if recent_posts else ""
        slot_moods = [
            m for m in REDDIT_PERSONA_MOODS
            if cal_ctx["period"] in m["preferred_slots"] and m["mood_id"] != last_mood
        ]
        if not slot_moods:
            slot_moods = [m for m in REDDIT_PERSONA_MOODS if m["mood_id"] != last_mood] or REDDIT_PERSONA_MOODS

        mood_by_id = {m["mood_id"]: m for m in slot_moods}
        picked_mood_id = reddit_redis.pick_least_used_item(
            pool_name="reddit_moods",
            candidate_ids=list(mood_by_id.keys()),
            exclude_ids=[last_mood] if last_mood else [],
        )
        chosen_mood = mood_by_id.get(picked_mood_id or "", random.choice(slot_moods))

        # 2. Tarik Calon Pos Reddit & Tapis yang Sudah Diguna dalam Tempoh 60 Hari
        with console.status("[bold cyan]Menarik calon pos PS2 dari komuniti Reddit & Mirror API...[/bold cyan]"):
            all_cands, _ = harvest_reddit_ps2_candidates()

        if not all_cands:
            raise RuntimeError("Tiada pos sah berjaya ditarik daripada mana-mana sumber Reddit PS2.")

        fresh_cands: List[Dict[str, Any]] = []
        for c in all_cands:
            is_used, _ = coordinator.is_reddit_post_used_within_cooldown(
                reddit_post_id=c["post_id"],
                reddit_title=c["title"],
                now_epoch=cal_ctx["now_epoch"],
            )
            if not is_used:
                fresh_cands.append(c)

        working_cands = fresh_cands if fresh_cands else all_cands
        working_cands.sort(
            key=lambda x: (1 if x["image_count"] > 0 else 0, x["curation_score"], x["num_comments"]),
            reverse=True,
        )

        # 3. Pilih Calon + Muat Turun Gambar + Semak 100% Anti-Duplikasi Gambar SHA-256 (60 Hari)
        chosen_post: Optional[Dict[str, Any]] = None
        local_images: List[Path] = []
        image_sha256_list: List[str] = []
        image_source_type = "REDDIT_ORIGINAL"

        candidate_pool = working_cands[: min(10, len(working_cands))]
        random.shuffle(candidate_pool[: min(4, len(candidate_pool))])

        v_items: List[Dict[str, Any]] = []
        v_meta: Dict[str, Any] = {}
        try:
            for cand in candidate_pool:
                cand_imgs: List[Path] = []
                cand_src_type = "REDDIT_ORIGINAL"

                if not args.force_unsplash and cand["image_slots"]:
                    cand_imgs = download_reddit_images(
                        cand["image_slots"],
                        post_id=cand["post_id"],
                        max_images=4,
                    )

                if not cand_imgs:
                    cand_src_type = "UNSPLASH_FALLBACK"
                    cand_imgs = fetch_unsplash_fallback_images(cand["title"], count=2)

                if not cand_imgs:
                    continue

                dup_img, img_hashes, dup_reason = coordinator.are_images_used_within_60_days(
                    cand_imgs,
                    now_epoch=cal_ctx["now_epoch"],
                )
                if dup_img:
                    console.print(f"[yellow]⏭️ Langkau r/{cand['subreddit']} ({cand['post_id']}): {dup_reason}[/yellow]")
                    for p_del in cand_imgs:
                        p_del.unlink(missing_ok=True)
                    continue

                with console.status(f"[bold cyan]Menganalisis {len(cand_imgs)} gambar r/{cand['subreddit']} (Vision + OCR X07 < 30KB)...[/bold cyan]"):
                    ok_v, items_v, meta_v = vision_analyzer.analyze_images(
                        cand_imgs,
                        context_hint=cand["title"],
                        auto_unload=False,
                        skip_failed=True,
                    )

                if not ok_v or not items_v:
                    console.print(f"[yellow]⏭️ Langkau r/{cand['subreddit']} ({cand['post_id']}): Vision/OCR tidak sah, mencuba calon seterusnya...[/yellow]")
                    for p_del in cand_imgs:
                        p_del.unlink(missing_ok=True)
                    continue

                chosen_post = cand
                local_images = cand_imgs
                image_sha256_list = img_hashes
                image_source_type = cand_src_type
                v_items = items_v
                v_meta = meta_v
                break
        finally:
            vision_analyzer.unload()

        if chosen_post is None or not local_images or not v_items:
            raise RuntimeError("Tiada calon pos dengan gambar unik & analisis Vision/OCR sah ditemui pada sesi ini.")

        top_comments = fetch_top_reddit_comments(chosen_post["subreddit"], chosen_post["post_id"], max_comments=5)

        if image_source_type == "UNSPLASH_FALLBACK":
            valid_imgs: List[Path] = []
            valid_hashes: List[str] = []
            valid_v: List[Dict[str, Any]] = []
            for idx, v_obj in enumerate(v_items):
                if v_obj.get("is_ps2_relevant", True) and not v_obj.get("has_human_face", False):
                    if idx < len(local_images):
                        valid_imgs.append(local_images[idx])
                        if idx < len(image_sha256_list):
                            valid_hashes.append(image_sha256_list[idx])
                        valid_v.append(v_obj)
            if valid_imgs:
                local_images = valid_imgs
                image_sha256_list = valid_hashes
                v_items = valid_v

        # 5. Baca Otak Memori (.db) + Padankan Produk Shopee + Olah Cerita Santai (500-750 Aksara)
        combined_topic = f"{chosen_post['title']} {chosen_post['cleaned_text']} {' '.join(top_comments)}"
        brain_ctx = coordinator.fetch_brain_context_from_databases(combined_topic, recent_posts=recent_posts)
        product_rec = coordinator.pick_rotating_shopee_product(combined_topic, recent_posts=recent_posts)
        comment_text, comment_var_idx = coordinator.build_rotating_first_comment(
            product=product_rec,
            recent_posts=recent_posts,
        )

        plan_tbl = Table(title="1. Pelan Kandungan Reddit + Vision OCR + Shopee", box=box.ROUNDED, header_style="bold yellow")
        plan_tbl.add_column("Komponen", style="bold white")
        plan_tbl.add_column("Pilihan Sesi Ini", style="cyan")
        plan_tbl.add_row("Subreddit & Sumber", f"r/{chosen_post['subreddit']} ({chosen_post['source_engine']})")
        plan_tbl.add_row("Tajuk Idea Reddit", chosen_post["title"][:72])
        plan_tbl.add_row("Watak 2-Hari & Mood", f"{persona_cycle['persona_id']} | {chosen_mood['mood_id']}")
        plan_tbl.add_row("Media & SHA-256", f"{len(local_images)} gambar ({image_source_type}) | Unik 60-Hari")
        plan_tbl.add_row("Produk Shopee", f"{product_rec['product_code']} | {product_rec['compatibility']} | {product_rec['shopee_url']}")
        plan_tbl.add_row("Variasi Komen", f"#{comment_var_idx + 1} daripada {len(REDDIT_COMMENT_VARIATIONS_20)} templat")
        console.print(plan_tbl)

        with console.status("[bold cyan]Mengolah cerita santai Persona AI (500-750 aksara)...[/bold cyan]"):
            ok_gen, gen_pkg = generate_reddit_persona_post(
                reddit_post=chosen_post,
                top_comments=top_comments,
                vision_items=v_items,
                image_source_type=image_source_type,
                chosen_mood=chosen_mood,
                product_rec=product_rec,
                recent_posts=recent_posts,
                brain_ctx=brain_ctx,
                cal_ctx=cal_ctx,
            )
        llm_engine.unload()

        if not ok_gen:
            raise RuntimeError(f"Gagal menjana olahan cerita sah: {gen_pkg}")

        bm = gen_pkg["metrics"]
        console.print(
            Panel(
                f"[bold yellow]KAPSYEN HANTARAN FB ({len(local_images)} Gambar | Persona: {gen_pkg['persona_ref']} | "
                f"{gen_pkg['story_chars']} aksara | Sifar Emoji):[/bold yellow]\n"
                f"[white]{gen_pkg['post_caption']}[/white]\n\n"
                f"[bold green]KOMEN PERTAMA AUTOMATIK (Variasi #{comment_var_idx + 1} | {product_rec['product_code']}):[/bold green]\n"
                f"[cyan]{comment_text}[/cyan]\n\n"
                f"[dim]Metrik LLM: {bm['elapsed_sec']}s ({bm['tok_per_sec']} tok/s, cubaan #{bm['attempt']}) | {bm['status']}[/dim]",
                title="2. Pratonton Hantaran & Komen Pertama",
                border_style="green",
            )
        )

        # 6. Terbit ke Facebook Page & Hantar Komen Pertama
        fb_res = {"post_id": "DRY_RUN", "comment_id": "DRY_RUN"}
        if not args.dry_run:
            with console.status("[bold cyan]Menerbitkan foto & cerita ke Facebook Page + komen Shopee...[/bold cyan]"):
                publisher = FacebookRedditPhotoPublisher()
                fb_res = publisher.publish_photos_and_comment(
                    image_paths=local_images,
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

        post_uid = f"x13_{cal_ctx['now_epoch']}_{gen_pkg['story_hash'][:10]}"
        b2_media_records: List[Dict[str, Any]] = []
        if not args.dry_run:
            b2_media_records = coordinator.upload_media_to_b2_proxy(local_images, post_uid=post_uid)

        final_record = {
            "post_uid": post_uid,
            "reddit_post_id": chosen_post["post_id"],
            "subreddit": chosen_post["subreddit"],
            "reddit_title": chosen_post["title"],
            "reddit_permalink": chosen_post["permalink"],
            "posted_at_iso": cal_ctx["now_iso"],
            "posted_epoch": cal_ctx["now_epoch"],
            "run_env": run_env,
            "persona_ref": gen_pkg["persona_ref"],
            "persona_cycle_id": persona_cycle["persona_id"],
            "pillar_id": f"{persona_cycle['persona_id']}:{chosen_mood['mood_id']}",
            "mood_id": chosen_mood["mood_id"],
            "game_title": f"r/{chosen_post['subreddit']}: {chosen_post['title'][:55]}",
            "image_source_type": image_source_type,
            "image_count": len(local_images),
            "image_sha256_list": image_sha256_list,
            "ocr_summary": gen_pkg["ocr_summary"],
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
            "b2_uploaded_media": b2_media_records,
            "llm_metrics": bm,
        }

        if not args.dry_run:
            coordinator.record_published_reddit_post(
                record=final_record,
                post_uid=post_uid,
                b2_uploaded_media=b2_media_records,
            )
            console.print("[bold green]✅ Rekod disimpan ke SQLite (<30MB), Vector (ps2n_reddit), Redis (ps2n_reddit), & B2 Proxy.[/bold green]")

        LAST_OUTPUT_JSON.write_text(json.dumps(final_record, indent=2, ensure_ascii=False), encoding="utf-8")
        console.print(f"[dim]Laporan JSON disimpan di: {LAST_OUTPUT_JSON}[/dim]")
    finally:
        if lock_token:
            reddit_redis.release_lock("x13_reddit_fb_post", lock_token)


if __name__ == "__main__":
    main()