#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - PEMAMPAT IMEJ & ENJIN VISION/OCR (X07)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/live-engine-01/X07_vision_ocr_analyzer.py
#
# FUNGSI UTAMA:
# 1. Memampatkan sebarang gambar (dari laluan fail tempatan, URL HTTP/Reddit/Imgur,
#    PIL Image, atau bait mentah) supaya saiznya WAJIB < 30 KB (sasaran <= 28 KB).
# 2. Menyokong pelarasan dimensi dinamik (`max_dim=512` untuk OCR teks tajam
#    seperti X13/dokumen, atau `max_dim=384` untuk analisis babak video pantas X16).
# 3. Menggunakan projektor multimodal Qwen3.5-4B-Q4_K_M.gguf + mmproj-F16.gguf
#    dengan `n_threads_batch`, `repeat_penalty`, dan Penyelamat JSON 3-Lapisan.
# 4. Pengesanan objek, teks OCR, kesesuaian PS2, pengesanan wajah manusia, dan
#    pengkelasan kategori visual untuk X05_vector_sharded.py.
# ==============================================================================

import base64
import gc
import html
import importlib
import json
import os
import re
import sys
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import requests
from PIL import Image

# ------------------------------------------------------------------------------
# IMPORT KONFIGURASI PROJEK (X01_config)
# ------------------------------------------------------------------------------
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for p in [CURRENT_DIR, PROJECT_ROOT]:
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

try:
    cfg: Any = importlib.import_module("X01_config")
except Exception:
    cfg = None


# ==============================================================================
# [BAHAGIAN TUNING MANUAL VISION & PEMAMPATAN] - OPTIMUM CPU & GITHUB ACTIONS
# ==============================================================================
VISION_HARD_MAX_BYTES: int = 30 * 1024  # 30,720 bait (had keras < 30 KB)
VISION_TARGET_BYTES: int = int(os.getenv("PS2N_VISION_TARGET_BYTES", str(28 * 1024)))
VISION_INITIAL_MAX_DIM: int = int(os.getenv("PS2N_VISION_MAX_DIM", "512"))
VISION_INITIAL_QUALITY: int = int(os.getenv("PS2N_VISION_INIT_QUALITY", "78"))
VISION_MAX_IMAGES_PER_REQ: int = 5

VISION_TEMPERATURE: float = float(os.getenv("PS2N_VISION_TEMP", "0.22"))
VISION_TOP_P: float = float(os.getenv("PS2N_VISION_TOP_P", "0.90"))
VISION_REPEAT_PENALTY: float = float(os.getenv("PS2N_VISION_REPEAT_PENALTY", "1.18"))
VISION_MAX_TOKENS: int = int(os.getenv("PS2N_VISION_MAX_TOKENS", "960"))
VISION_N_CTX: int = int(os.getenv("PS2N_VISION_N_CTX", "3584"))
VISION_N_THREADS: int = int(os.getenv("PS2N_VISION_N_THREADS", str(min(6, max(4, os.cpu_count() or 4)))))
VISION_N_BATCH: int = int(os.getenv("PS2N_VISION_N_BATCH", "512"))
VISION_KV_CACHE_TYPE: int = int(os.getenv("PS2N_VISION_KV_CACHE_TYPE", "8"))
VISION_FLASH_ATTN: bool = os.getenv("PS2N_VISION_FLASH_ATTN", "true").lower() in ("1", "true", "yes")

HF_DEFAULT_REPO_ID: str = "unsloth/Qwen3.5-4B-GGUF"
HF_DEFAULT_MODEL_FILE: str = "Qwen3.5-4B-Q4_K_M.gguf"
HF_DEFAULT_MMPROJ_FILE: str = "mmproj-F16.gguf"
# ==============================================================================

HTTP_IMAGE_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/jpeg,image/png,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

COMPETITOR_CONSOLE_TERMS = (
    "nintendo",
    "super nintendo",
    "snes",
    "famicom",
    "gamecube",
    "game boy",
    "gameboy",
    "n64",
    "nintendo 64",
    "wii",
    "switch",
    "xbox",
    "sega",
    "dreamcast",
    "megadrive",
)

VALID_VISUAL_CATEGORIES = {
    "cat_controller",
    "cat_console_setup",
    "cat_multiplayer",
    "cat_storage_tech",
    "cat_reels_clip",
}


