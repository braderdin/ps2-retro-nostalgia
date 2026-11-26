#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - UJIAN PERSONA AI & PENCERITAAN PS2 (EXP04)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/exp/exp04_ps2_persona_story_debug.py
#
# FUNGSI UJIAN:
# 1. Menguji penjanaan penceritaan nostalgia PS2 Bahasa Melayu Malaysia tulen
#    menggunakan model tempatan Qwen3.5-4B-Q4_K_M.gguf.
# 2. Menjalankan penapis kualiti ketat (anti-header, anti-perkataan Indonesia,
#    anti-emoji AI, dan kawalan panjang aksara).
# 3. Menyemak skor anti-duplikasi melalui X05_vector_sharded (dry check).
# 4. Menggabungkan kapsyen hantaran FB (Photo & Reels) bersama pautan produk
#    Shopee (Memory Card / Pendrive) di ruangan komen pertama dari X02.
# ==============================================================================

import importlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

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

try:
    cfg: Any = importlib.import_module("X01_config")
    _ledger_mod: Any = importlib.import_module("X02_sqlite_ledger")
    _vector_mod: Any = importlib.import_module("X05_vector_sharded")

    ledger_db: Any = getattr(_ledger_mod, "ledger_db")
    vector_sharded: Any = getattr(_vector_mod, "vector_sharded")
except Exception as e:
    console.print(f"[bold red]❌ Gagal mengimport modul teras dari live-engine-01: {e}[/bold red]")
    sys.exit(1)

MYT = timezone(timedelta(hours=8))

# Senario ujian penceritaan klasik PS2
TEST_SCENARIOS: List[Dict[str, str]] = [
    {
        "game_title": "GTA San Andreas",
        "post_type": "reel",
        "visual_context": "Gameplay footage of CJ riding a BMX bike through Grove Street in GTA San Andreas on PS2 with classic orange sunset sky.",
        "angle_hint": "Kenangan hafal kod cheat atas kertas buku latihan sekolah dan takut memory card penuh masa nak simpan misi akhir.",
    },
    {
        "game_title": "Winning Eleven 8",
        "post_type": "photo",
        "visual_context": "Close-up of a classic black PlayStation 2 console with DualShock 2 controller and 8MB Memory Card plugged in.",
        "angle_hint": "Suasana lepak main liga dengan kawan-kawan zaman dulu, berebut pilih Roberto Carlos ambil sepakan percuma dan tiup disk bila sangkut di skrin loading.",
    },
]

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

    full_str = f"{days_bm[now.weekday()]}, {now.day} {months_bm[now.month - 1]} {now.year} ({now.strftime('%I:%M %p')} - Waktu {period})"
    return full_str, period


def clean_and_scrub_bm_story(raw_text: str) -> str:
    if not raw_text:
        return ""

    # 1. Buang blok pemikiran <think> dan pembungkus markdown
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw_text, flags=re.DOTALL)
    cleaned = re.sub(r"```[a-zA-Z]*\s*|```\s*", "", cleaned)

    # 2. Normalisasi tanda baca
    for orig, rep in {"’": "'", "‘": "'", "“": '"', "”": '"', "—": "-", "–": "-", "…": "..."}.items():
        cleaned = cleaned.replace(orig, rep)

    # 3. Buang semua emoji Unicode daripada output mentah AI
    emoji_pattern = re.compile(
        "[\U00010000-\U0010ffff\uD800-\uDBFF\uDC00-\uDFFF\u2600-\u26FF\u2700-\u27BF]",
        flags=re.UNICODE,
    )
    cleaned = emoji_pattern.sub("", cleaned).strip().strip('"').strip("'")

    # 4. Buang baris pertama jika AI cuba meletakkan tajuk/header
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

    # 6. Pastikan berhenti pada noktah/tanda seruan ayat lengkap
    match = re.search(r"^([\s\S]*[.!?])", cleaned.strip())
    if match:
        cleaned = match.group(1).strip()

    return re.sub(r"\s+", " ", cleaned).strip()


def validate_story_quality(text: str, min_len: int = 200, max_len: int = 650) -> Tuple[bool, str]:
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
            if freq[w] > 7:
                return False, f"Glitch pengulangan: perkataan '{w}' muncul {freq[w]} kali."

    return True, "Lulus"


