#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - CACHE 50 MEDIA TEMPATAN & TUNED AI TEST (EXP05)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/exp/exp05_media_cache_and_tuned_story_debug.py
#
# FUNGSI UTAMA:
# 1. Menyimpan 50 gambar ujian (25 Unsplash + 25 Pexels Photos) ke dalam
#    /home/braderdin/ps2-retro-nostalgia/temp/test_media_pool_50/ (versi asal +
#    versi mampat < 30 KB untuk Vision/OCR) supaya ujian berterusan tidak perlu
#    membuat panggilan API berulang kali (gunakan --refresh untuk muat turun baru).
# 2. Menguji Vision + OCR (Qwen25VLChatHandler) ke atas 2 gambar tempatan < 30 KB.
# 3. Menguji penjanaan cerita nostalgia PS2 (n_threads=4, max_tokens=768,
#    had aksara 220..1150, pemotong noktah lengkap) + padanan komen Shopee.
# ==============================================================================

import argparse
import base64
import gc
import importlib
import json
import os
import random
import re
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
# KONFIGURASI LALUAN & IMPORT MODUL TERAS DARI live-engine-01
# ------------------------------------------------------------------------------
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
LIVE_ENGINE_DIR = PROJECT_ROOT / "live-engine-01"

for p in [CURRENT_DIR, LIVE_ENGINE_DIR, PROJECT_ROOT]:
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

try:
    cfg: Any = importlib.import_module("X01_config")
    _ledger_mod: Any = importlib.import_module("X02_sqlite_ledger")
    _vector_mod: Any = importlib.import_module("X05_vector_sharded")

    ledger_db: Any = getattr(_ledger_mod, "ledger_db")
    vector_sharded: Any = getattr(_vector_mod, "vector_sharded")
except Exception as e:
    console.print(f"[bold red]❌ Gagal mengimport modul teras dari live-engine-01: {e}[/bold red]")
    sys.exit(1)

# ------------------------------------------------------------------------------
# PEMALAR & HAD KESELAMATAN
# ------------------------------------------------------------------------------
MYT = timezone(timedelta(hours=8))

UNSPLASH_HARD_MAX_PER_REQ = 40
PEXELS_HARD_MAX_PER_REQ = 70
TARGET_POOL_TOTAL = 50
TARGET_UNSPLASH_COUNT = 25
TARGET_PEXELS_COUNT = 25

VISION_MAX_IMAGE_BYTES = 30 * 1024       # Wajib bawah 30 KB (30,720 bait)
VISION_TARGET_IMAGE_BYTES = 28 * 1024    # Sasaran selamat 28 KB

MEDIA_POOL_DIR = cfg.TEMP_DIR / "test_media_pool_50"
RAW_POOL_DIR = MEDIA_POOL_DIR / "raw"
VISION_POOL_DIR = MEDIA_POOL_DIR / "vision_30kb"
MANIFEST_PATH = MEDIA_POOL_DIR / "manifest_50.json"

# Kamus penyingkiran istilah Indonesia -> Bahasa Melayu harian Malaysia
INDONESIAN_SCRUB_MAP: Dict[str, str] = {
    r"\bbisa\b": "boleh",
    r"\bbanget\b": "sangat",
    r"\bnggak\b": "tak",
    r"\bngak\b": "tak",
    r"\bgampang\b": "senang",
    r"\bbikin\b": "buat",
    r"\bcobain\b": "cuba",
    r"\bcocok\b": "sesuai",
    r"\byuk\b": "jom",
    r"\bgimana\b": "macam mana",
    r"\bkulkas\b": "peti sejuk",
    r"\bberantakan\b": "berselerak",
    r"\bcapek\b": "penat",
    r"\buang\b": "duit",
    r"\bAnda\b": "korang",
    r"\banda\b": "korang",
    r"\bkalian\b": "korang",
}

