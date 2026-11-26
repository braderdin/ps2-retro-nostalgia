#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - PENGUMPUL & PENYELARAS MEDIA VISUAL (X09)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/live-engine-01/X09_media_harvester.py
#
# FUNGSI UTAMA:
# 1. Mengumpul media visual (gambar dari Unsplash / Pexels Photos / pool tempatan,
#    atau video pendek dari Pexels Videos / arkib klip PS2 tempatan).
# 2. Menguatkuasakan had keras API:
#    - Unsplash: Maksimum 40 gambar per permintaan (per_page <= 30).
#    - Pexels  : Maksimum 70 item per permintaan.
# 3. PENAPIS KESERAGAMAN MULTI-GAMBAR (Visual Cohesiveness Filter):
#    Apabila bilangan gambar melebihi 1 (count > 1), X09 memastikan semua gambar
#    yang dipilih adalah "agak sama / hampir sama" dari segi:
#    - Kluster tema carian & orientasi yang sama (landscape/square),
#    - Pengesahan objek Vision/OCR (< 30 KB melalui X07_vision_ocr_analyzer),
#    - Skor keserupaan tona warna RGB & pencahayaan (Pillow Histogram Cosine).
# 4. Muat naik terus aset terpilih ke Backblaze B2 melalui Cloudflare Worker Proxy
#    (X03_b2_proxy_client) untuk mendapatkan URL awam Meta Graph API v26.0.
# ==============================================================================

import hashlib
import importlib
import inspect
import json
import math
import os
import random
import re
import sys
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from PIL import Image

# ------------------------------------------------------------------------------
# IMPORT MODUL TERAS DARI live-engine-01
# ------------------------------------------------------------------------------
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for p in [CURRENT_DIR, PROJECT_ROOT]:
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

cfg: Any = importlib.import_module("X01_config")
_b2_mod: Any = importlib.import_module("X03_b2_proxy_client")
_vision_mod: Any = importlib.import_module("X07_vision_ocr_analyzer")

b2_proxy: Any = getattr(_b2_mod, "b2_proxy")
vision_analyzer: Any = getattr(_vision_mod, "vision_analyzer")


# ==============================================================================
# [BAHAGIAN TUNING MANUAL MEDIA & KESERAGAMAN MULTI-GAMBAR]
# ==============================================================================
UNSPLASH_HARD_MAX_PER_REQ: int = 40
PEXELS_HARD_MAX_PER_REQ: int = 70
MAX_MULTI_PHOTOS_PER_POST: int = 4     # Maksimum 4 gambar seragam dalam 1 post FB
MIN_VISUAL_SIMILARITY_SCORE: float = float(os.getenv("PS2N_MIN_VISUAL_SIM", "0.62"))
USE_VISION_CONSISTENCY_CHECK: bool = os.getenv("PS2N_USE_VISION_CHECK", "true").lower() in ("1", "true", "yes")

TEMP_DIR: Path = Path(getattr(cfg, "TEMP_DIR", PROJECT_ROOT / "temp"))
DATA_DIR: Path = Path(getattr(cfg, "DATA_DIR", PROJECT_ROOT / "data"))
LOCAL_POOL_MANIFEST: Path = TEMP_DIR / "test_media_pool_50" / "manifest_50.json"
LOCAL_ARCHIVE_DIR: Path = DATA_DIR / "local_ps2_media"
HARVEST_WORK_DIR: Path = TEMP_DIR / "harvested_ready"
# ==============================================================================

# Kluster kata kunci carian yang rapat supaya set gambar sentiasa senada
COHESIVE_PHOTO_CLUSTERS: List[Dict[str, str]] = [
    {
        "cluster_id": "ps_controller_closeup",
        "unsplash_q": "playstation dualshock controller gaming",
        "pexels_q": "playstation controller dark gaming",
        "label": "Kontroler DualShock & Konsol PlayStation",
    },
    {
        "cluster_id": "retro_console_setup",
        "unsplash_q": "retro video game console controller",
        "pexels_q": "retro gaming console setup",
        "label": "Set Konsol Retro & Perkakasan Klasik",
    },
    {
        "cluster_id": "neon_gaming_desk",
        "unsplash_q": "playstation gaming setup neon",
        "pexels_q": "gaming controller neon desk",
        "label": "Suasana Bilik Gaming Malam & Pencahayaan Neon",
    },
]