def assemble_fb_post_package(
    game_title: str,
    post_type: str,
    story_bm: str,
) -> Dict[str, Any]:
    """
    Membina kapsyen hantaran utama + komen pertama Shopee secara automatik.
    """
    offer = ledger_db.pick_shopee_comment_offer(f"{game_title} {story_bm}")
    tag_slug = re.sub(r"[^a-zA-Z0-9]", "", game_title)

    hashtags = f"#PS2RetroNostalgia #PS2Malaysia #RetroGaming #{tag_slug} #Nostalgia2000an"

    if post_type == "reel":
        hook_footer = "Korang dulu paling kerap layan misi atau mode apa dalam game ni? Komen kat bawah!"
    else:
        hook_footer = "Siapa masih simpan konsol PS2 lama kat rumah sampai sekarang? Cer kongsi kenangan korang!"

    post_caption = (
        f"{story_bm}\n\n"
        f"{hook_footer}\n\n"
        f"{hashtags}"
    )

    first_comment = (
        f"{offer['cta_template']}\n"
        f"👉 {offer['shopee_url']}"
    )

    return {
        "game_title": game_title,
        "post_type": post_type,
        "story_bm": story_bm,
        "post_caption": post_caption,
        "caption_chars": len(post_caption),
        "shopee_product_code": offer["product_code"],
        "shopee_product_name": offer["product_name"],
        "shopee_url": offer["shopee_url"],
        "first_comment": first_comment,
    }


