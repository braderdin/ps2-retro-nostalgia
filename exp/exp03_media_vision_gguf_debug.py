#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - DIAGNOSTIK UNSPLASH, PEXELS & VISION GGUF (EXP03)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/exp/exp03_media_vision_gguf_debug.py
#
# KAWALAN & HAD KETAT:
# 1. Unsplash API : Maksimum 40 gambar per request (ujian tarik 5 gambar).
# 2. Pexels API   : Maksimum 70 video per request (ujian tarik 5 video).
# 3. Vision/OCR   : Setiap gambar dimampatkan STRICT < 30 KB (sasaran <= 28 KB)
#                   dan maksimum 5 gambar sahaja dihantar per request model.
# ==============================================================================

import base64
import importlib
import json
import os
import re
import shutil
import sys
import time
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

try:
    cfg: Any = importlib.import_module("X01_config")
except Exception as e:
    console.print(f"[bold red]❌ Gagal mengimport modul X01_config: {e}[/bold red]")
    sys.exit(1)

# Had keselamatan mengikut spesifikasi projek
UNSPLASH_HARD_MAX_PER_REQ = 40
PEXELS_HARD_MAX_PER_REQ = 70
VISION_MAX_IMAGES_PER_REQ = 5
VISION_MAX_IMAGE_BYTES = 30 * 1024       # Wajib bawah 30 KB (30,720 bait)
VISION_TARGET_IMAGE_BYTES = 28 * 1024    # Sasaran selamat 28 KB

PROBE_TEMP_DIR = cfg.TEMP_DIR / "exp03_vision_probe"


def mask_key(val: str, head: int = 6, tail: int = 5) -> str:
    if not val:
        return "-"
    if len(val) <= (head + tail):
        return "*" * len(val)
    return f"{val[:head]}...{val[-tail:]}"


# ==============================================================================
# 1. UJIAN API UNSPLASH (MAX 40 GAMBAR / REQUEST)
# ==============================================================================
def fetch_unsplash_test_photos(
    query: str = "playstation retro gaming controller",
    count: int = 5,
) -> Tuple[bool, List[Dict[str, Any]], str]:
    api_key = cfg.PS2N_UNSPLASH_ACCESS_KEY
    if not api_key:
        return False, [], "PS2N_UNSPLASH_ACCESS_KEY tiada dalam .env.local"

    safe_per_page = min(max(1, count), UNSPLASH_HARD_MAX_PER_REQ, 30)
    url = "https://api.unsplash.com/search/photos"
    headers = {
        "Authorization": f"Client-ID {api_key}",
        "Accept-Version": "v1",
    }
    params = {
        "query": query,
        "per_page": safe_per_page,
        "orientation": "landscape",
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=20)
        remaining = resp.headers.get("X-Ratelimit-Remaining", "?")
        if resp.status_code != 200:
            return False, [], f"HTTP {resp.status_code}: {resp.text[:140]}"

        results = resp.json().get("results", [])
        photos = []
        for item in results[:count]:
            urls = item.get("urls", {})
            photos.append(
                {
                    "id": item.get("id", ""),
                    "description": item.get("alt_description") or item.get("description") or "Retro Gaming Visual",
                    "width": item.get("width", 0),
                    "height": item.get("height", 0),
                    "download_url": urls.get("small") or urls.get("regular"),
                    "photographer": item.get("user", {}).get("name", "Unsplash"),
                    "ratelimit_remaining": remaining,
                }
            )
        return True, photos, f"Berjaya tarik {len(photos)} gambar (Baki RateLimit/Jam: {remaining})"
    except Exception as exc:
        return False, [], str(exc)


