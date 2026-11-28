#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - UJIAN KALIS RALAT & FALLBACK PINTAR (EXP12)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/exp/exp12_reels_resilience_and_smart_fallback_debug.py
#
# TUJUAN UJIAN:
# 1. Menguji Gelung Penggantian Pintar Klip Berwajah (Smart Face Replacement):
#    Jika 2 atau 3 klip dikesan berwajah manusia, sistem mengunci ID video
#    tersebut dalam Redis (180 hari) dan memuat turun klip pengganti daripada
#    baki calon Pexels (atau kategori objek/alam) sehingga cukup >= 3 klip bersih.
# 2. Menguji Penjana Kapsyen Anti-Priming + Maklum Balas Pembetulan Kendiri:
#    - Tidak menyalin ayat penuh sejarah ke dalam prompt (elak model meniru
#      4 perkataan pembuka yang sama).
#    - Menyenaraikan secara eksplisit pembuka ayat terlarang + menyuntik
#      sudut pembuka (Hook Angle) berbeza bagi setiap cubaan.
#    - Jika cubaan #1 ditolak, sebab penolakan disuntik terus ke cubaan #2.
#    - Dilengkapi Penjana Sandaran Kecemasan (Failsafe Synthesizer) supaya
#      penjanaan kapsyen 100% kalis kegagalan (zero RuntimeError).
# 3. Memaparkan setiap langkah & keputusan dalam jadual Rich di terminal.
# ==============================================================================

import argparse
import hashlib
import importlib
import json
import os
import random
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# ------------------------------------------------------------------------------
# PENYEDIAAN LALUAN & IMPORT MODUL TERAS (BEBAS AMARAN PYLANCE)
# ------------------------------------------------------------------------------
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
ENGINE_DIR = PROJECT_ROOT / "live-engine-01"

for p in [ENGINE_DIR, PROJECT_ROOT]:
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
_x17_mod: Any = importlib.import_module("X17_fb_reels_persona_poster")

compute_file_sha256: Any = getattr(_b2_mod, "compute_file_sha256")
PS2RedisShardedManager: Any = getattr(_redis_mod, "PS2RedisShardedManager")
PS2VectorShardedManager: Any = getattr(_vector_mod, "PS2VectorShardedManager")
llm_engine: Any = getattr(_llm_mod, "llm_engine")
clean_and_scrub_bm_story: Any = getattr(_llm_mod, "clean_and_scrub_bm_story")

PEXELS_REELS_CATEGORIES: Dict[str, Any] = getattr(_x14_mod, "PEXELS_REELS_CATEGORIES")
is_pexels_clip_safe: Any = getattr(_x14_mod, "is_pexels_clip_safe_faceless_or_backview")
sanitize_mp4: Any = getattr(_x14_mod, "sanitize_mp4_strip_tmcd_and_audio")
prepare_reels_music_track: Any = getattr(_x15_mod, "prepare_reels_music_track")
extract_and_analyze_clips_ocr: Any = getattr(_x16_mod, "extract_and_analyze_clips_ocr")
render_vertical_reels_video: Any = getattr(_x16_mod, "render_vertical_reels_video")
get_dynamic_myt_context: Any = getattr(_x17_mod, "get_dynamic_myt_context")
def _local_polish_reels_bm_phrasing(text: str, self_ref: str) -> str:
    text = re.sub(r"\b(aku|saya)\b", self_ref, text, flags=re.IGNORECASE)
    text = re.sub(rf"(^|[.!?]\s+){self_ref}\b", lambda m: f"{m.group(1)}{self_ref.capitalize()}", text)
    for pat, rep in {
        r"\btetesan\b": "titisan",
        r"\btetes\b": "titis",
        r"\bfinally\b": "akhirnya",
        r"\bkejauhan\b": "dari jauh",
        r"\blewat masuk\b": "menembusi",
        r"\bsuara ketukan kaki\b": "bunyi tapak kaki",
    }.items():
        text = re.sub(pat, rep, text, flags=re.IGNORECASE)
    text = re.sub(r"[^\x20-\x7E\n]", "", text)
    return re.sub(r"\s+", " ", text).strip()