PEXELS_VIDEO_QUERIES: List[str] = [
    "retro arcade video game",
    "playing video games controller",
    "retro gaming console",
]


# ==============================================================================
# 1. PENGIRA KESERUPAAN VISUAL (TONA WARNA RGB + NISBAH ASPEK)
# ==============================================================================
def compute_image_visual_signature(img_path: Path) -> Optional[Dict[str, Any]]:
    """
    Mengekstrak tandatangan visual ringan (histogram RGB ternormalisasi + nisbah aspek
    + kecerahan purata) menggunakan Pillow tanpa memerlukan pustaka berat tambahan.
    """
    try:
        with Image.open(img_path) as im:
            rgb = im.convert("RGB")
            w, h = rgb.size
            aspect = round(w / max(1, h), 3)

            # Kecilkan ke 64x64 untuk pengiraan histogram warna yang pantas & stabil
            small = rgb.resize((64, 64), Image.Resampling.BILINEAR)
            hist = small.histogram()  # 768 bin (256 R + 256 G + 256 B)

            # Kumpulkan kepada 48 bin (16 R + 16 G + 16 B)
            binned: List[float] = []
            for ch in range(3):
                offset = ch * 256
                for b in range(16):
                    s = sum(hist[offset + b * 16 : offset + (b + 1) * 16])
                    binned.append(float(s))

            norm = math.sqrt(sum(v * v for v in binned)) or 1.0
            vec = [v / norm for v in binned]

            # Purata kecerahan (0..255)
            pixels = list(small.getdata())
            brightness = sum((r * 0.299 + g * 0.587 + b * 0.114) for r, g, b in pixels) / max(1, len(pixels))

            return {
                "width": w,
                "height": h,
                "aspect": aspect,
                "brightness": round(brightness, 2),
                "color_vec": vec,
            }
    except Exception:
        return None


def compute_visual_similarity(sig_a: Dict[str, Any], sig_b: Dict[str, Any]) -> float:
    """
    Mengira skor keserupaan visual (0.0 hingga 1.0) antara dua gambar berdasarkan:
    - 65% kesamaan taburan warna & tona (Cosine Similarity),
    - 20% kesamaan tahap kecerahan (suasana gelap/terang),
    - 15% kesamaan orientasi/nisbah aspek.
    """
    va = sig_a["color_vec"]
    vb = sig_b["color_vec"]
    color_sim = sum(x * y for x, y in zip(va, vb))
    color_sim = max(0.0, min(1.0, color_sim))

    bright_diff = abs(sig_a["brightness"] - sig_b["brightness"]) / 255.0
    bright_sim = max(0.0, 1.0 - bright_diff)

    aspect_diff = abs(sig_a["aspect"] - sig_b["aspect"])
    aspect_sim = max(0.0, 1.0 - min(1.0, aspect_diff))

    total_score = (0.65 * color_sim) + (0.20 * bright_sim) + (0.15 * aspect_sim)
    return round(total_score, 4)