# ==============================================================================
# 2. UJIAN API PEXELS VIDEO (MAX 70 VIDEO / REQUEST)
# ==============================================================================
def fetch_pexels_test_videos(
    query: str = "retro arcade video game",
    count: int = 5,
) -> Tuple[bool, List[Dict[str, Any]], str]:
    api_key = cfg.PS2N_PEXELS_API_KEY
    if not api_key:
        return False, [], "PS2N_PEXELS_API_KEY tiada dalam .env.local"

    safe_per_page = min(max(1, count), PEXELS_HARD_MAX_PER_REQ)
    url = "https://api.pexels.com/videos/search"
    headers = {"Authorization": api_key}
    params = {
        "query": query,
        "per_page": safe_per_page,
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=20)
        remaining = resp.headers.get("X-Ratelimit-Remaining", "?")
        if resp.status_code != 200:
            return False, [], f"HTTP {resp.status_code}: {resp.text[:140]}"

        raw_videos = resp.json().get("videos", [])
        videos = []
        for v in raw_videos[:count]:
            v_files = v.get("video_files", [])
            mp4_files = [f for f in v_files if f.get("file_type") == "video/mp4"]
            mp4_files.sort(key=lambda x: int(x.get("width") or 0))
            chosen_file = mp4_files[0] if mp4_files else (v_files[0] if v_files else {})

            videos.append(
                {
                    "id": v.get("id", ""),
                    "duration": v.get("duration", 0),
                    "width": chosen_file.get("width", 0),
                    "height": chosen_file.get("height", 0),
                    "link": chosen_file.get("link", ""),
                    "user": v.get("user", {}).get("name", "Pexels"),
                    "ratelimit_remaining": remaining,
                }
            )
        return True, videos, f"Berjaya tarik {len(videos)} video (Baki RateLimit: {remaining})"
    except Exception as exc:
        return False, [], str(exc)