polish_reels_bm_phrasing: Any = getattr(_x17_mod, "polish_reels_bm_phrasing", _local_polish_reels_bm_phrasing)

reels_redis = PS2RedisShardedManager(namespace="ps2n_reels")
reels_vector = PS2VectorShardedManager(namespace="ps2n_reels")

MYT = timezone(timedelta(hours=8))
TEMP_DIR: Path = Path(getattr(cfg, "TEMP_DIR", PROJECT_ROOT / "temp"))
WORK_DIR: Path = TEMP_DIR / "exp12_resilience_work"
REPORT_JSON: Path = TEMP_DIR / "exp12_resilience_report.json"

MIN_CLEAN_CLIPS_TARGET: int = 3
MAX_REPLACEMENT_ROUNDS: int = 2

# Sudut pembuka (Hook) pelbagai supaya AI tidak mengulang pembuka ayat yang sama
DYNAMIC_HOOK_ANGLES: List[Dict[str, str]] = [
    {
        "hook_id": "HOOK_SOALAN_SANTAI",
        "directive": "Mulakan ayat pertama dengan SOALAN santai kepada geng/korang (contoh: 'Pernah tak korang rasa...', 'Siapa kat sini yang masih...', 'Korang perasan tak...').",
    },
    {
        "hook_id": "HOOK_VISUAL_TERUS",
        "directive": "Mulakan ayat pertama dengan menggambarkan terus objek atau cahaya dalam video tanpa menyebut masa di awal ayat (contoh: 'Tengok cahaya neon...', 'Sekali pandang setup macam ni...', 'Bunyi dan suasana dalam klip ni...').",
    },
    {
        "hook_id": "HOOK_NOSTALGIA_DULU",
        "directive": "Mulakan ayat pertama dengan mengimbau kenangan zaman sekolah atau kedai game era 2000-an (contoh: 'Zaman balik sekolah dulu...', 'Kalau ingat balik era 2000-an...', 'Dulu masa lepak depan TV...').",
    },
    {
        "hook_id": "HOOK_TERAPI_REHAT",
        "directive": "Mulakan ayat pertama dengan mengajak pembaca berehat sekejap dari penat kerja (contoh: 'Lepas seharian menghadap kerja...', 'Kadang-kadang kita cuma perlukan...', 'Tarik nafas sekejap geng...').",
    },
    {
        "hook_id": "HOOK_FAKTA_TEKNOLOGI",
        "directive": "Mulakan ayat pertama dengan pemerhatian teknikal atau perkakasan retro yang ringkas (contoh: 'Ramai yang tak sedar...', 'Walaupun teknologi dah jauh berubah...', 'Setup ringkas macam ni pun...').",
    },
]