def resolve_vision_model_files() -> Tuple[str, str, str]:
    repo_id = HF_DEFAULT_REPO_ID
    model_file = HF_DEFAULT_MODEL_FILE
    mmproj_file = HF_DEFAULT_MMPROJ_FILE
    local_model: Optional[Path] = None
    local_mmproj: Optional[Path] = None

    if cfg is not None and hasattr(cfg, "resolve_gguf_paths"):
        info = cfg.resolve_gguf_paths()
        repo_id = info.get("repo_id", repo_id)
        if info.get("model_path"):
            local_model = Path(info["model_path"])
            model_file = local_model.name or model_file
        if info.get("mmproj_path"):
            local_mmproj = Path(info["mmproj_path"])
            mmproj_file = local_mmproj.name or mmproj_file

    local_dir = Path(os.getenv("PS2N_LOCAL_GGUF_DIR", "/home/braderdin/X-HF-model-gguf"))
    if local_model is None:
        local_model = local_dir / model_file
    if local_mmproj is None:
        local_mmproj = local_dir / mmproj_file

    if (
        local_model.exists()
        and local_mmproj.exists()
        and local_model.stat().st_size > 100 * 1024 * 1024
        and local_mmproj.stat().st_size > 50 * 1024 * 1024
    ):
        return str(local_model), str(local_mmproj), "LOCAL_DISK"

    hf_hub: Any = importlib.import_module("huggingface_hub")
    hf_hub_download: Any = getattr(hf_hub, "hf_hub_download")
    token = os.getenv("HF_TOKEN") or None

    cached_model = hf_hub_download(repo_id=repo_id, filename=model_file, token=token)
    cached_mmproj = hf_hub_download(repo_id=repo_id, filename=mmproj_file, token=token)
    return str(cached_model), str(cached_mmproj), "HF_HUB_CACHE"


def _download_http_image_bytes(raw_url: str) -> Tuple[bool, bytes, str]:
    clean_url = html.unescape(raw_url).strip().replace("&amp;", "&")
    if not clean_url.startswith(("http://", "https://")):
        return False, b"", "URL tidak sah"

    headers = dict(HTTP_IMAGE_HEADERS)
    if "redd.it" in clean_url or "reddit.com" in clean_url:
        headers["Referer"] = "https://www.reddit.com/"
    elif "imgur.com" in clean_url:
        headers["Referer"] = "https://imgur.com/"

    try:
        resp = requests.get(clean_url, headers=headers, timeout=25, allow_redirects=True)
        if resp.status_code != 200 or not resp.content:
            return False, b"", f"Gagal muat turun URL (HTTP {resp.status_code})"
        if len(resp.content) < 512:
            return False, b"", "Saiz respons imej terlalu kecil (< 512 B)"
        return True, resp.content, clean_url
    except Exception as exc:
        return False, b"", f"Ralat sambungan URL: {exc}"


def compress_image_under_30kb(
    source: Union[str, Path, bytes, Image.Image],
    save_path: Optional[Union[str, Path]] = None,
    target_bytes: int = VISION_TARGET_BYTES,
    initial_max_dim: int = VISION_INITIAL_MAX_DIM,
    initial_quality: int = VISION_INITIAL_QUALITY,
) -> Tuple[bool, Dict[str, Any]]:
    try:
        raw_size = 0
        source_label = "memory_object"
        img: Image.Image

        if isinstance(source, Image.Image):
            img = source.copy()
            source_label = "pil_image"
        elif isinstance(source, bytes):
            raw_size = len(source)
            source_label = "raw_bytes"
            img = Image.open(BytesIO(source))
        elif isinstance(source, (str, Path)):
            src_str = str(source).strip()
            if src_str.startswith(("http://", "https://")):
                dl_ok, dl_bytes, clean_or_err = _download_http_image_bytes(src_str)
                if not dl_ok:
                    return False, {"error": clean_or_err}
                raw_size = len(dl_bytes)
                source_label = clean_or_err
                img = Image.open(BytesIO(dl_bytes))
            else:
                p = Path(src_str)
                if not p.exists():
                    return False, {"error": f"Fail imej tidak ditemui: {p}"}
                raw_size = p.stat().st_size
                source_label = p.name
                img = Image.open(p)
        else:
            return False, {"error": "Jenis input imej tidak disokong."}

        if img.mode != "RGB":
            img = img.convert("RGB")

        img.thumbnail((initial_max_dim, initial_max_dim), Image.Resampling.LANCZOS)

        safe_target = min(target_bytes, VISION_HARD_MAX_BYTES - 512)
        quality = initial_quality
        best_buf: Optional[bytes] = None

        while True:
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            best_buf = data

            if len(data) <= safe_target:
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

        if not best_buf or len(best_buf) >= VISION_HARD_MAX_BYTES:
            return False, {"error": f"Gagal memampat imej di bawah 30 KB ({len(best_buf or b'')} B)"}

        saved_to = ""
        if save_path is not None:
            out_p = Path(save_path)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_bytes(best_buf)
            saved_to = str(out_p)

        b64_str = base64.b64encode(best_buf).decode("utf-8")
        return True, {
            "source_ref": source_label,
            "saved_path": saved_to,
            "raw_kb": round(raw_size / 1024, 2) if raw_size > 0 else 0.0,
            "compressed_bytes": len(best_buf),
            "compressed_kb": round(len(best_buf) / 1024, 2),
            "dimensions": f"{img.width}x{img.height}",
            "quality": quality,
            "data_url": f"data:image/jpeg;base64,{b64_str}",
        }
    except Exception as exc:
        return False, {"error": str(exc)}