# ==============================================================================
# 2. PENARIK MEDIA DARI SUMBER TEMPATAN, UNSPLASH & PEXELS
# ==============================================================================
class MediaHarvester:
    """
    Mengumpul, menapis keseragaman multi-gambar, menganalisis dengan Vision AI,
    dan memuat naik aset media ke Backblaze B2 melalui Cloudflare Worker Proxy.
    """

    def __init__(self) -> None:
        HARVEST_WORK_DIR.mkdir(parents=True, exist_ok=True)
        LOCAL_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    def _fetch_unsplash_photos(self, query: str, count: int = 10) -> List[Dict[str, Any]]:
        api_key = getattr(cfg, "PS2N_UNSPLASH_ACCESS_KEY", "")
        if not api_key:
            return []

        safe_per_page = min(max(1, count), UNSPLASH_HARD_MAX_PER_REQ, 30)
        try:
            resp = requests.get(
                "https://api.unsplash.com/search/photos",
                headers={"Authorization": f"Client-ID {api_key}", "Accept-Version": "v1"},
                params={"query": query, "per_page": safe_per_page, "orientation": "landscape"},
                timeout=20,
            )
            if resp.status_code != 200:
                return []

            results: List[Dict[str, Any]] = []
            for item in resp.json().get("results", []):
                urls = item.get("urls", {})
                dl_url = urls.get("regular") or urls.get("small")
                if not dl_url:
                    continue
                results.append(
                    {
                        "source": "unsplash",
                        "source_id": str(item.get("id", "")),
                        "description": item.get("alt_description") or item.get("description") or query,
                        "photographer": item.get("user", {}).get("name", "Unsplash"),
                        "download_url": dl_url,
                    }
                )
            return results
        except Exception:
            return []

    def _fetch_pexels_photos(self, query: str, count: int = 10) -> List[Dict[str, Any]]:
        api_key = getattr(cfg, "PS2N_PEXELS_API_KEY", "")
        if not api_key:
            return []

        safe_per_page = min(max(1, count), PEXELS_HARD_MAX_PER_REQ)
        try:
            resp = requests.get(
                "https://api.pexels.com/v1/search",
                headers={"Authorization": api_key},
                params={"query": query, "per_page": safe_per_page, "orientation": "landscape"},
                timeout=20,
            )
            if resp.status_code != 200:
                return []

            results: List[Dict[str, Any]] = []
            for item in resp.json().get("photos", []):
                src = item.get("src", {})
                dl_url = src.get("large") or src.get("medium") or src.get("original")
                if not dl_url:
                    continue
                results.append(
                    {
                        "source": "pexels",
                        "source_id": str(item.get("id", "")),
                        "description": item.get("alt") or query,
                        "photographer": item.get("photographer", "Pexels"),
                        "download_url": dl_url,
                    }
                )
            return results
        except Exception:
            return []

    def _download_image_to_workdir(self, cand: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Memuat turun calon gambar ke folder kerja sementara jika belum wujud secara tempatan."""
        if cand.get("local_path") and Path(cand["local_path"]).exists():
            p = Path(cand["local_path"])
            sig = compute_image_visual_signature(p)
            if not sig:
                return None
            cand["signature"] = sig
            return cand

        dl_url = cand.get("download_url", "")
        if not dl_url:
            return None

        try:
            resp = requests.get(dl_url, timeout=25)
            if resp.status_code != 200 or not resp.content:
                return None

            img = Image.open(BytesIO(resp.content))
            if img.mode != "RGB":
                img = img.convert("RGB")

            safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(cand.get("source_id", "img")))[:24]
            fname = f"{cand.get('source', 'web')}_{safe_id}.jpg"
            out_path = HARVEST_WORK_DIR / fname
            img.save(out_path, format="JPEG", quality=88, optimize=True)

            sig = compute_image_visual_signature(out_path)
            if not sig:
                out_path.unlink(missing_ok=True)
                return None

            cand["local_path"] = str(out_path)
            cand["signature"] = sig
            return cand
        except Exception:
            return None

    def _load_local_pool_candidates(self) -> List[Dict[str, Any]]:
        """Membaca gambar sedia ada dari arkib tempatan atau cache 50 gambar ujian."""
        items: List[Dict[str, Any]] = []

        # 1. Arkib rasmi PS2 tempatan (jika pengguna letak screenshot/foto PS2 sendiri)
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
            for f in sorted(LOCAL_ARCHIVE_DIR.glob(ext)):
                items.append(
                    {
                        "source": "local_archive",
                        "source_id": f.stem,
                        "description": f"Koleksi arkib PS2 tempatan ({f.stem})",
                        "photographer": "PS2 Retro Nostalgia",
                        "local_path": str(f),
                    }
                )

        # 2. Pool cache 50 gambar di temp/test_media_pool_50/manifest_50.json
        if LOCAL_POOL_MANIFEST.exists():
            try:
                manifest = json.loads(LOCAL_POOL_MANIFEST.read_text(encoding="utf-8"))
                for rec in manifest:
                    raw_p = rec.get("raw_path", "")
                    if raw_p and Path(raw_p).exists():
                        items.append(
                            {
                                "source": rec.get("source", "local_pool"),
                                "source_id": str(rec.get("source_id", "")),
                                "description": rec.get("description", "Retro PlayStation Gaming"),
                                "photographer": rec.get("photographer", "Archive"),
                                "local_path": raw_p,
                            }
                        )
            except Exception:
                pass

        return items

    def _select_cohesive_photo_group(
        self,
        candidates: List[Dict[str, Any]],
        desired_count: int,
        run_vision_check: bool = USE_VISION_CONSISTENCY_CHECK,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """
        Memilih 1 hingga `desired_count` gambar yang PALING SERAGAM ("agak sama / hampir sama"):
        1. Menapis calon menggunakan Vision AI (< 30 KB, maks 5 gambar) supaya hanya
           gambar yang relevan dengan PS2 / kontroler klasik dipilih.
        2. Memilih gambar induk (anchor) dan memadankan gambar lain yang mempunyai
           objek utama sama serta skor keserupaan tona warna & pencahayaan tertinggi.
        """
        valid_cands: List[Dict[str, Any]] = []
        for c in candidates:
            hydrated = self._download_image_to_workdir(c)
            if hydrated:
                valid_cands.append(hydrated)
            if len(valid_cands) >= 8:
                break

        if not valid_cands:
            return [], ""

        # Jika hanya 1 gambar diminta
        if desired_count <= 1:
            chosen = valid_cands[0]
            if run_vision_check:
                v_ok, v_res, _ = vision_analyzer.analyze_images([chosen["local_path"]], auto_unload=True)
                if v_ok and v_res:
                    chosen["vision"] = v_res[0]
                    return [chosen], v_res[0].get("retro_nostalgia_angle", chosen["description"])
            return [chosen], chosen["description"]

        # Untuk multi-gambar (2..4 gambar): jalankan Vision check pada maks 5 calon teratas
        shortlist = valid_cands[:5]
        if run_vision_check:
            v_ok, v_res, _ = vision_analyzer.analyze_images(
                [item["local_path"] for item in shortlist],
                auto_unload=True,
            )
            if v_ok and v_res:
                for idx, v_item in enumerate(v_res):
                    if idx < len(shortlist):
                        shortlist[idx]["vision"] = v_item

                # Utamakan calon yang disahkan relevan dengan PS2 / konsol klasik
                ps2_only = [
                    it for it in shortlist
                    if it.get("vision", {}).get("is_ps2_relevant", True) is True
                ]
                if len(ps2_only) >= 2:
                    shortlist = ps2_only

        # Pilih anchor pertama dan susun rakan gambar berdasarkan keserupaan visual + objek
        anchor = shortlist[0]
        anchor_objs = set(
            str(x).lower() for x in anchor.get("vision", {}).get("detected_objects", [])
        )

        scored_companions: List[Tuple[float, Dict[str, Any]]] = []
        for cand in shortlist[1:]:
            vis_sim = compute_visual_similarity(anchor["signature"], cand["signature"])
            cand_objs = set(
                str(x).lower() for x in cand.get("vision", {}).get("detected_objects", [])
            )
            obj_bonus = 0.15 if (anchor_objs and cand_objs and (anchor_objs & cand_objs)) else 0.0
            combined_score = min(1.0, round(vis_sim + obj_bonus, 4))
            cand["similarity_to_anchor"] = combined_score
            scored_companions.append((combined_score, cand))

        scored_companions.sort(key=lambda x: x[0], reverse=True)

        anchor["similarity_to_anchor"] = 1.0
        selected_group: List[Dict[str, Any]] = [anchor]
        for sim_score, comp in scored_companions:
            if len(selected_group) >= desired_count:
                break
            if sim_score >= MIN_VISUAL_SIMILARITY_SCORE:
                selected_group.append(comp)

        # Gabungkan ringkasan konteks visual untuk dihantar ke X08_ps2_persona_writer
        angles = []
        for idx, item in enumerate(selected_group, 1):
            v_angle = item.get("vision", {}).get("retro_nostalgia_angle") or item.get("description", "")
            angles.append(f"Gambar {idx}: {v_angle}")
        combined_visual_summary = " | ".join(angles)

        return selected_group, combined_visual_summary

    def _upload_file_to_b2_safe(self, local_path: str, category: str = "photos") -> Dict[str, Any]:
        """
        Memuat naik fail ke Backblaze B2 melalui X03_b2_proxy_client dan memulangkan
        rekod baldi serta URL awam proksi Cloudflare.
        """
        p = Path(local_path)
        sha_short = hashlib.sha256(p.read_bytes()).hexdigest()[:12]
        remote_key = f"{category}/{time.strftime('%Y%m')}/{p.stem}_{sha_short}{p.suffix.lower()}"

        # Semak tandatangan kaedah b2_proxy.upload_file untuk keserasian penuh
        sig = inspect.signature(b2_proxy.upload_file)
        params = list(sig.parameters.keys())

        if "local_path" in params and "remote_path" in params:
            res = b2_proxy.upload_file(local_path=str(p), remote_path=remote_key)
        elif "file_path" in params and "object_key" in params:
            res = b2_proxy.upload_file(file_path=str(p), object_key=remote_key)
        elif "file_path" in params and "remote_path" in params:
            res = b2_proxy.upload_file(file_path=str(p), remote_path=remote_key)
        else:
            res = b2_proxy.upload_file(str(p), remote_key)

        if isinstance(res, dict):
            return res
        return {"ok": True, "public_url": str(res), "remote_key": remote_key}

    def harvest_cohesive_photos(
        self,
        desired_count: int = 2,
        prefer_local_pool: bool = True,
        upload_to_b2: bool = False,
        run_vision_check: bool = USE_VISION_CONSISTENCY_CHECK,
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Mengumpul 1 hingga 4 gambar yang seragam ("agak sama / hampir sama")
        untuk hantaran Facebook Photo / Album.
        """
        target_count = max(1, min(desired_count, MAX_MULTI_PHOTOS_PER_POST))
        cluster = random.choice(COHESIVE_PHOTO_CLUSTERS)

        candidates: List[Dict[str, Any]] = []
        if prefer_local_pool:
            local_items = self._load_local_pool_candidates()
            if local_items:
                random.shuffle(local_items)
                candidates.extend(local_items[:10])

        # Jika calon tempatan tidak mencukupi, tarik daripada 1 kluster tema yang sama
        if len(candidates) < 6:
            u_items = self._fetch_unsplash_photos(cluster["unsplash_q"], count=6)
            p_items = self._fetch_pexels_photos(cluster["pexels_q"], count=6)
            # Utamakan satu sumber dahulu supaya gaya fotografi & warna lebih seragam
            if len(u_items) >= target_count + 1:
                candidates.extend(u_items)
            elif len(p_items) >= target_count + 1:
                candidates.extend(p_items)
            else:
                candidates.extend(u_items + p_items)

        selected, visual_summary = self._select_cohesive_photo_group(
            candidates=candidates,
            desired_count=target_count,
            run_vision_check=run_vision_check,
        )

        if not selected:
            return False, {"error": "Tiada gambar seragam berjaya dikumpulkan."}

        if upload_to_b2:
            for item in selected:
                b2_res = self._upload_file_to_b2_safe(item["local_path"], category="photos")
                item["b2_upload"] = b2_res

        return True, {
            "media_type": "photo",
            "cluster_label": cluster["label"],
            "requested_count": target_count,
            "delivered_count": len(selected),
            "visual_context_summary": visual_summary,
            "items": selected,
        }

    def harvest_reel_video(
        self,
        query: Optional[str] = None,
        upload_to_b2: bool = False,
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Mengumpul 1 video pendek daripada arkib klip PS2 tempatan atau Pexels Videos
        (had keras <= 70 video/req) untuk penerbitan Facebook Reels.
        """
        # 1. Semak klip gameplay PS2 tempatan dahulu jika ada
        local_videos = sorted(LOCAL_ARCHIVE_DIR.glob("*.mp4"))
        if local_videos:
            chosen_vid = random.choice(local_videos)
            rec: Dict[str, Any] = {
                "source": "local_archive",
                "source_id": chosen_vid.stem,
                "local_path": str(chosen_vid),
                "description": f"Klip gameplay PS2 klasik ({chosen_vid.stem})",
            }
            if upload_to_b2:
                rec["b2_upload"] = self._upload_file_to_b2_safe(str(chosen_vid), category="reels")
            return True, {
                "media_type": "reel",
                "visual_context_summary": rec["description"],
                "item": rec,
            }

        # 2. Tarik daripada Pexels Videos API (<= 70 video/req)
        api_key = getattr(cfg, "PS2N_PEXELS_API_KEY", "")
        if not api_key:
            return False, {"error": "PS2N_PEXELS_API_KEY tiada dan tiada video tempatan ditemui."}

        search_q = query or random.choice(PEXELS_VIDEO_QUERIES)
        resp = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": api_key},
            params={"query": search_q, "per_page": min(15, PEXELS_HARD_MAX_PER_REQ), "orientation": "portrait"},
            timeout=25,
        )
        if resp.status_code != 200:
            return False, {"error": f"Pexels Video HTTP {resp.status_code}"}

        videos = resp.json().get("videos", [])
        if not videos:
            return False, {"error": "Tiada video ditemui di Pexels."}

        chosen = random.choice(videos[:8])
        v_files = [f for f in chosen.get("video_files", []) if f.get("file_type") == "video/mp4"]
        # Pilih resolusi HD sederhana (720p ke bawah) supaya muat turun & muat naik pantas
        v_files.sort(key=lambda x: abs(int(x.get("width") or 720) - 720))
        if not v_files:
            return False, {"error": "Tiada fail MP4 sah dalam rekod Pexels."}

        best_mp4 = v_files[0]
        vid_id = str(chosen.get("id", "reel"))
        out_mp4 = HARVEST_WORK_DIR / f"pexels_reel_{vid_id}.mp4"

        dl = requests.get(best_mp4["link"], timeout=45)
        if dl.status_code != 200 or not dl.content:
            return False, {"error": "Gagal memuat turun fail MP4 dari Pexels."}

        out_mp4.write_bytes(dl.content)
        rec = {
            "source": "pexels_video",
            "source_id": vid_id,
            "duration": chosen.get("duration", 0),
            "width": best_mp4.get("width", 0),
            "height": best_mp4.get("height", 0),
            "local_path": str(out_mp4),
            "description": f"Sedutan video retro gaming ({search_q})",
        }
        if upload_to_b2:
            rec["b2_upload"] = self._upload_file_to_b2_safe(str(out_mp4), category="reels")

        return True, {
            "media_type": "reel",
            "visual_context_summary": rec["description"],
            "item": rec,
        }


# Instans sedia diimport oleh X10 & X11
media_harvester = MediaHarvester()


if __name__ == "__main__":
    ok_h, res_h = media_harvester.harvest_cohesive_photos(
        desired_count=2,
        prefer_local_pool=True,
        upload_to_b2=False,
        run_vision_check=True,
    )
    print("Status Harvest Multi-Foto Seragam:", ok_h)
    print(json.dumps(res_h, indent=2, ensure_ascii=False))