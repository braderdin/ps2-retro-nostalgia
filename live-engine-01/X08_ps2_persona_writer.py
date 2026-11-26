#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - PENULIS PERSONA AI & KATALOG GAME PS2 (X08)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/live-engine-01/X08_ps2_persona_writer.py
#
# FUNGSI UTAMA:
# 1. Persona AI: Lelaki Malaysia anggaran umur 30-an yang membahasakan dirinya
#    "admin" atau "min", bijak dan berpengetahuan luas tentang konsol PS2,
#    perkakasan (Emotion Engine, Memory Card 8MB, FMCB, OPL), dan sejarah game.
# 2. Kawalan Panjang Cerita: Anggaran 500 - 650 aksara (potongan noktah lengkap)
#    supaya ringkas, padat, dan tinggi kadar bacaan di Facebook.
# 3. Pangkalan Data Katalog Game PS2 (SQLite3: data/ps2_games_catalog.db):
#    Menyimpan senarai judul PS2, tahun, genre, dan nota trivia untuk rujukan AI.
#    Sedia menerima import senarai penuh ribuan game PS2 pada bila-bila masa.
# 4. Menyokong format hantaran WITH IMAGE ('photo', 'reel') & TEXT-ONLY ('text'),
#    semakan anti-duplikasi semantik (X05), serta penjanaan komen pertama Shopee (X02).
# ==============================================================================

import hashlib
import importlib
import json
import os
import random
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ------------------------------------------------------------------------------
# IMPORT MODUL TERAS DARI live-engine-01
# ------------------------------------------------------------------------------
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for p in [CURRENT_DIR, PROJECT_ROOT]:
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

cfg: Any = importlib.import_module("X01_config")
_ledger_mod: Any = importlib.import_module("X02_sqlite_ledger")
_vector_mod: Any = importlib.import_module("X05_vector_sharded")
_llm_mod: Any = importlib.import_module("X06_local_llm_engine")

ledger_db: Any = getattr(_ledger_mod, "ledger_db")
vector_sharded: Any = getattr(_vector_mod, "vector_sharded")
llm_engine: Any = getattr(_llm_mod, "llm_engine")

try:
    _redis_mod: Any = importlib.import_module("X04_redis_sharded")
    redis_sharded: Any = getattr(_redis_mod, "redis_sharded", None)
except Exception:
    redis_sharded = None


# ==============================================================================
# [BAHAGIAN TUNING MANUAL PERSONA & PANJANG AYAT FB]
# ==============================================================================
STORY_TARGET_MIN_CHARS: int = int(os.getenv("PS2N_STORY_MIN_CHARS", "440"))
STORY_TARGET_MAX_CHARS: int = int(os.getenv("PS2N_STORY_MAX_CHARS", "660"))
STORY_SOFT_TRIM_CHARS: int = int(os.getenv("PS2N_STORY_TRIM_CHARS", "650"))
STORY_LLM_MAX_TOKENS: int = int(os.getenv("PS2N_STORY_MAX_TOKENS", "360"))
STORY_TEMPERATURE: float = float(os.getenv("PS2N_STORY_TEMP", "0.72"))
SEMANTIC_SIMILARITY_THRESHOLD: float = float(os.getenv("PS2N_SEMANTIC_THRESHOLD", "0.86"))

# Lokasi SQLite3 Katalog Judul Game PS2 (boleh diisi ribuan judul PS2 kemudian)
DATA_DIR: Path = Path(getattr(cfg, "DATA_DIR", PROJECT_ROOT / "data"))
PS2_GAMES_DB_PATH: Path = DATA_DIR / "ps2_games_catalog.db"
# ==============================================================================

MYT = timezone(timedelta(hours=8))