def _extract_vision_items_resilient(cleaned_text: str, expected_count: int) -> List[Dict[str, Any]]:
    """
    Menghuraikan output JSON VLM dengan 3 lapisan perlindungan:
    1. json.loads() standard jika JSON sah.
    2. Pembaikan kurungan penutup jika output VLM terpotong pada max_tokens.
    3. Penyelamat medan (field salvage) jika ocr_text mengandungi tanda petik berganda
       tidak sah atau senarai perkataan berulang.
    """
    # Lapisan 1: JSON sah terus
    json_match = re.search(r"\{[\s\S]*\}", cleaned_text)
    if json_match:
        try:
            data_obj = json.loads(json_match.group(0))
            imgs = data_obj.get("images", [])
            if isinstance(imgs, list) and imgs:
                return [x for x in imgs if isinstance(x, dict)]
        except Exception:
            pass

    # Lapisan 2: Cuba tutup kurungan jika terpotong di hujung
    start_idx = cleaned_text.find("{")
    if start_idx != -1:
        frag = cleaned_text[start_idx:].rstrip().rstrip(",")
        for suffix in ('"}]}', '"]}', '"}]}', ']}', '}'):
            try:
                repaired = json.loads(frag + suffix)
                imgs = repaired.get("images", [])
                if isinstance(imgs, list) and imgs:
                    return [x for x in imgs if isinstance(x, dict)]
            except Exception:
                continue

    # Lapisan 3: Penyelamat medan secara regex
    salvaged: List[Dict[str, Any]] = []
    blocks = re.split(r'(?="image_number"\s*:)', cleaned_text)
    for blk in blocks:
        num_m = re.search(r'"image_number"\s*:\s*(\d+)', blk)
        if not num_m:
            continue
        img_num = int(num_m.group(1))

        objs: List[str] = []
        obj_m = re.search(r'"detected_objects"\s*:\s*\[([^\]]*)', blk)
        if obj_m:
            raw_objs = re.findall(r'"([^"]+)"', obj_m.group(1))
            objs = list(dict.fromkeys(x.strip() for x in raw_objs if x.strip()))[:6]

        ocr_str = "None"
        ocr_m = re.search(
            r'"ocr_text"\s*:\s*([\s\S]*?)(?=,\s*"(?:console_platform|is_ps2_relevant|has_human_face|visual_category|retro_nostalgia_angle)"|\}|$)',
            blk,
        )
        if ocr_m:
            tokens = re.findall(r'"([^"]{2,60})"', ocr_m.group(1))
            unique_tokens = list(dict.fromkeys(t.strip() for t in tokens if t.strip()))
            if unique_tokens:
                ocr_str = ", ".join(unique_tokens[:10])

        plat_m = re.search(r'"console_platform"\s*:\s*"([^"]+)"', blk)
        rel_m = re.search(r'"is_ps2_relevant"\s*:\s*(true|false)', blk, re.I)
        face_m = re.search(r'"has_human_face"\s*:\s*(true|false)', blk, re.I)
        cat_m = re.search(r'"visual_category"\s*:\s*"([^"]+)"', blk)
        ang_m = re.search(r'"retro_nostalgia_angle"\s*:\s*"([^"]+)"', blk)

        salvaged.append(
            {
                "image_number": img_num,
                "detected_objects": objs or ["ps2_hardware"],
                "ocr_text": ocr_str,
                "console_platform": plat_m.group(1) if plat_m else "PS2",
                "is_ps2_relevant": (rel_m.group(1).lower() == "true") if rel_m else True,
                "has_human_face": (face_m.group(1).lower() == "true") if face_m else False,
                "visual_category": cat_m.group(1) if cat_m else "cat_console_setup",
                "retro_nostalgia_angle": (
                    ang_m.group(1)
                    if ang_m
                    else f"Koleksi permainan dan perkakasan PlayStation 2 ({ocr_str})."
                ),
            }
        )

    return salvaged[:expected_count]