TEST_SCENARIOS: List[Dict[str, str]] = [
    {
        "game_title": "GTA San Andreas",
        "post_type": "reel",
        "angle_hint": "Kenangan hafal kod cheat atas kertas buku latihan sekolah dan takut memory card penuh masa nak simpan misi akhir.",
    },
    {
        "game_title": "Winning Eleven 8",
        "post_type": "photo",
        "angle_hint": "Suasana lepak main liga dengan kawan-kawan zaman dulu, berebut pilih Roberto Carlos ambil sepakan percuma dan tiup disk bila sangkut di skrin loading.",
    },
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
# 1. PENGURUS POOL 50 GAMBAR UJIAN TEMPATAN (UNSPLASH + PEXELS)
# ==============================================================================
def fetch_unsplash_batch(query: str, count: int) -> List[Dict[str, Any]]:
    api_key = cfg.PS2N_UNSPLASH_ACCESS_KEY
    if not api_key:
        return []

    safe_per_page = min(max(1, count), UNSPLASH_HARD_MAX_PER_REQ, 30)
    resp = requests.get(
        "https://api.unsplash.com/search/photos",
        headers={"Authorization": f"Client-ID {api_key}", "Accept-Version": "v1"},
        params={"query": query, "per_page": safe_per_page, "orientation": "landscape"},
        timeout=25,
    )
    if resp.status_code != 200:
        console.print(f"[yellow]⚠️ Amaran Unsplash HTTP {resp.status_code}: {resp.text[:120]}[/yellow]")
        return []

    items: List[Dict[str, Any]] = []
    for it in resp.json().get("results", [])[:count]:
        urls = it.get("urls", {})
        dl_url = urls.get("regular") or urls.get("small")
        if not dl_url:
            continue
        items.append(
            {
                "source": "unsplash",
                "source_id": str(it.get("id", "")),
                "description": it.get("alt_description") or it.get("description") or "Retro PlayStation Gaming",
                "photographer": it.get("user", {}).get("name", "Unsplash"),
                "download_url": dl_url,
            }
        )
    return items


def fetch_pexels_photos_batch(query: str, count: int) -> List[Dict[str, Any]]:
    api_key = cfg.PS2N_PEXELS_API_KEY
    if not api_key:
        return []

    safe_per_page = min(max(1, count), PEXELS_HARD_MAX_PER_REQ)
    resp = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": api_key},
        params={"query": query, "per_page": safe_per_page, "orientation": "landscape"},
        timeout=25,
    )
    if resp.status_code != 200:
        console.print(f"[yellow]⚠️ Amaran Pexels HTTP {resp.status_code}: {resp.text[:120]}[/yellow]")
        return []

    items: List[Dict[str, Any]] = []
    for it in resp.json().get("photos", [])[:count]:
        src = it.get("src", {})
        dl_url = src.get("large") or src.get("medium") or src.get("original")
        if not dl_url:
            continue
        items.append(
            {
                "source": "pexels",
                "source_id": str(it.get("id", "")),
                "description": it.get("alt") or "Retro Console Controller Gaming",
                "photographer": it.get("photographer", "Pexels"),
                "download_url": dl_url,
            }
        )
    return items