# ==============================================================================
# 1. ENJIN PENARIK PEXELS DENGAN SIMPANAN BAKI CALON + PENGGANTIAN KLIP BERWAJAH
# ==============================================================================
def fetch_pexels_with_reserve_pool(
    work_dir: Path,
    desired_count: int = 3,
    query_override: str = "",
) -> Dict[str, Any]:
    """
    Menarik calon Pexels (1 request), memuat turun `desired_count` klip bersih,
    dan menyimpan baki calon (`reserve_pool`) untuk penggantian automatik jika
    ada klip yang dikesan berwajah manusia semasa imbasan OCR.
    """
    api_key = (getattr(cfg, "PS2N_PEXELS_API_KEY", "") or os.getenv("PS2N_PEXELS_API_KEY", "")).strip()
    if not api_key:
        raise RuntimeError("PS2N_PEXELS_API_KEY tiada.")

    work_dir.mkdir(parents=True, exist_ok=True)
    if query_override.strip():
        cat_name = "CUSTOM_TOPIC"
        query_str = query_override.strip()
        is_gaming = True
    else:
        cat_name = random.choice(list(PEXELS_REELS_CATEGORIES.keys()))
        cat_info = PEXELS_REELS_CATEGORIES[cat_name]
        query_str = random.choice(cat_info["queries"])
        is_gaming = bool(cat_info["is_gaming_promo_eligible"])

    resp = requests.get(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": api_key},
        params={"query": query_str, "orientation": "portrait", "per_page": 80, "size": "medium"},
        timeout=25,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Pexels HTTP {resp.status_code}: {resp.text[:120]}")

    raw_videos = resp.json().get("videos", [])
    vetted: List[Dict[str, Any]] = []
    for vid in raw_videos:
        vid_id = str(vid.get("id", "")).strip()
        if not vid_id or not is_pexels_clip_safe(vid):
            continue
        dup_r, _ = reels_redis.is_duplicate_fingerprint(f"pexels_vid:{vid_id}")
        if dup_r:
            continue
        dur = int(vid.get("duration") or 0)
        if dur < 4 or dur > 45:
            continue

        best_vf = None
        for vf in vid.get("video_files", []):
            if vf.get("file_type") == "video/mp4" and int(vf.get("height") or 0) >= int(vf.get("width") or 0):
                if vf.get("link"):
                    best_vf = vf
                    break
        if best_vf:
            vetted.append(
                {
                    "id": vid_id,
                    "duration": dur,
                    "width": int(best_vf.get("width") or 720),
                    "height": int(best_vf.get("height") or 1280),
                    "download_link": best_vf["link"],
                }
            )

    random.shuffle(vetted)
    active_clips: List[Dict[str, Any]] = []
    reserve_pool: List[Dict[str, Any]] = []
    seen_shas: Set[str] = set()

    for idx, cand in enumerate(vetted):
        if len(active_clips) >= desired_count:
            reserve_pool.extend(vetted[idx:])
            break
        dl_ok = _download_and_clean_single_candidate(cand, work_dir, seen_shas)
        if dl_ok:
            active_clips.append(cand)

    return {
        "category": cat_name,
        "query": query_str,
        "is_gaming_promo_eligible": is_gaming,
        "clips": active_clips,
        "reserve_pool": reserve_pool,
    }


def _download_and_clean_single_candidate(
    cand: Dict[str, Any],
    work_dir: Path,
    seen_shas: Set[str],
) -> bool:
    raw_p = work_dir / f"raw_{cand['id']}.mp4"
    clean_p = work_dir / f"clip_{cand['id']}.mp4"
    try:
        r = requests.get(cand["download_link"], headers={"User-Agent": "Mozilla/5.0"}, stream=True, timeout=35)
        if r.status_code != 200:
            return False
        with open(raw_p, "wb") as f:
            for ch in r.iter_content(chunk_size=1024 * 512):
                if ch:
                    f.write(ch)
        if not raw_p.exists() or raw_p.stat().st_size < 50_000:
            raw_p.unlink(missing_ok=True)
            return False

        ok_s = sanitize_mp4(raw_p, clean_p)
        raw_p.unlink(missing_ok=True)
        if not ok_s:
            clean_p.unlink(missing_ok=True)
            return False

        sha = compute_file_sha256(clean_p)
        dup_sha, _ = reels_redis.is_duplicate_fingerprint(f"pexels_sha256:{sha}")
        if dup_sha or sha in seen_shas:
            clean_p.unlink(missing_ok=True)
            return False

        seen_shas.add(sha)
        cand["local_path"] = clean_p
        cand["sha256"] = sha
        cand["size_mb"] = round(clean_p.stat().st_size / (1024 * 1024), 2)
        return True
    except Exception:
        raw_p.unlink(missing_ok=True)
        clean_p.unlink(missing_ok=True)
        return False


def run_smart_face_replacement_pipeline(
    initial_pkg: Dict[str, Any],
    work_dir: Path,
    target_clean_count: int = MIN_CLEAN_CLIPS_TARGET,
    dry_run_redis: bool = True,
) -> Dict[str, Any]:
    """
    Menjalankan imbasan OCR + penapis wajah pada klip awal. Jika ada klip
    disingkirkan kerana wajah manusia, fungsi ini secara automatik menarik
    klip pengganti daripada `reserve_pool` (atau 1 carian kategori alam/neon
    tanpa manusia) sehingga berjaya mengumpul `target_clean_count` klip bersih.
    """
    current_clips: List[Dict[str, Any]] = list(initial_pkg["clips"])
    reserve_pool: List[Dict[str, Any]] = list(initial_pkg["reserve_pool"])
    seen_shas: Set[str] = {str(c.get("sha256", "")) for c in current_clips if c.get("sha256")}
    verified_clean_clips: List[Dict[str, Any]] = []
    verified_frames: List[Dict[str, Any]] = []
    rejected_face_ids: List[str] = []
    replacement_rounds_used = 0

    for round_no in range(0, MAX_REPLACEMENT_ROUNDS + 1):
        if not current_clips:
            break

        console.print(
            f"[cyan]🔄 [Pusingan Imbasan Visual #{round_no + 1}][/cyan] "
            f"Menyemak {len(current_clips)} klip (Klip bersih terkumpul: {len(verified_clean_clips)}/{target_clean_count})..."
        )
        ocr_res = extract_and_analyze_clips_ocr(
            clips=current_clips,
            work_dir=work_dir / f"round_{round_no + 1}",
            topic_hint=f"{initial_pkg['category']} {initial_pkg['query']}",
            json_ref_path=TEMP_DIR / f"exp12_ocr_r{round_no + 1}.json",
            db_ref_path=TEMP_DIR / f"exp12_ocr_r{round_no + 1}.db",
        )

        round_clean = ocr_res["clean_clips"]
        clean_ids_set = {str(c["id"]) for c in round_clean}

        for c in current_clips:
            cid = str(c["id"])
            if cid in clean_ids_set:
                verified_clean_clips.append(c)
            else:
                rejected_face_ids.append(cid)
                if not dry_run_redis:
                    reels_redis.mark_dedup_fingerprint(
                        f"pexels_vid:{cid}",
                        post_uid="face_rejected_auto",
                        ttl_seconds=180 * 86400,
                    )

        verified_frames.extend(ocr_res["frames"])

        if len(verified_clean_clips) >= target_clean_count:
            break

        # Perlukan klip pengganti!
        needed = target_clean_count - len(verified_clean_clips)
        replacement_rounds_used += 1
        console.print(
            f"[bold yellow]⚠️ Dikesan {len(rejected_face_ids)} klip berwajah manusia. "
            f"Menggantikan {needed} klip baharu secara automatik (Pusingan Ganti #{replacement_rounds_used})...[/bold yellow]"
        )

        next_batch: List[Dict[str, Any]] = []
        while reserve_pool and len(next_batch) < needed:
            cand = reserve_pool.pop(0)
            if str(cand["id"]) in rejected_face_ids:
                continue
            if _download_and_clean_single_candidate(cand, work_dir, seen_shas):
                next_batch.append(cand)

        # Jika baki calon dari carian pertama tidak mencukupi, tarik dari kategori alam/neon tulen
        if len(next_batch) < needed:
            safe_fallback_cat = random.choice(["NATURE_CALM_ESCAPE", "CYBER_NEON_VIBE", "COZY_RAIN_NIGHT"])
            fb_q = random.choice(PEXELS_REELS_CATEGORIES[safe_fallback_cat]["queries"])
            console.print(f"  [cyan]📡 Menarik klip sandaran bebas manusia dari kategori {safe_fallback_cat} ('{fb_q}')...[/cyan]")
            fb_pkg = fetch_pexels_with_reserve_pool(
                work_dir=work_dir,
                desired_count=needed - len(next_batch),
                query_override=fb_q,
            )
            next_batch.extend(fb_pkg["clips"])
            reserve_pool.extend(fb_pkg["reserve_pool"])

        current_clips = next_batch

    return {
        "clean_clips": verified_clean_clips[:target_clean_count],
        "frames": verified_frames,
        "rejected_face_ids": rejected_face_ids,
        "replacement_rounds_used": replacement_rounds_used,
    }


# ==============================================================================
# 2. PENJANA KAPSYEN ANTI-PRIMING + SELF-CORRECTION + FAILSAFE SYNTHESIZER
# ==============================================================================
def _extract_forbidden_openings(recent_posts: List[Dict[str, Any]], limit: int = 12) -> List[str]:
    """Mengekstrak 3-4 perkataan pembuka daripada rekod lepas untuk disekat secara eksplisit."""
    openings: List[str] = []
    for p in recent_posts[:limit]:
        words = re.findall(r"\b[a-zA-Z0-9]{2,}\b", str(p.get("story_bm", "")).strip())
        if len(words) >= 3:
            phrase = " ".join(words[:3])
            if phrase.lower() not in {o.lower() for o in openings}:
                openings.append(phrase)
    return openings


def build_failsafe_reels_story(
    self_ref: str,
    topic_category: str,
    pexels_query: str,
    music_info: Dict[str, Any],
    cal_ctx: Dict[str, Any],
) -> str:
    """
    Penjana sandaran kecemasan apabila kesemua cubaan LLM ditolak.
    Membina cerita Bahasa Melayu santai yang 100% unik, patuh waktu MYT,
    dan tiada pertindihan dengan pembuka ayat terdahulu.
    """
    period = cal_ctx["period"]
    song_title = music_info.get("title", "alunan muzik retro")
    bpm = music_info.get("bpm", 110)

    openers = [
        f"Sekali pandang visual {pexels_query} dalam klip ni, terus teringat suasana santai era 2000-an.",
        f"Jarang-jarang dapat layan visual tenang macam ni sambil ditemani rentak {song_title}.",
        f"Kalau korang jenis suka suasana retro dan pencahayaan kemas, klip kali ni memang kena dengan jiwa.",
        f"Tengok pergerakan visual dalam video ni pun dah cukup buat fikiran rasa lapang seketika.",
    ]
    chosen_open = random.choice(openers)

    middle = (
        f" Gabungan visual bertema {topic_category.replace('_', ' ').lower()} dengan alunan muzik berentak {bpm} BPM "
        f"memang ngam untuk menemani waktu {period.lower()} {self_ref} dan korang semua di Malaysia."
    )
    closing = (
        f" Kadang-kadang benda ringkas macam ni pun dah mampu mengubat rindu zaman membesar depan konsol klasik bersama kawan-kawan."
    )
    return f"{chosen_open}{middle}{closing}"


def generate_resilient_reels_caption(
    cinematic_brief: str,
    topic_category: str,
    pexels_query: str,
    music_info: Dict[str, Any],
    cal_ctx: Dict[str, Any],
    recent_posts: List[Dict[str, Any]],
) -> Tuple[str, str, Dict[str, Any]]:
    self_ref = random.choice(["admin", "min"])
    char_obj = cal_ctx["character"]
    forbidden_openings = _extract_forbidden_openings(recent_posts, limit=12)
    forbidden_open_str = ", ".join(f"'{o}'" for o in forbidden_openings) if forbidden_openings else "'Malam ni', 'Geng, malam'"

    # Guna kata kunci tema sahaja (BUKAN ayat penuh) supaya model 4B tidak meniru pembuka ayat lepas!
    recent_themes = [
        f"- {p.get('topic_category', '-')}: {p.get('pexels_query', '-')}"
        for p in recent_posts[:6]
    ]
    themes_block = "\n".join(recent_themes) if recent_themes else "- Tiada rekod tema lepas."

    hooks_shuffled = list(DYNAMIC_HOOK_ANGLES)
    random.shuffle(hooks_shuffled)

    retry_feedback = ""
    chosen_story = ""
    diag_meta: Dict[str, Any] = {}

    try:
        for attempt in range(1, 5):
            hook_choice = hooks_shuffled[(attempt - 1) % len(hooks_shuffled)]
            adj_temp = min(0.88, round(0.72 + ((attempt - 1) * 0.04), 2))

            system_prompt = (
                f"Kau ialah '{self_ref}', lelaki Malaysia berusia 35 tahun yang mengendalikan halaman "
                "Facebook 'PS2 Retro Nostalgia'.\n\n"
                f"WATAK HARI INI ({cal_ctx['day_name']} — {char_obj['label']}):\n"
                f"{char_obj['persona_guide']}\n\n"
                f"ARAHAN GAYA PEMBUKA AYAT WAJIB ({hook_choice['hook_id']}):\n"
                f"{hook_choice['directive']}\n\n"
                "PERATURAN KETAT:\n"
                f"1. HARAM mulakan ayat dengan frasa pembuka yang sudah digunakan ini: {forbidden_open_str}.\n"
                "2. Tulis 1 perenggan cerita pendek yang hidup dan santai dalam Bahasa Melayu Malaysia harian "
                "(macam abang-abang bersembang di kedai kopi, sifar campuran Bahasa Inggeris).\n"
                f"3. Bahasakan diri sebagai '{self_ref}' (DILARANG guna 'aku' atau 'saya') dan sapa penonton sebagai 'korang' atau 'geng'.\n"
                f"4. MASA SEBENAR MALAYSIA: {cal_ctx['timestamp_display']} | Mood: {cal_ctx['mood']}.\n"
                f"   Jika sebut waktu, HANYA guna '{cal_ctx['allowed_cues']}' dan HARAM sebut '{cal_ctx['forbidden_cues']}'.\n"
                "5. Panjang teks 340 hingga 520 aksara, SIFAR emoji, tanpa tajuk di baris atas, tanpa link URL atau harga RM, "
                "dan tutup dengan noktah (.) lengkap."
            )

            feedback_section = f"\n\nPEMBETULAN WAJIB DARI CUBAAN SEBELUM INI:\n{retry_feedback}\n" if retry_feedback else ""

            user_prompt = (
                f"KATEGORI VISUAL ({topic_category}): {pexels_query}\n"
                f"MUZIK LATAR: '{music_info['title']}' — {music_info['vibe_bm']}\n\n"
                f"URUTAN BABAK VISUAL VIDEO:\n{cinematic_brief}\n\n"
                f"TEMA LEPAS (ELAKKAN SUDUT SAMA):\n{themes_block}"
                f"{feedback_section}\n"
                f"Tulis kapsyen Reels baharu daripada watak '{self_ref}' (340 - 520 aksara, tanpa emoji):"
            )

            inf = llm_engine.generate_chat(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=adj_temp,
                max_tokens=380,
            )
            cleaned = clean_and_scrub_bm_story(inf["raw_text"], max_chars=540)
            cleaned = polish_reels_bm_phrasing(cleaned, self_ref=self_ref)

            low = cleaned.lower()
            if len(cleaned) < 320 or len(cleaned) > 560:
                retry_feedback = f"Cubaan lepas terlalu pendek/panjang ({len(cleaned)} aksara). Pastikan panjang antara 360 hingga 500 aksara."
                diag_meta = {"attempt": attempt, "status": retry_feedback}
                continue

            if re.search(cal_ctx["forbidden_regex"], low):
                retry_feedback = f"Cubaan lepas menyebut waktu yang salah. Sekarang ialah waktu {cal_ctx['period']}."
                diag_meta = {"attempt": attempt, "status": retry_feedback}
                continue

            # Semak 4 perkataan pembuka terhadap semua rekod lepas
            cand_words = re.findall(r"\b[a-z0-9]{3,}\b", low)
            cand_open_4 = cand_words[:4]
            open_conflict = False
            for prev in recent_posts[:12]:
                p_words = re.findall(r"\b[a-z0-9]{3,}\b", str(prev.get("story_bm", "")).lower())
                if len(cand_open_4) >= 4 and cand_open_4 == p_words[:4]:
                    bad_start = " ".join(cand_open_4)
                    retry_feedback = (
                        f"DITOLAK kerana permulaan ayat '{bad_start}' sama dengan post lama! "
                        f"Tukar terus perkataan pertama ayat supaya langsung tidak menyebut '{bad_start}'."
                    )
                    diag_meta = {"attempt": attempt, "status": f"Pembuka sama ('{bad_start}') -> mencuba semula dengan pembetulan"}
                    open_conflict = True
                    break
            if open_conflict:
                continue

            chosen_story = cleaned
            diag_meta = {
                "attempt": attempt,
                "hook_used": hook_choice["hook_id"],
                "elapsed_sec": inf["elapsed_sec"],
                "tok_per_sec": inf["tok_per_sec"],
                "story_chars": len(cleaned),
                "mode": "LLM_AI_VERIFIED",
            }
            break
    finally:
        llm_engine.unload()

    # Jaminan Kalis Ralat (Failsafe Synthesizer jika 4 cubaan LLM masih gagal)
    if not chosen_story:
        chosen_story = build_failsafe_reels_story(
            self_ref=self_ref,
            topic_category=topic_category,
            pexels_query=pexels_query,
            music_info=music_info,
            cal_ctx=cal_ctx,
        )
        diag_meta = {
            "attempt": 5,
            "story_chars": len(chosen_story),
            "mode": "FAILSAFE_SYNTHESIZER_ACTIVATED",
        }

    hashtags = "#PS2RetroNostalgia #PS2Malaysia #ReelsMalaysia #RetroGaming #GamingMalaysia #Nostalgia2000an"
    return chosen_story, f"{chosen_story}\n\n{hashtags}", diag_meta


# ==============================================================================
# FUNGSI UTAMA UJIAN EXP12
# ==============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="EXP12: Ujian Kalis Ralat & Smart Fallback Reels")
    parser.add_argument("--render-video", action="store_true", help="Turut jalankan cantuman MP4 akhir (30-50s).")
    parser.add_argument("--query", type=str, default="", help="Kata kunci Pexels pilihan untuk ujian.")
    args = parser.parse_args()

    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        cal_ctx = get_dynamic_myt_context()
        console.print(
            Panel.fit(
                "[bold cyan]PS2-RETRO-NOSTALGIA :: UJIAN KALIS RALAT & SMART FALLBACK (EXP12)[/bold cyan]\n"
                f"[dim]{cal_ctx['timestamp_display']} | Watak: {cal_ctx['character']['label']}[/dim]",
                border_style="cyan",
            )
        )

        # UJIAN 1: Simulasi Penjana Kapsyen Anti-Priming (Dengan Rekod Lama 'Malam ni...')
        console.print("\n[bold yellow]Ujian 1: Penjanaan Kapsyen Anti-Priming & Pembetulan Kendiri...[/bold yellow]")
        simulated_recent_posts = [
            {
                "topic_category": "COZY_RAIN_NIGHT",
                "pexels_query": "rainy window view",
                "story_bm": "Malam ni, cuaca luar memang takkan pernah rasa nak masuk rumah sebab hujan monsun ni.",
            },
            {
                "topic_category": "CRT_ARCADE_NOSTALGIA",
                "pexels_query": "vintage arcade cabinet",
                "story_bm": "Malam ni, bila senyap-senyap turun ke ruang bawah tanah tu, nampak macam masa dulu lagi.",
            },
            {
                "topic_category": "GAMING_SETUP_RGB",
                "pexels_query": "gaming setup rgb desk",
                "story_bm": "Malam ni, malam-malam macam ni memang masa terbaik untuk tengok setup RGB yang bercahaya.",
            },
        ]
        sample_music = {"title": "Night Vision", "bpm": 117.5, "vibe_bm": "Alunan Sederhana Santai (117.5 BPM)"}
        sample_brief = (
            "- Babak Pembukaan (0-10s): Cahaya lampu meja menyinari fon kepala retro di atas meja kayu.\n"
            "- Babak Pertengahan (10-25s): Kaset lama dan pemain piring hitam berputar perlahan dalam bilik malap."
        )

        story_bm, full_cap, cap_diag = generate_resilient_reels_caption(
            cinematic_brief=sample_brief,
            topic_category="TECH_AUDIO_RETRO",
            pexels_query="headphones gaming desk",
            music_info=sample_music,
            cal_ctx=cal_ctx,
            recent_posts=simulated_recent_posts,
        )
        console.print(
            Panel(
                f"[bold green]✅ Lulus Anti-Priming ({cap_diag}):[/bold green]\n[white]{full_cap}[/white]",
                title="Keputusan Ujian 1 (Kapsyen Unik Tanpa Meniru Pembuka Lama)",
                border_style="green",
            )
        )

        # UJIAN 2: Tarik Pexels + Muzik + Penggantian Automatik Klip Berwajah
        console.print("\n[bold yellow]Ujian 2: Pexels Reserve Pool + Penggantian Automatik Klip Berwajah...[/bold yellow]")
        audio_clip, music_info = prepare_reels_music_track(target_duration_sec=36)
        pex_pkg = fetch_pexels_with_reserve_pool(
            work_dir=WORK_DIR,
            desired_count=MIN_CLEAN_CLIPS_TARGET,
            query_override=args.query,
        )
        console.print(
            f"  📥 Carian Pexels: [cyan]{pex_pkg['category']}[/cyan] ('{pex_pkg['query']}') | "
            f"Klip awal: {len(pex_pkg['clips'])} | Baki simpanan (Reserve): {len(pex_pkg['reserve_pool'])}"
        )

        smart_vis = run_smart_face_replacement_pipeline(
            initial_pkg=pex_pkg,
            work_dir=WORK_DIR,
            target_clean_count=MIN_CLEAN_CLIPS_TARGET,
            dry_run_redis=True,
        )

        res_tbl = Table(title="Keputusan Ujian 2: Penggantian Klip Bersih", box=box.ROUNDED, header_style="bold cyan")
        res_tbl.add_column("Metrik", style="bold white")
        res_tbl.add_column("Nilai", style="cyan")
        res_tbl.add_row("Klip Bersih Akhir", f"{len(smart_vis['clean_clips'])} klip tanpa wajah")
        res_tbl.add_row("Klip Disingkirkan (Wajah)", str(smart_vis["rejected_face_ids"] or "0 (Semua bersih)"))
        res_tbl.add_row("Pusingan Penggantian", str(smart_vis["replacement_rounds_used"]))
        res_tbl.add_row("Jumlah Bingkai OCR Sah", str(len(smart_vis["frames"])))
        console.print(res_tbl)

        final_mp4_str = "(Dilangkau — guna --render-video untuk uji cantuman MP4)"
        if args.render_video and smart_vis["clean_clips"]:
            out_mp4 = WORK_DIR / f"exp12_reels_{int(time.time())}.mp4"
            rendered_p, dur_sec = render_vertical_reels_video(
                clips=smart_vis["clean_clips"],
                audio_clip=audio_clip,
                music_info=music_info,
                output_mp4_path=out_mp4,
                target_duration_sec=36,
            )
            final_mp4_str = f"{rendered_p} ({dur_sec}s)"
        elif audio_clip:
            audio_clip.close()

        summary = {
            "status": "SUCCESS",
            "caption_test": cap_diag,
            "story_bm": story_bm,
            "clean_clips_count": len(smart_vis["clean_clips"]),
            "rejected_face_ids": smart_vis["rejected_face_ids"],
            "replacement_rounds_used": smart_vis["replacement_rounds_used"],
            "video_output": final_mp4_str,
        }
        REPORT_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        console.print(f"\n[bold green]✅ Ujian EXP12 selesai sepenuhnya! Laporan disimpan di: {REPORT_JSON}[/bold green]")

    except Exception as exc:
        console.print(
            Panel(
                f"[bold red]❌ Ralat Ujian EXP12:[/bold red] {exc}\n\n[dim]{traceback.format_exc()}[/dim]",
                border_style="red",
            )
        )
        sys.exit(1)


if __name__ == "__main__":
    main()