def _normalize_vision_item(raw_item: Dict[str, Any], comp_meta: Dict[str, Any], fallback_idx: int) -> Dict[str, Any]:
    item = dict(raw_item)
    item["image_number"] = int(item.get("image_number") or fallback_idx)

    objs = item.get("detected_objects")
    if isinstance(objs, str):
        objs = [x.strip() for x in objs.split(",") if x.strip()]
    elif not isinstance(objs, list):
        objs = []
    item["detected_objects"] = list(dict.fromkeys(str(x).strip() for x in objs if str(x).strip()))[:6]

    ocr_txt = str(item.get("ocr_text") or "None").strip()
    if "," in ocr_txt:
        parts = list(dict.fromkeys(p.strip() for p in ocr_txt.split(",") if p.strip()))
        ocr_txt = ", ".join(parts[:10])
    item["ocr_text"] = ocr_txt[:180] if ocr_txt else "None"

    platform = str(item.get("console_platform") or "PlayStation / Retro").strip()
    item["console_platform"] = platform

    combined_check = f"{ocr_txt} {platform} {' '.join(item['detected_objects'])}".lower()
    has_ps_marker = any(k in combined_check for k in ("ps2", "playstation", "dualshock", "sony", "ps1", "psx"))
    has_competitor = any(re.search(rf"\b{re.escape(term)}\b", combined_check) for term in COMPETITOR_CONSOLE_TERMS)

    raw_relevant = bool(item.get("is_ps2_relevant", True))
    if has_competitor and not has_ps_marker:
        item["is_ps2_relevant"] = False
    else:
        item["is_ps2_relevant"] = raw_relevant

    item["has_human_face"] = bool(item.get("has_human_face", False))

    raw_cat = str(item.get("visual_category") or "").strip().lower()
    if raw_cat not in VALID_VISUAL_CATEGORIES:
        obj_joined = " ".join(item["detected_objects"]).lower()
        if any(w in obj_joined for w in ("usb", "pendrive", "hard drive", "hdd", "memory card", "storage")):
            raw_cat = "cat_storage_tech"
        elif any(w in obj_joined for w in ("tv", "crt", "monitor", "screen", "console", "desk", "game_case")):
            raw_cat = "cat_console_setup"
        else:
            raw_cat = "cat_controller"
    item["visual_category"] = raw_cat

    angle = str(item.get("retro_nostalgia_angle") or "").strip()
    item["retro_nostalgia_angle"] = angle or "Peralatan permainan video klasik yang menggamit nostalgia."

    item["compressed_kb"] = comp_meta["compressed_kb"]
    item["dimensions"] = comp_meta["dimensions"]
    item["source_ref"] = comp_meta.get("source_ref", "")
    if comp_meta.get("saved_path"):
        item["saved_path"] = comp_meta["saved_path"]

    return item


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


