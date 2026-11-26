#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - UJIAN END-TO-END VISION -> PERSONA -> FB (EXP06)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/exp/exp06_vision_persona_fb_test.py
#
# ALIRAN UJIAN:
# 1. Pilih 1 hingga 4 gambar secara rawak dari /temp/test_media_pool_50/raw
#    (jika >1 gambar, padankan yang seragam & elak gambar yang sudah diguna).
# 2. Baca gambar dahulu menggunakan Vision + OCR (X07_vision_ocr_analyzer < 30KB).
# 3. Semak memori sementara SQLite3 (temp/exp06_fb_memory_temp.db) + katalog PS2
#    supaya AI ingat hantaran lepas dan tidak mengulang cerita.
# 4. Jana cerita persona lelaki 30-an ("admin" / "min") sepanjang 500-650 aksara
#    berdasarkan hasil bacaan Vision/OCR gambar tersebut (X06_local_llm_engine).
# 5. Terbit ke Facebook Page PS2 Retro Nostalgia (1 gambar / multi-gambar) &
#    hantar komen pertama dengan pautan Shopee dummy.
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
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil
import requests
from PIL import Image
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# ------------------------------------------------------------------------------
# KONFIGURASI LALUAN & IMPORT MODUL DARI live-engine-01
# ------------------------------------------------------------------------------
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
LIVE_ENGINE_DIR = PROJECT_ROOT / "live-engine-01"

for p in [CURRENT_DIR, LIVE_ENGINE_DIR, PROJECT_ROOT]:
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

cfg: Any = importlib.import_module("X01_config")
_llm_mod: Any = importlib.import_module("X06_local_llm_engine")
_vision_mod: Any = importlib.import_module("X07_vision_ocr_analyzer")
_persona_mod: Any = importlib.import_module("X08_ps2_persona_writer")
_harvest_mod: Any = importlib.import_module("X09_media_harvester")

llm_engine: Any = getattr(_llm_mod, "llm_engine")
clean_and_scrub_bm_story: Any = getattr(_llm_mod, "clean_and_scrub_bm_story")
validate_story_quality: Any = getattr(_llm_mod, "validate_story_quality")
vision_analyzer: Any = getattr(_vision_mod, "vision_analyzer")
ps2_catalog_db: Any = getattr(_persona_mod, "ps2_catalog_db")
compute_image_visual_signature: Any = getattr(_harvest_mod, "compute_image_visual_signature")
compute_visual_similarity: Any = getattr(_harvest_mod, "compute_visual_similarity")


# ==============================================================================
# [BAHAGIAN TUNING MANUAL UJIAN EXP06]
# ==============================================================================
STORY_MIN_CHARS: int = int(os.getenv("PS2N_EXP06_MIN_CHARS", "450"))
STORY_MAX_CHARS: int = int(os.getenv("PS2N_EXP06_MAX_CHARS", "660"))
STORY_SOFT_TRIM: int = int(os.getenv("PS2N_EXP06_TRIM_CHARS", "650"))
STORY_MAX_TOKENS: int = int(os.getenv("PS2N_EXP06_MAX_TOKENS", "360"))
STORY_TEMPERATURE: float = float(os.getenv("PS2N_EXP06_TEMP", "0.72"))

DUMMY_SHOPEE_URL: str = "https://shopee.com.my/ps2-retro-nostalgia-demo"
RAW_POOL_DIR: Path = PROJECT_ROOT / "temp" / "test_media_pool_50" / "raw"
TEMP_MEMORY_DB_PATH: Path = PROJECT_ROOT / "temp" / "exp06_fb_memory_temp.db"
PREVIEW_JSON_PATH: Path = PROJECT_ROOT / "temp" / "exp06_last_result.json"
# ==============================================================================

MYT = timezone(timedelta(hours=8))