# Koleksi permulaan 25 judul ikonik PS2 di Malaysia (auto-seed jika .db masih kosong)
STARTER_PS2_GAMES: List[Tuple[str, str, int, str, str]] = [
    ("Grand Theft Auto: San Andreas", "Action-Adventure", 2004, "Rockstar North", "Kod cheat HESOYAM & AEZAKMI ditulis atas kertas buku latihan sekolah, misi kapal terbang kecil Zero yang buat ramai putus asa."),
    ("Winning Eleven 8", "Sports / Football", 2004, "Konami", "Roberto Carlos diletak sebagai penyerang utama sebab rembatan padu, penalti klasik dan tiup disk bila sangkut skrin hitam."),
    ("Need for Speed: Most Wanted", "Racing", 2005, "EA Black Box", "Kereta BMW M3 GTR ikonik, kejar-mengejar polis tahap Heat 5 dan senarai Blacklist 15 yang wajib ditewaskan."),
    ("Resident Evil 4", "Survival Horror", 2005, "Capcom", "Leon S. Kennedy selamatkan Ashley di kampung terpencil, bunyi gergaji rantai Dr. Salvador yang buat tangan berpeluh."),
    ("God of War II", "Action / Hack and Slash", 2007, "Santa Monica Studio", "Kratos menentang Zeus dengan Blades of Athena, grafik tahap maksimum yang memerah habis kuasa cip Emotion Engine PS2."),
    ("Tekken 5", "Fighting", 2005, "Namco", "Pertarungan sengit lawan Jinpachi Mishima di pusingan akhir dan mini-game Devil Within yang jarang orang habiskan."),
    ("Bully", "Action-Adventure", 2006, "Rockstar Vancouver", "Jimmy Hopkins di Akademi Bullworth, ponteng kelas masuk kelas Kimia & Bahasa Inggeris, naik basikal BMX keliling bandar."),
    ("Downhill Domination", "Extreme Sports / Racing", 2003, "Incognito Entertainment", "Lumba basikal turun bukit sambil baling botol air dan tendang pihak lawan, antara game multiplayer paling kecoh di kedai game."),
    ("Guitar Hero III: Legends of Rock", "Music / Rhythm", 2007, "Neversoft", "Tekan butang L1, L2, R1, R2 dan X guna kontroler DualShock 2 sampai jari kebas semata-mata nak lepas lagu Through the Fire and Flames."),
    ("Kingdom Hearts II", "Action RPG", 2005, "Square Enix", "Gabungan watak Disney dan Final Fantasy bersama Sora, Donald dan Goofy, runut bunyi Sanctuary yang mengimbau memori."),
    ("Final Fantasy X", "JRPG", 2001, "Square", "Pengembaraan Tidus dan Yuna di dunia Spira, sistem pertarungan giliran CTB dan babak ketawa ikonik di Luca."),
    ("Metal Gear Solid 3: Snake Eater", "Stealth Action", 2004, "Konami", "Naked Snake menyusup hutan era Perang Dingin, rawat luka sendiri, makan ular dan pertarungan penembak tepat lawan The End."),
    ("Gran Turismo 4", "Sim Racing", 2004, "Polyphony Digital", "Lebih 700 buah kereta, ujian lesen memandu yang menguji kesabaran dan mod ketahanan 24 jam tanpa henti."),
    ("WWE SmackDown! Here Comes the Pain", "Wrestling / Sports", 2003, "Yuke's", "Lompat dari atas helikopter di Times Square, Brock Lesnar dan Goldberg mendominasi gelanggang dengan grafik paling lancar."),
    ("Dragon Ball Z: Budokai Tenkaichi 3", "Fighting", 2007, "Spike", "Lebih 160 watak boleh dimainkan, laga Kamehameha sampai butang analog pusing laju dan rosak getah kontroler."),
    ("Naruto: Ultimate Ninja 3", "Fighting", 2005, "CyberConnect2", "Pertarungan ninja pantas di kampung Konoha, kumpul kapsul watak dan jurus rahsia sebelum era Shippuden."),
    ("Shadow of the Colossus", "Action-Adventure", 2005, "Team Ico", "Menunggang kuda Agro di tanah luas tanpa musuh kecil, hanya memanjat 16 gergasi Colossi dengan muzik orkestra epik."),
    ("Devil May Cry 3: Dante's Awakening", "Hack and Slash", 2005, "Capcom", "Gaya pertarungan bergaya Dante melawan abangnya Vergil, tahap kesukaran yang terkenal mencabar pemain."),
    ("Burnout 3: Takedown", "Arcade Racing", 2004, "Criterion Games", "Kepuasan melanggar kereta lawan ke dinding penghadang dalam mod Crash dan efek gerak perlahan Impact Time."),
    ("Black", "First-Person Shooter", 2006, "Criterion Games", "Permainan menembak dengan efek letupan dan bunyi senjata paling realistik di atas konsol PS2."),
    ("Def Jam: Fight for NY", "Fighting", 2004, "EA Canada", "Pertarungan jalanan hip-hop bersama Snoop Dogg, Method Man dan Redman dengan gaya tempur campuran yang ganas."),
    ("Digimon Rumble Arena 2", "Party Fighting", 2004, "Bandai", "Lawan 4 pemain serentak berebut bola evolusi untuk bertukar menjadi WarGreymon atau MetalGarurumon."),
    ("Harvest Moon: Save the Homeland", "Life Simulation", 2001, "Victor Interactive", "Mengurus ladang dalam tempoh setahun untuk menyelamatkan kampung daripada dijadikan taman tema."),
    ("Pro Evolution Soccer 6", "Sports / Football", 2006, "Konami", "Adriano dengan kuasa rembatan 99 yang hampir mustahil ditepis penjaga gol, raja permainan bola di pusat siber dan rumah."),
    ("Mortal Kombat: Shaolin Monks", "Beat 'em up / Co-op", 2005, "Midway", "Mod cerita kerjasama dua pemain guna Liu Kang dan Kung Lao, hafal kombinasi Fatality atas kertas."),
]