class VisionOCRAnalyzer:
    def __init__(self) -> None:
        self._llm: Any = None
        self._chat_handler: Any = None
        self._handler_name: str = ""
        self._source_mode: str = ""
        self._load_seconds: float = 0.0

    def ensure_loaded(
        self,
        n_ctx: int = VISION_N_CTX,
        n_threads: int = VISION_N_THREADS,
        n_batch: int = VISION_N_BATCH,
        kv_cache_type: int = VISION_KV_CACHE_TYPE,
        flash_attn: bool = VISION_FLASH_ATTN,
    ) -> Any:
        if self._llm is not None:
            return self._llm

        model_path, mmproj_path, source_mode = resolve_vision_model_files()
        llama_mod: Any = importlib.import_module("llama_cpp")
        Llama: Any = getattr(llama_mod, "Llama")

        t0 = time.perf_counter()
        chat_handler, handler_name = load_vision_chat_handler(mmproj_path)

        try:
            llm: Any = Llama(
                model_path=model_path,
                chat_handler=chat_handler,
                n_ctx=n_ctx,
                n_threads=n_threads,
                n_threads_batch=n_threads,
                n_batch=n_batch,
                type_k=kv_cache_type,
                type_v=kv_cache_type,
                flash_attn=flash_attn,
                verbose=False,
            )
        except TypeError:
            llm = Llama(
                model_path=model_path,
                chat_handler=chat_handler,
                n_ctx=n_ctx,
                n_threads=n_threads,
                n_batch=n_batch,
                verbose=False,
            )

        self._load_seconds = round(time.perf_counter() - t0, 2)
        self._chat_handler = chat_handler
        self._handler_name = handler_name
        self._source_mode = source_mode
        self._llm = llm
        return self._llm

    def unload(self) -> None:
        if self._llm is not None:
            del self._llm
            self._llm = None
        if self._chat_handler is not None:
            del self._chat_handler
            self._chat_handler = None
        gc.collect()

    def _run_vlm_once(
        self,
        llm: Any,
        user_content: List[Dict[str, Any]],
        temperature: float,
        max_tokens: int,
        repeat_penalty: float,
    ) -> Any:
        messages = [
            {
                "role": "system",
                "content": "You are a concise multimodal vision and OCR engine. Output strict valid JSON only without repeating words.\n/no_think",
            },
            {"role": "user", "content": user_content},
        ]
        try:
            return llm.create_chat_completion(
                messages=messages,
                temperature=temperature,
                top_p=VISION_TOP_P,
                repeat_penalty=repeat_penalty,
                max_tokens=max_tokens,
                stream=False,
            )
        except TypeError:
            return llm.create_chat_completion(
                messages=messages,
                temperature=temperature,
                top_p=VISION_TOP_P,
                max_tokens=max_tokens,
                stream=False,
            )

    def analyze_images(
        self,
        image_sources: Sequence[Union[str, Path, bytes, Image.Image]],
        save_compressed_dir: Optional[Union[str, Path]] = None,
        auto_unload: bool = True,
        temperature: float = VISION_TEMPERATURE,
        max_tokens: int = VISION_MAX_TOKENS,
        context_hint: str = "",
        skip_failed: bool = True,
        max_dim: int = VISION_INITIAL_MAX_DIM,
        target_bytes: int = VISION_TARGET_BYTES,
    ) -> Tuple[bool, List[Dict[str, Any]], Dict[str, Any]]:
        if not image_sources:
            return False, [], {"error": "Tiada sumber gambar diberikan."}

        capped_sources = image_sources[:VISION_MAX_IMAGES_PER_REQ]
        compressed_items: List[Dict[str, Any]] = []
        skipped_errors: List[str] = []

        for idx, src in enumerate(capped_sources, 1):
            out_file = None
            if save_compressed_dir is not None:
                out_file = Path(save_compressed_dir) / f"vision_input_{idx:02d}.jpg"

            c_ok, c_info = compress_image_under_30kb(
                src,
                save_path=out_file,
                target_bytes=target_bytes,
                initial_max_dim=max_dim,
            )
            if not c_ok:
                err_msg = f"Gambar #{idx} gagal dimampat <30KB: {c_info.get('error')}"
                if skip_failed:
                    skipped_errors.append(err_msg)
                    continue
                return False, [], {"error": err_msg}
            compressed_items.append(c_info)

        if not compressed_items:
            return False, [], {
                "error": "Semua gambar gagal dimuat turun atau dimampatkan < 30 KB.",
                "skipped_errors": skipped_errors,
            }

        try:
            llm = self.ensure_loaded()

            hint_block = (
                f"Context / Topic Hint: {context_hint.strip()}\n"
                if context_hint.strip()
                else ""
            )

            prompt_text = (
                f"You are a visual & OCR analyst for a PlayStation 2 (PS2) & retro gaming archive page in Malaysia.\n"
                f"{hint_block}"
                f"Analyze all {len(compressed_items)} attached images in order (Image 1 to Image {len(compressed_items)}).\n"
                "Return ONLY a valid JSON object with key 'images' containing a list of objects, each having:\n"
                "- image_number (integer 1..N)\n"
                "- detected_objects (list of up to 4 UNIQUE items, e.g. ['console', 'controller', 'game_case'] — NO duplicate items)\n"
                "- ocr_text (a single concise string under 100 chars listing UNIQUE visible game titles, brands, or model numbers — NO repeated words)\n"
                "- console_platform (string: 'PS2', 'PlayStation', 'CRT/Retro Setup', 'Nintendo/Other', or 'General Gaming')\n"
                "- is_ps2_relevant (boolean: true ONLY if PS2, PlayStation, DualShock, CRT retro gaming, or USB/HDD gaming setup)\n"
                "- has_human_face (boolean: true if a clear human face/portrait/selfie is visible, false otherwise)\n"
                "- visual_category (one of: 'cat_controller', 'cat_console_setup', 'cat_multiplayer', 'cat_storage_tech', 'cat_reels_clip')\n"
                "- retro_nostalgia_angle (1 short natural sentence under 18 words in Bahasa Melayu Malaysia describing the scene)\n"
                "Do not include markdown code fences or extra commentary.\n/no_think"
            )

            user_content: List[Dict[str, Any]] = [{"type": "text", "text": prompt_text}]
            for item in compressed_items:
                user_content.append({"type": "image_url", "image_url": {"url": item["data_url"]}})
            user_content.append(
                {
                    "type": "text",
                    "text": "Output the valid JSON object immediately. Do not think.\n/no_think",
                }
            )

            total_inf_sec = 0.0
            last_cleaned = ""
            last_usage: Dict[str, Any] = {}
            raw_parsed_images: List[Dict[str, Any]] = []

            for attempt in range(1, 3):
                t_inf = time.perf_counter()
                res: Any = self._run_vlm_once(
                    llm=llm,
                    user_content=user_content,
                    temperature=temperature if attempt == 1 else min(0.40, temperature + 0.12),
                    max_tokens=max_tokens,
                    repeat_penalty=VISION_REPEAT_PENALTY if attempt == 1 else 1.24,
                )
                total_inf_sec += time.perf_counter() - t_inf

                raw_out = str(res["choices"][0]["message"]["content"] or "")
                cleaned = re.sub(r"<think>[\s\S]*?(?:</think>|$)", "", raw_out, flags=re.DOTALL)
                cleaned = re.sub(r"```json\s*|```\s*", "", cleaned).strip()
                last_cleaned = cleaned
                last_usage = res.get("usage", {}) or {}

                raw_parsed_images = _extract_vision_items_resilient(cleaned, expected_count=len(compressed_items))
                if raw_parsed_images:
                    break

            normalized_images: List[Dict[str, Any]] = []
            for idx, comp in enumerate(compressed_items):
                raw_entry = (
                    raw_parsed_images[idx]
                    if idx < len(raw_parsed_images) and isinstance(raw_parsed_images[idx], dict)
                    else {}
                )
                if raw_entry:
                    normalized_images.append(_normalize_vision_item(raw_entry, comp, idx + 1))

            comp_tok = int(last_usage.get("completion_tokens", max(1, len(last_cleaned.split()))))
            meta = {
                "handler": self._handler_name,
                "source_mode": self._source_mode,
                "images_processed": len(compressed_items),
                "skipped_count": len(skipped_errors),
                "max_compressed_kb": max(c["compressed_kb"] for c in compressed_items),
                "load_sec": self._load_seconds,
                "inference_sec": round(total_inf_sec, 2),
                "completion_tokens": comp_tok,
                "tok_per_sec": round(comp_tok / total_inf_sec, 2) if total_inf_sec > 0 else 0.0,
            }
            if skipped_errors:
                meta["skipped_errors"] = skipped_errors

            if not normalized_images:
                meta["raw_output_fallback"] = last_cleaned[:500]
                return False, [], meta

            return True, normalized_images, meta
        finally:
            if auto_unload:
                self.unload()


vision_analyzer = VisionOCRAnalyzer()


if __name__ == "__main__":
    sample_dir = PROJECT_ROOT / "temp" / "test_media_pool_50" / "raw"
    samples = sorted(sample_dir.glob("*.jpg"))[:2] if sample_dir.exists() else []
    if samples:
        ok, results, meta = vision_analyzer.analyze_images(
            samples,
            context_hint="PlayStation 2 controller and retro gaming hardware",
            auto_unload=True,
        )
        print("Status Vision:", ok, "| Meta:", meta)
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print("Tiada sampel gambar di temp/test_media_pool_50/raw untuk ujian kendiri.")