def save_raw_and_compressed_image(
    item: Dict[str, Any],
    index_num: int,
) -> Optional[Dict[str, Any]]:
    """
    Memuat turun 1 gambar ke raw/ dan menjana salinan mampat < 30 KB di vision_30kb/.
    """
    try:
        resp = requests.get(item["download_url"], timeout=25)
        if resp.status_code != 200 or not resp.content:
            return None

        raw_bytes = resp.content
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", item["source_id"])[:24] or f"item{index_num}"
        base_name = f"img_{index_num:03d}_{item['source']}_{safe_id}.jpg"

        raw_path = RAW_POOL_DIR / base_name
        vision_path = VISION_POOL_DIR / base_name

        img = Image.open(BytesIO(raw_bytes))
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")

        # Simpan versi HD/asal ke raw/
        img.save(raw_path, format="JPEG", quality=88, optimize=True)

        # Mampatkan versi Vision/OCR wajib < 30 KB (sasaran <= 28 KB)
        v_img = img.copy()
        v_img.thumbnail((512, 512), Image.Resampling.LANCZOS)
        quality = 78
        best_buf: Optional[bytes] = None

        while True:
            buf = BytesIO()
            v_img.save(buf, format="JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            best_buf = data

            if len(data) <= VISION_TARGET_IMAGE_BYTES:
                break

            if quality > 22:
                quality -= 8
            else:
                new_w = max(160, int(v_img.width * 0.80))
                new_h = max(160, int(v_img.height * 0.80))
                if new_w == v_img.width and new_h == v_img.height:
                    break
                v_img = v_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                quality = 55

        if not best_buf or len(best_buf) >= VISION_MAX_IMAGE_BYTES:
            raw_path.unlink(missing_ok=True)
            return None

        vision_path.write_bytes(best_buf)

        return {
            "index": index_num,
            "source": item["source"],
            "source_id": item["source_id"],
            "description": item["description"],
            "photographer": item["photographer"],
            "raw_path": str(raw_path),
            "raw_kb": round(raw_path.stat().st_size / 1024, 2),
            "vision_path": str(vision_path),
            "vision_kb": round(len(best_buf) / 1024, 2),
            "vision_dimensions": f"{v_img.width}x{v_img.height}",
            "vision_quality": quality,
        }
    except Exception:
        return None


def ensure_local_50_media_pool(force_refresh: bool = False) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Memastikan 50 gambar ujian tersedia di /temp/test_media_pool_50/.
    Mengembalikan (senarai_rekod, digunakan_dari_cache_tempatan).
    """
    RAW_POOL_DIR.mkdir(parents=True, exist_ok=True)
    VISION_POOL_DIR.mkdir(parents=True, exist_ok=True)

    if not force_refresh and MANIFEST_PATH.exists():
        try:
            existing = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            valid_records = [
                rec
                for rec in existing
                if Path(rec.get("raw_path", "")).exists()
                and Path(rec.get("vision_path", "")).exists()
                and Path(rec.get("vision_path", "")).stat().st_size < VISION_MAX_IMAGE_BYTES
            ]
            if len(valid_records) >= TARGET_POOL_TOTAL:
                return valid_records[:TARGET_POOL_TOTAL], True
        except Exception:
            pass

    console.print(
        f"[bold cyan]📥 Memuat turun & menyimpan {TARGET_POOL_TOTAL} gambar ujian "
        f"(25 Unsplash + 25 Pexels) ke {MEDIA_POOL_DIR}...[/bold cyan]"
    )

    unsplash_candidates = fetch_unsplash_batch("playstation retro gaming controller console", 30)
    pexels_candidates = fetch_pexels_photos_batch("retro video game controller console", 30)

    selected_candidates: List[Dict[str, Any]] = []
    selected_candidates.extend(unsplash_candidates[:TARGET_UNSPLASH_COUNT])
    selected_candidates.extend(pexels_candidates[:TARGET_PEXELS_COUNT])

    # Jika salah satu sumber kurang daripada 25, tampung dari baki sumber satu lagi
    if len(selected_candidates) < TARGET_POOL_TOTAL:
        extra_pool = unsplash_candidates[TARGET_UNSPLASH_COUNT:] + pexels_candidates[TARGET_PEXELS_COUNT:]
        for extra in extra_pool:
            if len(selected_candidates) >= TARGET_POOL_TOTAL + 6:
                break
            selected_candidates.append(extra)

    saved_records: List[Dict[str, Any]] = []
    with console.status("[bold cyan]Menyimpan versi asal & mampatan < 30 KB ke cakera tempatan...[/bold cyan]"):
        for cand in selected_candidates:
            if len(saved_records) >= TARGET_POOL_TOTAL:
                break
            next_idx = len(saved_records) + 1
            rec = save_raw_and_compressed_image(cand, next_idx)
            if rec:
                saved_records.append(rec)

    MANIFEST_PATH.write_text(json.dumps(saved_records, indent=2, ensure_ascii=False), encoding="utf-8")
    return saved_records, False


# ==============================================================================
# 2. ENJIN VISION & OCR TEMPATAN (MEMBACA TERUS DARI POOL < 30 KB)
# ==============================================================================
def load_vision_chat_handler(mmproj_path: str) -> Tuple[Any, str]:
    chat_fmt: Any = importlib.import_module("llama_cpp.llama_chat_format")
    for handler_name in ["Qwen3VLChatHandler", "Qwen25VLChatHandler", "Qwen2VLChatHandler"]:
        handler_cls = getattr(chat_fmt, handler_name, None)
        if handler_cls is not None:
            try:
                return handler_cls(clip_model_path=mmproj_path, verbose=False), handler_name
            except Exception:
                continue
    raise RuntimeError("Tiada vision chat handler serasi ditemui dalam llama_cpp.")


def analyze_local_vision_samples(
    samples: List[Dict[str, Any]],
    n_threads: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    gguf_info = cfg.resolve_gguf_paths()
    model_path = gguf_info["model_path"]
    mmproj_path = gguf_info["mmproj_path"]

    llama_mod: Any = importlib.import_module("llama_cpp")
    Llama: Any = getattr(llama_mod, "Llama")

    t_load = time.perf_counter()
    chat_handler, handler_name = load_vision_chat_handler(mmproj_path)
    llm: Any = Llama(
        model_path=model_path,
        chat_handler=chat_handler,
        n_ctx=4096,
        n_threads=n_threads,
        n_batch=512,
        type_k=8,
        type_v=8,
        flash_attn=True,
        verbose=False,
    )
    load_sec = time.perf_counter() - t_load

    prompt_text = (
        f"Analyze all {len(samples)} attached retro gaming images (Image 1 to Image {len(samples)}).\n"
        "Return ONLY valid JSON with key 'images' containing objects with keys:\n"
        "- image_number (1..N)\n"
        "- detected_objects (list of gaming items visible)\n"
        "- ocr_text (visible brand/button text or 'None')\n"
        "- retro_nostalgia_angle (1 short sentence in Bahasa Melayu Malaysia)\n"
        "/no_think"
    )

    user_content: List[Dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    for s in samples:
        img_bytes = Path(s["vision_path"]).read_bytes()
        b64_str = base64.b64encode(img_bytes).decode("utf-8")
        user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_str}"}})

    t_inf = time.perf_counter()
    res: Any = llm.create_chat_completion(
        messages=[
            {"role": "system", "content": "Output strict valid JSON only."},
            {"role": "user", "content": user_content},
        ],
        temperature=0.2,
        top_p=0.9,
        max_tokens=512,
        stream=False,
    )
    inf_sec = time.perf_counter() - t_inf

    raw_out = str(res["choices"][0]["message"]["content"] or "")
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw_out, flags=re.DOTALL)
    cleaned = re.sub(r"```json\s*|```\s*", "", cleaned).strip()

    parsed_images: List[Dict[str, Any]] = []
    json_match = re.search(r"\{[\s\S]*\}", cleaned)
    if json_match:
        try:
            parsed_images = json.loads(json_match.group(0)).get("images", [])
        except Exception:
            parsed_images = []

    usage = res.get("usage", {})
    comp_tok = int(usage.get("completion_tokens", len(cleaned.split())))
    meta = {
        "handler": handler_name,
        "load_sec": round(load_sec, 2),
        "inf_sec": round(inf_sec, 2),
        "tok_per_sec": round(comp_tok / inf_sec, 2) if inf_sec > 0 else 0.0,
    }

    # Bebaskan RAM sebelum memuatkan model teks tulen
    del llm
    del chat_handler
    gc.collect()

    return parsed_images, meta


# ==============================================================================
# 3. PEMBERSIHAN, PEMOTONG NOKTAH AYAT & PENJANA CERITA TUNED (QWEN3.5-4B)
# ==============================================================================
def trim_to_sentence_boundary(text: str, max_chars: int) -> str:
    """
    Jika teks melebihi max_chars, potong secara kemas pada penghujung ayat lengkap
    (., !, ?) yang terakhir di bawah had supaya cerita tetap sah dan tidak terputus.
    """
    if len(text) <= max_chars:
        return text

    sliced = text[:max_chars]
    last_punct = max(sliced.rfind("."), sliced.rfind("!"), sliced.rfind("?"))
    if last_punct >= 200:
        return sliced[: last_punct + 1].strip()
    return sliced.strip()


def clean_and_scrub_bm_story(raw_text: str, max_chars: int = 1100) -> str:
    if not raw_text:
        return ""

    # 1. Buang blok pemikiran <think> dan markdown
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw_text, flags=re.DOTALL)
    cleaned = re.sub(r"```[a-zA-Z]*\s*|```\s*", "", cleaned)

    # 2. Normalisasi tanda baca
    for orig, rep in {"’": "'", "‘": "'", "“": '"', "”": '"', "—": "-", "–": "-", "…": "..."}.items():
        cleaned = cleaned.replace(orig, rep)

    # 3. Buang emoji Unicode daripada output mentah AI
    emoji_pattern = re.compile(
        "[\U00010000-\U0010ffff\uD800-\uDBFF\uDC00-\uDFFF\u2600-\u26FF\u2700-\u27BF]",
        flags=re.UNICODE,
    )
    cleaned = emoji_pattern.sub("", cleaned).strip().strip('"').strip("'")

    # 4. Buang tajuk/header di baris pertama jika wujud
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    if lines and (
        lines[0].lower().startswith(("tajuk", "title", "headline"))
        or lines[0].startswith(("#", "**"))
    ):
        lines.pop(0)
    cleaned = " ".join(lines)

    # 5. Gantikan istilah Indonesia kepada Bahasa Melayu Malaysia
    for pattern, bm_word in INDONESIAN_SCRUB_MAP.items():
        cleaned = re.sub(pattern, bm_word, cleaned, flags=re.IGNORECASE)

    # 6. Pastikan berakhir pada tanda noktah/seruan ayat lengkap
    match = re.search(r"^([\s\S]*[.!?])", cleaned.strip())
    if match:
        cleaned = match.group(1).strip()

    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return trim_to_sentence_boundary(cleaned, max_chars=max_chars)


def validate_story_quality(text: str, min_len: int = 220, max_len: int = 1150) -> Tuple[bool, str]:
    if not text or len(text) < min_len:
        return False, f"Teks terlalu pendek ({len(text)} aksara, minimum {min_len})."
    if len(text) > max_len:
        return False, f"Teks melebihi had ({len(text)} aksara, maksimum {max_len})."

    allowed_re = re.compile(r"^[a-zA-Z0-9\s.,!?'\"\-\(\)/%:;&]+$")
    if not allowed_re.match(text):
        return False, "Dikesan aksara atau simbol tidak standard di luar abjad Latin."

    words = re.findall(r"\b\w+\b", text.lower())
    freq: Dict[str, int] = {}
    for w in words:
        if len(w) > 3:
            freq[w] = freq.get(w, 0) + 1
            if freq[w] > 9:
                return False, f"Glitch pengulangan: perkataan '{w}' muncul {freq[w]} kali."

    return True, "Lulus"


def assemble_fb_post_package(
    game_title: str,
    post_type: str,
    story_bm: str,
    selected_media: Dict[str, Any],
) -> Dict[str, Any]:
    offer = ledger_db.pick_shopee_comment_offer(f"{game_title} {story_bm}")
    tag_slug = re.sub(r"[^a-zA-Z0-9]", "", game_title)
    hashtags = f"#PS2RetroNostalgia #PS2Malaysia #RetroGaming #{tag_slug} #Nostalgia2000an"

    if post_type == "reel":
        hook_footer = "Korang dulu paling kerap layan misi atau mode apa dalam game ni? Komen kat bawah!"
    else:
        hook_footer = "Siapa masih simpan konsol PS2 lama kat rumah sampai sekarang? Cer kongsi kenangan korang!"

    post_caption = f"{story_bm}\n\n{hook_footer}\n\n{hashtags}"
    first_comment = f"{offer['cta_template']}\n👉 {offer['shopee_url']}"

    return {
        "game_title": game_title,
        "post_type": post_type,
        "media_file": selected_media["raw_path"],
        "media_source": f"{selected_media['source']} ({selected_media['source_id']})",
        "story_bm": story_bm,
        "story_chars": len(story_bm),
        "post_caption": post_caption,
        "caption_chars": len(post_caption),
        "shopee_product_code": offer["product_code"],
        "shopee_url": offer["shopee_url"],
        "first_comment": first_comment,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="EXP05: Cache 50 Media Tempatan & Tuned PS2 AI Test")
    parser.add_argument("--refresh", action="store_true", help="Muat turun semula 50 gambar dari Unsplash & Pexels")
    args = parser.parse_args()

    vm = psutil.virtual_memory()
    cpu_count = os.cpu_count() or 4
    n_threads = min(4, max(3, cpu_count))
    myt_full, myt_period = get_myt_context()

    console.print(
        Panel.fit(
            "[bold cyan]PS2-RETRO-NOSTALGIA :: 50 MEDIA POOL CACHE & TUNED STORY TEST (EXP05)[/bold cyan]\n"
            f"[dim]Masa MYT: {myt_full} | RAM Tersedia: {vm.available / (1024**3):.2f} GB | Threads: {n_threads}[/dim]",
            border_style="cyan",
        )
    )

    # Langkah 1: Pastikan 50 gambar tersedia di temp/test_media_pool_50/
    pool_records, from_cache = ensure_local_50_media_pool(force_refresh=args.refresh)
    u_count = sum(1 for r in pool_records if r["source"] == "unsplash")
    p_count = sum(1 for r in pool_records if r["source"] == "pexels")

    pool_tbl = Table(title="1. Status Pool 50 Gambar Ujian Tempatan", box=box.ROUNDED, header_style="bold green")
    pool_tbl.add_column("Perkara", style="bold white")
    pool_tbl.add_column("Nilai", style="cyan")
    pool_tbl.add_row("Direktori Pool", str(MEDIA_POOL_DIR))
    pool_tbl.add_row(
        "Sumber Data",
        "[bold green]CACHE TEMPATAN (0 Panggilan API)[/bold green]" if from_cache else "[yellow]MUAT TURUN BARU API[/yellow]",
    )
    pool_tbl.add_row("Jumlah Gambar Tersimpan", f"{len(pool_records)} gambar (Unsplash: {u_count}, Pexels: {p_count})")
    pool_tbl.add_row("Saiz Maksimum Vision (<30KB)", f"{max(r['vision_kb'] for r in pool_records):.2f} KB")
    console.print(pool_tbl)

    # Pilih 2 gambar dari pool tempatan (1 Unsplash + 1 Pexels) untuk ujian Vision + Cerita
    u_pool = [r for r in pool_records if r["source"] == "unsplash"]
    p_pool = [r for r in pool_records if r["source"] == "pexels"]
    chosen_samples = [
        random.choice(u_pool) if u_pool else pool_records[0],
        random.choice(p_pool) if p_pool else pool_records[1],
    ]

    # Langkah 2: Analisis Vision + OCR pantas ke atas 2 gambar tempatan (< 30 KB)
    with console.status("[bold cyan]Menjalankan Vision + OCR ke atas 2 gambar tempatan (< 30 KB)...[/bold cyan]"):
        vision_results, v_meta = analyze_local_vision_samples(chosen_samples, n_threads=n_threads)

    console.print(
        f"[bold green]✅ Vision + OCR selesai dalam {v_meta['inf_sec']}s "
        f"({v_meta['tok_per_sec']} tok/s | Handler: {v_meta['handler']}).[/bold green]"
    )

    # Langkah 3: Muatkan model teks Qwen3.5-4B dengan konfigurasi yang telah ditala
    gguf_info = cfg.resolve_gguf_paths()
    model_path = gguf_info["model_path"]
    llama_mod: Any = importlib.import_module("llama_cpp")
    Llama: Any = getattr(llama_mod, "Llama")

    with console.status("[bold cyan]Memuatkan Qwen3.5-4B-Q4_K_M.gguf (Tuned 4 Threads, 768 Max Tokens)...[/bold cyan]"):
        t_load = time.perf_counter()
        llm: Any = Llama(
            model_path=model_path,
            n_ctx=4096,
            n_threads=n_threads,
            n_batch=512,
            type_k=8,
            type_v=8,
            flash_attn=True,
            verbose=False,
        )
        load_sec = time.perf_counter() - t_load

    console.print(f"[bold green]✅ Model teks dimuatkan dalam {load_sec:.2f} saat ({n_threads} threads).[/bold green]\n")

    system_prompt = (
        "Anda ialah pengendali halaman arkib 'PS2 Retro Nostalgia' di Malaysia — seorang peminat tegar konsol "
        "PlayStation 2 zaman 2000-an yang berkongsi kisah nostalgia, fakta tersembunyi, dan kenangan bermain "
        "game bersama rakan-rakan dalam Bahasa Melayu Malaysia yang santai dan hidup.\n\n"
        "TUGASAN:\n"
        "1. Tulis 2 perenggan ringkas cerita nostalgia (sekitar 60 hingga 110 patah perkataan) mengenai game PS2 yang diberikan.\n"
        "2. TERUS MULA dengan situasi atau memori sebenar pemain PS2 di Malaysia (contoh: bunyi logo boot merah/biru PS2, "
        "kertas kod cheat bertulis tangan, tiup disk calar, atau berebut slot memory card 8MB).\n"
        "3. Gaya bahasa mestilah santai macam bersembang dengan rakan gamer tempatan.\n\n"
        "PANTANG LARANG KETAT:\n"
        "- DILARANG menulis tajuk atau header di baris pertama (terus masuk ayat cerita).\n"
        "- DILARANG menjual secara terus atau menyebut harga/link Shopee dalam cerita utama.\n"
        "- DILARANG menggunakan sebarang emoji atau simbol grafik.\n"
        "- DILARANG menggunakan perkataan Indonesia (bisa, banget, nggak, ngak, bikin, gampang, cobain, cocok, yuk, kalian, anda).\n"
        "- Pastikan ayat berakhir dengan tanda noktah (.) yang lengkap."
    )

    generated_results: List[Dict[str, Any]] = []

    for idx, sc in enumerate(TEST_SCENARIOS, 1):
        game_title = sc["game_title"]
        post_type = sc["post_type"]
        media_item = chosen_samples[idx - 1]
        v_info = vision_results[idx - 1] if idx - 1 < len(vision_results) else {}
        visual_context = (
            f"{media_item['description']} | "
            f"Analisis Vision: {v_info.get('retro_nostalgia_angle', 'Kontroler & konsol klasik PS2')}"
        )

        user_prompt = (
            f"Konteks Waktu Siaran: {myt_full} (Suasana {myt_period})\n"
            f"Tajuk Game PS2: {game_title}\n"
            f"Format Hantaran: Facebook {post_type.upper()}\n"
            f"Rujukan Visual: {visual_context}\n"
            f"Fokus Nostalgia: {sc['angle_hint']}\n\n"
            f"Tulis cerita nostalgia PS2 dalam Bahasa Melayu Malaysia (350 hingga 850 aksara):\n/no_think"
        )

        accepted_story = ""
        inf_meta: Dict[str, Any] = {}

        with console.status(f"[bold cyan]Menjana cerita #{idx}: {game_title} ({post_type.upper()})...[/bold cyan]"):
            for attempt in range(1, 4):
                t0 = time.perf_counter()
                res: Any = llm.create_chat_completion(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.65 + (attempt * 0.05),
                    top_p=0.90,
                    top_k=35,
                    repeat_penalty=1.16,
                    max_tokens=768,
                    stream=False,
                )
                elapsed = time.perf_counter() - t0
                raw_out = str(res["choices"][0]["message"]["content"] or "")
                cleaned = clean_and_scrub_bm_story(raw_out, max_chars=1100)
                ok_qual, qual_msg = validate_story_quality(cleaned, min_len=220, max_len=1150)

                usage = res.get("usage", {})
                comp_tok = int(usage.get("completion_tokens", len(cleaned.split())))
                inf_meta = {
                    "attempt": attempt,
                    "elapsed_sec": round(elapsed, 2),
                    "tokens": comp_tok,
                    "tok_per_sec": round(comp_tok / elapsed, 2) if elapsed > 0 else 0.0,
                    "story_chars": len(cleaned),
                    "quality_status": qual_msg,
                }

                if ok_qual:
                    accepted_story = cleaned
                    break

        if not accepted_story:
            console.print(f"[bold red]❌ Gagal menjana cerita sah untuk {game_title}: {inf_meta}[/bold red]")
            continue

        is_dup, _dup_match, vec_idx = vector_sharded.check_semantic_duplicate(
            game_title=game_title,
            candidate_story=accepted_story,
            similarity_threshold=0.86,
        )

        pkg = assemble_fb_post_package(
            game_title=game_title,
            post_type=post_type,
            story_bm=accepted_story,
            selected_media=media_item,
        )
        pkg["benchmark"] = inf_meta
        pkg["vector_shard_index"] = vec_idx
        pkg["semantic_duplicate"] = is_dup
        generated_results.append(pkg)

        preview_body = (
            f"[bold yellow]KAPSYEN HANTARAN UTAMA ({post_type.upper()} | Cerita: {pkg['story_chars']} aksara | "
            f"Jumlah Kapsyen: {pkg['caption_chars']} aksara):[/bold yellow]\n"
            f"[white]{pkg['post_caption']}[/white]\n\n"
            f"[bold green]KOMEN PERTAMA AUTOMATIK (Produk Shopee: {pkg['shopee_product_code']}):[/bold green]\n"
            f"[cyan]{pkg['first_comment']}[/cyan]\n\n"
            f"[dim]Media Tempatan: {pkg['media_source']} | "
            f"AI: {inf_meta['elapsed_sec']}s ({inf_meta['tok_per_sec']} tok/s, percubaan #{inf_meta['attempt']}) | "
            f"Vector #{vec_idx:03d}: {'UNIK' if not is_dup else 'PENDUA'}[/dim]"
        )
        console.print(
            Panel(
                preview_body,
                title=f"Pratonton #{idx} :: {game_title}",
                border_style="bold green" if not is_dup else "yellow",
            )
        )

    preview_out = cfg.TEMP_DIR / "exp05_tuned_persona_preview.json"
    preview_out.write_text(json.dumps(generated_results, indent=2, ensure_ascii=False), encoding="utf-8")

    summary_tbl = Table(title="Ringkasan Prestasi Tuned AI (EXP05)", box=box.ROUNDED, header_style="bold cyan")
    summary_tbl.add_column("Game PS2", style="bold white")
    summary_tbl.add_column("Format", justify="center")
    summary_tbl.add_column("Aksara Cerita", justify="right", style="cyan")
    summary_tbl.add_column("Masa Janaan", justify="right", style="yellow")
    summary_tbl.add_column("Kelajuan", justify="right", style="green")
    summary_tbl.add_column("Padanan Shopee", style="magenta")

    for r in generated_results:
        bm = r["benchmark"]
        summary_tbl.add_row(
            r["game_title"],
            r["post_type"].upper(),
            str(r["story_chars"]),
            f"{bm['elapsed_sec']}s",
            f"{bm['tok_per_sec']} tok/s",
            r["shopee_product_code"],
        )
    console.print(summary_tbl)
    console.print(f"[dim]Fail pratonton disimpan di: {preview_out}[/dim]")


if __name__ == "__main__":
    main()