def get_myt_context() -> Tuple[str, str]:
    now = datetime.now(tz=MYT)
    days_bm = ["Isnin", "Selasa", "Rabu", "Khamis", "Jumaat", "Sabtu", "Ahad"]
    months_bm = [
        "Januari", "Februari", "Mac", "April", "Mei", "Jun",
        "Julai", "Ogos", "September", "Oktober", "November", "Disember",
    ]
    hour = now.hour
    if 5 <= hour < 12:
        period = "Pagi"
    elif 12 <= hour < 14:
        period = "Tengah Hari"
    elif 14 <= hour < 19:
        period = "Petang"
    else:
        period = "Malam"

    full_str = (
        f"{days_bm[now.weekday()]}, {now.day} {months_bm[now.month - 1]} {now.year} "
        f"({now.strftime('%I:%M %p')} - Waktu {period})"
    )
    return full_str, period


# ==============================================================================
# 1. PENGURUS PANGKALAN DATA KATALOG GAME PS2 (SQLITE3 .DB)
# ==============================================================================
class PS2GamesCatalogDB:
    """
    Menguruskan pangkalan data SQLite3 untuk senarai permainan PS2.
    Menyokong carian judul, penggiliran automatik judul yang paling jarang
    diterbitkan, serta import pukal daripada senarai/JSON/SQLite luar.
    """

    def __init__(self, db_path: Path = PS2_GAMES_DB_PATH) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ps2_games (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL UNIQUE,
                    slug TEXT NOT NULL,
                    genre TEXT DEFAULT 'Retro PS2',
                    release_year INTEGER DEFAULT 2004,
                    developer TEXT DEFAULT '',
                    trivia_hint TEXT DEFAULT '',
                    times_posted INTEGER DEFAULT 0,
                    last_posted_at TEXT DEFAULT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ps2_games_posted ON ps2_games(times_posted, last_posted_at)")

            cur = conn.execute("SELECT COUNT(*) AS cnt FROM ps2_games")
            if int(cur.fetchone()["cnt"]) == 0:
                for title, genre, year, dev, hint in STARTER_PS2_GAMES:
                    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO ps2_games (title, slug, genre, release_year, developer, trivia_hint)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (title, slug, genre, year, dev, hint),
                    )
            conn.commit()

    def get_total_games(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM ps2_games").fetchone()
            return int(row["cnt"]) if row else 0

    def bulk_upsert_games(self, games: List[Dict[str, Any]]) -> int:
        """
        Fungsi sedia untuk import senarai penuh ribuan judul game PS2 ke dalam .db.
        Setiap item dict menerima: title (wajib), genre, release_year, developer, trivia_hint.
        """
        inserted = 0
        with self._connect() as conn:
            for g in games:
                title = str(g.get("title", "")).strip()
                if not title:
                    continue
                slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
                cur = conn.execute(
                    """
                    INSERT INTO ps2_games (title, slug, genre, release_year, developer, trivia_hint)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(title) DO UPDATE SET
                        genre = COALESCE(NULLIF(excluded.genre, ''), ps2_games.genre),
                        release_year = COALESCE(excluded.release_year, ps2_games.release_year),
                        developer = COALESCE(NULLIF(excluded.developer, ''), ps2_games.developer),
                        trivia_hint = COALESCE(NULLIF(excluded.trivia_hint, ''), ps2_games.trivia_hint)
                    """,
                    (
                        title,
                        slug,
                        str(g.get("genre") or "Retro PS2").strip(),
                        int(g.get("release_year") or 2004),
                        str(g.get("developer") or "").strip(),
                        str(g.get("trivia_hint") or "").strip(),
                    ),
                )
                if cur.rowcount > 0:
                    inserted += 1
            conn.commit()
        return inserted

    def pick_next_game(self, specific_title: Optional[str] = None) -> Dict[str, Any]:
        """
        Memilih satu judul PS2 daripada pangkalan data:
        - Jika specific_title diberikan, cari padanan dalam .db (atau bina rekod on-the-fly).
        - Jika tidak, pilih secara rawak daripada kelompok game dengan times_posted paling rendah.
        """
        with self._connect() as conn:
            if specific_title:
                row = conn.execute(
                    "SELECT * FROM ps2_games WHERE LOWER(title) LIKE ? ORDER BY times_posted ASC LIMIT 1",
                    (f"%{specific_title.strip().lower()}%",),
                ).fetchone()
                if row:
                    return dict(row)
                return {
                    "id": None,
                    "title": specific_title.strip(),
                    "slug": re.sub(r"[^a-z0-9]+", "-", specific_title.lower()).strip("-"),
                    "genre": "Retro PS2",
                    "release_year": 2004,
                    "developer": "",
                    "trivia_hint": "Kenangan klasik bermain di konsol PlayStation 2 bersama kawan-kawan zaman dulu.",
                    "times_posted": 0,
                }

            rows = conn.execute(
                """
                SELECT * FROM ps2_games
                ORDER BY times_posted ASC, COALESCE(last_posted_at, '1970-01-01') ASC
                LIMIT 10
                """
            ).fetchall()
            if not rows:
                return {
                    "id": None,
                    "title": "PlayStation 2 Classics",
                    "slug": "ps2-classics",
                    "genre": "Konsol Klasik",
                    "release_year": 2000,
                    "developer": "Sony Computer Entertainment",
                    "trivia_hint": "Bunyi skrin permulaan PlayStation 2 dan lampu biru butang eject yang menggamit nostalgia.",
                    "times_posted": 0,
                }
            chosen = random.choice(rows)
            return dict(chosen)

    def mark_game_posted(self, title: str) -> None:
        now_iso = datetime.now(tz=MYT).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE ps2_games
                SET times_posted = times_posted + 1, last_posted_at = ?
                WHERE LOWER(title) = ?
                """,
                (now_iso, title.strip().lower()),
            )
            conn.commit()


# ==============================================================================
# 2. PEMBINA PERSONA AI (LELAKI 30-AN | ADMIN / MIN) & PAKEJ HANTARAN FB
# ==============================================================================
class PS2PersonaWriter:
    """
    Menjana penceritaan nostalgia PS2 daripada sudut pandang 'admin' / 'min'
    (lelaki berusia 30-an yang membesar dengan PS2 di Malaysia), dengan panjang
    cerita 500 - 650 aksara, sokongan hantaran bergambar atau teks sahaja,
    anti-duplikasi semantik, dan komen pertama Shopee automatik.
    """

    def __init__(self, catalog_db: Optional[PS2GamesCatalogDB] = None) -> None:
        self.catalog = catalog_db or PS2GamesCatalogDB()

    def _build_system_prompt(self, self_ref: str) -> str:
        return (
            f"Kau ialah '{self_ref}', seorang lelaki Malaysia berumur pertengahan 30-an yang mengendalikan "
            "halaman Facebook 'PS2 Retro Nostalgia'. Kau membesar di zaman kegemilangan PlayStation 2 tahun 2000-an, "
            "sangat berpengetahuan tentang sejarah game PS2, cip Emotion Engine, Memory Card 8MB, FMCB, OPL, "
            "dan budaya gamer PS2 di Malaysia.\n\n"
            "GAYA BAHASA & PERSONA:\n"
            f"- Bahasakan diri sebagai '{self_ref}' dan sapa pembaca sebagai 'korang' atau 'geng'.\n"
            "- Nada bijak, matang, santai, mesra, dan berinformasi — macam abang-abang 30-an bersembang pasal "
            "fakta menarik game atau perkakasan PS2 dengan kawan-kawan lama.\n"
            "- Gunakan Bahasa Melayu Malaysia harian yang natural.\n\n"
            "TUGASAN & KAWALAN PANJANG:\n"
            "- Tulis 1 hingga 2 perenggan ringkas bernilai nostalgia/fakta menarik berjumlah ANTARA 500 HINGGA 620 AKSARA.\n"
            "- Terus mulakan ayat dengan fakta menarik, situasi sebenar, atau memori ikonik berkaitan topik/game yang diberi.\n\n"
            "LARANGAN KETAT:\n"
            "- DILARANG menulis tajuk atau header pada baris pertama.\n"
            "- DILARANG menjual barang, menyebut harga, atau meletakkan pautan Shopee dalam cerita utama.\n"
            "- DILARANG menggunakan sebarang emoji atau simbol grafik.\n"
            "- DILARANG menggunakan perkataan Indonesia (bisa, banget, nggak, ngak, bikin, gampang, cobain, cocok, yuk, kalian, anda).\n"
            "- Pastikan ayat berakhir dengan noktah (.) yang lengkap."
        )

    def _build_hashtags(self, game_title: str, genre: str) -> str:
        clean_title = re.sub(r"[^a-zA-Z0-9]", "", game_title)
        clean_genre = re.sub(r"[^a-zA-Z0-9]", "", genre.split("/")[0])
        tags = ["#PS2RetroNostalgia", "#PS2Malaysia", "#RetroGaming", "#Nostalgia2000an"]
        if clean_title and len(clean_title) <= 28:
            tags.insert(2, f"#{clean_title}")
        elif clean_genre:
            tags.insert(2, f"#PS2{clean_genre}")
        return " ".join(tags)

    def _build_engagement_question(self, post_mode: str, self_ref: str) -> str:
        questions_photo_reel = [
            f"Korang dulu paling kerap layan game ni sorang-sorang atau beramai-ramai dengan member? Cuba komen kat bawah, {self_ref} nak baca!",
            f"Siapa masih simpan konsol PS2 atau memory card lama kat rumah sampai sekarang? Cer kongsi memori korang dengan {self_ref}!",
            f"Kalau PS2 korang boleh hidup balik malam ni, game pertama apa yang korang terus buka? Komen kat bawah!",
        ]
        questions_text_only = [
            f"Jom borak santai malam ni geng — antara banyak-banyak game PS2 dulu, mana satu yang paling susah korang nak habiskan? Komen kat bawah!",
            f"Siapa masih ingat bunyi skrin boot PS2 dan debaran tunggu logo PlayStation 2 keluar? Cer kongsi pengalaman korang kat ruangan komen!",
            f"Kalau diberi peluang putar masa balik ke zaman sekolah dulu, korang nak ulang main game PS2 apa dulu? Tulis kat bawah, {self_ref} nak tengok!",
        ]
        pool = questions_text_only if post_mode == "text" else questions_photo_reel
        return random.choice(pool)

    def generate_post_package(
        self,
        post_mode: str = "photo",
        game_title: Optional[str] = None,
        visual_context: str = "",
        topic_angle: str = "",
        check_semantic_dup: bool = True,
        save_vector_on_success: bool = False,
        mark_catalog_posted: bool = False,
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Menjana pakej hantaran Facebook lengkap (kapsyen utama + komen pertama Shopee).
        Parameter `post_mode`:
          - 'photo' : Hantaran bergambar (1 atau beberapa gambar seragam)
          - 'reel'  : Hantaran video pendek / Facebook Reels
          - 'text'  : Hantaran status teks sahaja (tanpa gambar/video)
        """
        mode = post_mode.strip().lower()
        if mode not in ("photo", "reel", "text"):
            mode = "photo"

        game_rec = self.catalog.pick_next_game(specific_title=game_title)
        chosen_title = game_rec["title"]
        genre = game_rec.get("genre", "Retro PS2")
        year = game_rec.get("release_year", 2004)
        dev = game_rec.get("developer", "")
        db_hint = game_rec.get("trivia_hint", "")

        self_ref = random.choice(["admin", "min"])
        myt_full, myt_period = get_myt_context()
        system_prompt = self._build_system_prompt(self_ref=self_ref)

        angle_notes = topic_angle.strip() or db_hint
        visual_line = (
            f"Konteks Visual Lampiran: {visual_context.strip()}\n"
            if (mode in ("photo", "reel") and visual_context.strip())
            else "Format Hantaran: Hantaran Teks Sahaja (fokus pada penceritaan nostalgia dan borak santai komuniti PS2).\n"
        )

        user_prompt = (
            f"Konteks Masa Siaran: {myt_full} (Suasana {myt_period})\n"
            f"Tajuk Game / Topik PS2: {chosen_title} (Tahun {year}, Genre: {genre}"
            f"{f', Pembangun: {dev}' if dev else ''})\n"
            f"{visual_line}"
            f"Fokus Penceritaan & Fakta: {angle_notes}\n\n"
            f"Tulis cerita daripada sudut pandang '{self_ref}' (lelaki 30-an peminat PS2) "
            f"dalam Bahasa Melayu Malaysia sepanjang 500 hingga 620 aksara:"
        )

        ok, story_bm, inf_meta = llm_engine.generate_bm_story(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_attempts=3,
            min_chars=STORY_TARGET_MIN_CHARS,
            max_chars=STORY_TARGET_MAX_CHARS,
            soft_trim_chars=STORY_SOFT_TRIM_CHARS,
            temperature=STORY_TEMPERATURE,
            max_tokens=STORY_LLM_MAX_TOKENS,
        )

        if not ok or not story_bm:
            return False, {
                "error": "Gagal menjana cerita persona yang menepati had 500-650 aksara.",
                "game_title": chosen_title,
                "post_mode": mode,
                "metrics": inf_meta,
            }

        # Semakan anti-duplikasi semantik (Upstash Vector 5-Indeks)
        is_dup = False
        vec_shard = 0
        dup_match: Any = None
        if check_semantic_dup:
            is_dup, dup_match, vec_shard = vector_sharded.check_semantic_duplicate(
                game_title=chosen_title,
                candidate_story=story_bm,
                similarity_threshold=SEMANTIC_SIMILARITY_THRESHOLD,
            )

        # Semakan cap jari pantas Redis (jika aktif)
        story_fp = hashlib.sha256(f"{chosen_title.lower()}::{story_bm.lower()}".encode("utf-8")).hexdigest()[:24]
        if redis_sharded is not None and hasattr(redis_sharded, "is_duplicate_fingerprint"):
            try:
                if redis_sharded.is_duplicate_fingerprint(story_fp):
                    is_dup = True
            except Exception:
                pass

        # Padankan produk Shopee paling relevan untuk komen pertama (Memory Card / Pendrive)
        offer = ledger_db.pick_shopee_comment_offer(f"{chosen_title} {angle_notes} {story_bm}")
        engagement_q = self._build_engagement_question(post_mode=mode, self_ref=self_ref)
        hashtags = self._build_hashtags(game_title=chosen_title, genre=genre)

        post_caption = f"{story_bm}\n\n{engagement_q}\n\n{hashtags}"
        first_comment = f"{offer['cta_template']}\n👉 {offer['shopee_url']}"

        if save_vector_on_success and not is_dup:
            try:
                vector_sharded.upsert_story_vector(
                    story_id=f"ps2n_{story_fp}",
                    game_title=chosen_title,
                    story_text=story_bm,
                    metadata={"post_mode": mode, "self_ref": self_ref, "myt": myt_full},
                )
            except Exception:
                pass

        if mark_catalog_posted:
            self.catalog.mark_game_posted(chosen_title)

        return True, {
            "game_title": chosen_title,
            "genre": genre,
            "release_year": year,
            "post_mode": mode,
            "persona_ref": self_ref,
            "myt_context": myt_full,
            "story_bm": story_bm,
            "story_chars": len(story_bm),
            "post_caption": post_caption,
            "caption_chars": len(post_caption),
            "shopee_product_code": offer["product_code"],
            "shopee_product_name": offer["product_name"],
            "shopee_url": offer["shopee_url"],
            "first_comment": first_comment,
            "fingerprint": story_fp,
            "vector_shard_index": vec_shard,
            "semantic_duplicate": is_dup,
            "duplicate_match": dup_match,
            "metrics": inf_meta,
        }


# Instans sedia diimport oleh X10 & X11
ps2_catalog_db = PS2GamesCatalogDB()
persona_writer = PS2PersonaWriter(catalog_db=ps2_catalog_db)


if __name__ == "__main__":
    print(f"Jumlah judul dalam katalog PS2 (.db): {ps2_catalog_db.get_total_games()}")
    ok_t, pkg_t = persona_writer.generate_post_package(
        post_mode="text",
        game_title="Need for Speed: Most Wanted",
        check_semantic_dup=True,
    )
    print("\n--- UJIAN HANTARAN TANPA GAMBAR (TEXT-ONLY) ---")
    print("Berjaya:", ok_t, "| Aksara Cerita:", pkg_t.get("story_chars"))
    print(pkg_t.get("post_caption", pkg_t))
    print("\nKomen Pertama Shopee:\n", pkg_t.get("first_comment", ""))
    llm_engine.unload()