def get_myt_now() -> Tuple[str, str]:
    now = datetime.now(tz=MYT)
    days_bm = ["Isnin", "Selasa", "Rabu", "Khamis", "Jumaat", "Sabtu", "Ahad"]
    months_bm = [
        "Januari", "Februari", "Mac", "April", "Mei", "Jun",
        "Julai", "Ogos", "September", "Oktober", "November", "Disember",
    ]
    h = now.hour
    period = "Pagi" if 5 <= h < 12 else ("Tengah Hari" if 12 <= h < 14 else ("Petang" if 14 <= h < 19 else "Malam"))
    full_str = f"{days_bm[now.weekday()]}, {now.day} {months_bm[now.month - 1]} {now.year} ({now.strftime('%I:%M %p')} - Waktu {period})"
    return full_str, period


# ==============================================================================
# 1. PANGKALAN DATA SEMENTARA RUJUKAN AI (SUPAYA AI TAK LUPA)
# ==============================================================================
class TempAIMemoryDB:
    """
    Pangkalan data SQLite3 sementara di temp/exp06_fb_memory_temp.db untuk menyimpan
    sejarah gambar yang telah digunakan, ringkasan OCR, dan cerita yang telah dipost
    ke Facebook supaya AI tidak mengulang kandungan yang sama.
    """

    def __init__(self, db_path: Path = TEMP_MEMORY_DB_PATH) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS temp_post_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at_myt TEXT NOT NULL,
                    run_env TEXT DEFAULT 'LOCAL',
                    persona_ref TEXT NOT NULL,
                    game_topic TEXT NOT NULL,
                    image_count INTEGER NOT NULL,
                    image_names_json TEXT NOT NULL,
                    ocr_visual_summary TEXT NOT NULL,
                    story_bm TEXT NOT NULL,
                    post_caption TEXT NOT NULL,
                    comment_text TEXT NOT NULL,
                    fb_post_id TEXT DEFAULT '',
                    fb_comment_id TEXT DEFAULT ''
                )
                """
            )
            conn.commit()

    def get_used_image_names(self) -> set:
        used = set()
        with self._connect() as conn:
            rows = conn.execute("SELECT image_names_json FROM temp_post_memory").fetchall()
            for r in rows:
                try:
                    for name in json.loads(r["image_names_json"]):
                        used.add(name)
                except Exception:
                    pass
        return used

    def get_recent_memory_context(self, limit: int = 6) -> str:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT created_at_myt, game_topic, ocr_visual_summary, story_bm
                FROM temp_post_memory
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        if not rows:
            return "Tiada hantaran terdahulu dalam memori sementara (ini ujian pertama)."

        lines = []
        for idx, r in enumerate(rows, 1):
            snippet = r["story_bm"][:110].replace("\n", " ")
            lines.append(f"{idx}. [{r['created_at_myt']}] Topik: {r['game_topic']} | Ringkasan: {snippet}...")
        return "\n".join(lines)

    def save_record(self, rec: Dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO temp_post_memory (
                    created_at_myt, run_env, persona_ref, game_topic, image_count,
                    image_names_json, ocr_visual_summary, story_bm, post_caption,
                    comment_text, fb_post_id, fb_comment_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec["created_at_myt"],
                    rec["run_env"],
                    rec["persona_ref"],
                    rec["game_topic"],
                    rec["image_count"],
                    json.dumps(rec["image_names"], ensure_ascii=False),
                    rec["ocr_visual_summary"],
                    rec["story_bm"],
                    rec["post_caption"],
                    rec["comment_text"],
                    rec.get("fb_post_id", ""),
                    rec.get("fb_comment_id", ""),
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)


# ==============================================================================
# 2. PEMILIH 1..4 GAMBAR SERAGAM DARI temp/test_media_pool_50/raw
# ==============================================================================
def ensure_raw_pool_available() -> List[Path]:
    """
    Membaca gambar daripada /temp/test_media_pool_50/raw.
    Jika dijalankan di GitHub Actions (di mana folder temp/ tidak disimpan dalam Git),
    ia akan menarik 8 gambar sampel ke /temp/test_media_pool_50/raw secara automatik.
    """
    RAW_POOL_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(
        [p for p in RAW_POOL_DIR.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")]
    )
    if existing:
        return existing

    console.print("[yellow]⚠️ Folder temp/test_media_pool_50/raw kosong (mod GitHub Actions). Menarik 8 gambar sampel...[/yellow]")
    u_key = getattr(cfg, "PS2N_UNSPLASH_ACCESS_KEY", "")
    p_key = getattr(cfg, "PS2N_PEXELS_API_KEY", "")
    downloaded: List[Path] = []

    if u_key:
        try:
            r = requests.get(
                "https://api.unsplash.com/search/photos",
                headers={"Authorization": f"Client-ID {u_key}", "Accept-Version": "v1"},
                params={"query": "playstation controller retro gaming", "per_page": 6, "orientation": "landscape"},
                timeout=20,
            )
            if r.status_code == 200:
                for idx, item in enumerate(r.json().get("results", [])[:5], 1):
                    dl_url = item.get("urls", {}).get("regular")
                    if not dl_url:
                        continue
                    img_r = requests.get(dl_url, timeout=20)
                    if img_r.status_code == 200:
                        im = Image.open(BytesIO(img_r.content)).convert("RGB")
                        out_p = RAW_POOL_DIR / f"ci_unsplash_{idx:02d}_{item.get('id', idx)}.jpg"
                        im.save(out_p, format="JPEG", quality=88)
                        downloaded.append(out_p)
        except Exception:
            pass

    if len(downloaded) < 4 and p_key:
        try:
            r = requests.get(
                "https://api.pexels.com/v1/search",
                headers={"Authorization": p_key},
                params={"query": "retro video game controller", "per_page": 6, "orientation": "landscape"},
                timeout=20,
            )
            if r.status_code == 200:
                for idx, item in enumerate(r.json().get("photos", [])[:5], 1):
                    dl_url = item.get("src", {}).get("large")
                    if not dl_url:
                        continue
                    img_r = requests.get(dl_url, timeout=20)
                    if img_r.status_code == 200:
                        im = Image.open(BytesIO(img_r.content)).convert("RGB")
                        out_p = RAW_POOL_DIR / f"ci_pexels_{idx:02d}_{item.get('id', idx)}.jpg"
                        im.save(out_p, format="JPEG", quality=88)
                        downloaded.append(out_p)
        except Exception:
            pass

    return sorted(downloaded)


def pick_random_cohesive_images(
    mem_db: TempAIMemoryDB,
    forced_count: Optional[int] = None,
) -> List[Path]:
    """
    Memilih 1 hingga 4 gambar daripada /temp/test_media_pool_50/raw.
    Jika bilangan > 1, susun gambar yang paling hampir sama tona & pencahayaannya.
    """
    all_files = ensure_raw_pool_available()
    if not all_files:
        raise RuntimeError(f"Tiada gambar ditemui di {RAW_POOL_DIR}")

    used_names = mem_db.get_used_image_names()
    fresh_files = [f for f in all_files if f.name not in used_names]
    pool = fresh_files if len(fresh_files) >= 4 else all_files

    target_count = forced_count if forced_count in (1, 2, 3, 4) else random.randint(1, 4)
    target_count = min(target_count, len(pool))

    anchor = random.choice(pool)
    if target_count == 1:
        return [anchor]

    anchor_sig = compute_image_visual_signature(anchor)
    if not anchor_sig:
        return random.sample(pool, target_count)

    scored: List[Tuple[float, Path]] = []
    for cand in pool:
        if cand == anchor:
            continue
        c_sig = compute_image_visual_signature(cand)
        if c_sig:
            sim = compute_visual_similarity(anchor_sig, c_sig)
            scored.append((sim, cand))

    scored.sort(key=lambda x: x[0], reverse=True)
    selected = [anchor] + [p for _, p in scored[: target_count - 1]]
    return selected


# ==============================================================================
# 3. PENJANA PERSONA AI (LELAKI 30-AN: ADMIN / MIN) BERDASARKAN HASIL OCR
# ==============================================================================
def enforce_persona_pronouns(text: str, self_ref: str) -> str:
    """Memastikan AI kekal membahasakan diri sebagai 'admin' atau 'min' (bukan 'aku'/'saya')."""
    text = re.sub(r"\baku\b", self_ref, text, flags=re.IGNORECASE)
    text = re.sub(r"\bsaya\b", self_ref, text, flags=re.IGNORECASE)
    # Kapitalkan jika di permulaan ayat
    text = re.sub(rf"(^|[.!?]\s+){self_ref}\b", lambda m: f"{m.group(1)}{self_ref.capitalize()}", text)
    return text


def generate_story_from_vision_ocr(
    vision_items: List[Dict[str, Any]],
    memory_context: str,
    myt_full: str,
    myt_period: str,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Membina cerita nostalgia PS2 sepanjang 500-650 aksara berdasarkan apa yang
    benar-benar dilihat oleh Vision + OCR pada gambar lampiran.
    """
    self_ref = random.choice(["admin", "min"])
    game_rec = ps2_catalog_db.pick_next_game()
    game_title = game_rec["title"]
    game_year = game_rec.get("release_year", 2004)
    game_hint = game_rec.get("trivia_hint", "")

    # Susun laporan bacaan Vision & OCR setiap gambar
    ocr_lines: List[str] = []
    for idx, v in enumerate(vision_items, 1):
        objs = ", ".join(v.get("detected_objects", [])) or "perkakasan permainan"
        ocr_txt = v.get("ocr_text", "None")
        angle = v.get("retro_nostalgia_angle", "")
        ocr_lines.append(
            f"- Gambar {idx}: Objek=[{objs}], Teks OCR=[{ocr_txt}], Suasana=[{angle}]"
        )
    ocr_report = "\n".join(ocr_lines)

    system_prompt = (
        f"Kau ialah '{self_ref}', seorang lelaki Malaysia berumur pertengahan 30-an yang mengendalikan "
        "halaman Facebook 'PS2 Retro Nostalgia'. Kau membesar di zaman kegemilangan PlayStation 2 tahun 2000-an, "
        "bijak dan arif tentang konsol PS2, kontroler DualShock 2, Memory Card 8MB, FMCB, OPL, dan sejarah game PS2.\n\n"
        "PERATURAN WAJIB:\n"
        f"1. Bahasakan diri HANYA sebagai '{self_ref}' (DILARANG guna perkataan 'aku' atau 'saya') dan sapa pembaca sebagai 'korang' atau 'geng'.\n"
        "2. WAJIB kaitkan cerita terus dengan apa yang kelihatan dalam LAPORAN VISUAL & OCR GAMBAR yang dilampirkan "
        "(contoh: kalau gambar tunjuk kontroler, tangan pegang gamepad, dua orang main bersama, atau teks jenama tertentu, ceritakan suasana itu dahulu baru sambung ke nostalgia PS2).\n"
        "3. Tulis 1 hingga 2 perenggan santai berjumlah ANTARA 500 HINGGA 620 AKSARA.\n"
        "4. DILARANG menulis tajuk/header di baris pertama, DILARANG emoji, DILARANG perkataan Indonesia "
        "(bisa, banget, nggak, ngak, bikin, gampang, cobain, cocok, yuk, kalian, anda), dan DILARANG sebut harga/link dalam cerita utama.\n"
        "5. Pastikan ayat berakhir dengan noktah (.) yang lengkap."
    )

    user_prompt = (
        f"Masa Siaran MYT: {myt_full} (Waktu {myt_period})\n"
        f"Bilangan Gambar Dilampirkan: {len(vision_items)} keping\n"
        f"LAPORAN VISUAL & OCR GAMBAR:\n{ocr_report}\n\n"
        f"Rujukan Tajuk Game PS2 Tambahan (jika sesuai diselitkan): {game_title} ({game_year}) - {game_hint}\n\n"
        f"MEMORI HANTARAN LEPAS (JANGAN ULANG AYAT/CERITA YANG SAMA):\n{memory_context}\n\n"
        f"Tulis penceritaan daripada sudut pandang '{self_ref}' berdasarkan gambar di atas (500 hingga 620 aksara):"
    )

    last_meta: Dict[str, Any] = {}
    for attempt in range(1, 4):
        adj_temp = min(0.92, round(STORY_TEMPERATURE + ((attempt - 1) * 0.05), 2))
        inf = llm_engine.generate_chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=adj_temp,
            max_tokens=STORY_MAX_TOKENS,
        )
        cleaned = clean_and_scrub_bm_story(inf["raw_text"], max_chars=STORY_SOFT_TRIM)
        cleaned = enforce_persona_pronouns(cleaned, self_ref=self_ref)
        ok_qual, qual_msg = validate_story_quality(cleaned, min_len=STORY_MIN_CHARS, max_len=STORY_MAX_CHARS)

        last_meta = {
            "attempt": attempt,
            "elapsed_sec": inf["elapsed_sec"],
            "completion_tokens": inf["completion_tokens"],
            "tok_per_sec": inf["tok_per_sec"],
            "story_chars": len(cleaned),
            "quality_status": qual_msg,
        }

        if ok_qual:
            slug_tag = re.sub(r"[^a-zA-Z0-9]", "", game_title)[:24]
            engagement_q = random.choice(
                [
                    f"Korang dulu paling kerap layan game apa bila pegang kontroler macam ni? Cer komen kat bawah, {self_ref} nak baca!",
                    f"Siapa masih simpan set PS2 atau kontroler lama kat rumah sampai sekarang? Kongsi memori korang dengan {self_ref}!",
                    f"Bila tengok gambar ni, terus teringat zaman lepak main PS2 dengan member dulu. Game apa paling legend bagi korang?",
                ]
            )
            hashtags = f"#PS2RetroNostalgia #PS2Malaysia #RetroGaming #{slug_tag} #Nostalgia2000an"
            post_caption = f"{cleaned}\n\n{engagement_q}\n\n{hashtags}"

            comment_text = (
                f"Kalau korang masih simpan konsol PS2 kat rumah dan nak hidupkan balik nostalgia zaman dulu "
                f"(Memory Card siap save data / Pendrive Plug & Play), boleh tengok kat sini geng:\n"
                f"👉 {DUMMY_SHOPEE_URL}"
            )

            return True, {
                "persona_ref": self_ref,
                "game_topic": game_title,
                "ocr_report": ocr_report,
                "story_bm": cleaned,
                "story_chars": len(cleaned),
                "post_caption": post_caption,
                "caption_chars": len(post_caption),
                "comment_text": comment_text,
                "metrics": last_meta,
            }

    return False, {"error": "Gagal menjana cerita persona sah.", "metrics": last_meta}