# ==============================================================================
# 3. PEMAMPAT IMEJ STRICT < 30 KB UNTUK VISION / OCR PROJECTOR
# ==============================================================================
def download_and_compress_under_30kb(
    image_url: str,
    save_path: Path,
    max_bytes: int = VISION_TARGET_IMAGE_BYTES,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Memuat turun gambar dan memampatkannya sehingga saiz fail wajib < 30 KB
    sambil mengekalkan kejelasan teks OCR dan subjek utama.
    """
    try:
        resp = requests.get(image_url, timeout=25)
        if resp.status_code != 200:
            return False, {"error": f"HTTP {resp.status_code}"}

        raw_bytes = len(resp.content)
        img = Image.open(BytesIO(resp.content))
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")

        max_dim = 512
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)

        quality = 78
        best_buf: Optional[bytes] = None

        while True:
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            best_buf = data

            if len(data) <= max_bytes:
                break

            if quality > 22:
                quality -= 8
            else:
                new_w = max(160, int(img.width * 0.80))
                new_h = max(160, int(img.height * 0.80))
                if new_w == img.width and new_h == img.height:
                    break
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                quality = 55

        if not best_buf or len(best_buf) >= VISION_MAX_IMAGE_BYTES:
            return False, {"error": f"Gagal mampat bawah 30KB ({len(best_buf or b'')} B)"}

        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(best_buf)
        b64_str = base64.b64encode(best_buf).decode("utf-8")

        return True, {
            "path": str(save_path),
            "raw_kb": round(raw_bytes / 1024, 2),
            "compressed_kb": round(len(best_buf) / 1024, 2),
            "compressed_bytes": len(best_buf),
            "dimensions": f"{img.width}x{img.height}",
            "quality": quality,
            "data_url": f"data:image/jpeg;base64,{b64_str}",
        }
    except Exception as exc:
        return False, {"error": str(exc)}


# ==============================================================================
# 4. ENJIN VISION & OCR TEMPATAN (Qwen3.5-4B-Q4_K_M + mmproj-F16)
# ==============================================================================
def load_vision_chat_handler(mmproj_path: str) -> Tuple[Any, str]:
    chat_fmt: Any = importlib.import_module("llama_cpp.llama_chat_format")

    candidates = [
        "Qwen3VLChatHandler",
        "Qwen25VLChatHandler",
        "Qwen2VLChatHandler",
        "Llava15ChatHandler",
    ]
    last_err = ""
    for handler_name in candidates:
        handler_cls = getattr(chat_fmt, handler_name, None)
        if handler_cls is not None:
            try:
                handler = handler_cls(clip_model_path=mmproj_path, verbose=False)
                return handler, handler_name
            except Exception as exc:
                last_err = f"{handler_name}: {exc}"
    raise RuntimeError(f"Tiada vision chat handler serasi ditemui ({last_err})")


def run_batch_vision_ocr_inference(
    compressed_items: List[Dict[str, Any]],
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Menghantar sehingga 5 keping gambar (< 30 KB setiap satu) dalam 1 request
    kepada model tempatan Qwen3.5-4B + mmproj-F16.
    """
    batch = compressed_items[:VISION_MAX_IMAGES_PER_REQ]
    if not batch:
        return False, "Tiada gambar mampat untuk diuji.", {}

    gguf_info = cfg.resolve_gguf_paths()
    model_path = gguf_info["model_path"]
    mmproj_path = gguf_info["mmproj_path"]

    if not Path(model_path).exists() or not Path(mmproj_path).exists():
        return False, f"Fail GGUF tidak ditemui di {cfg.PS2N_LOCAL_GGUF_DIR}", {}

    llama_mod: Any = importlib.import_module("llama_cpp")
    Llama: Any = getattr(llama_mod, "Llama")

    cpu_count = os.cpu_count() or 4
    n_threads = max(2, min(6, cpu_count // 2))

    t_load_start = time.perf_counter()
    chat_handler, handler_name = load_vision_chat_handler(mmproj_path)

    llm: Any = Llama(
        model_path=model_path,
        chat_handler=chat_handler,
        n_ctx=8192,
        n_threads=n_threads,
        n_batch=512,
        type_k=8,
        type_v=8,
        flash_attn=True,
        verbose=False,
    )
    load_sec = time.perf_counter() - t_load_start

    prompt_text = (
        f"You are a visual & OCR analyst for a PlayStation 2 (PS2) & retro gaming archive page.\n"
        f"Analyze all {len(batch)} attached images in order (Image 1 to Image {len(batch)}).\n"
        "Return ONLY a valid JSON object with key 'images' containing a list of objects, each having:\n"
        "- image_number (1..N)\n"
        "- detected_objects (main console, controller, screen, or gaming hardware)\n"
        "- ocr_text (any visible brand, button text, label, or 'None')\n"
        "- retro_nostalgia_angle (1 short sentence in Bahasa Melayu describing the nostalgia vibe)\n"
        "Do not include markdown code fences or extra commentary.\n/no_think"
    )

    user_content: List[Dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    for item in batch:
        user_content.append({"type": "image_url", "image_url": {"url": item["data_url"]}})

    t_inf_start = time.perf_counter()
    response: Any = llm.create_chat_completion(
        messages=[
            {
                "role": "system",
                "content": "You are a precise multimodal vision and OCR engine. Output strict valid JSON only.",
            },
            {"role": "user", "content": user_content},
        ],
        temperature=0.2,
        top_p=0.9,
        max_tokens=1024,
        stream=False,
    )
    inf_sec = time.perf_counter() - t_inf_start

    raw_output = str(response["choices"][0]["message"]["content"] or "")
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw_output, flags=re.DOTALL)
    cleaned = re.sub(r"```json\s*|```\s*", "", cleaned).strip()

    usage = response.get("usage", {})
    comp_tokens = int(usage.get("completion_tokens", len(cleaned.split())))
    tok_per_sec = comp_tokens / inf_sec if inf_sec > 0 else 0.0

    meta = {
        "handler": handler_name,
        "n_threads": n_threads,
        "load_seconds": round(load_sec, 2),
        "inference_seconds": round(inf_sec, 2),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": comp_tokens,
        "tokens_per_second": round(tok_per_sec, 2),
    }
    return True, cleaned, meta


def main() -> None:
    vm = psutil.virtual_memory()
    console.print(
        Panel.fit(
            "[bold cyan]PS2-RETRO-NOSTALGIA :: UNSPLASH, PEXELS & LOCAL VISION GGUF TEST (EXP03)[/bold cyan]\n"
            f"[dim]RAM Sistem: {vm.available / (1024**3):.2f} GB tersedia / {vm.total / (1024**3):.2f} GB jumlah | "
            f"CPU Threads: {os.cpu_count()}[/dim]",
            border_style="cyan",
        )
    )

    # 1. Audit Kunci Media & Laluan GGUF
    gguf_info = cfg.resolve_gguf_paths()
    audit_tbl = Table(title="1. Audit Kunci Media & Fail Model GGUF Tempatan", box=box.ROUNDED, header_style="bold magenta")
    audit_tbl.add_column("Komponen", style="bold white")
    audit_tbl.add_column("Nilai / Laluan", style="cyan")
    audit_tbl.add_column("Had / Saiz", justify="right", style="yellow")
    audit_tbl.add_column("Status", justify="center")

    audit_tbl.add_row(
        "PS2N_UNSPLASH_ACCESS_KEY",
        mask_key(cfg.PS2N_UNSPLASH_ACCESS_KEY),
        f"Max {UNSPLASH_HARD_MAX_PER_REQ} img/req",
        "[bold green]ADA[/bold green]" if cfg.PS2N_UNSPLASH_ACCESS_KEY else "[bold red]TIADA[/bold red]",
    )
    audit_tbl.add_row(
        "PS2N_PEXELS_API_KEY",
        mask_key(cfg.PS2N_PEXELS_API_KEY),
        f"Max {PEXELS_HARD_MAX_PER_REQ} vid/req",
        "[bold green]ADA[/bold green]" if cfg.PS2N_PEXELS_API_KEY else "[bold red]TIADA[/bold red]",
    )

    m_path = Path(gguf_info["model_path"])
    p_path = Path(gguf_info["mmproj_path"])
    m_gb = f"{m_path.stat().st_size / (1024**3):.2f} GB" if m_path.exists() else "-"
    p_mb = f"{p_path.stat().st_size / (1024**2):.0f} MB" if p_path.exists() else "-"

    audit_tbl.add_row(
        "Qwen3.5-4B Model GGUF",
        str(m_path),
        m_gb,
        "[bold green]WUJUD[/bold green]" if m_path.exists() else "[bold red]TIADA[/bold red]",
    )
    audit_tbl.add_row(
        "Vision Projector (mmproj)",
        str(p_path),
        p_mb,
        "[bold green]WUJUD[/bold green]" if p_path.exists() else "[bold red]TIADA[/bold red]",
    )
    console.print(audit_tbl)

    # 2. Ujian Unsplash & Pexels API
    with console.status("[bold cyan]Menguji carian Unsplash (5 gambar) & Pexels (5 video)...[/bold cyan]"):
        u_ok, u_photos, u_msg = fetch_unsplash_test_photos(count=VISION_MAX_IMAGES_PER_REQ)
        p_ok, p_videos, p_msg = fetch_pexels_test_videos(count=5)

    media_tbl = Table(title="2. Keputusan Ujian API Unsplash & Pexels", box=box.ROUNDED, header_style="bold green")
    media_tbl.add_column("Sumber API", style="bold white")
    media_tbl.add_column("Keputusan Carian", style="cyan")
    media_tbl.add_column("Status", justify="center")
    media_tbl.add_row("Unsplash Photos", u_msg, "[bold green]OK[/bold green]" if u_ok else "[bold red]GAGAL[/bold red]")
    media_tbl.add_row("Pexels Videos", p_msg, "[bold green]OK[/bold green]" if p_ok else "[bold red]GAGAL[/bold red]")
    console.print(media_tbl)

    if p_ok and p_videos:
        vid_tbl = Table(title="Sampel 5 Video Pexels Ditemui", box=box.SIMPLE)
        vid_tbl.add_column("#", style="cyan", width=4)
        vid_tbl.add_column("ID Video", style="white")
        vid_tbl.add_column("Resolusi", justify="center")
        vid_tbl.add_column("Durasi", justify="right")
        vid_tbl.add_column("Pencipta", style="dim")
        for idx, v in enumerate(p_videos, 1):
            vid_tbl.add_row(str(idx), str(v["id"]), f"{v['width']}x{v['height']}", f"{v['duration']}s", v["user"])
        console.print(vid_tbl)

    if not u_ok or not u_photos:
        console.print("[bold red]❌ Tidak dapat meneruskan ujian Vision kerana tiada gambar Unsplash diterima.[/bold red]")
        sys.exit(1)

    # 3. Muat Turun & Mampatkan 5 Gambar (< 30 KB setiap satu)
    if PROBE_TEMP_DIR.exists():
        shutil.rmtree(PROBE_TEMP_DIR, ignore_errors=True)
    PROBE_TEMP_DIR.mkdir(parents=True, exist_ok=True)

    compressed_batch: List[Dict[str, Any]] = []
    comp_tbl = Table(
        title=f"3. Pemampatan Imej Vision (Wajib < 30 KB | Maks {VISION_MAX_IMAGES_PER_REQ} Gambar)",
        box=box.ROUNDED,
        header_style="bold yellow",
    )
    comp_tbl.add_column("#", justify="center", style="cyan")
    comp_tbl.add_column("ID Unsplash", style="white")
    comp_tbl.add_column("Saiz Asal", justify="right", style="dim")
    comp_tbl.add_column("Saiz Mampat", justify="right", style="bold green")
    comp_tbl.add_column("Dimensi / Q", justify="center")
    comp_tbl.add_column("Had < 30KB", justify="center")

    try:
        with console.status("[bold cyan]Memuat turun dan memampatkan 5 gambar di bawah 30 KB...[/bold cyan]"):
            for idx, photo in enumerate(u_photos[:VISION_MAX_IMAGES_PER_REQ], 1):
                out_jpg = PROBE_TEMP_DIR / f"vision_sample_{idx:02d}.jpg"
                c_ok, c_info = download_and_compress_under_30kb(photo["download_url"], out_jpg)
                if c_ok:
                    c_info["unsplash_id"] = photo["id"]
                    compressed_batch.append(c_info)
                    comp_tbl.add_row(
                        str(idx),
                        photo["id"],
                        f"{c_info['raw_kb']:.1f} KB",
                        f"{c_info['compressed_kb']:.2f} KB",
                        f"{c_info['dimensions']} (Q{c_info['quality']})",
                        "[bold green]LULUS (<30KB)[/bold green]",
                    )
                else:
                    comp_tbl.add_row(
                        str(idx),
                        photo["id"],
                        "-",
                        "-",
                        "-",
                        f"[bold red]{c_info.get('error', 'GAGAL')}[/bold red]",
                    )
        console.print(comp_tbl)

        # 4. Jalankan Inferens Multimodal Vision + OCR (5 gambar sekaligus)
        with console.status(
            f"[bold cyan]Memuatkan Qwen3.5-4B + mmproj-F16 dan menganalisis {len(compressed_batch)} gambar...[/bold cyan]"
        ):
            v_ok, v_raw, v_meta = run_batch_vision_ocr_inference(compressed_batch)

        if not v_ok:
            console.print(Panel(f"[bold red]Ralat Vision Inference:[/bold red] {v_raw}", border_style="red"))
            sys.exit(1)

        perf_tbl = Table(title="4. Prestasi Enjin Vision & OCR Tempatan", box=box.ROUNDED, header_style="bold cyan")
        perf_tbl.add_column("Metrik", style="bold white")
        perf_tbl.add_column("Nilai", style="cyan")
        perf_tbl.add_row("Chat Handler Aktif", str(v_meta["handler"]))
        perf_tbl.add_row("Bilangan Gambar Dihantar", f"{len(compressed_batch)} gambar (< 30 KB setiap satu)")
        perf_tbl.add_row("Masa Muat Model + Projector", f"{v_meta['load_seconds']} saat")
        perf_tbl.add_row("Masa Analisis Visual + OCR", f"{v_meta['inference_seconds']} saat")
        perf_tbl.add_row(
            "Kelajuan Janaan",
            f"{v_meta['tokens_per_second']} tok/s ({v_meta['completion_tokens']} token output)",
        )
        console.print(perf_tbl)

        pretty_output = v_raw
        json_match = re.search(r"\{[\s\S]*\}", v_raw)
        if json_match:
            try:
                parsed = json.loads(json_match.group(0))
                pretty_output = json.dumps(parsed, indent=2, ensure_ascii=False)
            except Exception:
                pass

        console.print(
            Panel(
                pretty_output,
                title="5. Hasil Analisis Visual & OCR (Qwen3.5-4B + mmproj-F16)",
                border_style="bold green",
            )
        )
    finally:
        if PROBE_TEMP_DIR.exists():
            shutil.rmtree(PROBE_TEMP_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()