def main() -> None:
    myt_full, myt_period = get_myt_context()
    gguf_info = cfg.resolve_gguf_paths()
    model_path = gguf_info["model_path"]

    console.print(
        Panel.fit(
            "[bold cyan]PS2-RETRO-NOSTALGIA :: LOCAL AI PERSONA & STORYTELLING TEST (EXP04)[/bold cyan]\n"
            f"[dim]Masa MYT: {myt_full}\nModel: {model_path}[/dim]",
            border_style="cyan",
        )
    )

    if not Path(model_path).exists():
        console.print(f"[bold red]❌ Fail model GGUF tidak ditemui: {model_path}[/bold red]")
        sys.exit(1)

    llama_mod: Any = importlib.import_module("llama_cpp")
    Llama: Any = getattr(llama_mod, "Llama")

    cpu_count = os.cpu_count() or 4
    n_threads = max(2, min(6, cpu_count // 2))

    with console.status("[bold cyan]Memuatkan Qwen3.5-4B-Q4_K_M.gguf ke dalam memori...[/bold cyan]"):
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

    console.print(f"[bold green]✅ Model berjaya dimuatkan dalam {load_sec:.2f} saat ({n_threads} threads).[/bold green]\n")

    system_prompt = (
        "Anda ialah pengendali halaman arkib 'PS2 Retro Nostalgia' di Malaysia — seorang peminat tegar konsol "
        "PlayStation 2 zaman 2000-an yang suka berkongsi kisah nostalgia, fakta tersembunyi, dan kenangan "
        "bermain game bersama rakan-rakan dalam Bahasa Melayu Malaysia yang santai dan hidup.\n\n"
        "TUGASAN:\n"
        "1. Tulis 1 hingga 2 perenggan pendek cerita nostalgia (sekitar 45 hingga 75 patah perkataan) mengenai game PS2 yang diberikan.\n"
        "2. TERUS MULA dengan situasi atau memori sebenar pemain PS2 di Malaysia (contoh: bunyi logo boot merah/biru PS2, "
        "kertas kod cheat bertulis tangan, tiup disk calar, atau berebut slot memory card 8MB).\n"
        "3. Gaya bahasa mestilah santai macam bersembang dengan rakan gamer tempatan.\n\n"
        "PANTANG LARANG KETAT:\n"
        "- DILARANG menulis tajuk atau header di baris pertama (terus masuk ayat cerita).\n"
        "- DILARANG menjual secara terus atau menyebut harga/link Shopee dalam cerita utama (link akan diletakkan di komen pertama oleh sistem).\n"
        "- DILARANG menggunakan sebarang emoji atau simbol grafik.\n"
        "- DILARANG menggunakan perkataan Indonesia (seperti bisa, banget, nggak, ngak, bikin, gampang, cobain, cocok, yuk, kalian, anda).\n"
        "- Pastikan ayat berakhir dengan tanda noktah (.) yang lengkap."
    )

    generated_results: List[Dict[str, Any]] = []

    for idx, sc in enumerate(TEST_SCENARIOS, 1):
        game_title = sc["game_title"]
        post_type = sc["post_type"]

        user_prompt = (
            f"Konteks Waktu Siaran: {myt_full} (Suasana {myt_period})\n"
            f"Tajuk Game PS2: {game_title}\n"
            f"Format Hantaran: Facebook {post_type.upper()}\n"
            f"Rujukan Visual: {sc['visual_context']}\n"
            f"Fokus Nostalgia: {sc['angle_hint']}\n\n"
            f"Tulis cerita nostalgia PS2 dalam Bahasa Melayu Malaysia (250 hingga 520 aksara):\n/no_think"
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
                    top_k=30,
                    repeat_penalty=1.18,
                    max_tokens=512,
                    stream=False,
                )
                elapsed = time.perf_counter() - t0
                raw_out = str(res["choices"][0]["message"]["content"] or "")
                cleaned = clean_and_scrub_bm_story(raw_out)
                ok_qual, qual_msg = validate_story_quality(cleaned, min_len=200, max_len=650)

                usage = res.get("usage", {})
                comp_tok = int(usage.get("completion_tokens", len(cleaned.split())))
                inf_meta = {
                    "attempt": attempt,
                    "elapsed_sec": round(elapsed, 2),
                    "tokens": comp_tok,
                    "tok_per_sec": round(comp_tok / elapsed, 2) if elapsed > 0 else 0.0,
                    "quality_status": qual_msg,
                }

                if ok_qual:
                    accepted_story = cleaned
                    break

        if not accepted_story:
            console.print(f"[bold red]❌ Gagal menjana cerita sah untuk {game_title}: {inf_meta}[/bold red]")
            continue

        # Semakan anti-duplikasi semantik melalui X05 (tanpa simpan rekod ujian)
        is_dup, _dup_match, vec_idx = vector_sharded.check_semantic_duplicate(
            game_title=game_title,
            candidate_story=accepted_story,
            similarity_threshold=0.86,
        )

        pkg = assemble_fb_post_package(game_title=game_title, post_type=post_type, story_bm=accepted_story)
        pkg["benchmark"] = inf_meta
        pkg["vector_shard_index"] = vec_idx
        pkg["semantic_duplicate"] = is_dup
        generated_results.append(pkg)

        # Paparan Pratonton Hantaran FB & Komen Pertama
        preview_body = (
            f"[bold yellow]KAPSYEN HANTARAN UTAMA ({post_type.upper()} | {pkg['caption_chars']} aksara):[/bold yellow]\n"
            f"[white]{pkg['post_caption']}[/white]\n\n"
            f"[bold green]KOMEN PERTAMA AUTOMATIK (Produk Shopee: {pkg['shopee_product_code']}):[/bold green]\n"
            f"[cyan]{pkg['first_comment']}[/cyan]\n\n"
            f"[dim]Metrik AI: {inf_meta['elapsed_sec']}s ({inf_meta['tok_per_sec']} tok/s, percubaan #{inf_meta['attempt']}) | "
            f"Vector Indeks #{vec_idx:03d}: {'UNIK' if not is_dup else 'PENDUA'}[/dim]"
        )
        console.print(
            Panel(
                preview_body,
                title=f"Pratonton #{idx} :: {game_title}",
                border_style="bold green" if not is_dup else "yellow",
            )
        )

    # Simpan hasil ujian ke folder temp/ untuk semakan lanjut
    preview_out = cfg.TEMP_DIR / "exp04_persona_preview.json"
    preview_out.write_text(json.dumps(generated_results, indent=2, ensure_ascii=False), encoding="utf-8")

    summary_tbl = Table(title="Ringkasan Prestasi Persona AI (EXP04)", box=box.ROUNDED, header_style="bold cyan")
    summary_tbl.add_column("Game PS2", style="bold white")
    summary_tbl.add_column("Format", justify="center")
    summary_tbl.add_column("Masa Janaan", justify="right", style="yellow")
    summary_tbl.add_column("Kelajuan", justify="right", style="green")
    summary_tbl.add_column("Padanan Shopee", style="magenta")

    for r in generated_results:
        bm = r["benchmark"]
        summary_tbl.add_row(
            r["game_title"],
            r["post_type"].upper(),
            f"{bm['elapsed_sec']}s",
            f"{bm['tok_per_sec']} tok/s",
            r["shopee_product_code"],
        )
    console.print(summary_tbl)
    console.print(f"[dim]Fail pratonton disimpan di: {preview_out}[/dim]")


if __name__ == "__main__":
    main()