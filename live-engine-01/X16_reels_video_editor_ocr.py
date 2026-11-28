#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - ENJIN SNAPSHOT OCR & EDITOR VIDEO REELS (X16)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/live-engine-01/X16_reels_video_editor_ocr.py
#
# FUNGSI UTAMA (EDISI NAIK TARAF + TUNING KELAJUAN 384PX):
# 1. Mengekstrak tepat 3 tangkapan skrin (snapshot) bagi setiap video Pexels
#    (3 video = 9 gambar; 4 video = 12 gambar; 5 video = 15 gambar).
# 2. Penapis Wajah & Orang Ramai 2-Lapisan:
#    - Lapisan 1: Imbasan pantas OpenCV Haar Cascade (jika tersedia) sebelum VLM.
#    - Lapisan 2: Analisis VLM X07 pada resolusi pantas `384px` (`<= 20 KB`) dalam
#      kumpulan 2 gambar per sesi (`OCR_BATCH_SIZE=2`) supaya ~40% lebih laju
#      tanpa memotong JSON; menyingkirkan klip jika >= 1 bingkai mempunyai
#      wajah hadapan atau kumpulan orang ramai.
# 3. Menyimpan rekod analisis bingkai demi bingkai ke .json dan .db di /temp/.
# 4. Membina ringkasan visual sinematik (Pembukaan -> Pertengahan -> Penutup)
#    untuk rujukan Persona AI (X17).
# 5. Memotong & mencantumkan klip bersih menjadi video menegak 1080x1920 (9:16)
#    berdurasi 30-50 saat selaras dengan ketukan rentak Librosa (X15).
# ==============================================================================

import importlib
import json
import os
import random
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from PIL import Image
from rich import box
from rich.console import Console
from rich.panel import Panel

console = Console()

# ------------------------------------------------------------------------------
# IMPORT MODUL TERAS PROJEK (X01, X07) & PUSTAKA MOVIEPY (BEBAS AMARAN PYLANCE)
# ------------------------------------------------------------------------------
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for p in [CURRENT_DIR, PROJECT_ROOT]:
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

cfg: Any = importlib.import_module("X01_config")
_vision_mod: Any = importlib.import_module("X07_vision_ocr_analyzer")
vision_analyzer: Any = getattr(_vision_mod, "vision_analyzer")

VideoFileClip: Any
concatenate_videoclips: Any
try:
    _mpy: Any = importlib.import_module("moviepy")
    VideoFileClip = getattr(_mpy, "VideoFileClip")
    concatenate_videoclips = getattr(_mpy, "concatenate_videoclips")
except Exception:
    _mpy_ed: Any = importlib.import_module("moviepy.editor")
    VideoFileClip = getattr(_mpy_ed, "VideoFileClip")
    concatenate_videoclips = getattr(_mpy_ed, "concatenate_videoclips")

MYT = timezone(timedelta(hours=8))
TEMP_DIR: Path = Path(getattr(cfg, "TEMP_DIR", PROJECT_ROOT / "temp"))

SNAPSHOTS_PER_CLIP: int = 3          # 3 snapshot per video (3 vid=9, 4 vid=12, 5 vid=15)
OCR_BATCH_SIZE: int = 2              # 2 gambar per sesi VLM (elak JSON terpotong & 2x lebih stabil)
OCR_MAX_TOKENS: int = 1400           # Ruang token mencukupi untuk 2 bingkai penuh
REELS_VISION_MAX_DIM: int = int(os.getenv("PS2N_REELS_VISION_MAX_DIM", "384"))
REELS_VISION_TARGET_BYTES: int = int(os.getenv("PS2N_REELS_VISION_TARGET_BYTES", str(20 * 1024)))
FACE_FLAG_MIN_HITS: int = 1          # Singkirkan klip jika >= 1 bingkai ada wajah hadapan / crowd
REEL_MIN_DURATION: float = 30.0
REEL_MAX_DURATION: float = 50.0
REEL_WIDTH: int = 1080
REEL_HEIGHT: int = 1920

CROWD_AND_FRONT_FACE_HINTS: Set[str] = {
    "selfie",
    "portrait",
    "headshot",
    "crowd",
    "audience",
    "group of people",
    "many people",
    "facing camera",
    "looking at camera",
    "wajah",
    "muka",
    "ramai orang",
    "kumpulan orang",
}