# ==============================================================================
# 4. PENERBIT FACEBOOK GRAPH API v26.0 (1 FOTO / MULTI-FOTO + KOMEN PERTAMA)
# ==============================================================================
class FacebookTestPublisher:
    """
    Menerbitkan hantaran bergambar (1 foto atau album 2-4 foto) ke halaman
    PS2 Retro Nostalgia melalui Meta Graph API v26.0 dan menghantar komen pertama.
    """

    def __init__(self) -> None:
        self.app_secret: str = getattr(cfg, "PS2N_FB_APP_SECRET", "")
        self.page_id: str = getattr(cfg, "PS2N_FB_PAGE_ID", "")
        self.page_token: str = getattr(cfg, "PS2N_FB_PAGE_TOKEN", "")
        self.graph_ver: str = getattr(cfg, "PS2N_FB_GRAPH_VERSION", "v26.0")
        self.base_url: str = f"https://graph.facebook.com/{self.graph_ver}"

        if not self.page_id or not self.page_token:
            raise RuntimeError("Kredensial PS2N_FB_PAGE_ID atau PS2N_FB_PAGE_TOKEN tiada dalam persekitaran.")

    def _auth_params(self) -> Dict[str, str]:
        params: Dict[str, str] = {"access_token": self.page_token}
        if self.app_secret:
            proof = hmac.new(
                self.app_secret.encode("utf-8"),
                self.page_token.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            params["appsecret_proof"] = proof
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
            # Muat naik setiap gambar sebagai unpublished terlebih dahulu
            attached_media: List[Dict[str, str]] = []
            for p in image_paths:
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
            raise RuntimeError("Meta Graph API tidak memulangkan post_id selepas penerbitan.")

        # Tunggu sebentar supaya FB mengindeks hantaran sebelum menghantar komen pertama
        time.sleep(2.5)
        comment_url = f"{self.base_url}/{post_id}/comments"
        comment_payload = self._auth_params()
        comment_payload["message"] = comment_text

        c_resp = requests.post(comment_url, data=comment_payload, timeout=30)
        c_data = c_resp.json()
        if c_resp.status_code != 200 or "error" in c_data:
            raise RuntimeError(f"Hantaran berjaya ({post_id}) tetapi gagal hantar komen: {json.dumps(c_data)}")

        comment_id = str(c_data.get("id", ""))
        return {"post_id": post_id, "comment_id": comment_id}


def main() -> None:
    parser = argparse.ArgumentParser(description="EXP06: Ujian Vision/OCR -> Persona AI -> Facebook Post & Komen")
    parser.add_argument("--count", type=int, default=0, help="Paksa bilangan gambar (1..4). 0 = rawak 1..4.")
    parser.add_argument("--dry-run", action="store_true", help="Uji janaan sahaja tanpa post ke Facebook sebenar.")
    args = parser.parse_args()

    is_github_actions = os.getenv("GITHUB_ACTIONS", "").lower() == "true"
    run_env = "GITHUB_ACTIONS" if is_github_actions else "LOCAL_WSL"
    vm = psutil.virtual_memory()
    myt_full, myt_period = get_myt_now()

    console.print(
        Panel.fit(
            "[bold cyan]PS2-RETRO-NOSTALGIA :: VISION OCR -> PERSONA AI -> FB LIVE TEST (EXP06)[/bold cyan]\n"
            f"[dim]Persekitaran: {run_env} | Masa MYT: {myt_full} | RAM Tersedia: {vm.available / (1024**3):.2f} GB[/dim]",
            border_style="cyan",
        )
    )

    mem_db = TempAIMemoryDB()
    forced = args.count if args.count in (1, 2, 3, 4) else None

    # Langkah 1: Pilih 1 hingga 4 gambar daripada temp/test_media_pool_50/raw
    selected_paths = pick_random_cohesive_images(mem_db=mem_db, forced_count=forced)
    img_tbl = Table(
        title=f"1. Gambar Dipilih dari temp/test_media_pool_50/raw ({len(selected_paths)} Keping)",
        box=box.ROUNDED,
        header_style="bold yellow",
    )
    img_tbl.add_column("#", justify="center", style="cyan")
    img_tbl.add_column("Nama Fail", style="white")
    img_tbl.add_column("Saiz Asal", justify="right", style="dim")
    for idx, p in enumerate(selected_paths, 1):
        img_tbl.add_row(str(idx), p.name, f"{p.stat().st_size / 1024:.1f} KB")
    console.print(img_tbl)

    # Langkah 2: Baca semua gambar terpilih menggunakan Vision + OCR (< 30 KB)
    with console.status(f"[bold cyan]Membaca {len(selected_paths)} gambar menggunakan Vision + OCR (< 30 KB)...[/bold cyan]"):
        v_ok, v_items, v_meta = vision_analyzer.analyze_images(selected_paths, auto_unload=True)

    if not v_ok or not v_items:
        console.print(f"[bold red]❌ Gagal membaca gambar melalui Vision/OCR: {v_meta}[/bold red]")
        sys.exit(1)

    console.print(
        f"[bold green]✅ Vision + OCR selesai dalam {v_meta['inference_sec']}s "
        f"({v_meta['tok_per_sec']} tok/s | Max Saiz Mampat: {v_meta['max_compressed_kb']} KB).[/bold green]"
    )
    for item in v_items:
        console.print(
            f"   [cyan]Gambar {item.get('image_number')}:[/cyan] Objek={item.get('detected_objects')} | "
            f"OCR='{item.get('ocr_text')}' | {item.get('retro_nostalgia_angle')}"
        )

    # Langkah 3: Bina penceritaan persona 'admin'/'min' (500-650 aksara) berdasarkan hasil Vision/OCR
    recent_mem = mem_db.get_recent_memory_context(limit=6)
    with console.status("[bold cyan]Menjana penceritaan persona lelaki 30-an ('admin'/'min')...[/bold cyan]"):
        s_ok, story_pkg = generate_story_from_vision_ocr(
            vision_items=v_items,
            memory_context=recent_mem,
            myt_full=myt_full,
            myt_period=myt_period,
        )
    llm_engine.unload()

    if not s_ok:
        console.print(f"[bold red]❌ Gagal menjana cerita persona: {story_pkg}[/bold red]")
        sys.exit(1)

    bm = story_pkg["metrics"]
    console.print(
        Panel(
            f"[bold yellow]KAPSYEN HANTARAN FB ({len(selected_paths)} Gambar | Persona: {story_pkg['persona_ref']} | "
            f"Cerita: {story_pkg['story_chars']} aksara):[/bold yellow]\n"
            f"[white]{story_pkg['post_caption']}[/white]\n\n"
            f"[bold green]KOMEN PERTAMA (Pautan Shopee Dummy):[/bold green]\n"
            f"[cyan]{story_pkg['comment_text']}[/cyan]\n\n"
            f"[dim]Metrik LLM: {bm['elapsed_sec']}s ({bm['tok_per_sec']} tok/s, percubaan #{bm['attempt']})[/dim]",
            title="2. Pratonton Kandungan Persona AI",
            border_style="green",
        )
    )

    # Langkah 4: Terbit ke Facebook Page & Hantar Komen Pertama
    fb_result = {"post_id": "DRY_RUN", "comment_id": "DRY_RUN"}
    if not args.dry_run:
        with console.status("[bold cyan]Menerbitkan hantaran ke Facebook PS2 Retro Nostalgia & menghantar komen...[/bold cyan]"):
            publisher = FacebookTestPublisher()
            fb_result = publisher.publish_photos_and_comment(
                image_paths=selected_paths,
                post_caption=story_pkg["post_caption"],
                comment_text=story_pkg["comment_text"],
            )
        console.print(
            f"[bold green]🚀 BERJAYA TERBIT KE FACEBOOK![/bold green] "
            f"Post ID: [bold white]{fb_result['post_id']}[/bold white] | "
            f"Comment ID: [bold white]{fb_result['comment_id']}[/bold white]"
        )
    else:
        console.print("[yellow]ℹ️ Mod --dry-run aktif: melangkau penerbitan sebenar ke Facebook.[/yellow]")

    # Langkah 5: Simpan ke memori sementara SQLite3 & fail JSON pratonton
    record_to_save = {
        "created_at_myt": myt_full,
        "run_env": run_env,
        "persona_ref": story_pkg["persona_ref"],
        "game_topic": story_pkg["game_topic"],
        "image_count": len(selected_paths),
        "image_names": [p.name for p in selected_paths],
        "ocr_visual_summary": story_pkg["ocr_report"],
        "story_bm": story_pkg["story_bm"],
        "post_caption": story_pkg["post_caption"],
        "comment_text": story_pkg["comment_text"],
        "fb_post_id": fb_result["post_id"],
        "fb_comment_id": fb_result["comment_id"],
    }
    row_id = mem_db.save_record(record_to_save)
    record_to_save["db_row_id"] = row_id
    record_to_save["vision_metrics"] = v_meta
    record_to_save["llm_metrics"] = bm
    PREVIEW_JSON_PATH.write_text(json.dumps(record_to_save, indent=2, ensure_ascii=False), encoding="utf-8")
    console.print(f"[dim]Rekod disimpan ke {TEMP_MEMORY_DB_PATH} (Row #{row_id}) & {PREVIEW_JSON_PATH}[/dim]")


if __name__ == "__main__":
    main()