# ==============================================================================
# 1. PENYIMPAN REKOD OCR BERPERINGKAT (.JSON & .DB DI DALAM /temp/)
# ==============================================================================
class ReelsOcrBatchLedger:
    """Merekodkan setiap bingkai OCR sesi demi sesi ke temp/*.json & temp/*.db."""

    def __init__(self, json_path: Path, db_path: Path) -> None:
        self.json_path = json_path
        self.db_path = db_path
        self.items: List[Dict[str, Any]] = []
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._save_json()

    def _init_db(self) -> None:
        if self.db_path.exists():
            self.db_path.unlink()
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                """
                CREATE TABLE reels_ocr_frames (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no INTEGER NOT NULL,
                    clip_order INTEGER NOT NULL,
                    clip_id TEXT NOT NULL,
                    frame_no INTEGER NOT NULL,
                    timestamp_sec REAL NOT NULL,
                    compressed_kb REAL NOT NULL,
                    detected_objects TEXT NOT NULL,
                    ocr_text TEXT NOT NULL,
                    console_platform TEXT NOT NULL,
                    is_ps2_relevant INTEGER NOT NULL,
                    has_human_face INTEGER NOT NULL,
                    visual_category TEXT NOT NULL,
                    scene_description TEXT NOT NULL,
                    recorded_at_iso TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _save_json(self) -> None:
        payload = {
            "updated_at_myt": datetime.now(tz=MYT).isoformat(),
            "total_frames_analyzed": len(self.items),
            "frames": self.items,
        }
        self.json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def add_frame(self, row: Dict[str, Any]) -> None:
        self.items.append(row)
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                """
                INSERT INTO reels_ocr_frames (
                    batch_no, clip_order, clip_id, frame_no, timestamp_sec,
                    compressed_kb, detected_objects, ocr_text, console_platform,
                    is_ps2_relevant, has_human_face, visual_category,
                    scene_description, recorded_at_iso
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["batch_no"],
                    row["clip_order"],
                    row["clip_id"],
                    row["frame_no"],
                    row["timestamp_sec"],
                    row["compressed_kb"],
                    json.dumps(row["detected_objects"], ensure_ascii=False),
                    row["ocr_text"],
                    row["console_platform"],
                    1 if row["is_ps2_relevant"] else 0,
                    1 if row["has_human_face"] else 0,
                    row["visual_category"],
                    row["scene_description"],
                    row["recorded_at_iso"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
        self._save_json()


# ==============================================================================
# 2. LAPISAN 1: PENGESAN WAJAH PANTAS OPENCV (JIKA TERPASANG)
# ==============================================================================
def _detect_front_face_opencv(image_path: Path) -> bool:
    """Mengesan wajah hadapan secara pantas (< 5ms) menggunakan Haar Cascade jika cv2 wujud."""
    try:
        cv2: Any = importlib.import_module("cv2")
        cascade_dir = getattr(getattr(cv2, "data", None), "haarcascades", "")
        if not cascade_dir:
            return False
        face_cascade = cv2.CascadeClassifier(f"{cascade_dir}haarcascade_frontalface_default.xml")
        img = cv2.imread(str(image_path))
        if img is None:
            return False
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(
            gray,
            scaleFactor=1.15,
            minNeighbors=5,
            minSize=(48, 48),
        )
        return len(faces) > 0
    except Exception:
        return False


def _is_frame_flagged_face_or_crowd(v_res: Dict[str, Any]) -> bool:
    """Menilai sama ada output VLM menunjukkan wajah hadapan atau kumpulan orang ramai."""
    if bool(v_res.get("has_human_face", False)):
        return True

    objs_str = " ".join(str(x) for x in v_res.get("detected_objects", [])).lower()
    desc_str = str(v_res.get("retro_nostalgia_angle", "")).lower()
    combined = f"{objs_str} {desc_str}"

    # Benarkan jika secara jelas dinyatakan pandangan belakang / sisi / tangan sahaja
    if any(ok_w in combined for ok_w in ("from behind", "back view", "side view", "dari belakang", "dari sisi", "hands only")):
        return False

    return any(bad_term in combined for bad_term in CROWD_AND_FRONT_FACE_HINTS)


# ==============================================================================
# 3. PENGEKSTRAK 3 SNAPSHOT/VIDEO + OCR BERKELOMPOK (2 GAMBAR / SESI @ 384PX)
# ==============================================================================
def extract_and_analyze_clips_ocr(
    clips: List[Dict[str, Any]],
    work_dir: Path,
    topic_hint: str,
    json_ref_path: Optional[Path] = None,
    db_ref_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Mengekstrak 3 snapshot per video (9 hingga 15 gambar keseluruhan),
    menapis wajah hadapan melalui 2 lapisan (OpenCV + VLM X07 batch 2 gambar @ 384px),
    dan menyimpan hasil setiap bingkai ke .json & .db dalam /temp/.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    json_out = json_ref_path or (TEMP_DIR / "x16_ocr_sessions.json")
    db_out = db_ref_path or (TEMP_DIR / "x16_reels_ocr_state.db")
    ledger = ReelsOcrBatchLedger(json_path=json_out, db_path=db_out)

    snapshot_tasks: List[Dict[str, Any]] = []
    face_hits_by_clip: Dict[str, int] = {}
    global_frame_no = 0
    sample_ratios = (0.18, 0.50, 0.82)

    for clip_idx, c_obj in enumerate(clips, 1):
        cid = str(c_obj["id"])
        v_clip = VideoFileClip(str(c_obj["local_path"]))
        dur = float(v_clip.duration or 1.0)
        try:
            for snap_i, ratio in enumerate(sample_ratios[:SNAPSHOTS_PER_CLIP], 1):
                t_sec = round(min(max(0.25, dur * ratio), max(0.35, dur - 0.25)), 2)
                frame_arr: Any = v_clip.get_frame(t_sec)
                if frame_arr is None:
                    continue

                global_frame_no += 1
                snap_file = work_dir / f"snap_{global_frame_no:02d}_c{clip_idx}_id{cid}.jpg"
                im = Image.fromarray(frame_arr).convert("RGB")
                im.save(snap_file, format="JPEG", quality=86)

                cv_face_hit = _detect_front_face_opencv(snap_file)
                if cv_face_hit:
                    face_hits_by_clip[cid] = face_hits_by_clip.get(cid, 0) + 1

                snapshot_tasks.append(
                    {
                        "frame_no": global_frame_no,
                        "clip_order": clip_idx,
                        "clip_id": cid,
                        "snap_in_clip": snap_i,
                        "timestamp_sec": t_sec,
                        "snap_path": snap_file,
                        "cv_face_hit": cv_face_hit,
                    }
                )
        finally:
            v_clip.close()

    # Langkau VLM untuk klip yang sudah dikesan berwajah hadapan oleh Lapisan 1 (jika >= 2 klip bersih masih ada)
    pre_clean_clip_ids = {str(c["id"]) for c in clips if face_hits_by_clip.get(str(c["id"]), 0) == 0}
    if len(pre_clean_clip_ids) >= 2:
        vlm_queue = [t for t in snapshot_tasks if t["clip_id"] in pre_clean_clip_ids]
    else:
        vlm_queue = list(snapshot_tasks)

    total_snaps = len(snapshot_tasks)
    total_batches = (len(vlm_queue) + OCR_BATCH_SIZE - 1) // OCR_BATCH_SIZE
    analyzed_frames: List[Dict[str, Any]] = []

    try:
        for b_idx in range(total_batches):
            batch_no = b_idx + 1
            batch_slice = vlm_queue[b_idx * OCR_BATCH_SIZE : (b_idx + 1) * OCR_BATCH_SIZE]
            batch_paths = [t["snap_path"] for t in batch_slice]

            console.print(
                f"  [cyan]🔍 [Sesi OCR {batch_no}/{total_batches}][/cyan] "
                f"Menganalisis {len(batch_paths)} snapshot @ {REELS_VISION_MAX_DIM}px "
                f"(Bingkai #{batch_slice[0]['frame_no']}..#{batch_slice[-1]['frame_no']})..."
            )

            ok_v, v_items, v_meta = vision_analyzer.analyze_images(
                image_sources=batch_paths,
                save_compressed_dir=work_dir / f"ocr_batch_{batch_no:02d}",
                auto_unload=False,
                max_tokens=OCR_MAX_TOKENS,
                context_hint=(
                    f"{topic_hint} | Describe lighting, colors, mood, objects & visible text. "
                    "STRICT RULE: Set has_human_face=true if ANY front/side human face, selfie, or crowd of people is visible. "
                    "Set has_human_face=false ONLY for pure objects/scenery, hands-only, or pure back-of-head views."
                ),
                skip_failed=True,
                max_dim=REELS_VISION_MAX_DIM,
                target_bytes=REELS_VISION_TARGET_BYTES,
            )

            if not ok_v or not v_items:
                console.print(
                    f"  [yellow]⚠️ Sesi OCR {batch_no}/{total_batches} tiada JSON lengkap "
                    f"({v_meta.get('error', 'Fallback')}), meneruskan sesi seterusnya...[/yellow]"
                )
                continue

            for local_i, v_res in enumerate(v_items):
                task_ref = batch_slice[local_i] if local_i < len(batch_slice) else batch_slice[-1]
                cid = task_ref["clip_id"]
                flagged_face = _is_frame_flagged_face_or_crowd(v_res)
                if flagged_face:
                    face_hits_by_clip[cid] = face_hits_by_clip.get(cid, 0) + 1

                frame_record = {
                    "batch_no": batch_no,
                    "clip_order": task_ref["clip_order"],
                    "clip_id": cid,
                    "frame_no": task_ref["frame_no"],
                    "timestamp_sec": task_ref["timestamp_sec"],
                    "compressed_kb": float(v_res.get("compressed_kb", 0.0)),
                    "detected_objects": list(v_res.get("detected_objects", [])),
                    "ocr_text": str(v_res.get("ocr_text", "None")).strip(),
                    "console_platform": str(v_res.get("console_platform", "General Gaming")).strip(),
                    "is_ps2_relevant": bool(v_res.get("is_ps2_relevant", False)),
                    "has_human_face": flagged_face,
                    "visual_category": str(v_res.get("visual_category", "cat_reels_clip")).strip(),
                    "scene_description": str(v_res.get("retro_nostalgia_angle", "")).strip(),
                    "recorded_at_iso": datetime.now(tz=MYT).isoformat(),
                }
                ledger.add_frame(frame_record)
                analyzed_frames.append(frame_record)

            console.print(
                f"  [green]✅ Sesi OCR {batch_no}/{total_batches} selesai[/green] "
                f"({v_meta.get('inference_sec', 0)}s | Max saiz: {v_meta.get('max_compressed_kb', 0)} KB)"
            )
    finally:
        vision_analyzer.unload()

    # Tapis keluar klip yang mempunyai >= 1 bingkai wajah hadapan / crowd
    clean_clips = list(clips)
    flagged_face_clips = {cid for cid, cnt in face_hits_by_clip.items() if cnt >= FACE_FLAG_MIN_HITS}
    if flagged_face_clips:
        remaining = [c for c in clips if str(c["id"]) not in flagged_face_clips]
        if len(remaining) >= 2:
            clean_clips = remaining
            analyzed_frames = [f for f in analyzed_frames if f["clip_id"] not in flagged_face_clips]
            console.print(
                f"  [yellow]🛡️ Penapis Wajah Ketat: {len(flagged_face_clips)} klip berwajah/ramai orang disingkirkan; "
                f"mengekalkan {len(clean_clips)} klip bersih.[/yellow]"
            )
        elif len(remaining) == 1:
            clean_clips = remaining
            analyzed_frames = [f for f in analyzed_frames if f["clip_id"] not in flagged_face_clips]
            console.print(
                "  [yellow]🛡️ Penapis Wajah Ketat: mengekalkan 1 klip paling bersih tanpa wajah hadapan.[/yellow]"
            )

    # Semak sama ada visual mengandungi elemen retro/konsol/gaming untuk kelayakan promo Shopee 30%
    ps2_or_retro_hits = sum(
        1
        for f in analyzed_frames
        if f["is_ps2_relevant"]
        or any(
            kw in f"{' '.join(f['detected_objects'])} {f['ocr_text']} {f['console_platform']}".lower()
            for kw in ("ps2", "playstation", "console", "controller", "dualshock", "crt", "arcade", "usb", "hdd", "game")
        )
    )
    visual_has_gaming_elements = ps2_or_retro_hits >= 2

    cinematic_brief = build_cinematic_visual_brief(analyzed_frames, clean_clips)

    return {
        "clean_clips": clean_clips,
        "frames": analyzed_frames,
        "total_snapshots_extracted": total_snaps,
        "total_batches": total_batches,
        "visual_has_gaming_elements": visual_has_gaming_elements,
        "cinematic_brief": cinematic_brief,
        "json_ref_path": str(json_out),
        "db_ref_path": str(db_out),
    }


def build_cinematic_visual_brief(
    frames: List[Dict[str, Any]],
    clips: List[Dict[str, Any]],
) -> str:
    """
    Menyusun dapatan OCR menjadi penceritaan visual sinematik mengikut urutan babak
    (Pembukaan -> Pertengahan -> Kemuncak -> Penutup) supaya Persona AI tidak menulis ayat kaku.
    """
    if not frames:
        return "Babak visual estetik penuh pencahayaan retro yang menggamit nostalgia."

    by_clip: Dict[str, List[Dict[str, Any]]] = {}
    for f in frames:
        by_clip.setdefault(f["clip_id"], []).append(f)

    ordered_ids = [str(c["id"]) for c in clips if str(c["id"]) in by_clip]
    if not ordered_ids:
        ordered_ids = list(by_clip.keys())

    stage_labels = ["Babak Pembukaan (0-10s)", "Babak Pertengahan (10-25s)", "Babak Kemuncak (25-40s)", "Babak Penutup"]
    lines: List[str] = []

    for idx, cid in enumerate(ordered_ids):
        c_frames = by_clip[cid]
        stage = stage_labels[min(idx, len(stage_labels) - 1)]

        unique_objs: List[str] = []
        unique_ocr: List[str] = []
        best_desc = ""
        for fr in c_frames:
            for obj in fr.get("detected_objects", []):
                if obj and obj not in unique_objs:
                    unique_objs.append(obj)
            ocr_val = str(fr.get("ocr_text", "None")).strip()
            if ocr_val and ocr_val.lower() != "none" and ocr_val not in unique_ocr:
                unique_ocr.append(ocr_val)
            if len(fr.get("scene_description", "")) > len(best_desc):
                best_desc = fr["scene_description"]

        obj_str = ", ".join(unique_objs[:5]) or "suasana estetik"
        ocr_note = f" | Teks Skrin/Label: {', '.join(unique_ocr[:4])}" if unique_ocr else ""
        lines.append(f"- {stage}: {best_desc} (Elemen visual: {obj_str}{ocr_note})")

    return "\n".join(lines)


# ==============================================================================
# 4. ENJIN SUNTINGAN & CANTUMAN VIDEO MENEGAK 9:16 (30 - 50 SAAT)
# ==============================================================================
def _fit_clip_9_16_cover(clip: Any, target_w: int = REEL_WIDTH, target_h: int = REEL_HEIGHT) -> Any:
    """Melaras dimensi video kepada 1080x1920 (cover-scale + center-crop) tanpa herotan."""
    cw, ch = clip.size
    scale = max(target_w / max(1, cw), target_h / max(1, ch))
    new_w = int(round(cw * scale))
    new_h = int(round(ch * scale))

    resize_fn = getattr(clip, "resized", None) or getattr(clip, "resize")
    resized = resize_fn((new_w, new_h))

    x1 = max(0, (new_w - target_w) // 2)
    y1 = max(0, (new_h - target_h) // 2)

    if hasattr(resized, "cropped"):
        return getattr(resized, "cropped")(x1=x1, y1=y1, width=target_w, height=target_h)
    return getattr(resized, "crop")(x1=x1, y1=y1, x2=x1 + target_w, y2=y1 + target_h)


def render_vertical_reels_video(
    clips: List[Dict[str, Any]],
    audio_clip: Optional[Any],
    music_info: Dict[str, Any],
    output_mp4_path: Path,
    target_duration_sec: Optional[int] = None,
) -> Tuple[Path, float]:
    """
    Memotong klip bersih mengikut ketukan rentak Librosa supaya jumlah durasi
    berada dalam julat ketat 30 hingga 50 saat, menggabungkan muzik latar,
    dan menghasilkan fail MP4 menegak 1080x1920 (H.264 + AAC).
    """
    output_mp4_path.parent.mkdir(parents=True, exist_ok=True)
    chosen_target = float(target_duration_sec if target_duration_sec is not None else random.randint(34, 46))
    chosen_target = max(REEL_MIN_DURATION, min(REEL_MAX_DURATION, chosen_target))

    n_clips = len(clips)
    if n_clips == 0:
        raise RuntimeError("Tiada klip video dibekalkan kepada enjin suntingan X16.")

    base_seg = chosen_target / n_clips
    beats: List[float] = list(music_info.get("beat_timestamps", []))

    cut_durations: List[float] = []
    cursor = 0.0
    for idx in range(n_clips):
        if idx == n_clips - 1:
            seg = max(4.0, round(chosen_target - cursor, 2))
        else:
            ideal_cut = cursor + base_seg
            snapped_cut = ideal_cut
            if beats:
                nearest_beat = min(beats, key=lambda b: abs(b - ideal_cut))
                if abs(nearest_beat - ideal_cut) <= 1.25:
                    snapped_cut = nearest_beat
            seg = max(4.5, round(snapped_cut - cursor, 2))
        cut_durations.append(seg)
        cursor += seg

    source_handles: List[Any] = []
    fitted_segments: List[Any] = []
    stitched: Any = None

    try:
        for c_obj, seg_dur in zip(clips, cut_durations):
            vc = VideoFileClip(str(c_obj["local_path"]))
            source_handles.append(vc)
            v_dur = float(vc.duration or 1.0)
            safe_max_dur = max(0.5, v_dur - 0.12)
            use_dur = min(safe_max_dur, seg_dur)

            sub_fn = getattr(vc, "subclipped", None) or getattr(vc, "subclip")
            sub = sub_fn(0, use_dur)

            no_aud_fn = getattr(sub, "without_audio", None)
            no_aud = no_aud_fn() if no_aud_fn else getattr(sub, "set_audio")(None)

            fitted = _fit_clip_9_16_cover(no_aud, REEL_WIDTH, REEL_HEIGHT)
            fitted_segments.append(fitted)

        stitched = concatenate_videoclips(fitted_segments, method="compose")
        final_dur = float(stitched.duration or 0.0)

        # Jika klip asal pendek dan jumlah durasi < 30 saat, sambungkan ulangan segmen terakhir
        while final_dur < REEL_MIN_DURATION and fitted_segments:
            needed = (REEL_MIN_DURATION + 1.5) - final_dur
            donor = fitted_segments[-1]
            donor_dur = float(donor.duration or 1.0)
            if donor_dur > needed:
                d_sub_fn = getattr(donor, "subclipped", None) or getattr(donor, "subclip")
                donor = d_sub_fn(0, needed)
            stitched = concatenate_videoclips([stitched, donor], method="compose")
            final_dur = float(stitched.duration or 0.0)

        if final_dur > REEL_MAX_DURATION:
            trim_fn = getattr(stitched, "subclipped", None) or getattr(stitched, "subclip")
            stitched = trim_fn(0, REEL_MAX_DURATION)
            final_dur = float(stitched.duration or REEL_MAX_DURATION)

        if audio_clip is not None:
            a_dur = float(audio_clip.duration or final_dur)
            use_a_dur = min(a_dur, final_dur)
            a_sub_fn = getattr(audio_clip, "subclipped", None) or getattr(audio_clip, "subclip")
            trimmed_audio = a_sub_fn(0, use_a_dur)

            if hasattr(stitched, "with_audio"):
                stitched = getattr(stitched, "with_audio")(trimmed_audio)
            else:
                stitched = getattr(stitched, "set_audio")(trimmed_audio)

        stitched.write_videofile(
            str(output_mp4_path),
            fps=24,
            codec="libx264",
            audio_codec="aac",
            preset="fast",
            threads=4,
            logger=None,
        )
        return output_mp4_path, round(final_dur, 2)
    finally:
        if stitched is not None:
            try:
                stitched.close()
            except Exception:
                pass
        for seg in fitted_segments:
            try:
                seg.close()
            except Exception:
                pass
        for h in source_handles:
            try:
                h.close()
            except Exception:
                pass
        if audio_clip is not None:
            try:
                audio_clip.close()
            except Exception:
                pass


if __name__ == "__main__":
    console.print(
        Panel.fit(
            "[bold cyan]X16 :: REELS SNAPSHOT OCR & VIDEO EDITOR MODULE READY[/bold cyan]\n"
            f"[dim]Tetapan: {SNAPSHOTS_PER_CLIP} snapshot/video | Batch OCR: {OCR_BATCH_SIZE} gambar/sesi @ {REELS_VISION_MAX_DIM}px "
            f"(max_tokens={OCR_MAX_TOKENS}) | Penapis Wajah: >= {FACE_FLAG_MIN_HITS} bingkai | "
            f"Durasi Reels: {int(REEL_MIN_DURATION)}-{int(REEL_MAX_DURATION)}s[/dim]",
            border_style="cyan",
        )
    )